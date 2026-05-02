"""
Aria Webhook Proxy
1. /trigger-call - Receives GHL webhook, stores contact details, triggers ElevenLabs call
2. /book - Receives Aria's booking request, injects stored contact details, forwards to GHL
"""

from flask import Flask, request, jsonify
import requests
import os
import logging
import json
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

ELEVEN_LABS_URL = "https://api.elevenlabs.io/v1/convai/twilio/outbound_call"
ELEVEN_LABS_KEY = os.environ.get("XI_API_KEY", "sk_1a2a2a07d19b6f993c0b98203dd355f2938c9f855ba76d97")
GHL_KEY = os.environ.get("GHL_KEY", "pit-987b2fbe-781e-463d-b6b2-2e9a42fe6be0")
AGENT_ID = "agent_6001kpa99tm7fm5sk5da7h057s3r"
AGENT_PHONE_ID = "phnum_3501kpvsp97afx0sy9d0pnzhqwnk"

# File-based store of contact details keyed by phone number
# Survives restarts unlike in-memory dict
STORE_PATH = "/tmp/aria-contacts.json"


def load_store():
    try:
        with open(STORE_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_store(store):
    with open(STORE_PATH, "w") as f:
        json.dump(store, f)


def get_store():
    store = load_store()
    # Clean entries older than 1 hour
    now = time.time()
    cleaned = {k: v for k, v in store.items() if now - v.get("stored_at", 0) < 3600}
    if len(cleaned) != len(store):
        save_store(cleaned)
    return cleaned


@app.route("/debug", methods=["GET", "POST", "PUT", "PATCH"])
def debug():
    """Debug endpoint - logs everything received."""
    logger.info(f"DEBUG HIT: {request.method} {request.url}")
    logger.info(f"Headers: {dict(request.headers)}")
    logger.info(f"Body: {request.get_data(as_text=True)[:1000]}")
    return jsonify({"received": True})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "aria-webhook-proxy", "contacts_cached": len(get_store())})


@app.route("/trigger-call", methods=["POST"])
def trigger_call():
    """Receive GHL webhook, store contact details, trigger ElevenLabs call."""
    try:
        # Log everything to diagnose GHL format
        logger.info(f"Content-Type: {request.content_type}")
        logger.info(f"Raw body: {request.get_data(as_text=True)[:500]}")
        logger.info(f"Args: {dict(request.args)}")
        logger.info(f"Form: {dict(request.form)}")

        data = request.json or request.form.to_dict() or dict(request.args)
        logger.info(f"Parsed data: {json.dumps(data)}")

        to_number = (
            data.get("to_number") or
            data.get("phone") or
            data.get("customer_phone", "")
        )
        customer_name = (
            data.get("customer_name") or
            data.get("first_name") or
            data.get("firstName", "")
        )
        customer_email = (
            data.get("customer_email") or
            data.get("email", "")
        )
        customer_phone = (
            data.get("customer_phone") or
            to_number or ""
        )
        contact_id = data.get("contact_id", "")

        if not to_number:
            return jsonify({"error": "No phone number"}), 400

        if not to_number.startswith("+"):
            to_number = "+" + to_number
        if customer_phone and not customer_phone.startswith("+"):
            customer_phone = "+" + customer_phone

        # Store contact details for when Aria books
        store = load_store()
        store[to_number] = {
            "firstName": customer_name,
            "email": customer_email,
            "phone": customer_phone or to_number,
            "contact_id": contact_id,
            "stored_at": time.time()
        }
        save_store(store)
        logger.info(f"Stored contact: {to_number} -> {customer_name} / {customer_email}")

        # Trigger ElevenLabs call
        eleven_payload = {
            "agent_id": AGENT_ID,
            "agent_phone_number_id": AGENT_PHONE_ID,
            "to_number": to_number,
        }

        r = requests.post(
            ELEVEN_LABS_URL,
            headers={"xi-api-key": ELEVEN_LABS_KEY, "Content-Type": "application/json"},
            json=eleven_payload,
            timeout=30
        )
        logger.info(f"ElevenLabs: {r.status_code} - {r.text[:200]}")

        return jsonify({"status": "call_triggered", "to": to_number}), 200

    except Exception as e:
        logger.exception("Error in trigger-call")
        return jsonify({"error": str(e)}), 500


@app.route("/book", methods=["POST"])
def book():
    """Receive booking request from Aria, inject stored contact details, forward to GHL."""
    try:
        data = request.json or {}
        logger.info(f"Booking request: {json.dumps(data)}")

        # Get the selected slot and other booking details from Aria
        calendar_id = data.get("calendarId", "Mh4aoOLuDqTBkh4aTbqC")
        location_id = data.get("locationId", "0TWza0nu95nr1KSlTgm7")
        selected_slot = data.get("selectedSlot", "")
        selected_timezone = data.get("selectedTimezone", "Australia/Sydney")
        title = data.get("title", "Venus Viva Full Face Skin Tightening")

        # Get contact details - first from the request, then from the store
        contact = data.get("contact", {})
        first_name = contact.get("firstName", data.get("firstName", ""))
        email = contact.get("email", data.get("email", ""))
        phone = contact.get("phone", data.get("phone", ""))

        # If phone is empty or placeholder, try the store
        store = get_store()
        if not phone or phone == "+61400000000" or len(phone) < 8:
            for stored_number, stored_data in store.items():
                if not first_name:
                    first_name = stored_data.get("firstName", "")
                if not email:
                    email = stored_data.get("email", "")
                if not phone or phone == "+61400000000":
                    phone = stored_data.get("phone", stored_number)
                logger.info(f"Injected from store: name={first_name}, email={email}, phone={phone}")
                break

        if not selected_slot:
            return jsonify({"error": "No slot selected"}), 400

        # Build GHL booking request
        ghl_body = {
            "calendarId": calendar_id,
            "locationId": location_id,
            "selectedSlot": selected_slot,
            "selectedTimezone": selected_timezone,
            "title": title,
            "contact": {
                "firstName": first_name or "Customer",
                "email": email or "noemail@placeholder.com",
                "phone": phone
            }
        }

        logger.info(f"Booking to GHL: {json.dumps(ghl_body)}")

        r = requests.post(
            "https://services.leadconnectorhq.com/calendars/events/appointments",
            headers={
                "Authorization": f"Bearer {GHL_KEY}",
                "Version": "2021-04-15",
                "Content-Type": "application/json"
            },
            json=ghl_body,
            timeout=30
        )

        logger.info(f"GHL response: {r.status_code} - {r.text[:300]}")
        return r.text, r.status_code, {"Content-Type": "application/json"}

    except Exception as e:
        logger.exception("Error in book")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port, debug=False)
