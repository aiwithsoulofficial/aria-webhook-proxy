"""
Aria Webhook Proxy - Multi-Tenant
1. /trigger-call/<clinic> - Receives GHL webhook, stores contact details, triggers ElevenLabs call
2. /book/<clinic> - Receives Aria's booking request, injects stored contact details, forwards to GHL
3. /trigger-call and /book (no clinic) - backwards-compatible, uses Lumiere defaults
"""

from flask import Flask, request, jsonify
import requests as http_requests
import os
import logging
import json
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

ELEVEN_LABS_URL = "https://api.elevenlabs.io/v1/convai/twilio/outbound_call"
ELEVEN_LABS_KEY = os.environ.get("XI_API_KEY", "sk_1a2a2a07d19b6f993c0b98203dd355f2938c9f855ba76d97")

# Multi-tenant clinic configurations
# Each clinic has its own agent, phone, calendar, location, and GHL key
CLINICS = {
    "lumiere": {
        "name": "Lumiere Skin and Body",
        "agent_id": "agent_6001kpa99tm7fm5sk5da7h057s3r",
        "agent_phone_id": "phnum_3501kpvsp97afx0sy9d0pnzhqwnk",
        "ghl_key": os.environ.get("GHL_KEY_LUMIERE", "pit-987b2fbe-781e-463d-b6b2-2e9a42fe6be0"),
        "calendar_id": "Mh4aoOLuDqTBkh4aTbqC",
        "location_id": "0TWza0nu95nr1KSlTgm7",
        "timezone": "Australia/Sydney",
        "default_title": "Venus Viva Full Face Skin Tightening"
    },
    "confiderm": {
        "name": "Confiderm Skin Care",
        "agent_id": os.environ.get("CONFIDERM_AGENT_ID", "agent_2201kr2kzb94emc9qc5cq8q9yexw"),
        "agent_phone_id": os.environ.get("CONFIDERM_PHONE_ID", "phnum_4801kr2ma3p9ft4aq4j9r5yv89kf"),
        "ghl_key": os.environ.get("GHL_KEY_CONFIDERM", "pit-19355b7e-29d6-4ddf-a036-cbe661c92234"),
        "calendar_id": "H9z1MTWykO17vGy1q5rn",
        "location_id": "ADOTS56fHDDghWSBIcOz",
        "timezone": "Australia/Brisbane",
        "default_title": "Confiderm Skin Consultation"
    }
}

# Default clinic for backwards compatibility (no slug in URL)
DEFAULT_CLINIC = "lumiere"

# Lookup agent_id -> clinic for /book endpoint (Aria sends agent context)
AGENT_TO_CLINIC = {cfg["agent_id"]: slug for slug, cfg in CLINICS.items()}

# File-based store of contact details keyed by phone number
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
    now = time.time()
    cleaned = {k: v for k, v in store.items() if now - v.get("stored_at", 0) < 3600}
    if len(cleaned) != len(store):
        save_store(cleaned)
    return cleaned


def get_clinic(clinic_slug=None):
    """Get clinic config by slug, with fallback to default."""
    slug = (clinic_slug or DEFAULT_CLINIC).lower()
    return CLINICS.get(slug), slug


@app.route("/debug", methods=["GET", "POST", "PUT", "PATCH"])
def debug():
    logger.info(f"DEBUG HIT: {request.method} {request.url}")
    logger.info(f"Headers: {dict(request.headers)}")
    logger.info(f"Body: {request.get_data(as_text=True)[:1000]}")
    return jsonify({"received": True})


@app.route("/health", methods=["GET"])
def health():
    store = get_store()
    return jsonify({
        "status": "ok",
        "service": "aria-webhook-proxy",
        "version": "2.0-multitenant",
        "clinics": list(CLINICS.keys()),
        "contacts_cached": len(store),
        "contacts": store
    })


@app.route("/trigger-call", methods=["POST"])
@app.route("/trigger-call/<clinic_slug>", methods=["POST"])
def trigger_call(clinic_slug=None):
    """Receive GHL webhook, store contact details, trigger ElevenLabs call."""
    try:
        clinic, slug = get_clinic(clinic_slug)
        if not clinic:
            return jsonify({"error": f"Unknown clinic: {clinic_slug}"}), 404

        logger.info(f"[{slug}] Trigger call received")
        logger.info(f"Content-Type: {request.content_type}")
        logger.info(f"Raw body: {request.get_data(as_text=True)[:500]}")
        logger.info(f"Args: {dict(request.args)}")
        logger.info(f"Form: {dict(request.form)}")

        data = request.json or request.form.to_dict() or dict(request.args)
        logger.info(f"[{slug}] Parsed data: {json.dumps(data)}")

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

        # Store contact details with clinic context
        store = load_store()
        store[to_number] = {
            "firstName": customer_name,
            "email": customer_email,
            "phone": customer_phone or to_number,
            "contact_id": contact_id,
            "clinic": slug,
            "stored_at": time.time()
        }
        save_store(store)
        logger.info(f"[{slug}] Stored contact: {to_number} -> {customer_name} / {customer_email}")

        # Trigger ElevenLabs call with clinic-specific agent
        eleven_payload = {
            "agent_id": clinic["agent_id"],
            "agent_phone_number_id": clinic["agent_phone_id"],
            "to_number": to_number,
        }

        r = http_requests.post(
            ELEVEN_LABS_URL,
            headers={"xi-api-key": ELEVEN_LABS_KEY, "Content-Type": "application/json"},
            json=eleven_payload,
            timeout=30
        )
        logger.info(f"[{slug}] ElevenLabs: {r.status_code} - {r.text[:200]}")

        return jsonify({"status": "call_triggered", "clinic": slug, "to": to_number}), 200

    except Exception as e:
        logger.exception(f"Error in trigger-call ({clinic_slug})")
        return jsonify({"error": str(e)}), 500


@app.route("/book", methods=["POST"])
@app.route("/book/<clinic_slug>", methods=["POST"])
def book(clinic_slug=None):
    """Receive booking request from Aria, inject stored contact details, forward to GHL."""
    try:
        data = request.json or {}
        logger.info(f"Booking request: {json.dumps(data)}")

        # Determine clinic from URL slug, or from stored contact, or default
        clinic, slug = get_clinic(clinic_slug)
        if not clinic:
            return jsonify({"error": f"Unknown clinic: {clinic_slug}"}), 404

        # Use clinic-specific defaults, allow override from request
        calendar_id = data.get("calendarId", clinic["calendar_id"])
        location_id = data.get("locationId", clinic["location_id"])
        selected_slot = data.get("selectedSlot", "")
        selected_timezone = data.get("selectedTimezone", clinic["timezone"])
        title = data.get("title", clinic["default_title"])

        # Get contact details - first from the request, then from the store
        contact = data.get("contact", {})
        first_name = contact.get("firstName", data.get("firstName", ""))
        email = contact.get("email", data.get("email", ""))
        phone = contact.get("phone", data.get("phone", ""))

        # ALWAYS inject from store - override whatever Aria sent
        store = get_store()
        if store:
            for stored_number, stored_data in store.items():
                # If we have multiple contacts, prefer the one for this clinic
                if stored_data.get("clinic") == slug or len(store) == 1:
                    first_name = stored_data.get("firstName", "") or first_name
                    email = stored_data.get("email", "") or email
                    phone = stored_data.get("phone", stored_number)
                    logger.info(f"[{slug}] Injected from store: name={first_name}, email={email}, phone={phone}")
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

        logger.info(f"[{slug}] Booking to GHL: {json.dumps(ghl_body)}")

        r = http_requests.post(
            "https://services.leadconnectorhq.com/calendars/events/appointments",
            headers={
                "Authorization": f"Bearer {clinic['ghl_key']}",
                "Version": "2021-04-15",
                "Content-Type": "application/json"
            },
            json=ghl_body,
            timeout=30
        )

        logger.info(f"[{slug}] GHL response: {r.status_code} - {r.text[:300]}")
        return r.text, r.status_code, {"Content-Type": "application/json"}

    except Exception as e:
        logger.exception(f"Error in book ({clinic_slug})")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port, debug=False)
