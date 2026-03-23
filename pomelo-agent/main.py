from flask import Flask, request, jsonify, send_from_directory
import requests, os, json

app = Flask(__name__, static_folder="static")

ODOO_URL      = "https://pomelo-derma.odoo.com"
ODOO_DB       = "pomelo-derma"
ODOO_USER     = os.environ.get("ODOO_USER", "carolmartinezderma@gmail.com")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ============================================================
# ODOO - Conexión y ejecución genérica
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
    """Ejecuta cualquier método de cualquier modelo de Odoo"""
    try:
        r = s.post(f"{ODOO_URL}/web/dataset/call_kw", json={
            "jsonrpc": "2.0", "method": "call",
            "params": {
                "model": model,
                "method": method,
                "args": args,
                "kwargs": kwargs
            }
        }, timeout=20)
        result = r.json()
        if "error" in result:
            raise Exception(result["error"]["data"].get("message", "Error Odoo"))
        return result.get("result")
    except Exception as e:
        raise Exception(str(e))

def ejecutar_accion_odoo(accion):
    """
    Ejecuta una acción genérica de Odoo.
    accion = {
        "model": "product.template",
        "method": "write",
        "args": [[1,2,3], {"available_in_pos": true}],
        "kwargs": {}  # opcional
    }
    """
    s, uid = odoo_session()
    if not s:
        return "❌ No se pudo conectar a Odoo"

    model   = accion.get("model")
    method  = accion.get("method")
    args    = accion.get("args", [])
    kwargs  = accion.get("kwargs", {})

    if not model or not method:
        return "❌ Acción incompleta: falta model o method"

    try:
        # Si el método es search o search_read sin IDs, primero busca
        resultado = odoo_call(s, model, method, args, kwargs)

        # Formatear resultado legible
        if isinstance(resultado, list):
            if len(resultado) == 0:
                return "✅ Operación completada. No se encontraron registros."
            if isinstance(resultado[0], dict):
                # Es un search_read — formatear bonito
                lines = []
                for r in resultado[:20]:  # máximo 20
                    name = r.get("name") or r.get("display_name") or str(r.get("id",""))
                    extras = []
                    for k, v in r.items():
                        if k not in ["id","name","display_name"] and v and v != False:
                            extras.append(f"{k}: {v}")
                    line = f"• {name}"
                    if extras:
                        line += f" ({', '.join(extras[:3])})"
                    lines.append(line)
                total = len(resultado)
                resp = "\n".join(lines)
                if total > 20:
                    resp += f"\n... y {total-20} más"
                return f"✅ {total} registros:\n{resp}"
            else:
                return f"✅ IDs afectados: {resultado[:10]}"
        elif isinstance(resultado, bool):
            return "✅ Operación completada exitosamente"
        elif isinstance(resultado, int):
            return f"✅ Registro creado con ID: {resultado}"
        else:
            return f"✅ Resultado: {resultado}"

    except Exception as e:
        return f"❌ Error ejecutando en Odoo: {str(e)}"

def get_odoo_summary():
    """Resumen rápido del estado de Odoo para el contexto"""
    try:
        s, uid = odoo_session()
        if not s:
            return {"status": "sin conexión"}

        n_productos = len(odoo_call(s, "product.template", "search", [[]], {"limit": 1000}) or [])
        n_clientes  = len(odoo_call(s, "res.partner", "search", [[["customer_rank",">",0]]], {"limit": 1000}) or [])
        n_facturas  = len(odoo_call(s, "account.move", "search", [[["move_type","=","out_invoice"]]], {"limit": 1000}) or [])

        return {
            "status": "conectado",
            "productos": n_productos,
            "clientes": n_clientes,
            "facturas": n_facturas
        }
    except:
        return {"status": "error al obtener resumen"}

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

@app.route("/api/chat", methods=["POST"])
def chat():
    data      = request.json
    mensaje   = data.get("mensaje", "")
    historial = data.get("historial", [])

    if not ANTHROPIC_KEY:
        return jsonify({"respuesta": "❌ API Key de Anthropic no configurada", "accion": None})

    # Resumen de contexto (rápido)
    contexto = get_odoo_summary()

    system = """Eres Claudio, el agente IA de Pomelo Derma — farmacia dermatológica premium en México.
Tienes acceso COMPLETO a la API de Odoo y puedes ejecutar CUALQUIER operación.

ESTADO ACTUAL DE ODOO:
""" + json.dumps(contexto, ensure_ascii=False) + """

CAPACIDADES:
Puedes ejecutar cualquier operación de Odoo usando este formato al FINAL de tu respuesta:

ODOO_ACTION:{"model":"MODELO","method":"METODO","args":[...],"kwargs":{...}}

MODELOS PRINCIPALES DE ODOO:
- product.template → productos (campos: name, list_price, type, available_in_pos, active)
- product.product → variantes de productos
- res.partner → clientes/proveedores (campos: name, email, phone, customer_rank, supplier_rank)
- account.move → facturas (move_type: out_invoice=venta, in_invoice=compra)
- account.move.line → líneas de factura
- pos.order → órdenes del punto de venta
- pos.session → sesiones del POS
- stock.quant → inventario/stock
- purchase.order → órdenes de compra
- sale.order → órdenes de venta
- hr.employee → empleados
- res.company → configuración de la empresa

MÉTODOS COMUNES:
- search_read → buscar y leer registros: args=[[dominio]], kwargs={"fields":["campo1","campo2"],"limit":N}
- write → actualizar: args=[[ids], {campos}]
- create → crear: args=[{campos}]
- unlink → eliminar: args=[[ids]]
- search → solo IDs: args=[[dominio]]

DOMINIOS DE BÚSQUEDA:
- Todos: [[]]
- Por nombre: [["name","ilike","texto"]]
- Por estado: [["state","=","draft"]]
- Combinados: [["customer_rank",">",0],["active","=",True]]

EJEMPLOS:
Usuario: "¿Cuántos productos tengo?"
ODOO_ACTION:{"model":"product.template","method":"search_read","args":[[]],"kwargs":{"fields":["name","list_price","available_in_pos"],"limit":1000}}

Usuario: "Activa todos los productos para el POS"
→ Primero busca IDs, luego actualiza. Usa search para obtener IDs:
ODOO_ACTION:{"model":"product.template","method":"write","args":[[],"available_in_pos_all"],"kwargs":{}}

Usuario: "Crea un cliente llamado Juan García con email juan@test.com"
ODOO_ACTION:{"model":"res.partner","method":"create","args":[{"name":"Juan García","email":"juan@test.com","customer_rank":1}],"kwargs":{}}

Usuario: "Muéstrame las últimas 10 facturas"
ODOO_ACTION:{"model":"account.move","method":"search_read","args":[[["move_type","=","out_invoice"]]],"kwargs":{"fields":["name","partner_id","amount_total","state","invoice_date"],"limit":10,"order":"id desc"}}

REGLAS IMPORTANTES:
1. Para write masivo (ej: activar todos los productos), usa args=[[]] como primer elemento — el sistema lo interpreta como "todos"
2. Siempre responde en español, de forma clara y directa
3. Si la operación puede afectar muchos registros, avisa cuántos serán afectados
4. Después de una acción, explica qué hiciste
5. Si no sabes exactamente qué campo usar, pregunta al usuario
6. NUNCA incluyas datos sensibles en tu respuesta"""

    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1500,
                "system": system,
                "messages": historial + [{"role": "user", "content": mensaje}]
            },
            timeout=30
        )

        response_data = r.json()

        if "content" not in response_data:
            error_msg = response_data.get("error", {}).get("message", "Error desconocido")
            return jsonify({"respuesta": f"❌ Error API Anthropic: {error_msg}", "accion": None})

        texto = response_data["content"][0]["text"]
        accion_resultado = None

        # Extraer y ejecutar acción de Odoo si existe
        if "ODOO_ACTION:" in texto:
            partes = texto.split("ODOO_ACTION:")
            texto_limpio = partes[0].strip()
            try:
                accion_str = partes[1].strip()
                # Limpiar posibles caracteres extra
                if "```" in accion_str:
                    accion_str = accion_str.split("```")[0].strip()

                accion = json.loads(accion_str)

                # Manejo especial para write masivo con [[]]
                if accion.get("method") == "write" and accion.get("args"):
                    args = accion["args"]
                    if len(args) >= 2 and args[0] == []:
                        # Obtener todos los IDs primero
                        s, uid = odoo_session()
                        if s:
                            ids = odoo_call(s, accion["model"], "search", [[]], {"limit": 5000})
                            if ids:
                                accion["args"][0] = ids

                accion_resultado = ejecutar_accion_odoo(accion)
            except json.JSONDecodeError as e:
                accion_resultado = f"⚠️ Error parseando acción: {str(e)}"
            except Exception as e:
                accion_resultado = f"❌ Error: {str(e)}"
        else:
            texto_limpio = texto

        return jsonify({
            "respuesta": texto_limpio,
            "accion": accion_resultado
        })

    except requests.Timeout:
        return jsonify({"respuesta": "❌ Timeout — Odoo tardó demasiado. Intenta de nuevo.", "accion": None})
    except Exception as e:
        return jsonify({"respuesta": f"❌ Error inesperado: {str(e)}", "accion": None})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
