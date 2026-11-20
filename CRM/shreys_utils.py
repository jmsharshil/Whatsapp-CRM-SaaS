import json
import logging
import re
import threading
import time
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

shreys_PHONE_NUMBER_ID = "1481244255068012"
SHREYS_DEFAULT_CALLBACK_URL = "https://shreyashfoundation-e6eagcdeebbbhwgd.centralindia-01.azurewebsites.net/api/whatsapp/webhook/"
META_SEND_URL = "https://graph.facebook.com/v22.0/{phone_id}/messages"


def forward_shreys_status_callback(
    message_id: str,
    recipient_id: str,
    status: str,
    timestamp: str = "",
    template_name: str = "",
    callback_url: str = "",
    submission_id=None,
):
    """
    POSTs delivery status ('sent', 'delivered', 'read', 'failed') back to Shreyas webhook URL.
    """
    target_url = (callback_url or SHREYS_DEFAULT_CALLBACK_URL).strip()
    if not target_url:
        return

    clean_recipient = re.sub(r"[^\d]", "", str(recipient_id or ""))
    ts = str(timestamp or int(time.time()))

    payload = {
        "message_id": message_id or "",
        "recipient_id": clean_recipient,
        "status": status,
        "timestamp": ts,
        "template_name": template_name or "",
    }
    if submission_id is not None:
        payload["submission_id"] = submission_id

    def _post_callback():
        try:
            res = requests.post(target_url, json=payload, timeout=8)
            logger.info(
                "[shreys] Status callback (%s) sent to %s for wamid=%s -> HTTP %s",
                status,
                target_url,
                message_id,
                res.status_code,
            )
        except Exception as e:
            logger.error("[shreys] Failed to send status callback to %s: %s", target_url, e)

    threading.Thread(target=_post_callback, daemon=True).start()


def send_shreys_template(
    to: str,
    template_name: str,
    language_code: str = "en",
    parameters=None,
    submission_id=None,
    webhook_callback_url: str = None,
) -> tuple:
    """
    Sends WhatsApp template via Shreys Phone Number ID (1481244255068012)
    and saves outbound message in CRM.
    """
    from CRM.models import ClientAccount, Customer, Conversation, Message
    from CRM.META.webhook_views import _normalize_e164_phone

    phone_id = shreys_PHONE_NUMBER_ID
    client_account_obj = ClientAccount.objects.filter(phone_number_id=phone_id).first()
    token = (
        client_account_obj.access_token
        if (client_account_obj and client_account_obj.access_token)
        else getattr(settings, "META_PERMANENT_TOKEN", "")
    )

    normalized_phone = _normalize_e164_phone(to)
    to_clean = normalized_phone.lstrip("+")
    if not to_clean or len(to_clean) < 10:
        return False, f"Invalid phone number: '{to}'"

    clean_template_name = template_name.strip()
    template_payload = {
        "name": clean_template_name,
        "language": {"code": language_code or "en"},
    }

    # Build body parameters from {"1": "...", "2": "..."} or list
    body_params = []
    param_vals = []
    if parameters:
        if isinstance(parameters, dict):
            for k in sorted(parameters.keys(), key=lambda x: int(x) if str(x).isdigit() else str(x)):
                val = str(parameters[k])
                body_params.append({"type": "text", "text": val})
                param_vals.append(val)
        elif isinstance(parameters, list):
            for v in parameters:
                val = str(v.get("text", v)) if isinstance(v, dict) else str(v)
                body_params.append({"type": "text", "text": val})
                param_vals.append(val)

    if body_params:
        template_payload["components"] = [
            {
                "type": "body",
                "parameters": body_params,
            }
        ]

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_clean,
        "type": "template",
        "template": template_payload,
    }

    url = META_SEND_URL.format(phone_id=phone_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    callback_url = (webhook_callback_url or SHREYS_DEFAULT_CALLBACK_URL).strip()
    content_str = f"[Template: {clean_template_name}]" + (f" | {', '.join(param_vals)}" if param_vals else "")

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        resp_json = r.json() if r.text else {}

        customer_obj = Customer.objects.filter(phone__in=[normalized_phone, to_clean]).first()
        if not customer_obj:
            customer_obj, _ = Customer.objects.get_or_create(
                phone=normalized_phone,
                defaults={"name": normalized_phone},
            )

        conv_obj, _ = Conversation.objects.get_or_create(
            customer=customer_obj,
            phone_number_id=phone_id,
            defaults={"client": client_account_obj},
        )

        if r.status_code not in (200, 201):
            err_msg = (
                resp_json.get("error", {}).get("message")
                if isinstance(resp_json, dict)
                else None
            ) or r.text
            logger.error("[shreys] Template send error: %s - %s", r.status_code, err_msg)

            Message.objects.create(
                conversation=conv_obj,
                client=client_account_obj,
                customer=customer_obj,
                meta_message_id="",
                direction="outbound",
                message_type="template",
                template_name=clean_template_name,
                content=content_str,
                status="failed",
                error_message=err_msg,
            )
            forward_shreys_status_callback(
                message_id="",
                recipient_id=to_clean,
                status="failed",
                timestamp=str(int(time.time())),
                template_name=clean_template_name,
                callback_url=callback_url,
                submission_id=submission_id,
            )
            return False, err_msg

        meta_id = ""
        if "messages" in resp_json and len(resp_json["messages"]) > 0:
            meta_id = resp_json["messages"][0].get("id", "")

        Message.objects.create(
            conversation=conv_obj,
            client=client_account_obj,
            customer=customer_obj,
            meta_message_id=meta_id,
            direction="outbound",
            message_type="template",
            template_name=clean_template_name,
            content=content_str,
            status="sent",
            reply_of=json.dumps({
                "webhook_callback_url": callback_url,
                "submission_id": submission_id,
            }),
        )

        return True, meta_id
    except Exception as exc:
        logger.error("[shreys] Template send exception: %s", exc)
        return False, str(exc)
