from flask import Flask, request, jsonify, send_from_directory
import requests, os, json, base64, hmac, hashlib, csv, io, logging
from datetime import date, datetime, timedelta
import paramiko
from threading import Thread
import time

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("claudio-mp")

app = Flask(__name__, static_folder="static")

ODOO_URL      = "https://pomelo-derma.odoo.com"
ODOO_DB       = "pomelo-derma"
ODOO_USER     = os.environ.get("ODOO_USER", "carolmartinezderma@gmail.com")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MP_ACCESS_TOKEN   = os.environ.get("MP_ACCESS_TOKEN", "")
MP_WEBHOOK_SECRET = os.environ.get("MP_WEBHOOK_SECRET", "")

# SFTP sftpcloud
SFTP_HOST = os.environ.get("SFTP_HOST", "us-east-1.sftpcloud.io")
SFTP_PORT = int(os.environ.get("SFTP_PORT", "22"))
SFTP_USER = os.environ.get("SFTP_USER") or "851d07d6229e48178ec899317dc87bee"
SFTP_PASS = os.environ.get("SFTP_PASS", "")

MP_JOURNAL_NAME = os.environ.get("MP_JOURNAL_NAME", "Mercado Pago")

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

# ============================================================
# SFTP helpers
# ============================================================
def sftp_connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(SFTP_HOST, port=SFTP_PORT, username=SFTP_USER, password=SFTP_PASS, timeout=15)
    return ssh

def sftp_list_files(remote_dir="/"):
    ssh = sftp_connect()
    sftp = ssh.open_sftp()
    files = sftp.listdir_attr(remote_dir)
    result = [{"name": f.filename, "size": f.st_size, "mtime": f.st_mtime}
              for f in files if f.filename.endswith(".csv")]
    sftp.close(); ssh.close()
    return result

def sftp_download_file(remote_path):
    ssh = sftp_connect()
    sftp = ssh.open_sftp()
    buf = io.BytesIO()
    sftp.getfo(remote_path, buf)
    sftp.close(); ssh.close()
    buf.seek(0)
    return buf.read().decode("utf-8-sig", errors="replace")

# ============================================================
# PARSER settlement CSV de MP
# ============================================================
def parse_settlement_csv(content):
    reader = csv.DictReader(io.StringIO(content))
    rows = []
    for row in reader:
        row = {k.strip().upper(): v.strip() for k, v in row.items()}
        credit = float(row.get("NET_CREDIT_AMOUNT", "0").replace(",", ".") or 0)
        debit  = float(row.get("NET_DEBIT_AMOUNT",  "0").replace(",", ".") or 0)
        amount = round(credit - debit, 2)
        fecha_str = row.get("DATE_APPROVED") or row.get("DATE_CREATED", "")
        try:
            fecha = datetime.fromisoformat(fecha_str[:10]).strftime("%Y-%m-%d")
        except:
            fecha = date.today().isoformat()
        ref = row.get("PAYMENT_ID") or row.get("EXTERNAL_ID") or row.get("SOURCE_ID", "")
        descripcion = row.get("DESCRIPTION", "") or row.get("RECORD_TYPE", "Movimiento MP")
        rows.append({
            "date": fecha,
            "payment_ref": f"MP-{ref}" if ref else "MP",
            "amount": amount,
            "partner_name": row.get("PAYER_NAME", "") or row.get("PAYER_EMAIL", ""),
            "note": descripcion,
        })
    return [r for r in rows if r["amount"] != 0]

# ============================================================
# ODOO — extracto bancario MP
# ============================================================
def get_or_create_mp_journal(s):
    journals = odoo_call(s, "account.journal", "search_read",
        [[["name", "=", MP_JOURNAL_NAME]]],
        {"fields": ["id", "name"], "limit": 1})
    if journals:
        return journals[0]["id"]
    return odoo_call(s, "account.journal", "create", [{
        "name": MP_JOURNAL_NAME, "type": "bank", "code": "MP01"
    }])

def ingresar_movimientos_odoo(movimientos):
    if not movimientos:
        return {"ok": False, "msg": "Sin movimientos"}
    s, uid = odoo_session()
    if not uid:
        return {"ok": False, "msg": "Sin conexion Odoo"}
    journal_id = get_or_create_mp_journal(s)
    from itertools import groupby
    movimientos_sorted = sorted(movimientos, key=lambda x: x["date"])
    resultados = []
    for fecha, grupo in groupby(movimientos_sorted, key=lambda x: x["date"]):
        lineas = list(grupo)
        existing = odoo_call(s, "account.bank.statement", "search_read",
            [[["date", "=", fecha], ["journal_id", "=", journal_id]]],
            {"fields": ["id"], "limit": 1})
        if existing:
            resultados.append({"fecha": fecha, "status": "ya_existe", "id": existing[0]["id"]})
            continue
        stmt_lines = [(0, 0, {
            "date": m["date"], "payment_ref": m["payment_ref"],
            "amount": m["amount"], "partner_name": m.get("partner_name", ""),
            "narration": m.get("note", ""),
        }) for m in lineas]
        stmt_id = odoo_call(s, "account.bank.statement", "create", [{
            "name": f"MP {fecha}", "date": fecha,
            "journal_id": journal_id, "line_ids": stmt_lines,
        }])
        log.info(f"Extracto MP creado ID={stmt_id} fecha={fecha} lineas={len(lineas)}")
        resultados.append({"fecha": fecha, "status": "creado", "id": stmt_id, "lineas": len(lineas)})
    return {"ok": True, "resultados": resultados}

# ============================================================
# SYNC principal
# ============================================================
def sync_mp_sftp(fecha_objetivo=None):
    log.info("Iniciando sync MP via SFTP...")
    try:
        archivos = sftp_list_files("/")
    except Exception as e:
        return {"ok": False, "error": f"SFTP error: {e}"}
    if not archivos:
        return {"ok": False, "error": "No hay CSVs en SFTP"}
    if fecha_objetivo:
        target = [f for f in archivos if fecha_objetivo in f["name"]]
        if not target:
            return {"ok": False, "error": f"No hay CSV para {fecha_objetivo}"}
        archivo = sorted(target, key=lambda x: x["mtime"], reverse=True)[0]
    else:
        archivo = sorted(archivos, key=lambda x: x["mtime"], reverse=True)[0]
    log.info(f"Descargando {archivo['name']}...")
    try:
        contenido = sftp_download_file("/" + archivo["name"])
    except Exception as e:
        return {"ok": False, "error": f"Error descargando: {e}"}
    movimientos = parse_settlement_csv(contenido)
    if not movimientos:
        return {"ok": False, "error": "CSV sin movimientos validos", "archivo": archivo["name"]}
    resultado = ingresar_movimientos_odoo(movimientos)
    resultado["archivo"] = archivo["name"]
    resultado["movimientos_total"] = len(movimientos)
    return resultado

# ============================================================
# SCHEDULER diario 6am CDMX (12:00 UTC)
# ============================================================
def scheduler_loop():
    log.info("Scheduler MP iniciado.")
    while True:
        now = datetime.utcnow()
        target = now.replace(hour=12, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait_secs = (target - now).total_seconds()
        log.info(f"Proximo sync MP en {wait_secs/3600:.1f}h")
        time.sleep(wait_secs)
        try:
            result = sync_mp_sftp()
            log.info(f"Sync automatico MP: {result}")
        except Exception as e:
            log.error(f"Error sync automatico MP: {e}")

Thread(target=scheduler_loop, daemon=True).start()

# ============================================================
# ENDPOINTS MP
# ============================================================
@app.route("/mp/sync", methods=["POST"])
def mp_sync_manual():
    data = request.get_json(silent=True) or {}
    result = sync_mp_sftp(fecha_objetivo=data.get("fecha"))
    return jsonify(result)

@app.route("/mp/sftp-test", methods=["GET"])
def mp_sftp_test():
    try:
        archivos = sftp_list_files("/")
        return jsonify({"ok": True, "archivos": archivos})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/mp/reporte-webhook", methods=["POST"])
def mp_reporte_webhook():
    if MP_WEBHOOK_SECRET:
        sig = request.headers.get("x-signature", "")
        body = request.get_data()
        expected = hmac.new(MP_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return jsonify({"error": "firma invalida"}), 403
    payload = request.get_json(silent=True) or {}
    log.info(f"Webhook MP: {payload}")
    Thread(target=sync_mp_sftp, daemon=True).start()
    return jsonify({"ok": True, "msg": "sync iniciado"}), 200

@app.route("/mp/movimientos", methods=["GET"])
def mp_movimientos():
    try:
        archivos = sftp_list_files("/")
        if not archivos:
            return jsonify({"ok": False, "error": "Sin archivos en SFTP"})
        archivo = sorted(archivos, key=lambda x: x["mtime"], reverse=True)[0]
        contenido = sftp_download_file("/" + archivo["name"])
        movimientos = parse_settlement_csv(contenido)
        return jsonify({
            "ok": True, "archivo": archivo["name"],
            "total": len(movimientos), "movimientos": movimientos[:50],
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ============================================================
# ENDPOINTS EXISTENTES
# ============================================================
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/facturas")
def facturas():
    return send_from_directory("static", "facturas.html")

@app.route("/ping")
def ping():
    return jsonify({"ok": True, "ts": datetime.utcnow().isoformat()})

@app.route("/api/estado")
def api_estado():
    s, uid = odoo_session()
    if not uid:
        return jsonify({"ok": False, "msg": "Sin conexion Odoo"})
    productos = odoo_call(s, "product.template", "search_count", [[["active", "=", True]]])
    ventas = odoo_call(s, "pos.order", "search_count", [[]])
    return jsonify({"ok": True, "productos": productos, "ventas": ventas})

@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json()
    mensaje = data.get("mensaje", "")
    historial = data.get("historial", [])
    s, uid = odoo_session()
    contexto = ""
    if uid:
        try:
            productos = odoo_call(s, "product.template", "search_count", [[["active", "=", True]]])
            ventas = odoo_call(s, "pos.order", "search_count", [[]])
            contexto = f"Odoo conectado. Productos: {productos}. Ventas POS: {ventas}."
        except:
            contexto = "Odoo conectado."
    system_prompt = f"""Eres Claudio, agente IA de Pomelo Derma, farmacia en Mexico.
Ayudas con ventas, inventario, proveedores y compras.
{contexto}
Responde en espanol, conciso y util."""
    messages = historial[-10:] + [{"role": "user", "content": mensaje}]
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        resp = client.messages.create(
            model="claude-opus-4-5", max_tokens=1024,
            system=system_prompt, messages=messages,
        )
        respuesta = resp.content[0].text
    except Exception as e:
        respuesta = f"Error IA: {e}"
    return jsonify({"respuesta": respuesta})

@app.route("/api/ocr-oc", methods=["POST"])
def api_ocr_oc():
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files["file"]
    img_data = base64.b64encode(f.read()).decode()
    ext = f.filename.rsplit(".", 1)[-1].lower()
    media_type = "application/pdf" if ext == "pdf" else f"image/{ext}"
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        resp = client.messages.create(
            model="claude-opus-4-5", max_tokens=2048,
            messages=[{"role": "user", "content": [
                {"type": "document" if ext == "pdf" else "image",
                 "source": {"type": "base64", "media_type": media_type, "data": img_data}},
                {"type": "text", "text": 'Extrae datos de esta OC/factura. Responde SOLO JSON: {"proveedor":"","fecha":"YYYY-MM-DD","numero_factura":"","productos":[{"nombre":"","cantidad":0,"precio_unitario":0,"total":0}],"subtotal":0,"impuestos":0,"total":0,"moneda":"MXN"}'}
            ]}]
        )
        resultado = json.loads(resp.content[0].text)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(resultado)

@app.route("/facturas/confirmar", methods=["POST"])
def facturas_confirmar():
    data = request.get_json()
    s, uid = odoo_session()
    if not uid:
        return jsonify({"ok": False, "error": "Sin conexion Odoo"}), 500
    proveedor_nombre = data.get("proveedor", "")
    productos = data.get("productos", [])
    fecha = data.get("fecha", date.today().isoformat())
    partners = odoo_call(s, "res.partner", "search_read",
        [[["name", "ilike", proveedor_nombre], ["supplier_rank", ">", 0]]],
        {"fields": ["id", "name"], "limit": 1})
    partner_id = partners[0]["id"] if partners else None
    resultados = []
    for p in productos:
        nombre = p.get("nombre", "")
        costo = float(p.get("precio_unitario", 0))
        prods = odoo_call(s, "product.template", "search_read",
            [[["name", "ilike", nombre]]],
            {"fields": ["id", "name"], "limit": 1})
        if prods:
            pid = prods[0]["id"]
            odoo_call(s, "product.template", "write", [[pid], {"standard_price": costo}])
            resultados.append({"producto": nombre, "ok": True, "costo": costo})
        else:
            resultados.append({"producto": nombre, "ok": False, "error": "no encontrado"})
    return jsonify({"ok": True, "resultados": resultados, "fecha": fecha})

@app.route("/facturas/analizar-xml", methods=["POST"])
def facturas_analizar_xml():
    import xml.etree.ElementTree as ET
    xml_file = request.files.get("xml")
    if not xml_file:
        return jsonify({"error": "No se recibio XML"}), 400
    try:
        content = xml_file.read().decode("utf-8",errors="replace")
        root = ET.fromstring(content)
        tag = root.tag
        if "{http://www.sat.gob.mx/cfd/4}" in tag: ns="http://www.sat.gob.mx/cfd/4"
        else: ns="http://www.sat.gob.mx/cfd/3"
        def g(el,attr): return el.get(attr,"") if el is not None else ""
        emisor=root.find(f"{{{ns}}}Emisor")
        rfc_emisor=g(emisor,"Rfc"); nombre_emisor=g(emisor,"Nombre")
        fecha=root.get("Fecha","")[:10]; folio=root.get("Serie","")+root.get("Folio","")
        subtotal=float(root.get("SubTotal",0)); total=float(root.get("Total",0)); moneda=root.get("Moneda","MXN")
        s,uid=odoo_session()
        proveedor_id=None; proveedor_nombre=nombre_emisor
        if s and uid and rfc_emisor:
            provs=odoo_call(s,"res.partner","search_read",[[[("vat","=",rfc_emisor)]]],{"fields":["id","name"],"limit":1})
            if provs: proveedor_id=provs[0]["id"]; proveedor_nombre=provs[0]["name"]
        productos=[]; total_iva=0.0
        for c in root.findall(f".//{{{ns}}}Concepto"):
            barcode=c.get("NoIdentificacion",""); desc=c.get("Descripcion","")
            qty=float(c.get("Cantidad",1)); unit_price=float(c.get("ValorUnitario",0)); importe=float(c.get("Importe",0))
            iva_pct=0.0
            imp=c.find(f"{{{ns}}}Impuestos")
            if imp is not None:
                for t in imp.findall(f".//{{{ns}}}Traslado"):
                    if t.get("Impuesto")=="002": iva_pct=float(t.get("TasaOCuota",0))*100; total_iva+=float(t.get("Importe",0))
            prod_id=None; prod_nom=desc
            if s and uid and barcode:
                prods=odoo_call(s,"product.product","search_read",[[[("barcode","=",barcode)]]],{"fields":["id","name"],"limit":1})
                if prods: prod_id=prods[0]["id"]; prod_nom=prods[0]["name"]
            productos.append({"barcode":barcode,"descripcion":desc,"cantidad":qty,"precio_unitario":unit_price,"subtotal":importe,"iva_pct":iva_pct,"producto_id":prod_id,"producto_nombre":prod_nom,"encontrado":prod_id is not None})
        return jsonify({"ok":True,"fuente":"xml_cfdi","proveedor":proveedor_nombre,"proveedor_id":proveedor_id,"rfc_emisor":rfc_emisor,"fecha":fecha,"folio":folio.strip(),"moneda":moneda,"subtotal":subtotal,"iva":round(total_iva,2),"total":total,"productos":productos})
    except Exception as e: return jsonify({"error":str(e)}), 500

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
