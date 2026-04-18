from flask import Flask, request, jsonify, send_from_directory
import requests, os, json, base64, csv, io, logging
from datetime import date, datetime, timedelta
import xml.etree.ElementTree as ET

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("claudio")

app = Flask(__name__, static_folder="static")

ODOO_URL = "https://pomelo-derma.odoo.com"
ODOO_DB = "pomelo-derma"
ODOO_USER = os.environ.get("ODOO_USER", "")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN", "")
MP_WEBHOOK_SECRET = os.environ.get("MP_WEBHOOK_SECRET", "")

# ============================================================
# ODOO helpers
# ============================================================
def odoo_session():
    try:
        s = requests.Session()
        r = s.post(f"{ODOO_URL}/web/session/authenticate", json={
            "jsonrpc": "2.0", "method": "call",
            "params": {"db": ODOO_DB, "login": ODOO_USER, "password": ODOO_PASSWORD}
        }, timeout=10)
        uid = r.json().get("result", {}).get("uid")
        return (s, uid) if uid else (None, None)
    except:
        return (None, None)

def odoo_call(s, model, method, args=[], kwargs={}):
    r = s.post(f"{ODOO_URL}/web/dataset/call_kw", json={
        "jsonrpc": "2.0", "method": "call",
        "params": {"model": model, "method": method, "args": args, "kwargs": kwargs}
    }, timeout=30)
    result = r.json()
    if "error" in result:
        raise Exception(result["error"]["data"].get("message", "Error Odoo"))
    return result.get("result")

def ejecutar_accion_odoo(accion):
    s, uid = odoo_session()
    if not s:
        return "No se pudo conectar a Odoo"
    model = accion.get("model")
    method = accion.get("method")
    args = accion.get("args", [])
    kwargs = accion.get("kwargs", {})
    if not model or not method:
        return "Falta model o method"
    try:
        if method == "write" and args and args[0] == []:
            ids = odoo_call(s, model, "search", [[]], {"limit": 5000})
            if ids:
                args[0] = ids
        resultado = odoo_call(s, model, method, args, kwargs)
        if isinstance(resultado, list):
            if len(resultado) == 0:
                return "No se encontraron registros."
            if isinstance(resultado[0], dict):
                lines = []
                for r in resultado[:25]:
                    name = r.get("name") or r.get("display_name") or str(r.get("id",""))
                    extras = []
                    for k, v in r.items():
                        if k not in ["id","name","display_name"] and v and v is not False:
                            if isinstance(v, (str, int, float)):
                                extras.append(f"{k}: {v}")
                            elif isinstance(v, list) and len(v) == 2:
                                extras.append(f"{k}: {v[1]}")
                    line = f"- {name}"
                    if extras:
                        line += f" ({', '.join(extras[:4])})"
                    lines.append(line)
                total = len(resultado)
                resp = "\n".join(lines)
                if total > 25:
                    resp += f"\n... y {total-25} mas"
                return f"{total} registros:\n{resp}"
            else:
                return f"Completado: {resultado[:10]}"
        elif isinstance(resultado, bool):
            return "Operacion completada"
        elif isinstance(resultado, int):
            return f"Creado con ID: {resultado}"
        else:
            return f"{resultado}"
    except Exception as e:
        return f"Error: {str(e)}"

def get_or_create_mp_journal(s):
    diarios = odoo_call(s, "account.journal", "search_read",
                       [[["code", "=", "MP"]]], {"fields": ["id","name"], "limit": 1})
    if diarios:
        return diarios[0]["id"]
    return odoo_call(s, "account.journal", "create", [{
        "name": "Mercado Pago", "type": "bank", "code": "MP",
    }])

# ============================================================
# CLASIFICADOR DE MOVIMIENTOS MP
# ============================================================
def clasificar_mp(row):
    """
    Clasifica una fila del settlement report en una categoria.
    Devuelve dict con: categoria, lineas (lista de dicts con date/payment_ref/label/amount/accion_manual)
    """
    t = row.get('TRANSACTION_TYPE', '')
    pm_type = row.get('PAYMENT_METHOD_TYPE', '')
    amount = float(row.get('TRANSACTION_AMOUNT', '0') or 0)
    fee = float(row.get('FEE_AMOUNT', '0') or 0)
    mp_id = row.get('SOURCE_ID', '')
    fecha_raw = row.get('TRANSACTION_DATE', '')
    fecha = fecha_raw[:10] if fecha_raw else date.today().isoformat()
    pm = row.get('PAYMENT_METHOD', '')
    last4 = row.get('LAST_FOUR_DIGITS', '')
    issuer = row.get('ISSUER_NAME', '')
    sale_detail = row.get('SALE_DETAIL', '').replace('"', '')
    pay_transfer_id = row.get('PAY_BANK_TRANSFER_ID', '')

    lineas = []

    if t == 'SETTLEMENT':
        if pm_type in ('credit_card', 'debit_card') and amount > 0:
            # Venta Point: la venta bruta YA viene del POS (journal MP directo via MP_Halus).
            # Aqui solo registramos la comision MP como gasto para que el journal MP
            # refleje correctamente el neto que llega a MP.
            if abs(fee) > 0:
                lineas.append({
                    'date': fecha, 'payment_ref': f'MP-{mp_id}-FEE',
                    'label': f'Comision MP venta {mp_id}',
                    'amount': round(fee, 2),
                    'accion_manual': False,
                })
            return 'comision_point', lineas

        elif pm_type == 'bank_transfer' and amount > 0:
            lineas.append({
                'date': fecha, 'payment_ref': f'MP-{mp_id}',
                'label': f'Money in - {sale_detail} (transfer_id: {pay_transfer_id})',
                'amount': round(amount, 2),
                'accion_manual': True,
            })
            return 'money_in', lineas

        elif pm_type == 'available_money' and amount < 0:
            lineas.append({
                'date': fecha, 'payment_ref': f'MP-{mp_id}',
                'label': 'Pago con saldo MP (available_money)',
                'amount': round(amount, 2),
                'accion_manual': True,
            })
            return 'gasto_saldo_mp', lineas

    elif t == 'PAYOUTS':
        lineas.append({
            'date': fecha, 'payment_ref': f'MP-{mp_id}',
            'label': f'Pago a proveedor (transfer_id: {pay_transfer_id})',
            'amount': round(amount, 2),
            'accion_manual': True,
        })
        return 'pago_proveedor', lineas

    elif t == 'REFUND':
        lineas.append({
            'date': fecha, 'payment_ref': f'MP-{mp_id}-REFUND',
            'label': f'Devolucion de venta MP-{mp_id}',
            'amount': round(amount, 2),
            'accion_manual': False,
        })
        if abs(fee) > 0:
            lineas.append({
                'date': fecha, 'payment_ref': f'MP-{mp_id}-REFUND-FEE',
                'label': f'Reverso comision MP devolucion {mp_id}',
                'amount': round(fee, 2),
                'accion_manual': False,
            })
        return 'reverso_venta', lineas

    # Desconocido: lo guardamos pero marcamos para revision
    lineas.append({
        'date': fecha, 'payment_ref': f'MP-{mp_id}',
        'label': f'DESCONOCIDO type={t} pm_type={pm_type} amount={amount}',
        'amount': round(amount, 2),
        'accion_manual': True,
    })
    return 'desconocido', lineas

def procesar_csv_settlement(csv_content, dry_run=False):
    """Procesa un CSV de settlement report y escribe las lineas a Odoo.
    Devuelve resumen con totales por categoria."""
    reader = csv.DictReader(io.StringIO(csv_content), delimiter=';')
    rows = list(reader)

    todas_lineas = []
    por_categoria = {}
    for r in rows:
        # Limpiar BOM y espacios en claves
        r_clean = {k.strip().replace('\ufeff', ''): (v.strip() if v else '') for k, v in r.items()}
        categoria, lineas = clasificar_mp(r_clean)
        por_categoria.setdefault(categoria, {'count': 0, 'sum': 0.0, 'filas_csv': 0})
        por_categoria[categoria]['filas_csv'] += 1
        for l in lineas:
            por_categoria[categoria]['count'] += 1
            por_categoria[categoria]['sum'] += l['amount']
            todas_lineas.append(l)

    if dry_run:
        return {
            "dry_run": True,
            "filas_csv": len(rows),
            "lineas_odoo_a_crear": len(todas_lineas),
            "por_categoria": por_categoria,
            "total_neto": round(sum(l['amount'] for l in todas_lineas), 2),
        }

    # Escribir a Odoo
    s, uid = odoo_session()
    if not s:
        return {"error": "Sin conexion Odoo"}

    journal_id = get_or_create_mp_journal(s)

    # Verificar duplicados existentes (por payment_ref)
    creados = 0
    ya_existian = 0
    errores = []

    for l in todas_lineas:
        try:
            existentes = odoo_call(s, "account.bank.statement.line", "search",
                                  [[["payment_ref", "=", l['payment_ref']]]], {"limit": 1})
            if existentes:
                ya_existian += 1
                continue
            vals = {
                "journal_id": journal_id,
                "date": l['date'],
                "payment_ref": l['payment_ref'],
                "amount": l['amount'],
                "narration": l['label'],
            }
            odoo_call(s, "account.bank.statement.line", "create", [vals])
            creados += 1
        except Exception as e:
            errores.append({"payment_ref": l['payment_ref'], "error": str(e)})

    return {
        "filas_csv": len(rows),
        "lineas_generadas": len(todas_lineas),
        "creados": creados,
        "ya_existian": ya_existian,
        "errores": len(errores),
        "detalle_errores": errores[:5],
        "por_categoria": por_categoria,
        "total_neto": round(sum(l['amount'] for l in todas_lineas), 2),
    }

# ============================================================
# WEBHOOK MP
# ============================================================
def registrar_pago_webhook(pago_mp):
    """Registra un pago recibido por webhook (solo el bruto, sin comision).
    El settlement sync posterior ya lo detectara por payment_ref y agregara la comision."""
    s, uid = odoo_session()
    if not s:
        return False, "Sin conexion a Odoo"
    try:
        monto = float(pago_mp.get("transaction_amount", 0))
        mp_id = str(pago_mp.get("id", ""))
        status = pago_mp.get("status", "")
        email = pago_mp.get("payer", {}).get("email", "")
        descripcion = pago_mp.get("description", f"Pago MP #{mp_id}")
        fecha_raw = pago_mp.get("date_approved", "")
        fecha = fecha_raw[:10] if fecha_raw else date.today().isoformat()

        if status != "approved":
            return True, f"Pago {mp_id} ignorado (status: {status})"

        payment_ref = f"MP-{mp_id}"
        existentes = odoo_call(s, "account.bank.statement.line", "search",
                              [[["payment_ref", "=", payment_ref]]], {"limit": 1})
        if existentes:
            return True, f"Pago MP #{mp_id} ya registrado"

        journal_id = get_or_create_mp_journal(s)
        partner_id = None
        if email:
            partners = odoo_call(s, "res.partner", "search_read",
                                [[["email", "=", email]]], {"fields": ["id"], "limit": 1})
            if partners:
                partner_id = partners[0]["id"]

        vals = {
            "journal_id": journal_id,
            "date": fecha,
            "payment_ref": payment_ref,
            "amount": monto,
            "narration": f"[webhook pendiente comision] {descripcion}",
        }
        if partner_id:
            vals["partner_id"] = partner_id

        odoo_call(s, "account.bank.statement.line", "create", [vals])
        return True, f"Pago MP #{mp_id} registrado: ${monto:,.2f} MXN"
    except Exception as e:
        return False, str(e)

# ============================================================
# ADMIN EXEC - Endpoint temporal para operaciones controladas
# QUITAR despues de completar migracion journal MP (abril 2026)
# ============================================================
ADMIN_EXEC_TOKEN = os.environ.get("ADMIN_EXEC_TOKEN", "")
ADMIN_EXEC_ENABLED = os.environ.get("ADMIN_EXEC_ENABLED", "false").lower() == "true"

ADMIN_EXEC_BLOCKED_MODELS = {
    "ir.config_parameter", "ir.mail_server",
    "res.users", "res.groups", "ir.model.access",
    "ir.rule", "res.company",
}
ADMIN_EXEC_READONLY_METHODS = {"search", "search_read", "search_count", "read", "fields_get", "name_search"}

@app.route("/admin/odoo-exec", methods=["POST"])
def admin_odoo_exec():
    if not ADMIN_EXEC_ENABLED:
        return jsonify({"error": "not found"}), 404

    token = request.headers.get("X-Admin-Token", "")
    if not token or not ADMIN_EXEC_TOKEN or token != ADMIN_EXEC_TOKEN:
        log.warning(f"[ADMIN EXEC] Auth failed from {request.remote_addr}")
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    model = data.get("model")
    method = data.get("method")
    args = data.get("args", [])
    kwargs = data.get("kwargs", {})

    if not model or not method:
        return jsonify({"error": "model y method requeridos"}), 400

    if model in ADMIN_EXEC_BLOCKED_MODELS and method not in ADMIN_EXEC_READONLY_METHODS:
        log.warning(f"[ADMIN EXEC] Bloqueado: {model}.{method}")
        return jsonify({"error": f"model {model} solo permite lectura"}), 403

    args_preview = json.dumps(args)[:200] if args else "[]"
    log.warning(f"[ADMIN EXEC] {model}.{method} args={args_preview}")

    s, uid = odoo_session()
    if not s:
        return jsonify({"error": "Sin conexion Odoo"}), 500

    try:
        resultado = odoo_call(s, model, method, args, kwargs)
        return jsonify({"ok": True, "result": resultado})
    except Exception as e:
        log.error(f"[ADMIN EXEC] Error: {str(e)}")
        return jsonify({"ok": False, "error": str(e)}), 500

# ============================================================
# SYSTEM PROMPT
# ============================================================
SYSTEM_PROMPT = """Eres Claudio, agente IA de Pomelo Derma, farmacia dermatologica premium en Mexico.
Tienes acceso COMPLETO a Odoo. Estado actual: {contexto}

MODELOS ODOO:
- pos.order: ventas POS (fields: name, amount_total, date_order, state, partner_id)
- purchase.order: OC (fields: name, partner_id, amount_total, state, date_order)
- res.partner: clientes/proveedores (customer_rank>0 = cliente, supplier_rank>0 = proveedor)
- account.move: facturas (move_type: out_invoice=venta, in_invoice=compra proveedor)
- account.bank.statement.line: movimientos bancarios MP
- product.template: productos
- stock.quant: inventario

REGLA CRITICA: Para CUALQUIER consulta, usa SIEMPRE search_read o search_count. NUNCA uses 'create' para una consulta. 'create' solo se usa cuando el usuario pide explicitamente crear algo nuevo.

EJEMPLOS:
Contar movimientos MP: ODOO_ACTION:{{"model":"account.bank.statement.line","method":"search_count","args":[[["journal_id.code","=","MP"]]]}}
Listar ventas POS: ODOO_ACTION:{{"model":"pos.order","method":"search_read","args":[[["state","in",["done","paid","invoiced"]]]],"kwargs":{{"fields":["name","amount_total","date_order"],"limit":10}}}}

REGLAS: Responde en espanol. SIEMPRE incluye ODOO_ACTION para consultas o acciones."""

# ============================================================
# ROUTES - BASE
# ============================================================
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/facturas")
def facturas():
    return send_from_directory("static", "facturas.html")

@app.route("/health")
def health():
    return "OK", 200

@app.route("/ping")
def ping():
    return jsonify({"ok": True, "ts": datetime.utcnow().isoformat()})

@app.route("/api/estado")
def api_estado():
    s, uid = odoo_session()
    if not uid:
        return jsonify({"ok": False, "msg": "Sin conexion Odoo"})
    try:
        productos = odoo_call(s, "product.template", "search_count", [[["active", "=", True]]])
        ventas = odoo_call(s, "pos.order", "search_count", [[]])
        return jsonify({"ok": True, "productos": productos, "ventas": ventas})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

# ============================================================
# MP WEBHOOK
# ============================================================
@app.route("/mp/webhook", methods=["GET", "POST"])
def mp_webhook():
    if request.method == "GET":
        return jsonify({"status": "ok", "service": "Pomelo Derma MP Webhook"}), 200
    try:
        data = request.json or {}
        topic = data.get("type") or data.get("topic") or request.args.get("topic", "")
        log.info(f"[MP WEBHOOK] {topic}: {json.dumps(data)[:200]}")

        if topic not in ["payment", "point_integration_v2", "point_integration_wh"]:
            return jsonify({"status": "ignored", "topic": topic}), 200

        payment_id = None
        if topic == "payment":
            payment_id = data.get("data", {}).get("id")
        elif topic in ["point_integration_v2", "point_integration_wh"]:
            payment_id = data.get("data", {}).get("payment_id") or data.get("data", {}).get("id")

        if not payment_id:
            return jsonify({"status": "no payment_id"}), 200

        r = requests.get(f"https://api.mercadopago.com/v1/payments/{payment_id}",
                        headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}, timeout=10)
        pago_mp = r.json()

        success, mensaje = registrar_pago_webhook(pago_mp)
        log.info(f"[MP WEBHOOK] {mensaje}")
        return jsonify({"status": "ok", "mensaje": mensaje}), 200
    except Exception as e:
        log.error(f"[MP WEBHOOK] Error: {str(e)}")
        return jsonify({"status": "error", "error": str(e)}), 500

# ============================================================
# MP ADMIN - BORRADO Y CARGA MANUAL DE CSV
# Endpoints TEMPORALES para setup inicial y debugging.
# Se pueden quitar despues de estabilizar el flujo.
# ============================================================
@app.route("/mp/admin/list", methods=["GET"])
def mp_admin_list():
    """Lista TODOS los registros del journal MP. Solo lectura."""
    s, uid = odoo_session()
    if not s:
        return jsonify({"error": "Sin conexion Odoo"}), 500
    try:
        journal_id = get_or_create_mp_journal(s)
        lineas = odoo_call(s, "account.bank.statement.line", "search_read",
            [[["journal_id", "=", journal_id]]],
            {"fields": ["id", "date", "payment_ref", "amount", "narration"],
             "order": "date desc, id desc", "limit": 500})
        total = sum(float(l.get("amount", 0)) for l in lineas)
        return jsonify({
            "count": len(lineas),
            "total_neto": round(total, 2),
            "lineas": lineas,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/mp/admin/purge", methods=["POST"])
def mp_admin_purge():
    """Borra TODOS los registros del journal MP. Requiere confirmacion en body."""
    data = request.get_json(silent=True) or {}
    if data.get("confirmacion") != "BORRAR-TODO-JOURNAL-MP":
        return jsonify({
            "error": "Falta confirmacion",
            "instruccion": "Envia body con confirmacion='BORRAR-TODO-JOURNAL-MP'"
        }), 400

    s, uid = odoo_session()
    if not s:
        return jsonify({"error": "Sin conexion Odoo"}), 500

    try:
        journal_id = get_or_create_mp_journal(s)

        # 1. Buscar lineas
        line_ids = odoo_call(s, "account.bank.statement.line", "search",
            [[["journal_id", "=", journal_id]]], {"limit": 10000})

        if not line_ids:
            return jsonify({"borrados": 0, "mensaje": "No habia registros"})

        total_originales = len(line_ids)

        # 2. Leer los move_id de cada linea
        lineas_read = odoo_call(s, "account.bank.statement.line", "read",
            [line_ids, ["move_id", "statement_id"]])

        move_ids = set()
        for l in lineas_read:
            mv = l.get("move_id")
            if mv and isinstance(mv, list) and len(mv) > 0:
                move_ids.add(mv[0])
            elif isinstance(mv, int):
                move_ids.add(mv)
        move_ids = list(move_ids)

        log.warning(f"[MP PURGE] Encontradas {total_originales} lineas con {len(move_ids)} moves asociados")

        # 3. Pasar los moves a borrador (button_draft) para desposter
        if move_ids:
            try:
                odoo_call(s, "account.move", "button_draft", [move_ids])
                log.info(f"[MP PURGE] {len(move_ids)} moves pasados a borrador")
            except Exception as e:
                log.error(f"[MP PURGE] button_draft fallo: {e}")
                # Intentar unlink directo sobre los moves (borra tambien las lines en cascada)
                pass

        # 4. Borrar los moves (esto desencadena el borrado de las lines via cascada)
        moves_borrados = 0
        if move_ids:
            try:
                odoo_call(s, "account.move", "unlink", [move_ids])
                moves_borrados = len(move_ids)
                log.warning(f"[MP PURGE] {moves_borrados} moves borrados")
            except Exception as e:
                log.error(f"[MP PURGE] Error borrando moves: {e}")
                return jsonify({
                    "error": f"No se pudieron borrar los moves: {str(e)}",
                    "lineas_originales": total_originales,
                    "moves_identificados": len(move_ids),
                }), 500

        # 5. Verificar que ya no quedan lineas
        residuales = odoo_call(s, "account.bank.statement.line", "search",
            [[["journal_id", "=", journal_id]]], {"limit": 10000})

        lineas_residuales_borradas = 0
        if residuales:
            log.warning(f"[MP PURGE] Quedan {len(residuales)} lineas residuales tras borrar moves")
            try:
                odoo_call(s, "account.bank.statement.line", "unlink", [residuales])
                lineas_residuales_borradas = len(residuales)
            except Exception as e:
                return jsonify({
                    "error": f"Quedaron {len(residuales)} lineas residuales sin borrar: {str(e)}",
                    "lineas_originales": total_originales,
                    "moves_borrados": moves_borrados,
                }), 500

        return jsonify({
            "lineas_originales": total_originales,
            "moves_borrados": moves_borrados,
            "lineas_residuales_borradas": lineas_residuales_borradas,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/mp/admin/load-csv", methods=["POST"])
def mp_admin_load_csv():
    """Carga un CSV de settlement report y crea las lineas en Odoo.
    Query param dry_run=1 para solo simular."""
    dry_run = request.args.get("dry_run", "0") == "1"

    csv_file = request.files.get("file")
    if not csv_file:
        return jsonify({"error": "No se recibio archivo (campo 'file')"}), 400

    try:
        content = csv_file.read().decode("utf-8-sig", errors="replace")
        resultado = procesar_csv_settlement(content, dry_run=dry_run)
        log.info(f"[MP LOAD-CSV] dry_run={dry_run} result={resultado}")
        return jsonify(resultado)
    except Exception as e:
        log.error(f"[MP LOAD-CSV] Error: {e}")
        return jsonify({"error": str(e)}), 500

# ============================================================
# MP SETTLEMENT SYNC (para cron diario)
# Pide el reporte a MP, espera a que este listo, lo descarga y procesa.
# ============================================================
@app.route("/mp/settlement-sync", methods=["GET", "POST"])
def mp_settlement_sync():
    """Solicita settlement report de MP, espera, descarga y procesa."""
    if not MP_ACCESS_TOKEN:
        return jsonify({"error": "MP_ACCESS_TOKEN no configurado"}), 500

    dias = 2
    if request.method == "POST" and request.json:
        dias = int(request.json.get("dias", 2))
    dias = min(dias, 30)

    end = datetime.utcnow()
    begin = end - timedelta(days=dias)
    begin_str = begin.strftime("%Y-%m-%dT00:00:00.000Z")
    end_str = end.strftime("%Y-%m-%dT23:59:59.000Z")

    headers_mp = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    # Paso 1: listar reportes ya generados
    try:
        r_list = requests.get(
            "https://api.mercadopago.com/v1/account/settlement_report/list",
            headers=headers_mp, timeout=15)
        reportes = r_list.json() if r_list.status_code == 200 else []
    except Exception as e:
        return jsonify({"error": f"Error listando reportes: {e}"}), 500

    # Buscar un reporte reciente que cubra nuestro rango
    ahora = datetime.utcnow()
    reporte_usable = None
    if isinstance(reportes, list):
        for rep in reportes:
            try:
                file_name = rep.get("file_name", "")
                created = rep.get("date_created", "")
                # Buscar reporte creado en ultimas 4 horas
                if "-" in created:
                    created_dt = datetime.fromisoformat(created.replace("Z", "+00:00").replace(".000", ""))
                    if (ahora - created_dt.replace(tzinfo=None)).total_seconds() < 4 * 3600:
                        reporte_usable = file_name
                        break
            except:
                continue

    if not reporte_usable:
        # Solicitar un nuevo reporte
        try:
            r_gen = requests.post(
                "https://api.mercadopago.com/v1/account/settlement_report",
                headers=headers_mp,
                json={"begin_date": begin_str, "end_date": end_str},
                timeout=30)
            if r_gen.status_code not in (200, 201, 202):
                return jsonify({
                    "error": f"No se pudo solicitar reporte (HTTP {r_gen.status_code})",
                    "respuesta": r_gen.text[:500],
                }), 500
            # Extraer el id del reporte solicitado (si MP lo devuelve en JSON)
            try:
                gen_data = r_gen.json()
                report_id = gen_data.get("id")
            except:
                report_id = None
            return jsonify({
                "mensaje": "Reporte solicitado. MP tarda algunos minutos en generarlo.",
                "siguiente_paso": "Volver a llamar este endpoint en 5-10 min",
                "report_id": report_id,
                "http_status": r_gen.status_code,
            })
        except Exception as e:
            return jsonify({"error": f"Error solicitando reporte: {e}"}), 500

    # Paso 2: descargar el reporte
    try:
        r_down = requests.get(
            f"https://api.mercadopago.com/v1/account/settlement_report/{reporte_usable}",
            headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
            timeout=60)
        if r_down.status_code != 200:
            return jsonify({
                "error": f"No se pudo descargar reporte (HTTP {r_down.status_code})",
                "file_name": reporte_usable,
            }), 500
        csv_content = r_down.text
    except Exception as e:
        return jsonify({"error": f"Error descargando: {e}"}), 500

    # Paso 3: procesar y guardar
    resultado = procesar_csv_settlement(csv_content, dry_run=False)
    resultado["fuente"] = reporte_usable
    log.info(f"[MP SETTLEMENT SYNC] {resultado}")
    return jsonify(resultado)

# ============================================================
# FACTURAS CFDI
# ============================================================
@app.route('/facturas/analizar-xml', methods=['POST'])
def facturas_analizar_xml():
    xml_file = request.files.get('xml')
    if not xml_file:
        return jsonify({'error': 'No se recibio XML'}), 400
    try:
        content = xml_file.read().decode('utf-8', errors='replace')
        root = ET.fromstring(content)
        tag = root.tag
        if '{http://www.sat.gob.mx/cfd/4}' in tag:
            ns = 'http://www.sat.gob.mx/cfd/4'
        else:
            ns = 'http://www.sat.gob.mx/cfd/3'

        def g(el, attr):
            return el.get(attr, '') if el is not None else ''

        emisor = root.find(f'{{{ns}}}Emisor')
        rfc_emisor = g(emisor, 'Rfc')
        nombre_emisor = g(emisor, 'Nombre')
        fecha = root.get('Fecha', '')[:10]
        folio = root.get('Serie', '') + root.get('Folio', '')
        subtotal = float(root.get('SubTotal', 0))
        total = float(root.get('Total', 0))
        moneda = root.get('Moneda', 'MXN')

        s, uid = odoo_session()
        proveedor_id = None
        proveedor_nombre = nombre_emisor
        if s and uid and rfc_emisor:
            provs = odoo_call(s, 'res.partner', 'search_read',
                             [[['vat', '=', rfc_emisor]]], {'fields': ['id', 'name'], 'limit': 1})
            if provs:
                proveedor_id = provs[0]['id']
                proveedor_nombre = provs[0]['name']

        productos = []
        total_iva = 0.0
        for c in root.findall(f'.//{{{ns}}}Concepto'):
            barcode = c.get('NoIdentificacion', '')
            desc = c.get('Descripcion', '')
            qty = float(c.get('Cantidad', 1))
            unit_price = float(c.get('ValorUnitario', 0))
            importe = float(c.get('Importe', 0))
            iva_pct = 0.0
            imp = c.find(f'{{{ns}}}Impuestos')
            if imp is not None:
                for t in imp.findall(f'.//{{{ns}}}Traslado'):
                    if t.get('Impuesto') == '002':
                        iva_pct = float(t.get('TasaOCuota', 0)) * 100
                        total_iva += float(t.get('Importe', 0))

            prod_id = None
            prod_nom = desc
            candidatos = []
            if s and uid:
                if barcode:
                    prods = odoo_call(s, 'product.product', 'search_read',
                                     [[['barcode', '=', barcode]]], {'fields': ['id', 'name', 'barcode'], 'limit': 1})
                    if prods:
                        prod_id = prods[0]['id']
                        prod_nom = prods[0]['name']
                if not prod_id and desc:
                    termino = (desc.split(']', 1)[1].strip() if desc.startswith('[') and ']' in desc else desc)[:40].strip()
                    cands = odoo_call(s, 'product.product', 'search_read',
                                     [[['name', 'ilike', termino]]], {'fields': ['id', 'name', 'barcode'], 'limit': 3})
                    if cands:
                        candidatos = [{'id': c['id'], 'nombre': c['name'], 'barcode': c.get('barcode', '')} for c in cands]
                        prod_id = cands[0]['id']
                        prod_nom = cands[0]['name']

            productos.append({
                'barcode': barcode, 'descripcion': desc, 'cantidad': qty,
                'precio_unitario': unit_price, 'subtotal': importe, 'iva_pct': iva_pct,
                'producto_id': prod_id, 'producto_nombre': prod_nom,
                'encontrado': prod_id is not None, 'candidatos': candidatos
            })

        return jsonify({
            'ok': True, 'fuente': 'xml_cfdi',
            'proveedor': proveedor_nombre, 'proveedor_id': proveedor_id,
            'rfc_emisor': rfc_emisor, 'fecha': fecha, 'folio': folio.strip(),
            'moneda': moneda, 'subtotal': subtotal, 'iva': round(total_iva, 2),
            'total': total, 'productos': productos
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/facturas/confirmar', methods=['POST'])
def facturas_confirmar():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No se recibieron datos'}), 400
        s, uid = odoo_session()
        if not s or not uid:
            return jsonify({'error': 'No se pudo conectar a Odoo'}), 500

        proveedor_id = data.get('proveedor_id')
        if not proveedor_id:
            return jsonify({'error': 'Proveedor no identificado'}), 400

        fecha = data.get('fecha') or str(date.today())
        folio = data.get('folio', '')
        productos = data.get('productos', [])

        cuentas_compra = odoo_call(s, 'account.account', 'search_read',
            [[['code', '=', '505.01.01']]], {'fields': ['id', 'name'], 'limit': 1})
        cuenta_compra_id = cuentas_compra[0]['id'] if cuentas_compra else None

        impuestos = odoo_call(s, 'account.tax', 'search_read',
            [[['type_tax_use', '=', 'purchase'], ['amount', '=', 16]]],
            {'fields': ['id', 'name'], 'limit': 1})
        impuesto_iva_id = impuestos[0]['id'] if impuestos else None

        lineas = []
        for p in productos:
            if not p.get('producto_id'):
                cands = p.get('candidatos', [])
                if cands:
                    p['producto_id'] = cands[0]['id']
                else:
                    continue
            line = {
                'product_id': p['producto_id'],
                'name': p.get('descripcion', ''),
                'quantity': float(p.get('cantidad', 1)),
                'price_unit': float(p.get('precio_unitario', 0)),
                'account_id': cuenta_compra_id,
            }
            iva_pct = float(p.get('iva_pct', 0))
            if iva_pct > 0 and impuesto_iva_id:
                line['tax_ids'] = [[6, 0, [impuesto_iva_id]]]
            else:
                line['tax_ids'] = [[6, 0, []]]
            lineas.append([0, 0, line])

        if not lineas:
            return jsonify({'error': 'No hay productos vinculados a Odoo'}), 400

        move_vals = {
            'move_type': 'in_invoice',
            'partner_id': proveedor_id,
            'invoice_date': fecha,
            'ref': folio,
            'invoice_line_ids': lineas,
        }
        move_id = odoo_call(s, 'account.move', 'create', [move_vals])

        actualizados = []
        for p in productos:
            if p.get('producto_id') and p.get('precio_unitario'):
                try:
                    odoo_call(s, 'product.product', 'write',
                        [[p['producto_id']], {'standard_price': float(p['precio_unitario'])}])
                    actualizados.append(p['producto_id'])
                except:
                    pass

        return jsonify({
            'ok': True, 'move_id': move_id,
            'mensaje': f'Factura creada en borrador (ID {move_id}). {len(actualizados)} costos actualizados.',
            'url_odoo': f'{ODOO_URL}/odoo/accounting/vendor-bills/{move_id}',
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route("/api/ocr-oc", methods=["POST"])
def api_ocr_oc():
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files["file"]
    img_data = base64.b64encode(f.read()).decode()
    ext = f.filename.rsplit(".", 1)[-1].lower()
    media_type = "application/pdf" if ext == "pdf" else f"image/{ext}"
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 2048,
                  "messages": [{"role": "user", "content": [
                      {"type": "document" if ext == "pdf" else "image",
                       "source": {"type": "base64", "media_type": media_type, "data": img_data}},
                      {"type": "text", "text": 'Extrae datos de esta OC/factura. Responde SOLO JSON: {"proveedor":"","fecha":"YYYY-MM-DD","numero_factura":"","productos":[{"nombre":"","cantidad":0,"precio_unitario":0,"total":0}],"subtotal":0,"impuestos":0,"total":0,"moneda":"MXN"}'}
                  ]}]},
            timeout=30)
        texto = r.json()["content"][0]["text"]
        if "```" in texto:
            texto = texto.split("```")[1].replace("json", "").strip()
        resultado = json.loads(texto)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(resultado)

# ============================================================
# CHAT (lo dejamos pero con prompt mejorado)
# ============================================================
@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json()
    mensaje = data.get("mensaje", "")
    historial = data.get("historial", [])

    if not ANTHROPIC_KEY:
        return jsonify({"respuesta": "API Key no configurada", "accion": None})

    s, uid = odoo_session()
    contexto = {"status": "sin conexion"}
    if uid:
        try:
            n_prod = odoo_call(s, "product.template", "search_count", [[["active", "=", True]]])
            n_ventas = odoo_call(s, "pos.order", "search_count", [[]])
            contexto = {"status": "conectado", "productos": n_prod, "ventas_pos": n_ventas}
        except:
            contexto = {"status": "conectado"}

    system = SYSTEM_PROMPT.replace("{contexto}", json.dumps(contexto, ensure_ascii=False))

    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 1500, "system": system,
                  "messages": historial[-10:] + [{"role": "user", "content": mensaje}]},
            timeout=30)
        response_data = r.json()

        if "content" not in response_data:
            return jsonify({"respuesta": f"Error: {response_data.get('error',{}).get('message','')}", "accion": None})

        texto = response_data["content"][0]["text"]
        accion_resultado = None
        if "ODOO_ACTION:" in texto:
            partes = texto.split("ODOO_ACTION:")
            texto_limpio = partes[0].strip()
            try:
                accion_str = partes[1].strip()
                if "```" in accion_str:
                    accion_str = accion_str.split("```")[0].strip()
                accion = json.loads(accion_str)
                # SAFETY: bloquear creates/writes/unlinks en el chat
                if accion.get("method") in ("create", "write", "unlink"):
                    accion_resultado = f"BLOQUEADO: metodo '{accion.get('method')}' no permitido via chat. Usa endpoint admin."
                else:
                    accion_resultado = ejecutar_accion_odoo(accion)
            except Exception as e:
                accion_resultado = f"Error: {str(e)}"
        else:
            texto_limpio = texto

        return jsonify({"respuesta": texto_limpio, "accion": accion_resultado})
    except requests.Timeout:
        return jsonify({"respuesta": "Timeout, intenta de nuevo.", "accion": None})
    except Exception as e:
        return jsonify({"respuesta": f"Error: {str(e)}", "accion": None})

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
