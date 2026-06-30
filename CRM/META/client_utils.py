"""
CRM/META/client_utils.py

Multi-Tenant WhatsApp Send Utilities
======================================

Each ClientAccount has its own access_token + phone_number_id.
These utilities send messages using the CLIENT's credentials,
not the JMS TechNova env-var credentials.

Usage:
    from CRM.META.client_utils import client_send_text, client_send_buttons, ...

    client = ClientAccount.objects.get(phone_number_id="1234")
    client_send_text(client, to="919999999999", text="Hello!")
"""

import logging
import queue
import threading
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

WHATSAPP_API_VERSION = "v22.0"


# ─────────────────────────────────────────────────────────────────────────────
# Per-client API helpers
# ─────────────────────────────────────────────────────────────────────────────

def _client_api_url(phone_number_id: str) -> str:
    """Build the Graph API messages endpoint for a specific phone_number_id."""
    return (
        f"https://graph.facebook.com/{WHATSAPP_API_VERSION}"
        f"/{phone_number_id}/messages"
    )


def _client_headers(access_token: str) -> dict:
    """Build auth headers using the client's own access_token."""
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Shared Send Queue — single queue, per-payload credentials
# ─────────────────────────────────────────────────────────────────────────────

class ClientSendQueue:
    """
    Serial send queue for multi-tenant message delivery.

    Unlike the JMS queue (single token), each payload carries its own
    api_url + headers derived from the ClientAccount it belongs to.

    Internal payload keys (prefixed with _):
        _api_url:  per-client Graph API endpoint
        _headers:  per-client Authorization headers
        _enqueued_at: timestamp
    """

    def __init__(
        self,
        max_retries: int = 5,
        pause_after_success: float = 0.4,
    ):
        self.session = requests.Session()
        self.queue = queue.Queue()
        self.max_retries = max_retries
        self.pause_after_success = pause_after_success
        self._worker_thread = threading.Thread(target=self._worker, daemon=True)
        self._worker_thread.start()
        logger.info("ClientSendQueue started (multi-tenant)")

    def enqueue(self, payload: dict, api_url: str, headers: dict) -> bool:
        """Enqueue a payload for sending with per-client credentials."""
        payload["_api_url"] = api_url
        payload["_headers"] = headers
        payload["_enqueued_at"] = time.time()
        self.queue.put(payload)
        return True

    def _worker(self):
        while True:
            payload = self.queue.get()
            try:
                self._process_payload(payload)
            except Exception as e:
                logger.exception("ClientSendQueue worker error: %s", e)
            finally:
                self.queue.task_done()

    def _process_payload(self, payload: dict):
        """Send one payload with retries and exponential backoff."""
        api_url = payload.pop("_api_url")
        headers = payload.pop("_headers")
        payload.pop("_enqueued_at", None)
        clean = {k: v for k, v in payload.items() if not k.startswith("_")}

        attempt = 0
        delay = 1.0
        max_attempts = self.max_retries

        while attempt < max_attempts:
            attempt += 1
            try:
                r = self.session.post(
                    api_url, json=clean, headers=headers, timeout=15
                )
                status_code = r.status_code

                if status_code < 300:
                    logger.info(
                        "✅ [Client] Message sent to %s (attempt %d)",
                        clean.get("to"), attempt,
                    )
                    if self.pause_after_success:
                        time.sleep(self.pause_after_success)
                    return

                if status_code == 429:
                    logger.warning(
                        "⚠️ [Client] Rate limited → %s (attempt %d/%d). Retry in %ds",
                        clean.get("to"), attempt, max_attempts, int(delay),
                    )
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue

                if 500 <= status_code < 600:
                    logger.warning(
                        "[Client] Server error %s → %s. Retry in %ds. resp=%s",
                        status_code, clean.get("to"), int(delay), r.text[:200],
                    )
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue

                # 4xx — client error, do not retry
                logger.error(
                    "[Client] Failed to send to %s: status=%s, resp=%s",
                    clean.get("to"), status_code, r.text[:400],
                )
                return

            except requests.exceptions.RequestException as e:
                logger.warning(
                    "[Client] Network error → %s (attempt %d/%d): %s. Retry in %ds",
                    clean.get("to"), attempt, max_attempts, e, int(delay),
                )
                time.sleep(delay)
                delay = min(delay * 2, 30)

        logger.error(
            "❌ [Client] Giving up after %d attempts for %s",
            max_attempts, clean.get("to"),
        )


# Global multi-tenant queue singleton
CLIENT_SEND_QUEUE = ClientSendQueue(max_retries=5, pause_after_success=0.4)


# ─────────────────────────────────────────────────────────────────────────────
# Public send helpers — all take a ClientAccount as first argument
# ─────────────────────────────────────────────────────────────────────────────

def _enqueue(client, payload: dict) -> bool:
    """
    Internal helper — resolves client credentials and enqueues.
    `client` is a ClientAccount model instance.
    """
    if not client.access_token or not client.phone_number_id:
        logger.error(
            "[Client] Cannot send — missing credentials for client=%s (id=%s)",
            client.name, client.id,
        )
        return False
    api_url = _client_api_url(client.phone_number_id)
    headers = _client_headers(client.access_token)
    return CLIENT_SEND_QUEUE.enqueue(payload, api_url, headers)


def client_send_text(client, to: str, text: str) -> bool:
    """Send a plain-text WhatsApp message using the client's credentials."""
    if not to:
        logger.error("[Client] send_text: missing recipient")
        return False
    to = to.lstrip("+")
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    return _enqueue(client, payload)


def client_send_buttons(client, to: str, body_text: str, buttons: list) -> bool:
    """
    Send WhatsApp interactive reply buttons (max 3).

    buttons = [
        {"id": "btn_yes", "title": "Yes"},
        {"id": "btn_no",  "title": "No"},
    ]
    """
    if not to:
        logger.error("[Client] send_buttons: missing recipient")
        return False
    to = to.lstrip("+")
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {"id": btn["id"], "title": btn["title"]},
                    }
                    for btn in buttons[:3]
                ]
            },
        },
    }
    return _enqueue(client, payload)


def client_send_list(
    client,
    to: str,
    body_text: str,
    button_text: str,
    sections: list,
) -> bool:
    """Send WhatsApp interactive list message using client's credentials."""
    if not to:
        logger.error("[Client] send_list: missing recipient")
        return False
    to = to.lstrip("+")
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body_text},
            "action": {
                "button": str(button_text)[:20],
                "sections": sections,
            },
        },
    }
    return _enqueue(client, payload)


def client_send_image(
    client, to: str, image_url: str, caption: str = ""
) -> bool:
    """Send an image message using client's credentials."""
    if not to:
        logger.error("[Client] send_image: missing recipient")
        return False
    to = to.lstrip("+")
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "image",
        "image": {"link": image_url, "caption": caption},
    }
    return _enqueue(client, payload)


def client_send_document(
    client, to: str, document_url: str, filename: str, caption: str = ""
) -> bool:
    """Send a document (PDF etc.) using client's credentials."""
    if not to:
        logger.error("[Client] send_document: missing recipient")
        return False
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
    return _enqueue(client, payload)


def client_send_template(
    client,
    to: str,
    template_name: str,
    language_code: str = "en",
    components: Optional[list] = None,
) -> bool:
    """
    Send a pre-approved WhatsApp template message using client's credentials.

    components example:
    [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": "Rahul"},
                {"type": "text", "text": "your order #1234"},
            ]
        }
    ]
    """
    if not to:
        logger.error("[Client] send_template: missing recipient")
        return False
    to = to.lstrip("+")
    template_obj = {
        "name": template_name,
        "language": {"code": language_code},
    }
    if components:
        template_obj["components"] = components

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "template",
        "template": template_obj,
    }
    return _enqueue(client, payload)
