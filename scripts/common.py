"""Outils partagés : chemins, lecture/écriture JSON, envoi de mail via Brevo."""
import json
import os
import sys
from datetime import datetime, timezone

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
DATA_DIR = os.path.join(ROOT, "data")
DOCS_DIR = os.path.join(ROOT, "docs")


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="minutes")


def send_mail(subject, html, text):
    """Envoie un mail via l'API transactionnelle de Brevo. Retourne True si l'envoi a réussi."""
    api_key = os.environ.get("BREVO_API_KEY")
    to = os.environ.get("MAIL_TO")
    sender = os.environ.get("MAIL_FROM")
    if not (api_key and to and sender):
        print("Envoi impossible : BREVO_API_KEY, MAIL_TO ou MAIL_FROM manquant.", file=sys.stderr)
        return False

    payload = {
        "sender": {"name": "Veille programmes", "email": sender},
        "to": [{"email": to}],
        "subject": subject,
        "htmlContent": html,
        "textContent": text,
    }
    headers = {
        "api-key": api_key,
        "accept": "application/json",
        "content-type": "application/json",
    }
    try:
        r = requests.post("https://api.brevo.com/v3/smtp/email", headers=headers, json=payload, timeout=30)
    except requests.RequestException as e:
        print(f"Erreur réseau vers Brevo : {e}", file=sys.stderr)
        return False
    if r.status_code >= 300:
        print(f"Brevo a refusé l'envoi ({r.status_code}) : {r.text}", file=sys.stderr)
        return False
    return True
