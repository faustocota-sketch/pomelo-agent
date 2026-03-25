from flask import Flask, request, jsonify, send_from_directory
import requests, os, json, base64

app = Flask(__name__, static_folder="static")

ODOO_URL      = "https://pomelo-derma.odoo.com"
ODOO_DB       = "pomelo-derma"
ODOO_USER     = os.environ.get("ODOO_USER", "carolmartinezderma@gmail.com")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

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
        return "❌ No se pudo conectar a Odoo"
    model  = accion.get("model")
    method = accion.get("method")
    args   = accion.get("args", [])
    kwargs = accion.get("kwargs", {})
    if not model or not method:
        return "❌ Falta model o method"
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
                    line = f"• {name}"
                    if extras:
                        line += f" ({', '.join(extras[:4])})"
                    lines.append(line)
                total = len(resultado)
                resp = "\n".join(lines)
                if total > 25:
                    resp += f"\n... y {total-25} más"
                return f"✅ {total} registros:\n{resp}"
            else:
                return f"✅ Completado: {resultado[:10]}"
        elif isinstance(resultado, bool):
            return "✅ Operación completada"
        elif isinstance(resultado, int):
            return f"✅ Creado con ID: {resultado}"
        else:
            return f"✅ {resultado}"
    except Exception as e:
        return f"❌ Error: {str(e)}"

def get_odoo_summary():
    try:
        s, uid = odoo_session()
        if not s:
            return {"status": "sin conexion"}
        n_productos  = len(odoo_call(s, "product.template", "search", [[]], {"limit": 2000}) or [])
        n_clientes   = len(odoo_call(s, "res.partner", "search", [[["customer_rank",">",0]]], {"limit": 2000}) or [])
        n_proveedores= len(odoo_call(s, "res.partner", "search", [[["supplier_rank",">",0]]], {"limit": 2000}) or [])
        n_pos        = len(odoo_call(s, "pos.order", "search", [[]], {"limit": 2000}) or [])
        n_oc         = len(odoo_call(s, "purchase.order", "search", [[]], {"limit": 2000}) or [])
        n_facturas   = len(odoo_call(s, "account.move", "search", [[["move_type","=","out_invoice"]]], {"limit": 2000}) or [])
        return {
            "status": "conectado",
            "productos": n_productos,
            "clientes": n_clientes,
            "proveedores": n_proveedores,
            "ventas_pos": n_pos,
            "ordenes_compra": n_oc,
            "facturas": n_facturas
        }
    except:
        return {"status": "error"}

# ============================================================
# OCR DE OC — leer imagen/PDF con Claude Vision
# ============================================================
def procesar_oc_imagen(image_base64, media_type="image/jpeg"):
    """Usa Claude Vision para extraer datos de una OC y crearla en Odoo"""
    
    prompt_ocr = """Analiza esta orden de compra/factura de proveedor y extrae TODA la información.
Responde SOLO con un JSON válido con esta estructura exacta:

{
  "proveedor": {
    "nombre": "nombre completo del proveedor",
    "rfc": "RFC si aparece",
    "email": "email si aparece",
    "telefono": "telefono si aparece",
    "direccion": "direccion si aparece"
  },
  "numero_oc": "número de orden/factura",
  "fecha": "fecha en formato YYYY-MM-DD",
  "fecha_vencimiento": "fecha de vencimiento/pago en YYYY-MM-DD si aparece",
  "moneda": "MXN o USD",
  "productos": [
    {
      "nombre": "nombre del producto",
      "cantidad": 1,
      "precio_unitario": 0.00,
      "subtotal": 0.00
    }
  ],
  "subtotal": 0.00,
  "iva": 0.00,
  "total": 0.00,
  "condiciones_pago": "texto de condiciones de pago si aparece",
  "notas": "cualquier nota adicional relevante"
}

Si no encuentras algún campo, usa null. Responde SOLO el JSON, sin texto adicional."""

    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 2000,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_base64}},
                        {"type": "text", "text": prompt_ocr}
                    ]
                }]
            },
            timeout=30)
        
        texto = r.json()["content"][0]["text"]
        # Limpiar el JSON
        if "```" in texto:
            texto = texto.split("```")[1].replace("json","").strip()
        datos_oc = json.loads(texto)
        return datos_oc
    except Exception as e:
        return {"error": str(e)}

def crear_oc_en_odoo(datos_oc):
    """Crea una OC en Odoo a partir de los datos extraídos"""
    s, uid = odoo_session()
    if not s:
        return "❌ No se pudo conectar a Odoo"
    
    try:
        # 1. Buscar o crear proveedor
        nombre_prov = datos_oc.get("proveedor", {}).get("nombre", "Proveedor Desconocido")
        proveedores = odoo_call(s, "res.partner", "search_read",
            [[["name", "ilike", nombre_prov]]], {"fields": ["id","name"], "limit": 1})
        
        if proveedores:
            partner_id = proveedores[0]["id"]
            msg_prov = f"Proveedor encontrado: {proveedores[0]['name']}"
        else:
            # Crear proveedor nuevo
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

        # 2. Crear líneas de OC
        lineas = []
        for prod in datos_oc.get("productos", []):
            nombre_prod = prod.get("nombre", "")
            # Buscar producto en Odoo
            productos = odoo_call(s, "product.product", "search_read",
                [[["name", "ilike", nombre_prod]]], {"fields": ["id","name"], "limit": 1})
            
            if productos:
                product_id = productos[0]["id"]
            else:
                # Crear producto nuevo
                tmpl_id = odoo_call(s, "product.template", "create", [{
                    "name": nombre_prod,
                    "type": "consu",
                    "purchase_ok": True,
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

        # 3. Crear la OC
        oc_vals = {
            "partner_id": partner_id,
            "order_line": lineas,
        }
        if datos_oc.get("fecha"):
            oc_vals["date_order"] = datos_oc["fecha"]
        if datos_oc.get("fecha_vencimiento"):
            oc_vals["date_planned"] = datos_oc["fecha_vencimiento"]
        if datos_oc.get("notas"):
            oc_vals["notes"] = datos_oc.get("notas","")
        
        oc_id = odoo_call(s, "purchase.order", "create", [oc_vals])
        
        return {
            "success": True,
            "oc_id": oc_id,
            "proveedor": msg_prov,
            "productos": len(lineas),
            "total": datos_oc.get("total", 0),
            "mensaje": f"✅ OC #{oc_id} creada exitosamente\n{msg_prov}\n{len(lineas)} productos registrados\nTotal: ${datos_oc.get('total', 0):,.2f} {datos_oc.get('moneda','MXN')}"
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

# ============================================================
# SYSTEM PROMPT
# ============================================================
SYSTEM_PROMPT = """Eres Claudio, el agente IA de Pomelo Derma — farmacia dermatologica premium en Mexico.
Tienes acceso COMPLETO a la API de Odoo y puedes ejecutar CUALQUIER operacion.

CONTEXTO: {contexto}

MODULOS DE ODOO DISPONIBLES:

PUNTO DE VENTA:
- pos.order → ventas del POS (fields: name, amount_total, date_order, state, partner_id)
- pos.order.line → lineas de venta (fields: product_id, qty, price_unit, price_subtotal)
- pos.session → sesiones POS

COMPRAS Y PROVEEDORES:
- purchase.order → ordenes de compra (fields: name, partner_id, amount_total, state, date_order, date_planned, notes)
- purchase.order.line → lineas de OC (fields: product_id, product_qty, price_unit, name)
- res.partner con supplier_rank>0 → proveedores

PRODUCTOS:
- product.template → productos (fields: name, list_price, standard_price, type, available_in_pos, barcode)
- stock.quant → inventario actual (fields: product_id, quantity, location_id)

CLIENTES:
- res.partner con customer_rank>0 → clientes

FACTURACION:
- account.move → facturas (move_type: out_invoice=venta, in_invoice=compra)
- account.move con move_type=in_invoice → facturas de proveedor/cuentas por pagar

METODOS:
- search_read → buscar: args=[[dominio]], kwargs={"fields":[...],"limit":N,"order":"campo desc"}
- write → actualizar: args=[[ids], {campos}]
- create → crear: args=[{campos}]

EJEMPLOS:

Ventas del POS:
ODOO_ACTION:{"model":"pos.order","method":"search_read","args":[[["state","in",["done","paid","invoiced"]]]],"kwargs":{"fields":["name","amount_total","date_order","partner_id","state"],"limit":10,"order":"date_order desc"}}

Ordenes de compra pendientes:
ODOO_ACTION:{"model":"purchase.order","method":"search_read","args":[[["state","in",["draft","sent","to approve"]]]],"kwargs":{"fields":["name","partner_id","amount_total","date_order","state"],"limit":20}}

Cuentas por pagar a proveedores:
ODOO_ACTION:{"model":"account.move","method":"search_read","args":[[["move_type","=","in_invoice"],["state","=","posted"],["payment_state","!=","paid"]]],"kwargs":{"fields":["name","partner_id","amount_total","invoice_date_due","payment_state"],"limit":20,"order":"invoice_date_due asc"}}

Stock bajo:
ODOO_ACTION:{"model":"stock.quant","method":"search_read","args":[[["quantity","<",5],["location_id.usage","=","internal"]]],"kwargs":{"fields":["product_id","quantity"],"limit":30}}

Crear proveedor:
ODOO_ACTION:{"model":"res.partner","method":"create","args":[{"name":"Nombre Proveedor","email":"email@prov.com","supplier_rank":1,"is_company":true}],"kwargs":{}}

REGLAS IMPORTANTES:
1. Ventas = pos.order (NO account.move)
2. Facturas de proveedor = account.move con move_type=in_invoice
3. Para pagos a proveedores = busca en account.move con move_type=in_invoice ordenado por invoice_date_due
4. Para write masivo usa args=[[]] — el sistema obtiene todos los IDs automaticamente
5. Responde en espanol, breve y directo
6. SIEMPRE incluye ODOO_ACTION cuando el usuario pide datos o quiere hacer algo en Odoo
7. Nunca inventes datos"""

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

@app.route("/api/ocr-oc", methods=["POST"])
def ocr_oc():
    """Endpoint para procesar imagen/PDF de OC"""
    try:
        data = request.json
        image_base64 = data.get("image")
        media_type   = data.get("media_type", "image/jpeg")
        
        if not image_base64:
            return jsonify({"error": "No se recibió imagen"}), 400
        
        # 1. Extraer datos con Claude Vision
        datos_oc = procesar_oc_imagen(image_base64, media_type)
        
        if "error" in datos_oc:
            return jsonify({"error": f"Error al leer imagen: {datos_oc['error']}"}), 500
        
        # 2. Crear OC en Odoo
        resultado = crear_oc_en_odoo(datos_oc)
        
        return jsonify({
            "datos_extraidos": datos_oc,
            "resultado_odoo": resultado
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/chat", methods=["POST"])
def chat():
    data      = request.json
    mensaje   = data.get("mensaje", "")
    historial = data.get("historial", [])

    if not ANTHROPIC_KEY:
        return jsonify({"respuesta": "❌ API Key no configurada", "accion": None})

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
            return jsonify({"respuesta": f"❌ Error: {response_data.get('error',{}).get('message','')}", "accion": None})

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
                accion_resultado = f"❌ Error: {str(e)}"
        else:
            texto_limpio = texto

        return jsonify({"respuesta": texto_limpio, "accion": accion_resultado})

    except requests.Timeout:
        return jsonify({"respuesta": "❌ Timeout — intenta de nuevo.", "accion": None})
    except Exception as e:
        return jsonify({"respuesta": f"❌ Error: {str(e)}", "accion": None})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
