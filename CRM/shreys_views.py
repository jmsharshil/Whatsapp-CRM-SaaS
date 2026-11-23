import json
import logging
import uuid
import requests
from django.conf import settings
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile

from CRM.models import Customer, Conversation, Message, ClientAccount
from .shreys_utils import (
    shreys_PHONE_NUMBER_ID,
    SHREYS_SECONDARY_PHONE_NUMBER_ID,
    SHREYS_PHONE_NUMBER_IDS,
    SHREYS_DEFAULT_CALLBACK_URL,
    send_shreys_template,
    forward_shreys_status_callback,
    _get_cached_shreys_client,
)

logger = logging.getLogger(__name__)


def download_media_from_whatsapp(media_id: str, access_token: str) -> str:
    try:
        url = f"https://graph.facebook.com/v22.0/{media_id}"
        headers = {"Authorization": f"Bearer {access_token}"}
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200:
            logger.error("[shreys] Failed to get media URL for %s: %s", media_id, res.text)
            return ""
        media_url = res.json().get("url")
        if not media_url:
            return ""

        file_res = requests.get(media_url, headers=headers, timeout=15)
        if file_res.status_code != 200:
            logger.error("[shreys] Failed to download media %s", media_id)
            return ""

        content_type = file_res.headers.get("Content-Type", "").split(";")[0].strip()
        ext_map = {
            "image/jpeg": "jpg",
            "image/jpg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
            "image/gif": "gif",
            "video/mp4": "mp4",
            "video/3gpp": "3gp",
            "video/quicktime": "mov",
            "audio/mp4": "m4a",
            "audio/aac": "aac",
            "audio/amr": "amr",
            "audio/ogg": "ogg",
            "audio/opus": "opus",
            "audio/mpeg": "mp3",
            "application/pdf": "pdf",
            "application/msword": "doc",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
            "application/vnd.ms-excel": "xls",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
            "application/vnd.ms-powerpoint": "ppt",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
            "text/plain": "txt",
        }

        ext = ext_map.get(content_type)
        if not ext:
            ext = content_type.split("/")[-1]
            if not ext or len(ext) > 6 or not ext.isalnum():
                ext = "bin"

        file_name = f"shreys_media/{uuid.uuid4().hex}.{ext}"
        saved_path = default_storage.save(file_name, ContentFile(file_res.content))
        return default_storage.url(saved_path)
    except Exception as e:
        logger.error("[shreys] Error downloading media: %s", e)
        return ""


def handle_shreys_status_update(
    status_payload: dict,
    raw_meta_payload: dict = None,
    sig_header: str = "",
    phone_number_id: str = "",
):
    """
    Forwards Meta delivery status ('sent', 'delivered', 'read', 'failed')
    for Shreys (1481244255068012 / 635086059677025) back to Shreyas Foundation's webhook.
    Uses cache deduplication on (wamid, status) so if Shreyas webhook forwards
    the event back to WHATSAPP_CRM_FORWARD_URL, it never loops.
    """
    if raw_meta_payload and raw_meta_payload.get("forwarded_from_saas_crm"):
        return {"skipped": True, "reason": "already_forwarded_from_saas_crm"}

    active_phone_id = (phone_number_id or shreys_PHONE_NUMBER_ID).strip()
    wa_msg_id = status_payload.get("id", "")
    new_status = status_payload.get("status", "")
    recipient_id = status_payload.get("recipient_id", "")
    timestamp_val = str(status_payload.get("timestamp", ""))

    if not wa_msg_id or not new_status:
        return {"skipped": True, "reason": "missing_id_or_status"}

    msg_row = (
        Message.objects.filter(meta_message_id=wa_msg_id)
        .order_by("-id")
        .values("template_name", "reply_of", "status")
        .first()
    )
    t_name = (msg_row.get("template_name") or "") if msg_row else ""
    cb_url = SHREYS_DEFAULT_CALLBACK_URL
    sub_id = None

    if msg_row and msg_row.get("reply_of"):
        try:
            meta_info = json.loads(msg_row["reply_of"])
            if isinstance(meta_info, dict):
                cb_url = meta_info.get("webhook_callback_url") or cb_url
                sub_id = meta_info.get("submission_id")
        except Exception:
            pass

    from django.core.cache import cache
    dedup_key = f"shreys_stat_fwd:{wa_msg_id}:{new_status}"
    already_forwarded = bool(cache.get(dedup_key))
    if not already_forwarded:
        cache.set(dedup_key, "1", timeout=600)
        forward_shreys_status_callback(
            message_id=wa_msg_id,
            recipient_id=recipient_id,
            status=new_status,
            timestamp=timestamp_val,
            template_name=t_name,
            callback_url=cb_url,
            submission_id=sub_id,
            raw_meta_payload=raw_meta_payload,
            sig_header=sig_header,
            phone_number_id=active_phone_id,
        )

    return {
        "event": "status_update",
        "phone_number_id": active_phone_id,
        "message_id": wa_msg_id,
        "recipient_id": recipient_id,
        "status": new_status,
        "timestamp": timestamp_val,
        "template_name": t_name,
        "submission_id": sub_id,
        "db_matched": bool(msg_row),
        "forwarded_to_shreyas": not already_forwarded,
        "callback_url": cb_url,
    }


def handle_shreys_message(
    msg: dict,
    contact_obj: dict = None,
    raw_meta_payload: dict = None,
    sig_header: str = "",
    phone_number_id: str = "",
):
    active_phone_id = str(
        phone_number_id
        or msg.get("phone_number_id")
        or shreys_PHONE_NUMBER_ID
    ).strip()
    if active_phone_id not in SHREYS_PHONE_NUMBER_IDS:
        active_phone_id = shreys_PHONE_NUMBER_ID

    # 1. If Shreyas Backend triggers or logs a template via webhook payload -> send/save template in CRM
    if msg.get("event") in ("TRIGGER_TEMPLATE", "LOG_TEMPLATE") or msg.get("template_name"):
        raw_phone = str(
            msg.get("phone")
            or msg.get("phone_number")
            or msg.get("to")
            or msg.get("from")
            or ""
        ).strip()
        template_name = str(msg.get("template_name") or "").strip()
        parameters = msg.get("parameters")
        raw_components = msg.get("components")
        language_code = str(msg.get("language") or msg.get("language_code") or "en").strip()
        submission_id = msg.get("submission_id")
        existing_msg_id = str(
            msg.get("message_id")
            or msg.get("meta_message_id")
            or msg.get("wamid")
            or ""
        ).strip()
        log_only = bool(
            msg.get("log_only")
            or msg.get("event") == "LOG_TEMPLATE"
            or existing_msg_id
        )
        webhook_callback_url = str(
            msg.get("webhook_callback_url") or SHREYS_DEFAULT_CALLBACK_URL
        ).strip()
        if raw_phone and template_name:
            ok, res_val = send_shreys_template(
                to=raw_phone,
                template_name=template_name,
                language_code=language_code,
                parameters=parameters,
                components=raw_components,
                submission_id=submission_id,
                webhook_callback_url=webhook_callback_url,
                existing_message_id=existing_msg_id,
                log_only=log_only,
                phone_number_id=active_phone_id,
            )
            return {
                "success": ok,
                "event": "LOG_TEMPLATE" if log_only else "TRIGGER_TEMPLATE",
                "phone_number_id": active_phone_id,
                "phone": raw_phone,
                "template_name": template_name,
                "submission_id": submission_id,
                "message_id": res_val if ok else "",
                "error": "" if ok else res_val,
            }
        return {"success": False, "error": "phone and template_name are required"}

    # 2. Normal inbound customer message -> normalize number, deduplicate, forward to Shreyas & save to CRM
    from CRM.META.webhook_views import _normalize_e164_phone

    if raw_meta_payload and raw_meta_payload.get("forwarded_from_saas_crm"):
        return

    raw_from = msg.get("from", "").strip()
    number = _normalize_e164_phone(raw_from)
    if not number:
        return

    msg_id = msg.get("id", "")
    if msg_id:
        from django.core.cache import cache
        in_dedup_key = f"shreys_in_wamid:{msg_id}"
        if cache.get(in_dedup_key):
            logger.info("[shreys] Duplicate inbound wamid=%s skipped via cache", msg_id)
            return
        cache.set(in_dedup_key, "1", timeout=600)

    # Forward inbound webhook to Shreyas Foundation URL (just like Insights)
    if raw_meta_payload and SHREYS_DEFAULT_CALLBACK_URL:
        try:
            import threading

            forward_data = dict(raw_meta_payload)
            forward_data["forwarded_from_saas_crm"] = True

            def _forward_inbound_to_shreys(data, sig):
                try:
                    headers = {
                        "Content-Type": "application/json",
                        "X-Forwarded-From-SaaS-CRM": "1",
                    }
                    if sig:
                        headers["X-Hub-Signature-256"] = sig
                    res = requests.post(SHREYS_DEFAULT_CALLBACK_URL, json=data, headers=headers, timeout=15)
                    logger.info("[shreys] Forwarded inbound message to Shreyas webhook. Status: %s", res.status_code)
                except Exception as e:
                    logger.error("[shreys] Forwarding inbound error to Shreyas webhook: %s", e)

            threading.Thread(
                target=_forward_inbound_to_shreys,
                args=(forward_data, sig_header),
                daemon=True,
            ).start()
        except Exception as e:
            logger.error("[shreys] Failed to start forwarding thread for Shreyas: %s", e)

    msg_type = msg.get("type", "text")

    display_body = ""
    if msg_type == "text":
        display_body = msg.get("text", {}).get("body", "").strip()
    elif msg_type == "interactive":
        idata = msg.get("interactive", {})
        itype = idata.get("type")
        if itype == "button_reply":
            display_body = idata["button_reply"].get("title") or idata["button_reply"].get("id", "")
        elif itype == "list_reply":
            display_body = idata["list_reply"].get("title") or idata["list_reply"].get("id", "")
    elif msg_type == "button":
        display_body = msg.get("button", {}).get("text") or msg.get("button", {}).get("payload", "").strip()
    elif msg_type in ["image", "video", "audio", "document", "sticker"]:
        media_id = msg.get(msg_type, {}).get("id")
        caption = msg.get(msg_type, {}).get("caption", "").strip()
        if media_id:
            client_account_obj = _get_cached_shreys_client(active_phone_id)
            token = (
                client_account_obj.access_token
                if client_account_obj and client_account_obj.access_token
                else getattr(settings, "META_PERMANENT_TOKEN", "")
            )
            dl_url = download_media_from_whatsapp(media_id, token)
            prefix = f"[{msg_type.upper()}]"
            display_body = f"{prefix} {dl_url}" if dl_url else prefix
        else:
            display_body = f"[{msg_type.upper()}]"
        if caption:
            display_body = f"{display_body}\n{caption}"
    elif msg_type == "location":
        loc = msg.get("location", {})
        lat, lng = loc.get("latitude"), loc.get("longitude")
        name = loc.get("name", "")
        display_body = f"📍 Location: {name} https://maps.google.com/?q={lat},{lng}".strip()
    elif msg_type == "contacts":
        c_list = msg.get("contacts", [])
        c_names = [c.get("name", {}).get("formatted_name", "Unknown") for c in c_list]
        display_body = f"👤 Contact: {', '.join(c_names)}"

    if not display_body:
        display_body = f"[{msg_type}]"

    contact_name = number
    if contact_obj and isinstance(contact_obj, dict):
        profile_name = contact_obj.get("profile", {}).get("name", "").strip()
        if profile_name:
            contact_name = profile_name

    customer_obj = Customer.objects.filter(phone__in=[number, number.lstrip("+")]).first()
    if not customer_obj:
        customer_obj, created = Customer.objects.get_or_create(phone=number, defaults={"name": contact_name})
    else:
        created = False

    if not created and contact_name != number and (customer_obj.name in (number, number.lstrip("+"), "Unknown") or not customer_obj.name):
        customer_obj.name = contact_name
        customer_obj.save(update_fields=["name"])

    client_account_obj = _get_cached_shreys_client(active_phone_id)
    conv_obj = Conversation.objects.filter(
        customer=customer_obj,
        phone_number_id=active_phone_id,
    ).order_by("-created_at").first()
    if not conv_obj:
        conv_obj = Conversation.objects.create(
            customer=customer_obj,
            phone_number_id=active_phone_id,
            client=client_account_obj,
        )
    elif client_account_obj and not conv_obj.client:
        conv_obj.client = client_account_obj
        conv_obj.save(update_fields=["client"])

    Message.objects.create(
        conversation=conv_obj,
        client=client_account_obj,
        customer=customer_obj,
        meta_message_id=msg_id,
        direction="inbound",
        message_type=msg_type if msg_type in ["text", "template", "image", "document", "video"] else "text",
        content=display_body,
        status="delivered",
    )
    return {
        "event": "inbound_message",
        "phone_number_id": active_phone_id,
        "message_id": msg_id,
        "from": number,
        "name": customer_obj.name,
        "type": msg_type,
        "content": display_body,
        "conversation_id": conv_obj.id,
        "forwarded_to_shreyas": bool(raw_meta_payload and SHREYS_DEFAULT_CALLBACK_URL),
    }
