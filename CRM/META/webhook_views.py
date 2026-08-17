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
import requests
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
    mf_sessions,
    shopify_sessions,
    # Constants
    INDUSTRY_TRIGGERS,
    SHOPIFY_BOT_TRIGGER,
    FAREWELL_KEYWORDS,
    WA_BOT_TRIGGER,
    MF_BOT_TRIGGER,
    BOT_TRIGGER_KEYWORD,
    IND_SESSION_TTL,
    # Bot handlers
    _process_meta_message,
    _jms_handle_message,
    _handle_wa_bot,
    _handle_mf_bot,
    _handle_shopify_bot,
    # Session key helper
    _sess_key,
    # DB helper
    save_message,
    _save_reply,
    # Shared send
    _send,
)
from CRM.jaivik_views import (
    avantika_sessions,
    AVANTIKA_SESSION_TTL,
    AVANTIKA_BOT_TRIGGER,
    _handle_avantika_bot,
)

from CRM.gigatel_views import handle_gigatel_message
from CRM.globestar_views import handle_globestar_message
from CRM.globestar_utils import GLOBESTAR_PHONE_NUMBER_ID
from CRM.gkd_views import handle_gkd_message
from CRM.gkd_utils import GKD_PHONE_NUMBER_ID
from CRM.amritcement_views import handle_amritcement_message
from CRM.amritcement_utils import AMRITCEMENT_PHONE_NUMBER_ID

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
            return (
                interactive.get("button_reply", {}).get("title", "").strip()
                or interactive.get("button_reply", {}).get("id", "").strip()
            )
        if itype == "list_reply":
            return (
                interactive.get("list_reply", {}).get("title", "").strip()
                or interactive.get("list_reply", {}).get("id", "").strip()
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
    msg_type = msg.get("type", "")
    text = _extract_text_for_routing(msg)
    if not text and msg_type != "image":
        return
    text = text or ""

    text_lower = text.lower().strip()
    logger.info("[Webhook/JMS] from=%s text=%r", raw_phone, text_lower)

    # ── Sessions ──────────────────────────────────────────────────────────
    ind_sess = ind_sessions.get(_sess_key(raw_phone))
    avantika_sess = avantika_sessions.get(raw_phone)
    mf_sess = mf_sessions.get(raw_phone)
    wa_sess = wa_sessions.get(raw_phone)
    shopify_sess = shopify_sessions.get(raw_phone)

    # ── Route decision ────────────────────────────────────────────────────
    go_industry = False
    go_avantika = False
    go_mf = False
    go_wa = False
    go_jms = False
    go_shopify = False
    go_navratri = False
    skip_all = False

    def _clear_other_sessions(keep_bot):
        if keep_bot != "industry":
            isess = ind_sessions.get(_sess_key(raw_phone))
            if isess and isess.get("stage", "idle") != "idle":
                isess["stage"] = "idle"
                isess["active_bot"] = None
                ind_sessions.set(_sess_key(raw_phone), isess, timeout=IND_SESSION_TTL)
        if keep_bot != "mf":
            mf_sessions.delete(raw_phone)
        if keep_bot != "wa":
            wa_sessions.delete(raw_phone)
        if keep_bot != "avantika":
            avantika_sessions.delete(raw_phone)
        if keep_bot != "shopify":
            shopify_sessions.delete(raw_phone)

    # 1. Explicit Triggers (Top Priority)
    if text_lower in INDUSTRY_TRIGGERS:
        go_industry = True
        _clear_other_sessions("industry")
    elif text_lower == AVANTIKA_BOT_TRIGGER:
        go_avantika = True
        _clear_other_sessions("avantika")
    elif text_lower in MF_BOT_TRIGGER:
        go_mf = True
        _clear_other_sessions("mf")
    elif text_lower == WA_BOT_TRIGGER:
        go_wa = True
        _clear_other_sessions("wa")
    elif text_lower == SHOPIFY_BOT_TRIGGER:
        go_shopify = True
        _clear_other_sessions("shopify")
    elif text_lower == "jms":
        go_jms = True
        _clear_other_sessions("jms")
    elif text_lower in FAREWELL_KEYWORDS:
        if ind_sess and ind_sess.get("stage", "idle") != "idle":
            ind_sess["stage"] = "idle"
            ind_sess["active_bot"] = None
            ind_sess["chat_history"] = []
            ind_sessions.set(_sess_key(raw_phone), ind_sess, timeout=IND_SESSION_TTL)
            _send(
                raw_phone,
                f"Thank you for using Technova AI! 😊\n\n"
                f"Send *{BOT_TRIGGER_KEYWORD}* anytime to start again.",
            )
        skip_all = True
    else:
        # 2. Active Sessions (Fallback)
        if ind_sess and ind_sess.get("stage", "idle") != "idle":
            go_industry = True
        elif avantika_sess and avantika_sess.get("stage") == "active":
            go_avantika = True
        elif mf_sess and mf_sess.get("stage") in ["active", "awaiting_search_query", "awaiting_category", "awaiting_llm_query"]:
            go_mf = True
        elif wa_sess and wa_sess.get("stage") == "active":
            go_wa = True
        elif shopify_sess and shopify_sess.get("stage", "idle") != "idle":
            go_shopify = True
        else:
            # Check if this is an active Navratri conversation
            phone_no_plus = raw_phone.replace("+", "")
            search_phones = [raw_phone, phone_no_plus]
            if len(phone_no_plus) > 10:
                search_phones.append(phone_no_plus[-10:])
            
            navratri_conv = Conversation.objects.filter(customer__phone__in=search_phones, bot_state="NAVRATRI").first()
            if navratri_conv:
                go_navratri = True
            else:
                go_jms = True

    if skip_all:
        return

    # ── ALWAYS Save Inbound Message ───────────────────────────────────────────
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
    inbound_msg_id = db_msg.id if db_msg else None

    # ── Dispatch ──────────────────────────────────────────────────────────
    if go_industry:
        logger.info("[Webhook/JMS] → INDUSTRY BOT for %s", raw_phone)
        try:
            ind_sess = ind_get_session(raw_phone)
            ind_sess["last_msg_id"] = inbound_msg_id
            ind_sess["last_user_text"] = text
            ind_save_session(ind_sess)

            _process_meta_message(msg, value)
        except Exception as e:
            logger.exception("Industry bot error: %s", e)

    elif go_avantika:
        logger.info("[Webhook/JMS] → AVANTIKA BOT for %s", raw_phone)
        try:
            _handle_avantika_bot(raw_phone, text, phone_number_id, inbound_msg_id=inbound_msg_id, client_name=client_name)
        except Exception as e:
            logger.exception("Avantika bot error: %s", e)
            
    elif go_mf:
        logger.info("[Webhook/JMS] → MUTUAL FUNDS BOT for %s", raw_phone)
        try:
            _handle_mf_bot(raw_phone, text, phone_number_id, inbound_msg_id=inbound_msg_id, raw_msg=msg)
        except Exception as e:
            logger.exception("Mutual Funds bot error: %s", e)
            
    elif go_wa:
        logger.info("[Webhook/JMS] → WHATSAPP BOT for %s", raw_phone)
        try:
            _handle_wa_bot(raw_phone, text, phone_number_id, inbound_msg_id=inbound_msg_id)
        except Exception as e:
            logger.exception("WhatsApp bot error: %s", e)
            
    elif go_navratri:
        logger.info("[Webhook/JMS] → NAVRATRI 2-WAY for %s", raw_phone)
        
        if msg_type == "image":
            logger.info("Received image for Navratri. Generating pass.")
            try:
                from CRM.models import NavratriRegistration
                from CRM.navratri_views import NavratriRegistrationAPIView
                phone_no_plus = raw_phone.replace("+", "")
                search_phones = [raw_phone, phone_no_plus]
                if len(phone_no_plus) > 10:
                    search_phones.append(phone_no_plus[-10:])
                
                # Get the most recent registration for this phone
                reg = NavratriRegistration.objects.filter(phone_number__in=search_phones).order_by('-id').first()
                if reg:
                    # Download the image from Meta
                    media_id = msg.get("image", {}).get("id")
                    if media_id:
                        try:
                            token = getattr(settings, "META_PERMANENT_TOKEN", os.getenv("META_ACCESS_TOKEN", ""))
                            # 1. Get media URL
                            media_url_req = requests.get(f"https://graph.facebook.com/v22.0/{media_id}", headers={"Authorization": f"Bearer {token}"})
                            if media_url_req.status_code == 200:
                                media_url = media_url_req.json().get("url")
                                # 2. Download binary data
                                img_resp = requests.get(media_url, headers={"Authorization": f"Bearer {token}"})
                                if img_resp.status_code == 200:
                                    # Save to media folder
                                    import os
                                    save_dir = os.path.join(settings.BASE_DIR, 'media', 'navratri_payments')
                                    os.makedirs(save_dir, exist_ok=True)
                                    file_path = os.path.join(save_dir, f"{reg.id}_{media_id}.jpg")
                                    with open(file_path, 'wb') as f:
                                        f.write(img_resp.content)
                                    logger.info(f"Downloaded Navratri payment screenshot for reg {reg.id}")
                        except Exception as dl_err:
                            logger.error(f"Failed to download Navratri image: {dl_err}")
                
                    import threading
                    view = NavratriRegistrationAPIView()
                    threading.Timer(1, view.send_navratri_pass_task, args=[reg.id]).start()
                else:
                    logger.error(f"Navratri registration not found for image from {raw_phone}")
            except Exception as e:
                logger.error(f"Navratri image processing error: {e}")
        else:
            # Check if the user is asking exactly for the approval status (without apostrophe to avoid encoding issues)
            expected_substring = "like to check the approval status of my pass. could you please let me know if it has been approved"
            
            if expected_substring in text_lower:
                reply_text = "Status in review. We will update you shortly!"
                
                # Send using correct Navratri Phone Number ID
                try:
                    from CRM.navratri_views import NAVRATRI_PHONE_NUMBER_ID
                    token = getattr(settings, "META_PERMANENT_TOKEN", os.getenv("META_ACCESS_TOKEN", ""))
                    url = f"https://graph.facebook.com/v22.0/{NAVRATRI_PHONE_NUMBER_ID}/messages"
                    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                    payload = {"messaging_product": "whatsapp", "to": raw_phone, "type": "text", "text": {"body": reply_text}}
                    requests.post(url, json=payload, headers=headers)
                except Exception as e:
                    logger.error(f"Navratri auto-reply send error: {e}")
                    
                _save_reply(inbound_msg_id, reply_text)
            else:
                # We just saved the message in CRM. We do not send any automated reply.
                # This allows human agents to handle it or a future Navratri bot to take over.
                pass
            
    elif go_shopify:
        logger.info("[Webhook/JMS] → SHOPIFY BOT for %s", raw_phone)
        try:
            _handle_shopify_bot(raw_phone, text, phone_number_id, inbound_msg_id=inbound_msg_id, raw_msg=msg)
        except Exception as e:
            logger.exception("Shopify bot error: %s", e)
            
    elif go_jms:
        logger.info("[Webhook/JMS] → JMS-TECH BOT for %s", raw_phone)
        try:
            _jms_handle_message(raw_phone, text, phone_number_id, inbound_msg_id=inbound_msg_id)
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
                        gkd_phone_id = GKD_PHONE_NUMBER_ID
                        amritcement_phone_id = AMRITCEMENT_PHONE_NUMBER_ID
                        jaivik_phone_id = "1232951769906831"
                        
                        if gigatel_phone_id and phone_number_id == gigatel_phone_id:
                            logger.info("[Webhook] Routing message to Gigatel Bot")
                            
                            # ── Forward webhook to Voicebot URL (ONLY FOR IMAGES) ─────────────────
                            if msg.get("type") == "image":
                                try:
                                    import threading
                                    import requests
                        
                                    def forward_payload(data, sig):
                                        try:
                                            headers = {"Content-Type": "application/json"}
                                            if sig:
                                                headers["X-Hub-Signature-256"] = sig
                                            # Forward to the URL required by the voicebot service
                                            res = requests.post("https://gigatel.online/webhook/whatsapp/", json=data, headers=headers, timeout=5)
                                            logger.info(f"[Webhook] Forwarded to voicebot. Status: {res.status_code}")
                                        except Exception as e:
                                            logger.error(f"[Webhook] Forwarding error: {e}")
                        
                                    sig = request.META.get("HTTP_X_HUB_SIGNATURE_256", "")
                                    threading.Thread(target=forward_payload, args=(payload, sig), daemon=True).start()
                                except Exception as e:
                                    logger.error(f"[Webhook] Failed to start forwarding thread: {e}")
                                
                            handle_gigatel_message(msg)
                        elif globestar_phone_id and phone_number_id == globestar_phone_id:
                            logger.info("[Webhook] Routing message to Globe Star Bot")
                            handle_globestar_message(msg)
                        elif gkd_phone_id and phone_number_id == gkd_phone_id:
                            logger.info("[Webhook] Routing message to GKD Bot")
                            handle_gkd_message(msg)
                        elif amritcement_phone_id and phone_number_id == amritcement_phone_id:
                            logger.info("[Webhook] Routing message to Amritcement Bot")
                            handle_amritcement_message(msg)
                        elif jaivik_phone_id and phone_number_id == jaivik_phone_id:
                            logger.info("[Webhook] Routing message to Jaivik (Avantika) Bot")
                            raw_phone = msg.get("from", "").strip()
                            if not raw_phone.startswith("+"):
                                raw_phone = f"+{raw_phone}"
                            text = _extract_text_for_routing(msg) or ""
                            _handle_avantika_bot(raw_phone, text, phone_number_id)
                        elif client:
                            # ── CLIENT BOT FLOW ───────────────────────────
                            # Route to client-specific bot
                            handle_client_message(
                                client,
                                msg,
                                contacts[0] if contacts else {},
                            )
                        else:
                            # ── JMS INTERNAL (4-bot router) ───────────────
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