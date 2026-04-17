from flask import Flask, request, jsonify, send_from_directory
import requests, os, json, base64, hmac, hashlib, logging
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
    try:
        r = s.post(f"{ODOO_URL}/web/dataset/call_kw", json={
            "jsonrpc": "2.0", "method": "call",
            "params": {"model": model, "method": method, "args": args, "kwargs": kwargs}
        }, timeout=20)
        result = r.json()
        if "error" in result:
            raise Exception(result["error"]["data"].get("message", "Error Odoo"))
        return result.get("result")
    except Exception as e:
        raise Exception(str(e))

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
                    name = r.get("name") or r.get("display_name") or r.get("pos_reference") or str(r.get("id",""))
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

# ============================================================
# WEBHOOK MERCADO PAGO -> ODOO
# ============================================================
def get_or_create_mp_journal(s):
    diarios = odoo_call(s, "account.journal", "search_read",
                       [[["code", "=", "MP"]]], {"fields": ["id","name"], "limit": 1})
    if diarios:
        return diarios[0]["id"]
    return odoo_call(s, "account.journal", "create", [{
        "name": "Mercado Pago",
        "type": "bank",
        "code": "MP",
    }])

def registrar_pago_odoo(pago_mp):
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

        existentes = odoo_call(s, "account.bank.statement.line", "search_read",
                              [[["payment_ref", "ilike", f"MP-{mp_id}"]]], {"fields": ["id"], "limit": 1})
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
            "payment_ref": f"MP-{mp_id} | {descripcion}",
            "amount": monto,
        }
        if partner_id:
            vals["partner_id"] = partner_id

        odoo_call(s, "account.bank.statement.line", "create", [vals])
        return True, f"Pago MP #{mp_id} registrado: ${monto:,.2f} MXN"
    except Exception as e:
        return False, str(e)

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
- account.bank.statement.line: movimientos bancarios MP (fields: payment_ref, amount, date, journal_id)
- product.template: productos
- stock.quant: inventario (fields: product_id, quantity, location_id)

EJEMPLOS:
Ventas POS: ODOO_ACTION:{{"model":"pos.order","method":"search_read","args":[[["state","in",["done","paid","invoiced"]]]],"kwargs":{{"fields":["name","amount_total","date_order","partner_id"],"limit":10,"order":"date_order desc"}}}}
OC pendientes: ODOO_ACTION:{{"model":"purchase.order","method":"search_read","args":[[["state","in",["draft","sent","purchase"]]]],"kwargs":{{"fields":["name","partner_id","amount_total","date_order","state"],"limit":20}}}}
Cuentas por pagar: ODOO_ACTION:{{"model":"account.move","method":"search_read","args":[[["move_type","=","in_invoice"],["payment_state","!=","paid"]]],"kwargs":{{"fields":["name","partner_id","amount_total","invoice_date_due"],"limit":20,"order":"invoice_date_due asc"}}}}
Movimientos MP: ODOO_ACTION:{{"model":"account.bank.statement.line","method":"search_read","args":[[["journal_id.code","=","MP"]]],"kwargs":{{"fields":["payment_ref","amount","date","partner_id"],"limit":20,"order":"date desc"}}}}
Stock bajo: ODOO_ACTION:{{"model":"stock.quant","method":"search_read","args":[[["quantity","<",5],["location_id.usage","=","internal"]]],"kwargs":{{"fields":["product_id","quantity"],"limit":30}}}}

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
# MERCADO PAGO ENDPOINTS
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

        success, mensaje = registrar_pago_odoo(pago_mp)
        log.info(f"[MP WEBHOOK] {mensaje}")
        return jsonify({"status": "ok", "mensaje": mensaje}), 200
    except Exception as e:
        log.error(f"[MP WEBHOOK] Error: {str(e)}")
        return jsonify({"status": "error", "error": str(e)}), 500

def sincronizar_movimientos_mp(dias=2):
    """Cron de respaldo: captura pagos que el webhook pudo haber perdido."""
    if not MP_ACCESS_TOKEN:
        return {"error": "MP_ACCESS_TOKEN no configurado", "total": 0}

    s, uid = odoo_session()
    if not s:
        return {"error": "Sin conexion a Odoo", "total": 0}

    fecha_desde = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00.000-06:00")

    try:
        r = requests.get("https://api.mercadopago.com/v1/payments/search",
                        headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
                        params={"sort": "date_created", "criteria": "desc", "begin_date": fecha_desde, "limit": 100, "offset": 0},
                        timeout=15)
        data = r.json()
        movimientos = data.get("results", [])

        if not movimientos:
            return {"mensaje": "Sin movimientos nuevos", "total": 0}

        journal_id = get_or_create_mp_journal(s)
        ok = 0
        skip = 0

        for mov in movimientos:
            mp_id = str(mov.get("id", ""))
            monto = float(mov.get("transaction_amount", 0))
            status = mov.get("status", "")
            desc = mov.get("description", f"Pago MP #{mp_id}")
            fecha_raw = mov.get("date_approved", "")
            fecha = fecha_raw[:10] if fecha_raw else date.today().isoformat()

            if monto == 0 or status != "approved":
                continue

            existentes = odoo_call(s, "account.bank.statement.line", "search_read",
                                  [[["payment_ref", "ilike", f"MP-{mp_id}"]]], {"fields": ["id"], "limit": 1})
            if existentes:
                skip += 1
                continue

            vals = {"journal_id": journal_id, "date": fecha,
                   "payment_ref": f"MP-{mp_id} | {desc}", "amount": monto}

            email = mov.get("payer", {}).get("email", "")
            if email:
                partners = odoo_call(s, "res.partner", "search_read",
                                    [[["email", "=", email]]], {"fields": ["id"], "limit": 1})
                if partners:
                    vals["partner_id"] = partners[0]["id"]

            odoo_call(s, "account.bank.statement.line", "create", [vals])
            ok += 1

        return {"mensaje": "Sincronizacion completa", "registrados": ok, "ya_existian": skip, "total_mp": len(movimientos)}
    except Exception as e:
        return {"error": str(e), "total": 0}

@app.route("/mp/sincronizar", methods=["GET", "POST"])
def mp_sincronizar():
    try:
        dias = 2
        if request.method == "POST" and request.json:
            dias = int(request.json.get("dias", 2))
            dias = min(dias, 30)
        resultado = sincronizar_movimientos_mp(dias)
        log.info(f"[MP SYNC] {resultado}")
        return jsonify(resultado), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ============================================================
# FACTURAS CFDI -> ODOO
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
        log.info(f'confirmar - proveedor_id={proveedor_id} keys={list(data.keys())}')
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
            return jsonify({'error': 'No hay productos vinculados a Odoo. Verifica los candidatos.'}), 400

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
# CHAT
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
