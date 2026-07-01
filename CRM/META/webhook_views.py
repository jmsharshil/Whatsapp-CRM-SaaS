"""
CRM/META/webhook_views.py

Multi-Tenant Meta WhatsApp Webhook
=====================================

Single endpoint handles ALL traffic — both JMS internal bots and client bots.

Routing via phone_number_id from Meta payload:

  Meta POST  →  Extract phone_number_id
               │
               ├─ ClientAccount match → client_views.handle_client_message()
               │                        (uses client's own access_token to reply)
               │
               └─ No match → JMS internal 3-bot router
                             (industry / whatsapp-api / jms-tech bot)
                             (uses JMS env-var credentials)

Each ClientAccount has its own phone_number_id / access_token / waba_id.
Multiple clients share ONE webhook URL. phone_number_id is the routing key.
"""

import hashlib
import hmac
import json
import logging
import os
from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from rest_framework.views import APIView
from rest_framework.permissions import AllowAny

from CRM.models import ClientAccount, Customer, Conversation, Message

# ── Client bot flow (multi-tenant) ────────────────────────────────────────────
from CRM.META.client_views import handle_client_message

# ── JMS internal bot imports ──────────────────────────────────────────────────
from CRM.jmschatagents_views import (
    # Session stores
    ind_sessions,
    ind_get_session,
    ind_save_session,
    wa_sessions,
    # Constants
    INDUSTRY_TRIGGERS,
    FAREWELL_KEYWORDS,
    WA_BOT_TRIGGER,
    BOT_TRIGGER_KEYWORD,
    IND_SESSION_TTL,
    # Bot handlers
    _process_meta_message,
    _jms_handle_message,
    _handle_wa_bot,
    # Session key helper
    _sess_key,
    # DB helper
    save_message,
    # Shared send
    _send,
)

from CRM.gigatel_views import handle_gigatel_message
from CRM.globestar_views import handle_globestar_message
from CRM.globestar_utils import GLOBESTAR_PHONE_NUMBER_ID

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Security
# ─────────────────────────────────────────────────────────────────────────────

def _verify_meta_signature(request) -> bool:
    """Verify X-Hub-Signature-256 header from Meta."""
    app_secret = getattr(settings, "META_APP_SECRET", "")
    if not app_secret:
        return True
    if settings.DEBUG:
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
    Returns None if no match (= JMS internal traffic).
    """
    try:
        return ClientAccount.objects.select_related("tech_provider").get(
            phone_number_id=phone_number_id,
            status="active",
        )
    except ClientAccount.DoesNotExist:
        return None
    except ClientAccount.MultipleObjectsReturned:
        logger.error(
            "[Webhook] Multiple clients for phone_number_id=%s — using first",
            phone_number_id,
        )
        return (
            ClientAccount.objects
            .filter(phone_number_id=phone_number_id, status="active")
            .select_related("tech_provider")
            .first()
        )


# ─────────────────────────────────────────────────────────────────────────────
# JMS internal 3-bot router (moved from jmschatagents_views.webhook)
# ─────────────────────────────────────────────────────────────────────────────

def _extract_text_for_routing(msg: dict) -> str | None:
    """Extract text from Meta message for JMS bot routing."""
    msg_type = msg.get("type", "")
    if msg_type == "text":
        return msg.get("text", {}).get("body", "").strip()
    if msg_type == "interactive":
        interactive = msg.get("interactive", {})
        itype = interactive.get("type", "")
        if itype == "button_reply":
            return interactive.get("button_reply", {}).get("id", "").strip()
        if itype == "list_reply":
            return (
                interactive.get("list_reply", {}).get("id", "").strip()
                or interactive.get("list_reply", {}).get("title", "").strip()
            )
    if msg_type == "image":
        return msg.get("image", {}).get("caption", "").strip() or None
    if msg_type == "document":
        return msg.get("document", {}).get("caption", "").strip() or None
    if msg_type == "button":
        return msg.get("button", {}).get("text", "").strip()
    return None


def _handle_jms_internal_message(msg: dict, value: dict, phone_number_id: str = None):
    """
    JMS TechNova's internal 3-bot router.
    Handles messages when phone_number_id doesn't match any ClientAccount.

    Routes between:
      - Industry / Technova AI bot (trigger: hi/hello/hey)
      - WhatsApp-API assistant bot (trigger: whatsapp)
      - JMS-Tech website analysis bot (trigger: jms)
    """
    raw_phone = msg.get("from", "").strip()
    if not raw_phone:
        return
    if not raw_phone.startswith("+"):
        raw_phone = f"+{raw_phone}"

    # ── Deduplication ─────────────────────────────────────────────────────
    msg_id_wa = msg.get("id", "")
    if msg_id_wa:
        dedup_key = f"wamid_u:{msg_id_wa}"
        if cache.get(dedup_key):
            logger.info("Duplicate wamid %s skipped", msg_id_wa)
            return
        cache.set(dedup_key, "1", timeout=300)

    # ── Extract text ──────────────────────────────────────────────────────
    text = _extract_text_for_routing(msg)
    if not text:
        return

    text_lower = text.lower().strip()
    logger.info("[Webhook/JMS] from=%s text=%r", raw_phone, text_lower)

    # ── Route decision ────────────────────────────────────────────────────
    go_industry = False
    go_wa_bot = False
    skip_all = False

    if text_lower in INDUSTRY_TRIGGERS:
        go_industry = True
    else:
        ind_sess = ind_sessions.get(_sess_key(raw_phone))

        if ind_sess and ind_sess.get("stage", "idle") != "idle":
            if text_lower in FAREWELL_KEYWORDS:
                ind_sess["stage"] = "idle"
                ind_sess["active_bot"] = None
                ind_sess["chat_history"] = []
                ind_sessions.set(
                    _sess_key(raw_phone), ind_sess, timeout=IND_SESSION_TTL
                )
                _send(
                    raw_phone,
                    f"Thank you for using Technova AI! 😊\n\n"
                    f"Send *{BOT_TRIGGER_KEYWORD}* anytime to start again.",
                )
                skip_all = True

            elif text_lower == "jms":
                ind_sess["stage"] = "idle"
                ind_sess["active_bot"] = None
                ind_sess["chat_history"] = []
                ind_sessions.set(
                    _sess_key(raw_phone), ind_sess, timeout=IND_SESSION_TTL
                )
                go_industry = False

            else:
                go_industry = True

    if skip_all:
        return

    # ── Dispatch ──────────────────────────────────────────────────────────
    if go_industry:
        logger.info("[Webhook/JMS] → INDUSTRY BOT for %s", raw_phone)
        try:
            contacts = value.get("contacts", [])
            client_name = (
                contacts[0].get("profile", {}).get("name", "")
                if contacts
                else ""
            )
            db_msg = save_message(
                phone=raw_phone,
                content=text,
                reply_of=None,
                client_name=client_name,
                phone_number_id=phone_number_id,
            )
            ind_msg_id = db_msg.id if db_msg else None

            ind_sess = ind_get_session(raw_phone)
            ind_sess["last_msg_id"] = ind_msg_id
            ind_sess["last_user_text"] = text
            ind_save_session(ind_sess)

            _process_meta_message(msg, value)
        except Exception as e:
            logger.exception("Industry bot error: %s", e)

    else:
        # Check WhatsApp-API bot first
        wa_sess = wa_sessions.get(raw_phone)
        if text_lower == WA_BOT_TRIGGER or (
            wa_sess and wa_sess.get("stage") == "active"
        ):
            logger.info("[Webhook/JMS] → WHATSAPP BOT for %s", raw_phone)
            try:
                _handle_wa_bot(raw_phone, text, phone_number_id)
            except Exception as e:
                logger.exception("WhatsApp bot error: %s", e)
        else:
            logger.info("[Webhook/JMS] → JMS-TECH BOT for %s", raw_phone)
            try:
                _jms_handle_message(raw_phone, text, phone_number_id)
            except Exception as e:
                logger.exception("JMS-Tech bot error: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Main Webhook View
# ─────────────────────────────────────────────────────────────────────────────

@method_decorator(csrf_exempt, name="dispatch")
class WhatsAppWebhookView(APIView):
    """
    GET  /api/webhook/whatsapp/  ->  Meta verification (one-time)
    POST /api/webhook/whatsapp/  ->  All client + JMS events

    One URL, N clients + JMS bots. Routing by phone_number_id.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    # ── GET: Meta hub verification ────────────────────────────────────────
    def get(self, request):
        mode = request.query_params.get("hub.mode", "")
        token = request.query_params.get("hub.verify_token", "")
        challenge = request.query_params.get("hub.challenge", "")
        verify_token = getattr(settings, "VERIFY_TOKEN", "")

        if mode == "subscribe" and token == verify_token:
            logger.info("[Webhook] Meta verification OK")
            return HttpResponse(challenge, content_type="text/plain", status=200)

        logger.warning("[Webhook] Verification FAILED mode=%s", mode)
        return HttpResponse("Forbidden", status=403)

    # ── POST: All inbound messages + status updates ───────────────────────
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

                value = change.get("value", {})
                metadata = value.get("metadata", {})
                phone_number_id = metadata.get("phone_number_id", "")

                # ── Multi-tenant routing ──────────────────────────────────
                client = _get_client_by_phone_number_id(phone_number_id)

                contacts = value.get("contacts", [])

                # ── Process messages ──────────────────────────────────────
                for msg in value.get("messages", []):
                    try:
                        gigatel_phone_id = os.environ.get("META_PHONE_NUMBER_ID", "").strip()
                        globestar_phone_id = GLOBESTAR_PHONE_NUMBER_ID
                        
                        if gigatel_phone_id and phone_number_id == gigatel_phone_id:
                            logger.info("[Webhook] Routing message to Gigatel Bot")
                            handle_gigatel_message(msg)
                        elif globestar_phone_id and phone_number_id == globestar_phone_id:
                            logger.info("[Webhook] Routing message to Globe Star Bot")
                            handle_globestar_message(msg)
                        elif client:
                            # ── CLIENT BOT FLOW ───────────────────────────
                            # Route to client-specific bot
                            handle_client_message(
                                client,
                                msg,
                                contacts[0] if contacts else {},
                            )
                        else:
                            # ── JMS INTERNAL (3-bot router) ───────────────
                            logger.info("[Webhook] Routing message to JMS Internal Bots")
                            _handle_jms_internal_message(msg, value, phone_number_id)

                    except Exception:
                        logger.exception(
                            "[Webhook] Error processing message "
                            "phone_number_id=%s client=%s",
                            phone_number_id,
                            client.name if client else "JMS/Gigatel-internal",
                        )

                # ── Process status updates ────────────────────────────────
                for stat in value.get("statuses", []):
                    try:
                        _handle_status_update(stat)
                    except Exception:
                        logger.exception("[Webhook] Error processing status update")

        return HttpResponse("OK", status=200)


# ─────────────────────────────────────────────────────────────────────────────
# Status update handler
# ─────────────────────────────────────────────────────────────────────────────

def _handle_status_update(status_payload: dict):
    """Update message delivery status from Meta webhook."""
    wa_msg_id = status_payload.get("id", "")
    new_status = status_payload.get("status", "")
    error_data = status_payload.get("errors", [])
    if not wa_msg_id or not new_status:
        return

    # Try meta_message_id first (client bot), then whatsapp_message_id fallback
    updated = Message.objects.filter(meta_message_id=wa_msg_id).update(
        status=new_status,
        error_message=json.dumps(error_data) if error_data else "",
    )
    if updated:
        logger.info("[Webhook] Status wamid=%s -> %s", wa_msg_id, new_status)
    else:
        logger.debug("[Webhook] Unknown wamid=%s status=%s", wa_msg_id, new_status)