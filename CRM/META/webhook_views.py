"""
user_auth/webhook_views.py

Multi-Tenant Meta WhatsApp Webhook
=====================================

Single endpoint handles ALL clients.
Routing via phone_number_id from Meta payload:

  Meta POST  →  Extract phone_number_id
               │
               ├─ ClientAccount match → process for that client
               └─ No match → log + skip (return 200 to stop retries)

Each ClientAccount has its own phone_number_id / access_token / waba_id.
"""

import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from rest_framework.views import APIView
from rest_framework.permissions import AllowAny

from CRM.models import ClientAccount
from CRM.models import Customer, Conversation, Message

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Security
# ─────────────────────────────────────────────────────────────────────────────

def _verify_meta_signature(request) -> bool:
    app_secret = getattr(settings, "META_APP_SECRET", "")
    if not app_secret:
        return True
    sig_header = request.META.get("HTTP_X_HUB_SIGNATURE_256", "")
    if not sig_header.startswith("sha256="):
        return False
    expected = hmac.new(
        app_secret.encode("utf-8"),
        request.body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, sig_header[7:])


# ─────────────────────────────────────────────────────────────────────────────
# Multi-tenant routing
# ─────────────────────────────────────────────────────────────────────────────

def _get_client_by_phone_number_id(phone_number_id: str):
    """
    Route incoming webhook to the correct ClientAccount.

    Client A  phone_number_id=1111  →  ClientAccount(name=A)
    Client B  phone_number_id=2222  →  ClientAccount(name=B)
    Client C  phone_number_id=3333  →  ClientAccount(name=C)

    All share ONE webhook URL. phone_number_id is the routing key.
    """
    try:
        return ClientAccount.objects.select_related("tech_provider").get(
            phone_number_id=phone_number_id,
            status="active",
        )
    except ClientAccount.DoesNotExist:
        logger.warning(
            "[Webhook] No active ClientAccount for phone_number_id=%s", phone_number_id
        )
        return None
    except ClientAccount.MultipleObjectsReturned:
        logger.error(
            "[Webhook] Multiple clients for phone_number_id=%s — using first", phone_number_id
        )
        return (
            ClientAccount.objects
            .filter(phone_number_id=phone_number_id, status="active")
            .select_related("tech_provider")
            .first()
        )


# ─────────────────────────────────────────────────────────────────────────────
# Customer + Conversation helpers
# ─────────────────────────────────────────────────────────────────────────────

def _upsert_customer(phone: str, name: str = "") -> Customer:
    """
    Customers are global (identified by phone number).
    Each client sees the same Customer row, but has their own Conversation.
    """
    customer, created = Customer.objects.get_or_create(
        phone=phone,
        defaults={"name": name or phone},
    )
    if not created and name and customer.name == customer.phone:
        customer.name = name
        customer.save(update_fields=["name"])
    if created:
        logger.info("[Webhook] New customer: %s", phone)
    return customer


def _get_or_create_conversation(customer: Customer, client: ClientAccount) -> Conversation:
    """
    Each client has ISOLATED conversations.
    Customer 9999 messaging Client A and Client B = 2 separate conversations.
    """
    conv = (
        Conversation.objects
        .filter(customer=customer, client=client, status="open")
        .order_by("-created_at")
        .first()
    )
    if not conv:
        conv = Conversation.objects.create(
            customer=customer,
            client=client,
            status="open",
        )
        logger.info(
            "[Webhook] New conversation id=%s customer=%s client=%s",
            conv.id, customer.phone, client.name,
        )
    return conv


# ─────────────────────────────────────────────────────────────────────────────
# Message parsing
# ─────────────────────────────────────────────────────────────────────────────

def _extract_body(msg_payload: dict) -> str:
    msg_type = msg_payload.get("type", "text")
    if msg_type == "text":
        return msg_payload.get("text", {}).get("body", "")
    if msg_type == "interactive":
        interactive = msg_payload.get("interactive", {})
        itype = interactive.get("type", "")
        if itype == "button_reply":
            return interactive.get("button_reply", {}).get("title", "")
        if itype == "list_reply":
            return interactive.get("list_reply", {}).get("title", "")
        return "[Interactive]"
    if msg_type == "button":
        return msg_payload.get("button", {}).get("text", "")
    if msg_type == "location":
        loc = msg_payload.get("location", {})
        lat = loc.get('latitude', '')
        lng = loc.get('longitude', '')
        return f"[Location: {lat}, {lng}]"
    return {
        "image": "[Image]", "audio": "[Audio]", "video": "[Video]",
        "document": "[Document]", "sticker": "[Sticker]",
        "reaction": "[Reaction]", "order": "[Order]", "contacts": "[Contacts]",
    }.get(msg_type, f"[{msg_type}]")


def _parse_timestamp(ts_str) -> object:
    if not ts_str:
        return timezone.now()
    try:
        from datetime import datetime
        return timezone.make_aware(datetime.utcfromtimestamp(int(ts_str)), timezone.utc)
    except Exception:
        return timezone.now()


# ─────────────────────────────────────────────────────────────────────────────
# Event handlers
# ─────────────────────────────────────────────────────────────────────────────

def _handle_inbound_message(client: ClientAccount, msg_payload: dict, contact: dict):
    phone        = msg_payload.get("from", "")
    wa_msg_id    = msg_payload.get("id", "")
    msg_type     = msg_payload.get("type", "text")
    profile_name = contact.get("profile", {}).get("name", "") if contact else ""
    timestamp    = _parse_timestamp(msg_payload.get("timestamp"))

    if not phone:
        return

    customer     = _upsert_customer(phone, profile_name)
    conversation = _get_or_create_conversation(customer, client)

    if wa_msg_id and Message.objects.filter(whatsapp_message_id=wa_msg_id).exists():
        logger.debug("[Webhook] Duplicate wamid=%s — skipping", wa_msg_id)
        return

    Message.objects.create(
        conversation=conversation,
        direction="inbound",
        message_type=msg_type,
        body=_extract_body(msg_payload),
        whatsapp_message_id=wa_msg_id,
        status="received",
        timestamp=timestamp,
    )
    logger.info(
        "[Webhook] Saved inbound wamid=%s phone=%s client=%s conv=%s",
        wa_msg_id, phone, client.name, conversation.id,
    )


def _handle_status_update(status_payload: dict):
    wa_msg_id  = status_payload.get("id", "")
    new_status = status_payload.get("status", "")
    error_data = status_payload.get("errors", [])
    if not wa_msg_id or not new_status:
        return
    updated = Message.objects.filter(whatsapp_message_id=wa_msg_id).update(
        status=new_status,
        error_details=json.dumps(error_data) if error_data else "",
    )
    if updated:
        logger.info("[Webhook] Status wamid=%s -> %s", wa_msg_id, new_status)
    else:
        logger.debug("[Webhook] Unknown wamid=%s status=%s", wa_msg_id, new_status)


# ─────────────────────────────────────────────────────────────────────────────
# Main view
# ─────────────────────────────────────────────────────────────────────────────

@method_decorator(csrf_exempt, name="dispatch")
class WhatsAppWebhookView(APIView):
    """
    GET  /api/webhook/whatsapp/  ->  Meta verification (one-time)
    POST /api/webhook/whatsapp/  ->  All client events

    One URL, N clients. Routing by phone_number_id.
    """
    permission_classes     = [AllowAny]
    authentication_classes = []

    def get(self, request):
        mode      = request.query_params.get("hub.mode", "")
        token     = request.query_params.get("hub.verify_token", "")
        challenge = request.query_params.get("hub.challenge", "")
        verify_token = getattr(settings, "VERIFY_TOKEN", "")

        if mode == "subscribe" and token == verify_token:
            logger.info("[Webhook] Meta verification OK")
            return HttpResponse(challenge, content_type="text/plain", status=200)

        logger.warning("[Webhook] Verification FAILED mode=%s", mode)
        return HttpResponse("Forbidden", status=403)

    def post(self, request):
        if not _verify_meta_signature(request):
            logger.error("[Webhook] Bad signature")
            return HttpResponse("Forbidden", status=403)

        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            return HttpResponse("OK", status=200)

        if payload.get("object") != "whatsapp_business_account":
            return HttpResponse("OK", status=200)

        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                if change.get("field") != "messages":
                    continue

                value           = change.get("value", {})
                metadata        = value.get("metadata", {})
                phone_number_id = metadata.get("phone_number_id", "")

                # Multi-tenant routing
                client = _get_client_by_phone_number_id(phone_number_id)
                if not client:
                    continue

                contacts = value.get("contacts", [])

                for msg in value.get("messages", []):
                    try:
                        _handle_inbound_message(client, msg, contacts[0] if contacts else {})
                    except Exception:
                        logger.exception(
                            "[Webhook] Error processing message client=%s", client.name
                        )

                for stat in value.get("statuses", []):
                    try:
                        _handle_status_update(stat)
                    except Exception:
                        logger.exception("[Webhook] Error processing status update")

        return HttpResponse("OK", status=200)