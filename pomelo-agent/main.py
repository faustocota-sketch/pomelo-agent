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
            "params": {"model": model, "method": method, "args": args, "kwargs": kwargsj}
        }, timeout=20)
        result = r.json()
        if "error" in result:
            raise Exception(result["error"]["data"].get("message", "Error"))
        return result.get("result")
    except Exception as e:
        raise Exception(str(e))

def get_or_create_mp_journal(s):
    diarios = odoo_call(s, "account.journal", "search_read",
        [[["code", "=", "MP"]]], {"fields": ["id","name"], "limit": 1})
    if diarios:
        return diarios[0]["id"]
    return odoo_call(s, "account.journal", "create", [{
        "name": "Mercado Pago", "type": "bank", "code": "MP"
    }])

def sincronizar_movimientos_mp(dias=1):
    if not MP_ACCESS_TOKEN:
        return {"error": "MP_ACCESS_TOKEN no configurado", "total": 0}
    s, uid = odoo_session()
    if not s:
        return {"error": "Sin conexion a Odoo", "total": 0}
    from datetime import datetime, timedelta
    fecha_desde = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%dT00:00:00.000-06:00")
    try:
        r = requests.get("https://api.mercadopago.com/v1/account/movements/search",
            headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
            params={"begin_date": fecha_desde, "limit": 200, "offset": 0},
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
            monto = float(mov.get("amount", 0))
            tipo = mov.get("type", "")
            desc = mov.get("description", tipo)
            fecha_raw = mov.get("date", "")
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

def get_odoo_summary():
    try:
        s, uid = odoo_session()
        if not s:
            return {"status": "sin conexion"}
        n_productos = len(odoo_call(s, "product.template", "search", [[]], {"limit": 2000}) or [])
        n_pos = len(odoo_call(s, "pos.order", "search", [[]], {"limit": 2000}) or [])
        return {"status": "conectado", "productos": n_productos, "ventas_pos": n_pos}
    except:
        return {"status": "error"}

app = Flask(__name__, static_folder="static")

@app.route("/")
def index():
    return send_from_directory(~.static", "index.html")

@app.route("/health")
def health():
    return "OK", 200

@app.route("/api/estado")
def estado():
    return jsonify(get_odoo_summary())

@app.route("/mp/sincronizar", methods=["GET", "POST"])
def mp_sincronizar():
    try:
        dias = 1
        if request.method == "POST" and request.json:
            dias = int(request.json.get("dias", 1))
        dias = min(dias, 30)
        resultado = sincronizar_movimientos_mp(dias)
        return jsonify(resultado), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/mp/webhook", methods=["GET", "POST"])
def mp_webhook():
    return jsonify({"status": "ok"}), 200

@app.route("/api/chat", methods=["POST"])
def chat():
    return jsonify({"respuesta": "Claudio listo", "accion": None})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
