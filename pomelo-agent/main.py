from flask import Flask, request, jsonify, send_from_directory
import requests, os, json

app = Flask(__name__, static_folder="static")

ODOO_URL      = "https://pomelo-derma.odoo.com"
ODOO_DB       = "pomelo-derma"
ODOO_USER     = os.environ.get("ODOO_USER", "carolmartinezderma@gmail.com")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "Cortisol15.")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

def odoo_session():
    try:
        s = requests.Session()
        r = s.post(f"{ODOO_URL}/web/session/authenticate", json={
            "jsonrpc": "2.0", "method": "call",
            "params": {"db": ODOO_DB, "login": ODOO_USER, "password": ODOO_PASSWORD}
        }, timeout=10)
        uid = r.json().get("result", {}).get("uid")
        return s if uid else None
    except:
        return None

def odoo_call(session, model, method, args=[], kwargs={}):
    try:
        r = session.post(f"{ODOO_URL}/web/dataset/call_kw", json={
            "jsonrpc": "2.0", "method": "call",
            "params": {"model": model, "method": method, "args": args, "kwargs": kwargs}
        }, timeout=15)
        result = r.json()
        if "error" in result:
            raise Exception(result["error"]["data"].get("message", "Error"))
        return result.get("result")
    except Exception as e:
        raise Exception(str(e))

def get_context():
    try:
        s = odoo_session()
        if not s: return {"error": "Sin conexión a Odoo"}
        productos = odoo_call(s, "product.template", "search_read", [[]], {"fields": ["name","list_price","type"], "limit": 50})
        clientes  = odoo_call(s, "res.partner", "search_read", [[["customer_rank",">",0]]], {"fields": ["name","email","phone"], "limit": 50})
        facturas  = odoo_call(s, "account.move", "search_read", [[["move_type","=","out_invoice"]]], {"fields": ["name","partner_id","amount_total","state"], "limit": 20})
        return {"productos": productos or [], "clientes": clientes or [], "facturas": facturas or []}
    except Exception as e:
        return {"error": str(e)}

def ejecutar(accion):
    try:
        s = odoo_session()
        if not s: return "❌ Sin conexión a Odoo"
        tipo  = accion.get("tipo")
        datos = accion.get("datos", {})
        if tipo == "crear_factura":
            partner = odoo_call(s, "res.partner", "search_read", [[["name","ilike",datos.get("cliente","")]]], {"fields":["id","name"],"limit":1})
            if not partner: return f"❌ Cliente no encontrado"
            lineas = []
            for item in datos.get("productos", []):
                prod = odoo_call(s, "product.product", "search_read", [[["name","ilike",item.get("nombre","")]]], {"fields":["id","list_price"],"limit":1})
                if prod:
                    lineas.append((0,0,{"product_id":prod[0]["id"],"quantity":item.get("cantidad",1),"price_unit":item.get("precio",prod[0]["list_price"])}))
            fid = odoo_call(s, "account.move", "create", [{"move_type":"out_invoice","partner_id":partner[0]["id"],"invoice_line_ids":lineas}])
            return f"✅ Factura creada para {partner[0]['name']} (ID: {fid})"
        elif tipo == "crear_producto":
            pid = odoo_call(s, "product.template", "create", [{"name":datos.get("nombre"),"list_price":datos.get("precio",0),"type":datos.get("tipo","consu")}])
            return f"✅ Producto '{datos.get('nombre')}' creado (ID: {pid})"
        elif tipo == "crear_cliente":
            cid = odoo_call(s, "res.partner", "create", [{"name":datos.get("nombre"),"email":datos.get("email",""),"phone":datos.get("telefono",""),"customer_rank":1}])
            return f"✅ Cliente '{datos.get('nombre')}' creado (ID: {cid})"
        elif tipo == "activar_pos":
            productos = odoo_call(s, "product.template", "search_read", [[]], {"fields":["id","name"], "limit":1000})
            ids = [p["id"] for p in productos]
            odoo_call(s, "product.template", "write", [ids, {"available_in_pos": True}])
            return f"✅ {len(ids)} productos activados para el POS"
        return None
    except Exception as e:
        return f"❌ Error: {str(e)}"

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/chat", methods=["POST"])
def chat():
    data     = request.json
    mensaje  = data.get("mensaje", "")
    historial = data.get("historial", [])

    if not ANTHROPIC_KEY:
        return jsonify({"respuesta": "❌ API Key de Anthropic no configurada", "accion": None})

    contexto = get_context()

    system = f"""Eres Claudio, el agente IA de Pomelo Derma — farmacia dermatológica premium en México.
Tienes acceso completo a Odoo y ejecutas acciones reales.

CONTEXTO ODOO:
{json.dumps(contexto, ensure_ascii=False, indent=2)}

Cuando el usuario pida una acción incluye al FINAL de tu respuesta:
ACCION_JSON:{{"tipo":"crear_factura"|"crear_producto"|"crear_cliente"|"activar_pos","datos":{{...}}}}

Para facturas: cliente(str), productos([{{nombre,cantidad,precio}}])
Para productos: nombre, precio, tipo(consu/service/product)
Para clientes: nombre, email, telefono
Para activar POS: no necesita datos

Si es solo consulta NO incluyas ACCION_JSON.
Responde en español, breve y directo."""

    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version":"2023-06-01","content-type":"application/json"},
            json={"model":"claude-sonnet-4-20250514","max_tokens":1000,"system":system,
                  "messages": historial + [{"role":"user","content":mensaje}]},
            timeout=30)
        
        response_data = r.json()
        if "content" not in response_data:
            return jsonify({"respuesta": f"❌ Error API: {response_data.get('error', {}).get('message', 'Error desconocido')}", "accion": None})
        
        texto = response_data["content"][0]["text"]
        accion_resultado = None
        if "ACCION_JSON:" in texto:
            partes = texto.split("ACCION_JSON:")
            texto  = partes[0].strip()
            try: accion_resultado = ejecutar(json.loads(partes[1].strip()))
            except: pass
        return jsonify({"respuesta": texto, "accion": accion_resultado})
    except Exception as e:
        return jsonify({"respuesta": f"❌ Error: {str(e)}", "accion": None})

@app.route("/api/estado")
def estado():
    return jsonify(get_context())

@app.route("/health")
def health():
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
