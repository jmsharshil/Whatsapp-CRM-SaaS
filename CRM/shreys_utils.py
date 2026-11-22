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
    raw_meta_payload: dict = None,
    sig_header: str = "",
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
        "forwarded_from_saas_crm": True,
    }
    if submission_id is not None:
        payload["submission_id"] = submission_id

    # Include standard Meta WhatsApp Business Account structure so Shreyas webhook
    # works whether it expects flat fields OR standard Meta webhook format (like Insights)
    if raw_meta_payload and isinstance(raw_meta_payload, dict):
        for k, v in raw_meta_payload.items():
            if k not in payload:
                payload[k] = v
    else:
        payload["object"] = "whatsapp_business_account"
        payload["entry"] = [
            {
                "id": shreys_PHONE_NUMBER_ID,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": shreys_PHONE_NUMBER_ID},
                            "statuses": [
                                {
                                    "id": message_id or "",
                                    "status": status,
                                    "timestamp": ts,
                                    "recipient_id": clean_recipient,
                                }
                            ],
                        },
                    }
                ],
            }
        ]

    def _post_callback():
        try:
            headers = {
                "Content-Type": "application/json",
                "X-Forwarded-From-SaaS-CRM": "1",
            }
            if sig_header:
                headers["X-Hub-Signature-256"] = sig_header
            res = requests.post(target_url, json=payload, headers=headers, timeout=15)
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


# In-memory cache to avoid redundant DB & Meta API queries on every request
_SHREYS_TEMPLATE_CACHE = {}
_SHREYS_CLIENT_CACHE = {"obj": None, "ts": 0}


def _get_cached_shreys_client():
    from CRM.models import ClientAccount
    now = time.time()
    if _SHREYS_CLIENT_CACHE["obj"] is not None and (now - _SHREYS_CLIENT_CACHE["ts"]) < 300:
        return _SHREYS_CLIENT_CACHE["obj"]
    obj = ClientAccount.objects.filter(phone_number_id=shreys_PHONE_NUMBER_ID).first()
    _SHREYS_CLIENT_CACHE["obj"] = obj
    _SHREYS_CLIENT_CACHE["ts"] = now
    return obj


def _get_cached_shreys_template(template_name: str, client_account_obj, token: str):
    from CRM.models import Template
    t_key = (template_name or "").strip().lower()
    if not t_key:
        return None
    if t_key in _SHREYS_TEMPLATE_CACHE:
        return _SHREYS_TEMPLATE_CACHE[t_key]

    db_template = Template.objects.filter(name__iexact=t_key).first()
    if not db_template:
        db_template = _fetch_and_sync_meta_template(template_name.strip(), client_account_obj, token)

    # Cache even if None so we don't repeatedly hit Meta API for missing templates
    _SHREYS_TEMPLATE_CACHE[t_key] = db_template
    return db_template


def _build_shreys_components(template_name: str, parameters, raw_components: list = None, db_template=None) -> tuple:
    """
    Dynamically builds Meta template components so even if template names change
    or new templates are added in the future, it works without code changes:
      1. If raw `components` array is passed in payload -> uses it directly.
      2. If `Template` exists in CRM DB -> inspects its structure (HEADER / BODY / BUTTONS).
      3. Otherwise auto-detects from `parameters` keys & `template_name`:
         - Document/Image/Video Header (`document_url`, `image_url`, `video_url`, or `booklet`)
         - Dynamic URL Button (`button_url`, `button_param`, or single `submission_id` / `admission`)
         - Body text parameters (`{"1": "...", "2": "..."}` or list)
    Returns (components_list, display_summary_str)
    """
    if raw_components and isinstance(raw_components, list):
        return raw_components, ""

    t_clean = (template_name or "").strip()
    t_lower = t_clean.lower()
    params = parameters if isinstance(parameters, dict) else {}

    # Check if template definition exists in CRM Template table (if not already passed)
    if db_template is None:
        try:
            from CRM.models import Template
            db_template = _SHREYS_TEMPLATE_CACHE.get(t_lower) or Template.objects.filter(name__iexact=t_clean).first()
        except Exception:
            db_template = None

    components = []
    summary_parts = []

    # ── 1. HEADER (Document / Image / Video / Text) ──────────────────────
    has_doc_header = (
        "document_url" in params
        or "booklet" in t_lower
        or (db_template and (db_template.header_type or "").upper() == "DOCUMENT")
    )
    has_img_header = "image_url" in params or (db_template and (db_template.header_type or "").upper() == "IMAGE")
    has_vid_header = "video_url" in params or (db_template and (db_template.header_type or "").upper() == "VIDEO")

    if has_doc_header:
        is_gu = "gu" in t_lower
        default_pdf = (
            "https://shreyasfoundation.blob.core.windows.net/media/documents/health_certificates/04_-_Shreyas_Booklet_Gujarati_-_Final_28.10.2026.pdf"
            if is_gu
            else "https://shreyasfoundation.blob.core.windows.net/media/documents/caste_certificates/02_-_Shreyas_Booklet_English_-_Final_28.10.2026.pdf"
        )
        default_fname = "Shreyas_Booklet_Gujarati.pdf" if is_gu else "Shreyas_Booklet_English.pdf"
        pdf_url = str(params.get("document_url") or default_pdf).strip()
        file_name = str(params.get("filename") or default_fname).strip()
        components.append({
            "type": "header",
            "parameters": [{
                "type": "document",
                "document": {"link": pdf_url, "filename": file_name},
            }],
        })
        summary_parts.append(f"📄 {file_name}")
    elif has_img_header and params.get("image_url"):
        components.append({
            "type": "header",
            "parameters": [{"type": "image", "image": {"link": str(params["image_url"]).strip()}}],
        })
        summary_parts.append("🖼️ Image")
    elif has_vid_header and params.get("video_url"):
        components.append({
            "type": "header",
            "parameters": [{"type": "video", "video": {"link": str(params["video_url"]).strip()}}],
        })
        summary_parts.append("🎥 Video")
    elif params.get("header_text"):
        components.append({
            "type": "header",
            "parameters": [{"type": "text", "text": str(params["header_text"])}],
        })

    # ── 2. DETERMINE IF PARAMETERS BELONG TO BUTTON OR BODY ──────────────
    # Check if DB template has dynamic URL button and 0 body variables
    db_has_dynamic_btn_only = False
    if db_template and db_template.variables_count == 0 and isinstance(db_template.buttons, list):
        for b in db_template.buttons:
            if isinstance(b, dict) and "{{" in str(b.get("url", "")):
                db_has_dynamic_btn_only = True
                break

    is_button_only_template = (
        db_has_dynamic_btn_only
        or "button_param" in params
        or "button_url" in params
        or t_lower in ("shreyas_admission__", "shreyas_admission_link", "shreyas_admission_reminder_")
        or ("admission" in t_lower and len(params) == 1 and ("1" in params or "submission_id" in params))
    )

    if is_button_only_template:
        btn_val = str(
            params.get("button_param")
            or params.get("button_url")
            or params.get("1")
            or params.get("submission_id")
            or ""
        ).strip()
        if btn_val:
            components.append({
                "type": "button",
                "sub_type": "url",
                "index": "0",
                "parameters": [{"type": "text", "text": btn_val}],
            })
            summary_parts.append(f"ID: {btn_val}")
        return components, " | ".join(summary_parts)

    # ── 3. BODY PARAMETERS (Any numbered keys "1", "2", "3"... or list) ──
    reserved_keys = {"document_url", "filename", "image_url", "video_url", "header_text", "button_param", "button_url"}
    body_params = []
    param_vals = []

    if isinstance(parameters, dict) and parameters:
        body_keys = [k for k in parameters.keys() if k not in reserved_keys]
        for k in sorted(body_keys, key=lambda x: int(x) if str(x).isdigit() else str(x)):
            val = str(parameters[k])
            body_params.append({"type": "text", "text": val})
            param_vals.append(val)
    elif isinstance(parameters, list) and parameters:
        for v in parameters:
            val = str(v.get("text", v)) if isinstance(v, dict) else str(v)
            body_params.append({"type": "text", "text": val})
            param_vals.append(val)

    if body_params:
        components.append({
            "type": "body",
            "parameters": body_params,
        })
        summary_parts.append(", ".join(param_vals))

    # Optional explicit button parameter alongside body parameters
    if params.get("button_param") or params.get("button_url"):
        b_val = str(params.get("button_param") or params.get("button_url")).strip()
        components.append({
            "type": "button",
            "sub_type": "url",
            "index": "0",
            "parameters": [{"type": "text", "text": b_val}],
        })

    return components, " | ".join(summary_parts)


def _fetch_and_sync_meta_template(template_name: str, client_account_obj, token: str):
    """
    If the template is not yet in CRM's local `Template` table, fetches its exact
    structure (Header, Body text, Footer text, Buttons) from Meta WABA so the CRM
    chat window renders the full message body and buttons as-is.
    """
    from CRM.models import Template, Organization

    waba_id = getattr(client_account_obj, "waba_id", None) if client_account_obj else None
    if not waba_id or not token:
        return None

    try:
        url = f"https://graph.facebook.com/v22.0/{waba_id}/message_templates"
        res = requests.get(
            url,
            params={"name": template_name},
            headers={"Authorization": f"Bearer {token}"},
            timeout=6,
        )
        if res.status_code != 200:
            return None
        data = res.json().get("data", [])
        if not data:
            return None

        mt = data[0]
        meta_components = mt.get("components", [])
        body_text = ""
        header_type = ""
        header_text = ""
        footer_text = ""
        buttons = []

        for comp in meta_components:
            c_type = comp.get("type")
            if c_type == "BODY":
                body_text = comp.get("text", "")
            elif c_type == "HEADER":
                header_type = comp.get("format", "")
                if header_type == "TEXT":
                    header_text = comp.get("text", "")
            elif c_type == "FOOTER":
                footer_text = comp.get("text", "")
            elif c_type == "BUTTONS":
                buttons = comp.get("buttons", [])

        org = getattr(client_account_obj, "tech_provider", None) or Organization.objects.first()
        if not org:
            return None

        vars_count = len(re.findall(r"\{\{\d+\}\}", body_text))
        return Template.objects.create(
            organization=org,
            template_id=mt.get("id", ""),
            name=mt.get("name", template_name),
            status=mt.get("status", "APPROVED").upper(),
            category=mt.get("category", "UTILITY").upper(),
            language=mt.get("language", "en"),
            body_text=body_text,
            variables_count=vars_count,
            header_type=header_type,
            header_text=header_text,
            footer_text=footer_text,
            buttons=buttons,
        )
    except Exception as e:
        logger.warning("[shreys] Could not auto-fetch template '%s' from Meta: %s", template_name, e)
        return None


def _render_shreys_template_content(
    template_name: str,
    db_template,
    components: list,
    param_summary: str,
) -> str:
    """
    Renders the full Meta template preview (Header + Body with {{1}}, {{2}} replaced + Footer + Buttons)
    so it shows in the CRM chat window exactly as created in Meta.
    """
    lines = []

    # 1. Header (Document / Image / Video / Text)
    for comp in components or []:
        if comp.get("type") == "header":
            for p in comp.get("parameters", []):
                p_type = p.get("type", "")
                if p_type == "document":
                    doc = p.get("document", {})
                    fname = doc.get("filename", "Document.pdf")
                    link = doc.get("link", "")
                    lines.append(f"[DOCUMENT] {link}\n📄 *{fname}*" if link else f"📄 *{fname}*")
                elif p_type == "image":
                    link = p.get("image", {}).get("link", "")
                    lines.append(f"[IMAGE] {link}" if link else "[IMAGE]")
                elif p_type == "video":
                    link = p.get("video", {}).get("link", "")
                    lines.append(f"[VIDEO] {link}" if link else "[VIDEO]")
                elif p_type == "text":
                    lines.append(f"*{p.get('text', '')}*")
    if not lines and db_template and db_template.header_text:
        lines.append(f"*{db_template.header_text.strip()}*")

    if not db_template:
        lines.append(f"[Template: {template_name}]" + (f"\n{param_summary}" if param_summary else ""))
        return "\n\n".join(lines).strip()

    # 2. Body Text with {{1}}, {{2}}, ... replaced
    body_text = (db_template.body_text or "").strip()
    if body_text:
        for comp in components or []:
            if comp.get("type") == "body":
                for idx, p in enumerate(comp.get("parameters", []), start=1):
                    val = p.get("text", "")
                    body_text = body_text.replace(f"{{{{{idx}}}}}", str(val))
        lines.append(body_text)

    # 3. Footer Text
    if db_template.footer_text:
        lines.append(f"_{db_template.footer_text.strip()}_")

    # 4. Buttons (with dynamic URL parameter substituted if present)
    btn_param_map = {}
    for comp in components or []:
        if comp.get("type") == "button":
            b_idx = str(comp.get("index", "0"))
            b_params = comp.get("parameters", [])
            if b_params:
                btn_param_map[b_idx] = b_params[0].get("text") or b_params[0].get("payload") or ""

    if isinstance(db_template.buttons, list) and db_template.buttons:
        btn_lines = []
        for idx, btn in enumerate(db_template.buttons):
            if not isinstance(btn, dict):
                continue
            b_text = (btn.get("text") or "Button").strip()
            b_url = (btn.get("url") or "").strip()
            dyn_val = btn_param_map.get(str(idx), "")
            if b_url:
                if dyn_val:
                    b_url = re.sub(r"\{\{\d+\}\}", str(dyn_val), b_url)
                btn_lines.append(f"🔗 [{b_text}] ({b_url})")
            else:
                btn_lines.append(f"🔘 [{b_text}]")
        if btn_lines:
            lines.append("\n".join(btn_lines))

    return "\n\n".join(lines).strip() or f"[Template: {template_name}]"


def send_shreys_template(
    to: str,
    template_name: str,
    language_code: str = "en",
    parameters=None,
    components: list = None,
    submission_id=None,
    webhook_callback_url: str = None,
    existing_message_id: str = None,
    log_only: bool = False,
) -> tuple:
    """
    Sends WhatsApp template via Shreys Phone Number ID (1481244255068012)
    (or logs an already-sent template if log_only=True / existing_message_id is provided)
    and saves outbound message in CRM.
    """
    from CRM.models import Customer, Conversation, Message
    from CRM.META.webhook_views import _normalize_e164_phone

    phone_id = shreys_PHONE_NUMBER_ID
    client_account_obj = _get_cached_shreys_client()
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

    # If submission_id was passed at top level and parameters is empty for admission template
    if not parameters and submission_id is not None:
        parameters = {"1": str(submission_id)}

    db_template = _get_cached_shreys_template(clean_template_name, client_account_obj, token)

    components, param_summary = _build_shreys_components(
        clean_template_name, parameters, raw_components=components, db_template=db_template
    )
    if components:
        template_payload["components"] = components

    content_str = _render_shreys_template_content(
        template_name=clean_template_name,
        db_template=db_template,
        components=components,
        param_summary=param_summary,
    )

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

    try:
        customer_obj = Customer.objects.filter(phone__in=[normalized_phone, to_clean]).first()
        if not customer_obj:
            customer_obj, _ = Customer.objects.get_or_create(
                phone=normalized_phone,
                defaults={"name": normalized_phone},
            )

        conv_obj = Conversation.objects.filter(
            customer=customer_obj,
            phone_number_id=phone_id,
        ).order_by("-created_at").first()
        if not conv_obj:
            conv_obj = Conversation.objects.create(
                customer=customer_obj,
                phone_number_id=phone_id,
                client=client_account_obj,
            )
        elif client_account_obj and not conv_obj.client:
            conv_obj.client = client_account_obj
            conv_obj.save(update_fields=["client"])

        # If Shreyas Backend already sent the template directly via Meta and is logging it to CRM:
        if log_only or existing_message_id:
            meta_id = str(existing_message_id or "").strip()
            if meta_id:
                from django.core.cache import cache
                log_dedup_key = f"shreys_log_wamid:{meta_id}"
                if cache.get(log_dedup_key):
                    logger.info("[shreys] Duplicate log skipped via cache for wamid=%s", meta_id)
                    return True, meta_id
                cache.set(log_dedup_key, "1", timeout=600)

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
            logger.info("[shreys] Logged outbound template '%s' to %s in CRM (wamid=%s)", clean_template_name, normalized_phone, meta_id)
            return True, meta_id

        r = requests.post(url, json=payload, headers=headers, timeout=15)
        resp_json = r.json() if r.text else {}

        if r.status_code not in (200, 201):
            err_msg = (
                resp_json.get("error", {}).get("message")
                if isinstance(resp_json, dict)
                else None
            ) or r.text
            logger.error("[shreys] Template send error (%s): %s - %s", clean_template_name, r.status_code, err_msg)

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
