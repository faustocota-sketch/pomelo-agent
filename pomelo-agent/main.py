from flask import Flask, request, jsonify, send_from_directory
import requests, os, json, base64, hmac, hashlib
from datetime import date

app = Flask(__name__, static_folder="static")

ODOO_URL      = "https://pomelo-derma.odoo.com"
ODOO_DB       = "pomelo-derma"
ODOO_USER     = os.environ.get("ODOO_USER", "carolmartinezderma@gmail.com")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN", "")
MP_WEBHOOK_SECRET = os.environ.get("MP_WEBHOOK_SECRET", "")

# ============================================================
# ODOO
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
        return "ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ No se pudo conectar a Odoo"
    model  = accion.get("model")
    method = accion.get("method")
    args   = accion.get("args", [])
    kwargs = accion.get("kwargs", {})
    if not model or not method:
        return "ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ Falta model o method"
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
                    line = f"ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¢ {name}"
                    if extras:
                        line += f" ({', '.join(extras[:4])})"
                    lines.append(line)
                total = len(resultado)
                resp = "\n".join(lines)
                if total > 25:
                    resp += f"\n... y {total-25} mÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ¡s"
                return f"ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ {total} registros:\n{resp}"
            else:
                return f"ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ Completado: {resultado[:10]}"
        elif isinstance(resultado, bool):
            return "ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ OperaciÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ³n completada"
        elif isinstance(resultado, int):
            return f"ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ Creado con ID: {resultado}"
        else:
            return f"ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ {resultado}"
    except Exception as e:
        return f"ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ Error: {str(e)}"

def get_odoo_summary():
    try:
        s, uid = odoo_session()
        if not s:
            return {"status": "sin conexion"}
        n_productos   = len(odoo_call(s, "product.template", "search", [[]], {"limit": 2000}) or [])
        n_clientes    = len(odoo_call(s, "res.partner", "search", [[["customer_rank",">",0]]], {"limit": 2000}) or [])
        n_proveedores = len(odoo_call(s, "res.partner", "search", [[["supplier_rank",">",0]]], {"limit": 2000}) or [])
        n_pos         = len(odoo_call(s, "pos.order", "search", [[]], {"limit": 2000}) or [])
        n_oc          = len(odoo_call(s, "purchase.order", "search", [[]], {"limit": 2000}) or [])
        n_facturas    = len(odoo_call(s, "account.move", "search", [[["move_type","=","out_invoice"]]], {"limit": 2000}) or [])
        return {"status": "conectado", "productos": n_productos, "clientes": n_clientes,
                "proveedores": n_proveedores, "ventas_pos": n_pos,
                "ordenes_compra": n_oc, "facturas": n_facturas}
    except:
        return {"status": "error"}

# ============================================================
# WEBHOOK MERCADO PAGO ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ ODOO
# ============================================================
def get_or_create_mp_journal(s):
    """Obtiene o crea el diario bancario de Mercado Pago en Odoo"""
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
    """Registra un pago de MP como movimiento bancario en Odoo"""
    s, uid = odoo_session()
    if not s:
        return False, "Sin conexiÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ³n a Odoo"
    try:
        monto       = float(pago_mp.get("transaction_amount", 0))
        mp_id       = str(pago_mp.get("id", ""))
        status      = pago_mp.get("status", "")
        email       = pago_mp.get("payer", {}).get("email", "")
        descripcion = pago_mp.get("description", f"Pago MP #{mp_id}")
        fecha_raw   = pago_mp.get("date_approved", "")
        fecha       = fecha_raw[:10] if fecha_raw else date.today().isoformat()

        if status != "approved":
            return True, f"Pago {mp_id} ignorado (status: {status})"

        # Verificar si ya existe para no duplicar
        existentes = odoo_call(s, "account.bank.statement.line", "search_read",
            [[["payment_ref", "ilike", f"MP-{mp_id}"]]], {"fields": ["id"], "limit": 1})
        if existentes:
            return True, f"Pago MP #{mp_id} ya registrado ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ ignorado"

        journal_id = get_or_create_mp_journal(s)

        # Buscar cliente por email
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
        return True, f"ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ Pago MP #{mp_id} registrado: ${monto:,.2f} MXN"

    except Exception as e:
        return False, str(e)

# ============================================================
# OCR DE OC
# ============================================================
def procesar_oc_imagen(image_base64, media_type="image/jpeg"):
    prompt_ocr = """Analiza esta orden de compra/factura de proveedor y extrae TODA la informacion.
Responde SOLO con un JSON valido con esta estructura:
{
  "proveedor": {"nombre": "", "rfc": null, "email": null, "telefono": null},
  "numero_oc": null,
  "fecha": null,
  "fecha_vencimiento": null,
  "moneda": "MXN",
  "productos": [{"nombre": "", "cantidad": 1, "precio_unitario": 0.0, "subtotal": 0.0}],
  "subtotal": 0.0,
  "iva": 0.0,
  "total": 0.0,
  "condiciones_pago": null,
  "notas": null
}
Fechas en formato YYYY-MM-DD. Solo el JSON, sin texto adicional."""
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 2000,
                  "messages": [{"role": "user", "content": [
                      {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_base64}},
                      {"type": "text", "text": prompt_ocr}
                  ]}]},
            timeout=30)
        texto = r.json()["content"][0]["text"]
        if "```" in texto:
            texto = texto.split("```")[1].replace("json","").strip()
        return json.loads(texto)
    except Exception as e:
        return {"error": str(e)}

def crear_oc_en_odoo(datos_oc):
    s, uid = odoo_session()
    if not s:
        return {"success": False, "error": "Sin conexiÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ³n a Odoo"}
    try:
        nombre_prov = datos_oc.get("proveedor", {}).get("nombre", "Proveedor Desconocido")
        proveedores = odoo_call(s, "res.partner", "search_read",
            [[["name", "ilike", nombre_prov]]], {"fields": ["id","name"], "limit": 1})
        if proveedores:
            partner_id = proveedores[0]["id"]
            msg_prov = f"Proveedor encontrado: {proveedores[0]['name']}"
        else:
            prov_data = datos_oc.get("proveedor", {})
            partner_id = odoo_call(s, "res.partner", "create", [{
                "name": nombre_prov,
                "email": prov_data.get("email") or "",
                "phone": prov_data.get("telefono") or "",
                "vat": prov_data.get("rfc") or "",
                "supplier_rank": 1,
                "is_company": True,
            }])
            msg_prov = f"Proveedor creado: {nombre_prov}"

        lineas = []
        for prod in datos_oc.get("productos", []):
            nombre_prod = prod.get("nombre", "")
            productos = odoo_call(s, "product.product", "search_read",
                [[["name", "ilike", nombre_prod]]], {"fields": ["id","name"], "limit": 1})
            if productos:
                product_id = productos[0]["id"]
            else:
                tmpl_id = odoo_call(s, "product.template", "create", [{
                    "name": nombre_prod, "type": "consu", "purchase_ok": True,
                }])
                prods = odoo_call(s, "product.product", "search_read",
                    [[["product_tmpl_id","=",tmpl_id]]], {"fields":["id"],"limit":1})
                product_id = prods[0]["id"] if prods else None
            if product_id:
                lineas.append((0, 0, {
                    "product_id": product_id,
                    "product_qty": float(prod.get("cantidad", 1)),
                    "price_unit": float(prod.get("precio_unitario", 0)),
                    "name": nombre_prod,
                }))

        oc_vals = {"partner_id": partner_id, "order_line": lineas}
        if datos_oc.get("fecha"):
            oc_vals["date_order"] = datos_oc["fecha"]
        if datos_oc.get("notas"):
            oc_vals["notes"] = datos_oc["notas"]

        oc_id = odoo_call(s, "purchase.order", "create", [oc_vals])
        return {
            "success": True, "oc_id": oc_id, "proveedor": msg_prov,
            "productos": len(lineas), "total": datos_oc.get("total", 0),
            "mensaje": f"ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ OC #{oc_id} creada\n{msg_prov}\n{len(lineas)} productos\nTotal: ${datos_oc.get('total',0):,.2f} {datos_oc.get('moneda','MXN')}"
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

# ============================================================
# SYSTEM PROMPT
# ============================================================
SYSTEM_PROMPT = """Eres Claudio, agente IA de Pomelo Derma ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ farmacia dermatologica premium en Mexico.
Tienes acceso COMPLETO a Odoo. Estado actual: {contexto}

MODELOS ODOO:
- pos.order ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ ventas POS (fields: name, amount_total, date_order, state, partner_id)
- purchase.order ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ OC (fields: name, partner_id, amount_total, state, date_order)
- res.partner ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ clientes/proveedores (customer_rank>0 = cliente, supplier_rank>0 = proveedor)
- account.move ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ facturas (move_type: out_invoice=venta, in_invoice=compra proveedor)
- account.bank.statement.line ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ movimientos bancarios MP (fields: payment_ref, amount, date, journal_id)
- product.template ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ productos
- stock.quant ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ inventario (fields: product_id, quantity, location_id)

EJEMPLOS:
Ventas POS: ODOO_ACTION:{{"model":"pos.order","method":"search_read","args":[[["state","in",["done","paid","invoiced"]]]],"kwargs":{{"fields":["name","amount_total","date_order","partner_id"],"limit":10,"order":"date_order desc"}}}}
OC pendientes: ODOO_ACTION:{{"model":"purchase.order","method":"search_read","args":[[["state","in",["draft","sent","purchase"]]]],"kwargs":{{"fields":["name","partner_id","amount_total","date_order","state"],"limit":20}}}}
Cuentas por pagar: ODOO_ACTION:{{"model":"account.move","method":"search_read","args":[[["move_type","=","in_invoice"],["payment_state","!=","paid"]]],"kwargs":{{"fields":["name","partner_id","amount_total","invoice_date_due"],"limit":20,"order":"invoice_date_due asc"}}}}
Movimientos MP: ODOO_ACTION:{{"model":"account.bank.statement.line","method":"search_read","args":[[["journal_id.code","=","MP"]]],"kwargs":{{"fields":["payment_ref","amount","date","partner_id"],"limit":20,"order":"date desc"}}}}
Stock bajo: ODOO_ACTION:{{"model":"stock.quant","method":"search_read","args":[[["quantity","<",5],["location_id.usage","=","internal"]]],"kwargs":{{"fields":["product_id","quantity"],"limit":30}}}}

REGLAS: Responde en espanol. SIEMPRE incluye ODOO_ACTION para consultas o acciones. Ventas=pos.order, Facturas proveedor=account.move in_invoice, Movimientos MP=account.bank.statement.line."""

# ============================================================
# FLASK ROUTES
# ============================================================
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/health")
def health():
    return "OK", 200

@app.route("/api/estado")
def estado():
    return jsonify(get_odoo_summary())

@app.route("/mp/webhook", methods=["GET", "POST"])
def mp_webhook():
    if request.method == "GET":
        return jsonify({"status": "ok", "service": "Pomelo Derma MP Webhook"}), 200
    try:
        data  = request.json or {}
        topic = data.get("type") or request.args.get("topic", "")
        print(f"[MP WEBHOOK] {topic}: {json.dumps(data)[:200]}")

        if topic not in ["payment", "point_integration_v2"]:
            return jsonify({"status": "ignored"}), 200

        payment_id = None
        if topic == "payment":
            payment_id = data.get("data", {}).get("id")
        elif topic == "point_integration_v2":
            payment_id = data.get("data", {}).get("payment_id")

        if not payment_id:
            return jsonify({"status": "no payment_id"}), 200

        r = requests.get(f"https://api.mercadopago.com/v1/payments/{payment_id}",
            headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}, timeout=10)
        pago_mp = r.json()

        success, mensaje = registrar_pago_odoo(pago_mp)
        print(f"[MP WEBHOOK] {mensaje}")
        return jsonify({"status": "ok", "mensaje": mensaje}), 200

    except Exception as e:
        print(f"[MP WEBHOOK] Error: {str(e)}")
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/api/ocr-oc", methods=["POST"])
def ocr_oc():
    try:
        data = request.json
        image_base64 = data.get("image")
        media_type   = data.get("media_type", "image/jpeg")
        if not image_base64:
            return jsonify({"error": "No se recibiÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ³ imagen"}), 400
        datos_oc  = procesar_oc_imagen(image_base64, media_type)
        if "error" in datos_oc:
            return jsonify({"error": datos_oc["error"]}), 500
        resultado = crear_oc_en_odoo(datos_oc)
        return jsonify({"datos_extraidos": datos_oc, "resultado_odoo": resultado})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/chat", methods=["POST"])
def chat():
    data      = request.json
    mensaje   = data.get("mensaje", "")
    historial = data.get("historial", [])
    if not ANTHROPIC_KEY:
        return jsonify({"respuesta": "ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ API Key no configurada", "accion": None})
    contexto = get_odoo_summary()
    system   = SYSTEM_PROMPT.replace("{contexto}", json.dumps(contexto, ensure_ascii=False))
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 1500, "system": system,
                  "messages": historial + [{"role": "user", "content": mensaje}]},
            timeout=30)
        response_data = r.json()
        if "content" not in response_data:
            return jsonify({"respuesta": f"ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ Error: {response_data.get('error',{}).get('message','')}", "accion": None})
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
                accion_resultado = f"ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ Error: {str(e)}"
        else:
            texto_limpio = texto
        return jsonify({"respuesta": texto_limpio, "accion": accion_resultado})
    except requests.Timeout:
        return jsonify({"respuesta": "ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ Timeout ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ intenta de nuevo.", "accion": None})
    except Exception as e:
        return jsonify({"respuesta": f"ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ Error: {str(e)}", "accion": None})


# ============================================================
# REPORTE TODAS LAS TRANSACCIONES MP -> ODOO
# ============================================================

def sincronizar_reporte_mp(dias=30):
    """
    Genera y procesa el Reporte de Todas las Transacciones de MP.
    Incluye ventas Point, comisiones, retiros, transferencias - TODO.
    """
    if not MP_ACCESS_TOKEN:
        return {"error": "MP_ACCESS_TOKEN no configurado", "total": 0}
    s, uid = odoo_session()
    if not s:
        return {"error": "Sin conexion a Odoo", "total": 0}
    from datetime import datetime, timedelta
    fecha_fin = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    fecha_ini = (datetime.utcnow() - timedelta(days=dias)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    try:
        # 1. Generar el reporte
        r = requests.post("https://api.mercadopago.com/v1/account/settlement-report",
            headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}", "Content-Type": "application/json"},
            json={"begin_date": fecha_ini, "end_date": fecha_fin},
            timeout=30)
        reporte = r.json()
        if r.status_code != 200 or "id" not in str(reporte):
            # Intentar listar reportes existentes
            r2 = requests.get("https://api.mercadopago.com/v1/account/settlement-report/list",
                headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
                timeout=15)
            lista = r2.json()
            if isinstance(lista, list) and lista:
                reporte_url = lista[0].get("file_url") or lista[0].get("url")
                if reporte_url:
                    return {"mensaje": f"Reporte disponible", "url": reporte_url, "total": len(lista)}
            return {"error": f"No se pudo generar reporte: {reporte}", "total": 0}

        # 2. Descargar el CSV del reporte
        file_url = reporte.get("file_url") or reporte.get("url")
        if not file_url:
            return {"mensaje": "Reporte generado, procesando...", "reporte_id": reporte.get("id"), "total": 0}

        r3 = requests.get(file_url, headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}, timeout=30)
        csv_content = r3.text

        # 3. Parsear CSV y registrar en Odoo
        import csv, io
        journal_id = get_or_create_mp_journal(s)
        ok = 0; skip = 0
        reader = csv.DictReader(io.StringIO(csv_content))
        for row in reader:
            mp_id = row.get("SOURCE_ID", row.get("TRANSACTION_ID", ""))
            monto_str = row.get("NET_CREDIT_AMOUNT", row.get("GROSS_AMOUNT", "0"))
            try:
                monto = float(str(monto_str).replace(",","").strip() or 0)
            except:
                monto = 0
            if monto == 0 or not mp_id:
                continue
            fecha_raw = row.get("DATE", row.get("OPERATION_DATE", ""))
            fecha = fecha_raw[:10] if fecha_raw else date.today().isoformat()
            desc = row.get("PAYMENT_METHOD_TYPE", row.get("TRANSACTION_TYPE", "Movimiento MP"))

            existentes = odoo_call(s, "account.bank.statement.line", "search_read",
                [[[" payment_ref", "ilike", f"MP-{mp_id}"]]], {"fields": ["id"], "limit": 1})
            if existentes:
                skip += 1; continue

            vals = {"journal_id": journal_id, "date": fecha,
                    "payment_ref": f"MP-{mp_id} | {desc}", "amount": monto}
            odoo_call(s, "account.bank.statement.line", "create", [vals])
            ok += 1

        return {"mensaje": "Reporte completo sincronizado", "registrados": ok, "ya_existian": skip}
    except Exception as e:
        return {"error": str(e), "total": 0}


@app.route("/mp/reporte", methods=["GET", "POST"])
def mp_reporte():
    """
    Endpoint para sincronizar el Reporte de Todas las Transacciones de MP.
    Incluye ventas Point, comisiones, retiros, transferencias - TODO el estado de cuenta.
    GET  -> sincroniza 30 dias
    POST -> {"dias": N} para N dias
    """
    try:
        dias = 30
        if request.method == "POST" and request.json:
            dias = int(request.json.get("dias", 30))
        dias = min(dias, 60)
        resultado = sincronizar_reporte_mp(dias)
        print(f"[MP REPORTE] {resultado}")
        return jsonify(resultado), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route("/mp/reporte-webhook", methods=["POST"])
def mp_reporte_webhook():
    """
    Recibe notificacion de MP cuando el reporte de Todas las Transacciones esta listo.
    Payload segun doc MP: {transaction_id, generation_date, files:[{type,url}], signature}
    """
    try:
        data = request.json or {}
        print(f"[MP REPORTE WEBHOOK] {json.dumps(data)[:400]}")

        # Buscar URL del CSV en files[] segun estructura de MP
        files = data.get("files", [])
        file_url = None
        for f in files:
            if "csv" in str(f.get("type", "")):
                file_url = f.get("url")
                break
        if not file_url and files:
            file_url = files[0].get("url")
        if not file_url:
            print(f"[MP REPORTE WEBHOOK] Sin URL: {data}")
            return jsonify({"status": "ok", "mensaje": "sin archivo aun"}), 200

        # Descargar el CSV
        r = requests.get(file_url,
            headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
            timeout=30)

        if r.status_code != 200:
            return jsonify({"status": "error", "error": f"No se pudo descargar: {r.status_code}"}), 200

        # Procesar CSV y subir a Odoo
        import csv, io
        s, uid = odoo_session()
        if not s:
            return jsonify({"status": "error", "error": "Sin conexion Odoo"}), 200

        journal_id = get_or_create_mp_journal(s)
        ok = 0; skip = 0; errors = 0

        reader = csv.DictReader(io.StringIO(r.text))
        for row in reader:
            try:
                mp_id = str(row.get("SOURCE_ID") or row.get("EXTERNAL_ID") or row.get("PAYMENT_ID") or "")
                monto_str = str(row.get("NET_CREDIT_AMOUNT") or row.get("GROSS_AMOUNT") or row.get("TRANSACTION_AMOUNT") or "0")
                monto = float(monto_str.replace(",","").strip() or 0)
                if monto == 0 or not mp_id:
                    continue
                fecha_raw = str(row.get("DATE") or row.get("DATE_CREATED") or "")
                fecha = fecha_raw[:10] if fecha_raw else date.today().isoformat()
                desc = str(row.get("PAYMENT_METHOD_TYPE") or row.get("TRANSACTION_TYPE") or "Movimiento MP")

                existentes = odoo_call(s, "account.bank.statement.line", "search_read",
                    [[[" payment_ref", "ilike", f"MP-{mp_id}"]]], {"fields": ["id"], "limit": 1})
                if existentes:
                    skip += 1; continue

                odoo_call(s, "account.bank.statement.line", "create", [{
                    "journal_id": journal_id, "date": fecha,
                    "payment_ref": f"MP-{mp_id} | {desc}", "amount": monto
                }])
                ok += 1
            except:
                errors += 1

        resultado = {"status": "ok", "registrados": ok, "ya_existian": skip, "errores": errors}
        print(f"[MP REPORTE WEBHOOK] {resultado}")
        return jsonify(resultado), 200

    except Exception as e:
        print(f"[MP REPORTE WEBHOOK] Error: {str(e)}")
        return jsonify({"status": "error", "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

# ============================================================
# SINCRONIZACION DIARIA MP ÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂÃÂÃÂ ODOO CONTABILIDAD
# ============================================================

def sincronizar_movimientos_mp(dias=1):
    if not MP_ACCESS_TOKEN:
        return {"error": "MP_ACCESS_TOKEN no configurado", "total": 0}
    s, uid = odoo_session()
    if not s:
        return {"error": "Sin conexion a Odoo", "total": 0}
    from datetime import datetime, timedelta
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
            tipo = mov.get("payment_type_id", "")
            desc = mov.get("description", f"Pago MP #{mp_id}")
            fecha_raw = mov.get("date_approved", "")
            fecha = fecha_raw[:10] if fecha_raw else date.today().isoformat()
            if monto == 0:
                continue
            existentes = odoo_call(s, "account.bank.statement.line", "search_read",
                [[["payment_ref", "ilike", f"MP-{mp_id}"]]], {"fields": ["id"], "limit": 1})
            if existentes:
                skip += 1
                continue
            vals = {"journal_id": journal_id, "date": fecha,
                    "payment_ref": f"MP-{mp_id} | {desc}", "amount": monto}
            email = mov.get("metadata", {}).get("payer_detail", {}).get("email", "")
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
        dias = 1
        if request.method == "POST" and request.json:
            dias = int(request.json.get("dias", 1))
        dias = min(dias, 30)
        resultado = sincronizar_movimientos_mp(dias)
        print(f"[MP SYNC] {resultado}")
        return jsonify(resultado), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
