"""
Aria Webhook Proxy
Receives GHL webhook (custom data format) and forwards as JSON body to ElevenLabs outbound call API.
Deploy to Render as a web service.
"""

from flask import Flask, request, jsonify
import requests
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

ELEVEN_LABS_URL = "https://api.elevenlabs.io/v1/convai/twilio/outbound_call"
ELEVEN_LABS_KEY = os.environ.get("XI_API_KEY", "sk_1a2a2a07d19b6f993c0b98203dd355f2938c9f855ba76d97")
AGENT_ID = "agent_6001kpa99tm7fm5sk5da7h057s3r"
AGENT_PHONE_ID = "phnum_3501kpvsp97afx0sy9d0pnzhqwnk"


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "aria-webhook-proxy"})


@app.route("/trigger-call", methods=["POST"])
def trigger_call():
    """Receive GHL webhook and trigger ElevenLabs outbound call."""
    try:
        # GHL sends data as JSON or form data
        data = request.json or request.form.to_dict()
        logger.info(f"Received webhook: {data}")

        # Extract contact details - GHL sends these in various formats
        to_number = (
            data.get("to_number") or
            data.get("phone") or
            data.get("contact_phone") or
            data.get("customer_phone", "")
        )
        customer_name = (
            data.get("customer_name") or
            data.get("contact_name") or
            data.get("first_name") or
            data.get("firstName", "")
        )
        customer_email = (
            data.get("customer_email") or
            data.get("contact_email") or
            data.get("email", "")
        )
        contact_id = data.get("contact_id", "")

        if not to_number:
            logger.error("No phone number provided")
            return jsonify({"error": "No phone number provided"}), 400

        # Ensure phone has + prefix
        if not to_number.startswith("+"):
            to_number = "+" + to_number

        # Build ElevenLabs request
        eleven_payload = {
            "agent_id": AGENT_ID,
            "agent_phone_number_id": AGENT_PHONE_ID,
            "to_number": to_number,
            "conversation_config_override": {
                "agent": {
                    "dynamic_variables": {
                        "dynamic_variable_placeholders": {
                            "customer_name": customer_name or "there",
                            "customer_email": customer_email,
                            "customer_phone": to_number,
                            "contact_id": contact_id
                        }
                    }
                }
            }
        }

        logger.info(f"Calling ElevenLabs for {to_number} ({customer_name})")

        r = requests.post(
            ELEVEN_LABS_URL,
            headers={
                "xi-api-key": ELEVEN_LABS_KEY,
                "Content-Type": "application/json"
            },
            json=eleven_payload,
            timeout=30
        )

        logger.info(f"ElevenLabs response: {r.status_code} - {r.text[:200]}")

        return jsonify({
            "status": "call_triggered",
            "eleven_status": r.status_code,
            "response": r.json() if r.status_code == 200 else r.text[:200],
            "customer_name": customer_name,
            "to_number": to_number
        }), 200

    except Exception as e:
        logger.exception("Error processing webhook")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port, debug=False)
