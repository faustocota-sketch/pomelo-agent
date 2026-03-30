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
