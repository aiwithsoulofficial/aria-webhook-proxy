"""
Aria Webhook Proxy - Multi-Tenant with Timely Integration
1. /trigger-call/<clinic> - Receives GHL webhook, stores contact details, triggers ElevenLabs call
2. /book/<clinic> - Receives Aria's booking request, injects stored contact details, forwards to GHL
3. /timely/availability - Check real Timely availability for Confiderm
4. /trigger-call and /book (no clinic) - backwards-compatible, uses Lumiere defaults
"""

from flask import Flask, request, jsonify
import requests as http_requests
import os
import logging
import json
import time
from datetime import datetime, timedelta
from threading import Lock, Thread

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

ELEVEN_LABS_URL = "https://api.elevenlabs.io/v1/convai/twilio/outbound_call"
ELEVEN_LABS_KEY = os.environ.get("XI_API_KEY", "sk_1a2a2a07d19b6f993c0b98203dd355f2938c9f855ba76d97")

# Multi-tenant clinic configurations
CLINICS = {
    "lumiere": {
        "name": "Lumiere Skin and Body",
        "agent_id": "agent_6001kpa99tm7fm5sk5da7h057s3r",
        "agent_phone_id": "phnum_3501kpvsp97afx0sy9d0pnzhqwnk",
        "ghl_key": os.environ.get("GHL_KEY_LUMIERE", "pit-987b2fbe-781e-463d-b6b2-2e9a42fe6be0"),
        "calendar_id": "Mh4aoOLuDqTBkh4aTbqC",
        "location_id": "0TWza0nu95nr1KSlTgm7",
        "timezone": "Australia/Sydney",
        "default_title": "Venus Viva Full Face Skin Tightening",
        "booking_system": "ghl"
    },
    "confiderm": {
        "name": "Confiderm Skin Care",
        "agent_id": os.environ.get("CONFIDERM_AGENT_ID", "agent_6801kr3767m9emc8s3fw403chxa2"),
        "agent_phone_id": os.environ.get("CONFIDERM_PHONE_ID", "phnum_4801kr2ma3p9ft4aq4j9r5yv89kf"),
        "ghl_key": os.environ.get("GHL_KEY_CONFIDERM", "pit-19355b7e-29d6-4ddf-a036-cbe661c92234"),
        "calendar_id": "H9z1MTWykO17vGy1q5rn",
        "location_id": "ADOTS56fHDDghWSBIcOz",
        "timezone": "Australia/Brisbane",
        "default_title": "Confiderm Skin Consultation",
        "booking_system": "timely",
        "timely": {
            "email": os.environ.get("TIMELY_EMAIL", "moj_b52@yahoo.com"),
            "password": os.environ.get("TIMELY_PASSWORD", "Ariasorush12?"),
            "location_id": "275601",
            "staff": {
                "486177": "Mojgan Broumand",
                "514556": "Dr. Yousef Khammar"
            },
            "open_hours": {
                "0": {"open": "09:00", "close": "16:00"},  # Sunday
                "1": {"open": "09:00", "close": "19:00"},  # Monday
                "2": {"open": "09:00", "close": "19:00"},  # Tuesday
                "3": {"open": "09:00", "close": "19:00"},  # Wednesday
                "4": {"open": "09:00", "close": "19:00"},  # Thursday
                "5": {"open": "09:00", "close": "19:00"},  # Friday
                "6": {"open": "09:00", "close": "16:00"},  # Saturday
            },
            "slot_duration_mins": 30,
            "mojgan_phone": "+61488874342"
        }
    }
}

DEFAULT_CLINIC = "lumiere"
AGENT_TO_CLINIC = {cfg["agent_id"]: slug for slug, cfg in CLINICS.items()}
STORE_PATH = "/tmp/aria-contacts.json"

# ── Timely session management ──────────────────────────────────────

TIMELY_SESSION_PATH = "/tmp/timely-session.json"
timely_lock = Lock()


def timely_login():
    """Login to Timely admin and store session cookies."""
    cfg = CLINICS["confiderm"]["timely"]
    session = http_requests.Session()

    # GET login page for CSRF token
    login_page = session.get("https://app.gettimely.com/Account/Login", timeout=15)

    # Extract __RequestVerificationToken from the page
    token = ""
    for line in login_page.text.split("\n"):
        if "__RequestVerificationToken" in line and 'value="' in line:
            token = line.split('value="')[1].split('"')[0]
            break

    # POST login
    login_data = {
        "EmailAddress": cfg["email"],
        "Password": cfg["password"],
        "RememberMe": "true",
    }
    if token:
        login_data["__RequestVerificationToken"] = token

    r = session.post(
        "https://app.gettimely.com/Account/Login",
        data=login_data,
        allow_redirects=True,
        timeout=15
    )

    if "calendar" in r.url.lower() or r.status_code == 200:
        # Save cookies
        cookies = dict(session.cookies)
        with open(TIMELY_SESSION_PATH, "w") as f:
            json.dump({"cookies": cookies, "logged_in_at": time.time()}, f)
        logger.info(f"[timely] Login successful, {len(cookies)} cookies saved")
        return session
    else:
        logger.error(f"[timely] Login failed: {r.status_code} -> {r.url}")
        return None


def get_timely_session():
    """Get an authenticated Timely session, re-login if needed."""
    with timely_lock:
        session = http_requests.Session()

        # Try loading existing session
        try:
            with open(TIMELY_SESSION_PATH, "r") as f:
                data = json.load(f)
                cookies = data.get("cookies", {})
                age = time.time() - data.get("logged_in_at", 0)

                # Re-login if session is older than 30 minutes
                if age > 1800:
                    logger.info("[timely] Session expired, re-logging in")
                    return timely_login()

                for k, v in cookies.items():
                    session.cookies.set(k, v)

                # Verify session is still valid
                r = session.get(
                    "https://app.gettimely.com/NotificationData/GetNotificationStats",
                    timeout=10
                )
                if r.status_code == 200:
                    return session
                else:
                    logger.info("[timely] Session invalid, re-logging in")
                    return timely_login()

        except (FileNotFoundError, json.JSONDecodeError):
            return timely_login()


def get_timely_bookings(session, start_date, end_date, location_id="275601"):
    """Fetch existing bookings from Timely CalendarData endpoint."""
    url = (
        f"https://app.gettimely.com/CalendarData/CalendarData"
        f"?locationId={location_id}&staffId=false&isMobile=false"
        f"&view=resourceDay&start={start_date}&end={end_date}"
        f"&_={int(time.time() * 1000)}"
    )
    r = session.get(url, timeout=15)
    if r.status_code == 200:
        return r.json()
    logger.error(f"[timely] CalendarData failed: {r.status_code}")
    return []


def calculate_free_slots(bookings, open_hours, start_date_str, end_date_str, slot_mins=30):
    """Calculate free slots by subtracting bookings from open hours."""
    result = {}
    start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date_str, "%Y-%m-%d")
    current = start_dt

    # Build a lookup of booked time ranges per day
    booked_ranges = {}
    for booking in bookings:
        if booking.get("type") != 1:  # type 1 = booking
            continue
        b_start = booking.get("start", "")
        b_end = booking.get("end", "")
        if not b_start or not b_end:
            continue
        try:
            bs = datetime.fromisoformat(b_start.replace(".0000000", ""))
            be = datetime.fromisoformat(b_end.replace(".0000000", ""))
            day_key = bs.strftime("%Y-%m-%d")
            if day_key not in booked_ranges:
                booked_ranges[day_key] = []
            booked_ranges[day_key].append((bs, be))
        except (ValueError, TypeError):
            continue

    while current < end_dt:
        day_key = current.strftime("%Y-%m-%d")
        dow = str(current.weekday())  # 0=Monday in Python
        # Convert to JS-style (0=Sunday)
        js_dow = str((current.weekday() + 1) % 7)

        hours = open_hours.get(js_dow)
        if not hours:
            current += timedelta(days=1)
            continue

        open_time = datetime.strptime(f"{day_key} {hours['open']}", "%Y-%m-%d %H:%M")
        close_time = datetime.strptime(f"{day_key} {hours['close']}", "%Y-%m-%d %H:%M")

        # Skip if the day is in the past
        now = datetime.now()
        if current.date() < now.date():
            current += timedelta(days=1)
            continue
        if current.date() == now.date():
            # Start from next available slot after now + 2 hours buffer
            earliest = now + timedelta(hours=2)
            if earliest > open_time:
                # Round up to next slot
                mins = earliest.minute
                rounded = earliest.replace(minute=0, second=0) + timedelta(
                    minutes=((mins // slot_mins) + 1) * slot_mins
                )
                open_time = rounded

        # Generate all possible slots
        day_booked = booked_ranges.get(day_key, [])
        slots = []
        slot_start = open_time

        while slot_start + timedelta(minutes=slot_mins) <= close_time:
            slot_end = slot_start + timedelta(minutes=slot_mins)

            # Check if this slot overlaps with any booking
            is_booked = False
            for bs, be in day_booked:
                if slot_start < be and slot_end > bs:
                    is_booked = True
                    break

            if not is_booked:
                slots.append(slot_start.strftime("%Y-%m-%dT%H:%M:%S+10:00"))

            slot_start = slot_end

        if slots:
            result[day_key] = {"slots": slots}

        current += timedelta(days=1)

    return result


# ── Contact store ──────────────────────────────────────────────────

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
    slug = (clinic_slug or DEFAULT_CLINIC).lower()
    return CLINICS.get(slug), slug


# ── Routes ─────────────────────────────────────────────────────────

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
        "version": "3.0-timely",
        "clinics": list(CLINICS.keys()),
        "contacts_cached": len(store),
        "contacts": store
    })


SUPABASE_URL = "https://jiquevvzrdavgqonvvug.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImppcXVldnZ6cmRhdmdxb252dnVnIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3MTk3MjE3MiwiZXhwIjoyMDg3NTQ4MTcyfQ.x2CFoCcJpyVApfnh5J77eT-UoMRRXdqhW9Xi-fK3hyE"


@app.route("/timely/availability", methods=["GET"])
def timely_availability():
    """Read cached Timely availability from Supabase.
    The local bridge syncs real Timely data every 5 minutes.
    Returns free slots in GHL-compatible format.
    """
    try:
        start_ms = request.args.get("startDate", "")
        end_ms = request.args.get("endDate", "")

        if not start_ms or not end_ms:
            return jsonify({"error": "startDate and endDate required (epoch ms)"}), 400

        start_dt = datetime.fromtimestamp(int(start_ms) / 1000)
        end_dt = datetime.fromtimestamp(int(end_ms) / 1000)
        start_str = start_dt.strftime("%Y-%m-%d")
        end_str = end_dt.strftime("%Y-%m-%d")

        logger.info(f"[timely] Reading cached availability {start_str} to {end_str}")

        # Read from Supabase cache
        r = http_requests.get(
            f"{SUPABASE_URL}/rest/v1/aria_availability_cache"
            f"?clinic_slug=eq.confiderm&date=gte.{start_str}&date=lte.{end_str}&order=date.asc",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
            },
            timeout=10
        )

        if r.status_code != 200:
            return jsonify({"error": f"Supabase read failed: {r.status_code}"}), 500

        rows = r.json()
        result = {}
        for row in rows:
            slots = row.get("slots", [])
            if isinstance(slots, str):
                slots = json.loads(slots)
            if slots:
                result[row["date"]] = {"slots": slots}

        logger.info(f"[timely] Returning {sum(len(d['slots']) for d in result.values())} cached slots across {len(result)} days")
        return jsonify(result)

    except Exception as e:
        logger.exception("[timely] Error reading cached availability")
        return jsonify({"error": str(e)}), 500


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
    """Receive booking request from Aria, inject stored contact details, forward to GHL.
    For Confiderm: also sends SMS notification to Mojgan to add booking in Timely."""
    try:
        data = request.json or {}
        logger.info(f"Booking request: {json.dumps(data)}")

        clinic, slug = get_clinic(clinic_slug)
        if not clinic:
            return jsonify({"error": f"Unknown clinic: {clinic_slug}"}), 404

        calendar_id = data.get("calendarId", clinic["calendar_id"])
        location_id = data.get("locationId", clinic["location_id"])
        selected_slot = data.get("selectedSlot", "")
        selected_timezone = data.get("selectedTimezone", clinic["timezone"])
        title = data.get("title", clinic["default_title"])

        contact = data.get("contact", {})
        first_name = contact.get("firstName", data.get("firstName", ""))
        email = contact.get("email", data.get("email", ""))
        phone = contact.get("phone", data.get("phone", ""))

        # ALWAYS inject from store
        store = get_store()
        if store:
            for stored_number, stored_data in store.items():
                if stored_data.get("clinic") == slug or len(store) == 1:
                    first_name = stored_data.get("firstName", "") or first_name
                    email = stored_data.get("email", "") or email
                    phone = stored_data.get("phone", stored_number)
                    logger.info(f"[{slug}] Injected from store: name={first_name}, email={email}, phone={phone}")
                    break

        if not selected_slot:
            return jsonify({"error": "No slot selected"}), 400

        # Parse the slot
        slot_dt = datetime.fromisoformat(selected_slot.replace("+10:00", "").replace("+11:00", ""))
        booking_date = slot_dt.strftime("%Y-%m-%d")
        booking_time = slot_dt.strftime("%-I:%M%p").lower()

        if slug == "confiderm":
            # Queue booking in Supabase for bridge to create in Timely
            logger.info(f"[confiderm] Queuing Timely booking: {first_name} on {booking_date} at {booking_time}")
            http_requests.post(
                f"{SUPABASE_URL}/rest/v1/aria_booking_queue",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"},
                json={
                    "clinic_slug": "confiderm",
                    "customer_name": first_name or "Customer",
                    "customer_phone": phone,
                    "customer_email": email,
                    "staff_id": "486177",
                    "booking_date": booking_date,
                    "booking_time": booking_time,
                    "service_key": "skin_consultation",
                    "status": "pending"
                },
                timeout=10
            )
            logger.info(f"[confiderm] Booking queued for bridge pickup")
            return jsonify({"status": "booked", "date": booking_date, "time": booking_time}), 200
        else:
            # Other clinics (Lumiere etc) still use GHL
            ghl_body = {
                "calendarId": calendar_id, "locationId": location_id,
                "selectedSlot": selected_slot, "selectedTimezone": selected_timezone,
                "title": title,
                "contact": {"firstName": first_name or "Customer", "email": email or "noemail@placeholder.com", "phone": phone}
            }
            r = http_requests.post(
                "https://services.leadconnectorhq.com/calendars/events/appointments",
                headers={"Authorization": f"Bearer {clinic['ghl_key']}", "Version": "2021-04-15", "Content-Type": "application/json"},
                json=ghl_body, timeout=30
            )
            return r.text, r.status_code, {"Content-Type": "application/json"}

    except Exception as e:
        logger.exception(f"Error in book ({clinic_slug})")
        return jsonify({"error": str(e)}), 500


@app.route("/postcall/confiderm", methods=["POST"])
def postcall_confiderm():
    """Receive ElevenLabs post-call webhook. Extract booking details and
    create the booking in Timely via Playwright (async in background thread)."""
    try:
        data = request.json or {}
        logger.info(f"[confiderm] Post-call webhook received")
        logger.info(f"[confiderm] Data keys: {list(data.keys())}")

        # Extract booking data from ElevenLabs webhook
        # Data collection fields we configured: appointment_date_time, practitioner_booked_with, skin_concern
        collected = data.get("data_collection", data.get("analysis", {}).get("data_collection", {}))
        transcript = data.get("transcript", [])

        appointment_time = collected.get("appointment_date_time", "")
        practitioner = collected.get("practitioner_booked_with", "Mojgan Broumand")

        # Get contact details from store (stored when call was triggered)
        store = get_store()
        customer_name = ""
        customer_phone = ""
        customer_email = ""
        for number, contact in store.items():
            if contact.get("clinic") == "confiderm":
                customer_name = contact.get("firstName", "")
                customer_phone = contact.get("phone", number)
                customer_email = contact.get("email", "")
                break

        if not appointment_time:
            logger.warning("[confiderm] No appointment_date_time in webhook data")
            return jsonify({"status": "no_booking_needed", "reason": "no appointment time collected"}), 200

        # Parse the appointment time
        # Could be ISO format "2026-05-12T10:00:00+10:00" or natural "May 12 at 10am"
        booking_date = ""
        booking_time = ""
        try:
            if "T" in appointment_time:
                dt = datetime.fromisoformat(appointment_time.replace("+10:00", "").replace("+11:00", ""))
                booking_date = dt.strftime("%Y-%m-%d")
                booking_time = dt.strftime("%-I:%M%p").lower()
            else:
                # Best effort parse
                booking_date = appointment_time
                booking_time = ""
        except (ValueError, TypeError) as e:
            logger.warning(f"[confiderm] Could not parse appointment time '{appointment_time}': {e}")

        # Map practitioner to staff ID
        staff_id = "486177"  # Mojgan default
        if practitioner and "Khammar" in practitioner:
            staff_id = "514556"

        cfg = CLINICS["confiderm"]["timely"]

        logger.info(f"[confiderm] Creating Timely booking: {customer_name} on {booking_date} at {booking_time} with staff {staff_id}")

        # Run Playwright booking in background thread (don't block the webhook response)
        def run_booking():
            try:
                from timely_booking import create_booking_sync
                result = create_booking_sync(
                    email=cfg["email"],
                    password=cfg["password"],
                    location_id=cfg["location_id"],
                    customer_name=customer_name or "Customer",
                    customer_phone=customer_phone,
                    customer_email=customer_email,
                    staff_id=staff_id,
                    booking_date=booking_date,
                    booking_time=booking_time,
                    service_name="Injectables Consultation",
                )
                logger.info(f"[confiderm] Timely booking result: {result}")
            except Exception as e:
                logger.exception(f"[confiderm] Timely booking failed: {e}")

        thread = Thread(target=run_booking)
        thread.start()

        return jsonify({
            "status": "booking_queued",
            "customer": customer_name,
            "time": f"{booking_date} {booking_time}",
            "staff": practitioner
        }), 200

    except Exception as e:
        logger.exception("[confiderm] Error in postcall webhook")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port, debug=False)
