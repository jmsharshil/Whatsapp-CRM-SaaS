import os
import requests
import logging
import threading
import queue
import time

logger = logging.getLogger(__name__)

# ── Meta WhatsApp Cloud API config ────────────────────────────────────────────
WHATSAPP_TOKEN           = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_API_VERSION     = os.getenv("WHATSAPP_API_VERSION", "v22.0")


def _api_url() -> str:
    return (
        f"https://graph.facebook.com/{WHATSAPP_API_VERSION}"
        f"/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    )


def _headers() -> dict:
    if not WHATSAPP_TOKEN:
        raise RuntimeError("WHATSAPP_TOKEN is not set")
    if not WHATSAPP_PHONE_NUMBER_ID:
        raise RuntimeError("WHATSAPP_PHONE_NUMBER_ID is not set")
    return {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }


# ── Send Queue ────────────────────────────────────────────────────────────────

class MetaSendQueue:
    """
    Serial send queue with per-payload retry/backoff for Meta Cloud API.
    Processes one message at a time to respect ordering and rate limits.
    """

    def __init__(
        self,
        headers_func,
        max_retries: int = 5,
        pause_after_success: float = 0.4,
    ):
        self._headers            = headers_func
        self.session             = requests.Session()
        self.queue               = queue.Queue()
        self.max_retries         = max_retries
        self.pause_after_success = pause_after_success
        self._worker_thread      = threading.Thread(target=self._worker, daemon=True)
        self._worker_thread.start()
        logger.info("MetaSendQueue started → %s", _api_url())

    def enqueue(self, payload: dict) -> bool:
        """Enqueue a payload for sending (non-blocking)."""
        payload["_enqueued_at"] = time.time()
        self.queue.put(payload)
        return True

    def _worker(self):
        while True:
            payload = self.queue.get()
            try:
                self._process_payload(payload)
            except Exception as e:
                logger.exception("Unexpected worker error: %s", e)
            finally:
                self.queue.task_done()

    def _process_payload(self, payload: dict):
        """Send one payload with retries and exponential backoff."""
        clean        = {k: v for k, v in payload.items() if not k.startswith("_")}
        attempt      = 0
        delay        = 1.0
        max_attempts = payload.get("_max_retries", self.max_retries)

        while attempt < max_attempts:
            attempt += 1
            try:
                logger.debug(
                    "Sending (attempt %d/%d) to %s",
                    attempt, max_attempts, clean.get("to"),
                )
                r = self.session.post(
                    _api_url(), json=clean, headers=self._headers(), timeout=15
                )
                status = r.status_code

                if status < 300:
                    logger.info(
                        "✅ Message sent to %s (attempt %d).", clean.get("to"), attempt
                    )
                    if self.pause_after_success:
                        time.sleep(self.pause_after_success)
                    return

                if status == 429:
                    logger.warning(
                        "⚠️ Rate limited → %s (attempt %d/%d). Retry in %ds",
                        clean.get("to"), attempt, max_attempts, int(delay),
                    )
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue

                if 500 <= status < 600:
                    logger.warning(
                        "Server error %s → %s. Retry in %ds. resp=%s",
                        status, clean.get("to"), int(delay), r.text[:200],
                    )
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue

                # 4xx — client error, do not retry
                logger.error(
                    "Failed to send to %s: status=%s, resp=%s",
                    clean.get("to"), status, r.text[:400],
                )
                return

            except requests.exceptions.RequestException as e:
                logger.warning(
                    "Network error → %s (attempt %d/%d): %s. Retry in %ds",
                    clean.get("to"), attempt, max_attempts, e, int(delay),
                )
                time.sleep(delay)
                delay = min(delay * 2, 30)

        logger.error(
            "❌ Giving up after %d attempts for %s", max_attempts, clean.get("to")
        )


# Global queue singleton — imported by all bot views
SEND_QUEUE = MetaSendQueue(_headers, max_retries=60, pause_after_success=0.4)


# ── Public send helpers ───────────────────────────────────────────────────────

def send_text(to: str, text: str):
    """Send a plain-text WhatsApp message via Meta Cloud API."""
    if not to:
        logger.error("send_text: missing recipient")
        return
    to = to.lstrip("+")
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    return SEND_QUEUE.enqueue(payload)


def send_template(to: str, template_name: str, language_code: str = "en", components: list = None):
    """Send a WhatsApp template message via Meta Cloud API."""
    if not to:
        logger.error("send_template: missing recipient")
        return
    to = to.lstrip("+")
    template_data = {
        "name": template_name,
        "language": {"code": language_code}
    }
    if components:
        template_data["components"] = components

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "template",
        "template": template_data,
    }
    return SEND_QUEUE.enqueue(payload)


def send_url_button(to: str, body_text: str, button_text: str, url: str):
    """
    Send WhatsApp interactive CTA URL button.
    """
    if not to:
        logger.error("send_url_button: missing recipient")
        return
    to = to.lstrip("+")
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "cta_url",
            "body": {"text": body_text},
            "action": {
                "name": "cta_url",
                "parameters": {
                    "display_text": button_text,
                    "url": url
                }
            }
        }
    }
    return SEND_QUEUE.enqueue(payload)


def send_buttons(to: str, body_text: str, buttons: list, header_image_url: str = None):
    """
    Send WhatsApp interactive reply buttons (max 3). Optionally include an image header.

    buttons = [
        {"id": "1", "title": "English"},
        {"id": "2", "title": "Hindi"},
        {"id": "3", "title": "Gujarati"},
    ]
    """
    if not to:
        logger.error("send_buttons: missing recipient")
        return
    to = to.lstrip("+")
    
    interactive = {
        "type": "button",
        "body": {"text": body_text[:1024]},
        "action": {
            "buttons": [
                {
                    "type": "reply",
                    "reply": {"id": btn["id"], "title": btn["title"][:20]},
                }
                for btn in buttons[:3]
            ]
        },
    }
    
    if header_image_url:
        interactive["header"] = {
            "type": "image",
            "image": {"link": header_image_url}
        }

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": interactive,
    }
    return SEND_QUEUE.enqueue(payload)


def send_list(
    to,
    body_text=None,
    button_text=None,
    sections=None,
    body=None,           # kept for callers using body= kwarg
    button_label=None,   # kept for callers using button_label= kwarg
):
    body_text   = body_text or body
    button_text = button_text or button_label
    sections    = sections or []

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to.lstrip("+"),
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body_text},
            "action": {
                "button": str(button_text)[:20],   # Meta caps this at 20 chars
                "sections": sections,
            },
        },
    }
    return SEND_QUEUE.enqueue(payload)


# Alias used in some places
send_interactive_list = send_list


def send_image(to: str, image_url: str, caption: str = ""):
    """Send an image message via Meta Cloud API."""
    if not to:
        logger.error("send_image: missing recipient")
        return
    to = to.lstrip("+")
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "image",
        "image": {"link": image_url, "caption": caption},
    }
    return SEND_QUEUE.enqueue(payload)


def send_document(to: str, document_url: str, filename: str, caption: str = ""):
    """
    Send a WhatsApp document (e.g. PDF) via Meta Cloud API.

    Args:
        to (str):           Recipient phone number
        document_url (str): Publicly accessible document URL
        filename (str):     Filename shown in WhatsApp
        caption (str):      Optional caption
    """
    if not to:
        logger.error("send_document: missing recipient")
        return
    to = to.lstrip("+")
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "document",
        "document": {
            "link": document_url,
            "filename": filename,
            "caption": caption,
        },
    }
    return SEND_QUEUE.enqueue(payload)


def send_location(phone: str, latitude: float, longitude: float, text: str, address: str):
    """
    Send a WhatsApp location pin via Meta Cloud API.

    If `text` is provided it is sent as a separate text message before the pin,
    because Meta's location message type does not support a freeform text field
    alongside the coordinate card.

    Args:
        phone (str):       Recipient in international format, e.g. '918401611072'
        latitude (float):  Latitude of the location
        longitude (float): Longitude of the location
        text (str):        Optional message sent before the pin
        address (str):     Address shown in the pin card
    """
    if not phone:
        logger.error("send_location: missing recipient")
        return
    phone = phone.lstrip("+")
    if text:
        send_text(phone, text)
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone,
        "type": "location",
        "location": {
            "latitude": latitude,
            "longitude": longitude,
            "name": "Dr. Shah's Super Speciality Eye Hospital",
            "address": address,
        },
    }
    try:
        return SEND_QUEUE.enqueue(payload)
    except Exception as e:
        logger.warning("send_location failed: %s", e)


def send_contact(phone: str, contact_name: str, contact_number: str):
    """
    Send a WhatsApp contact card via Meta Cloud API.

    Args:
        phone (str):          Recipient in international format
        contact_name (str):   Contact's full name
        contact_number (str): Contact's phone number in international format
    """
    if not phone:
        logger.error("send_contact: missing recipient")
        return
    phone = phone.lstrip("+")
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone,
        "type": "contacts",
        "contacts": [
            {
                "name": {
                    "formatted_name": contact_name,
                    "first_name": contact_name,
                },
                "phones": [
                    {
                        "phone": contact_number,
                        "type": "CELL",
                        "wa_id": contact_number.lstrip("+"),
                    }
                ],
            }
        ],
    }
    try:
        return SEND_QUEUE.enqueue(payload)
    except Exception as e:
        logger.warning("send_contact failed: %s", e)


def send_language_list(phone: str):
    """Send the language-selection prompt (plain text fallback)."""
    if not phone:
        return
    message = (
        "Thank you for contacting JMS TechNova.\n"
        "🌐 Please select your preferred language:\n\n"
        "1️⃣ English\n"
        "2️⃣ हिंदी\n"
        "3️⃣ ગુજરાતી\n\n"
        "Reply with the number of your choice."
    )
    return send_text(phone, message)