from flask import Flask, request, jsonify, send_from_directory
import requests, os, json

app = Flask(__name__, static_folder="static")

ODOO_URL      = "https://pomelo-derma.odoo.com"
ODOO_DB       = "pomelo-derma"
ODOO_USER     = os.environ.get("ODOO_USER", "carolmartinezderma@gmail.com")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

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
        return "Error: No se pudo conectar a Odoo"

    model  = accion.get("model")
    method = accion.get("method")
    args   = accion.get("args", [])
    kwargs = accion.get("kwargs", {})

    if not model or not method:
        return "Error: Falta model o method"

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
                    line = f"• {name}"
                    if extras:
                        line += f" ({', '.join(extras[:4])})"
                    lines.append(line)
                total = len(resultado)
                resp = "\n".join(lines)
                if total > 25:
                    resp += f"\n... y {total-25} más"
                return f"{total} registros:\n{resp}"
            else:
                return f"IDs: {resultado[:10]}"
        elif isinstance(resultado, bool):
            return "Operación completada exitosamente"
        elif isinstance(resultado, int):
            return f"Registro creado con ID: {resultado}"
        else:
            return f"Resultado: {resultado}"
    except Exception as e:
        return f"Error: {str(e)}"

def get_odoo_summary():
    try:
        s, uid = odoo_session()
        if not s:
            return {"status": "sin conexion"}
        n_productos = len(odoo_call(s, "product.template", "search", [[]], {"limit": 2000}) or [])
        n_clientes  = len(odoo_call(s, "res.partner", "search", [[["customer_rank",">",0]]], {"limit": 2000}) or [])
        n_pos       = len(odoo_call(s, "pos.order", "search", [[]], {"limit": 2000}) or [])
        n_facturas  = len(odoo_call(s, "account.move", "search", [[["move_type","=","out_invoice"]]], {"limit": 2000}) or [])
        return {"status": "conectado", "productos": n_productos, "clientes": n_clientes,
                "ventas_pos": n_pos, "facturas": n_facturas}
    except:
        return {"status": "error"}

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/health")
def health():
    return "OK", 200

@app.route("/api/estado")
def estado():
    return jsonify(get_odoo_summary())

@app.route("/api/chat", methods=["POST"])
def chat():
    data      = request.json
    mensaje   = data.get("mensaje", "")
    historial = data.get("historial", [])

    if not ANTHROPIC_KEY:
        return jsonify({"respuesta": "API Key de Anthropic no configurada", "accion": None})

    contexto = get_odoo_summary()

    system = """Eres Claudio, el agente IA de Pomelo Derma — farmacia dermatologica premium en Mexico.
Tienes acceso COMPLETO a la API de Odoo y puedes ejecutar CUALQUIER operacion.

ESTADO ACTUAL DE ODOO:
""" + json.dumps(contexto, ensure_ascii=False) + """

MODULOS DISPONIBLES Y SUS MODELOS:

PUNTO DE VENTA (POS):
- pos.order → ventas del POS (campos: name, pos_reference, amount_total, date_order, state, partner_id, lines)
- pos.order.line → lineas de cada venta (campos: product_id, qty, price_unit, price_subtotal)
- pos.session → sesiones del POS (campos: name, state, start_at, stop_at, cash_real_ending)
- pos.config → configuracion del POS

PRODUCTOS:
- product.template → productos (campos: name, list_price, type, available_in_pos, barcode, active)
- product.product → variantes de productos

CLIENTES Y PROVEEDORES:
- res.partner → contactos (campos: name, email, phone, customer_rank, supplier_rank)

FACTURACION:
- account.move → facturas (move_type: out_invoice=venta, in_invoice=compra)
- account.move.line → lineas de factura

INVENTARIO:
- stock.quant → stock actual (campos: product_id, quantity, location_id)
- stock.move → movimientos de inventario

COMPRAS:
- purchase.order → ordenes de compra (campos: name, partner_id, amount_total, state, date_order)
- purchase.order.line → lineas de OC

VENTAS:
- sale.order → ordenes de venta

EMPRESA:
- res.company → configuracion empresa
- res.users → usuarios del sistema

METODOS COMUNES:
- search_read → buscar y leer: args=[[dominio]], kwargs={"fields":["campo1","campo2"],"limit":N,"order":"campo desc"}
- write → actualizar: args=[[ids], {campos}]
- create → crear: args=[{campos}]
- search → solo IDs: args=[[dominio]]

EJEMPLOS IMPORTANTES:

Usuario: "ventas recientes del POS" o "ultimas ventas" o "ventas de hoy"
ODOO_ACTION:{"model":"pos.order","method":"search_read","args":[[["state","in",["done","paid","invoiced"]]]],"kwargs":{"fields":["name","amount_total","date_order","partner_id","state"],"limit":10,"order":"date_order desc"}}

Usuario: "cuanto vendi hoy"
ODOO_ACTION:{"model":"pos.order","method":"search_read","args":[[["state","in",["done","paid","invoiced"]]]],"kwargs":{"fields":["name","amount_total","date_order"],"limit":100,"order":"date_order desc"}}

Usuario: "facturas pendientes"
ODOO_ACTION:{"model":"account.move","method":"search_read","args":[[["move_type","=","out_invoice"],["state","=","posted"],["payment_state","!=","paid"]]],"kwargs":{"fields":["name","partner_id","amount_total","invoice_date_due"],"limit":20}}

Usuario: "productos con stock bajo"
ODOO_ACTION:{"model":"stock.quant","method":"search_read","args":[[["quantity","<",5]]],"kwargs":{"fields":["product_id","quantity","location_id"],"limit":20}}

Usuario: "activa todos los productos para el POS"
ODOO_ACTION:{"model":"product.template","method":"write","args":[[],{"available_in_pos":true}],"kwargs":{}}

REGLAS:
1. Ventas del POS = modelo pos.order (NO account.move)
2. Facturas = modelo account.move con move_type=out_invoice
3. Para write masivo usa args=[[]] — el sistema obtiene todos los IDs automaticamente
4. Responde en espanol, breve y directo
5. Siempre incluye ODOO_ACTION cuando el usuario pide consultar o modificar datos
6. Nunca inventes datos — si no hay registros, dilo claramente"""

    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 1500, "system": system,
                  "messages": historial + [{"role": "user", "content": mensaje}]},
            timeout=30)

        response_data = r.json()
        if "content" not in response_data:
            error_msg = response_data.get("error", {}).get("message", "Error desconocido")
            return jsonify({"respuesta": f"Error API: {error_msg}", "accion": None})

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
                accion_resultado = f"Error ejecutando accion: {str(e)}"
        else:
            texto_limpio = texto

        return jsonify({"respuesta": texto_limpio, "accion": accion_resultado})

    except requests.Timeout:
        return jsonify({"respuesta": "Timeout — intenta de nuevo.", "accion": None})
    except Exception as e:
        return jsonify({"respuesta": f"Error: {str(e)}", "accion": None})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
