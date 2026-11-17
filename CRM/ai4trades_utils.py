import json
import logging
import os
import requests
import io
import re
from django.conf import settings
import anthropic

try:
    import PyPDF2
except ImportError:
    PyPDF2 = None

try:
    import docx
except ImportError:
    docx = None

try:
    import openpyxl
except ImportError:
    openpyxl = None

logger = logging.getLogger(__name__)


AI4TRADES_PHONE_NUMBER_ID = "1357577430775484"

# Knowledgebase Document Link (Set your Blob storage file URL directly here)
AI4TRADES_KB_DOC_URL = ""

META_SEND_URL = "https://graph.facebook.com/v22.0/{phone_id}/messages"



# Global in-memory cache for extracted document text
_CACHED_DOC_TEXT = ""
_CACHED_DOC_URL = ""

# ─────────────────────────────────────────────────────────────
# Anthropic Claude Client Helper
# ─────────────────────────────────────────────────────────────
_claude_client = None

def get_claude_client():
    global _claude_client
    if _claude_client is None:
        api_key = (
            os.getenv("ANTHROPIC_API_KEY")
            or os.getenv("CLAUDE_API_KEY")
            or os.getenv("AI4TRADES_CLAUDE_KEY")
            or getattr(settings, "ANTHROPIC_API_KEY", "")
            or getattr(settings, "CLAUDE_API_KEY", "")
            or getattr(settings, "AI4TRADES_CLAUDE_KEY", "")
        )
        if api_key:
            try:
                _claude_client = anthropic.Anthropic(api_key=api_key)
            except Exception as e:
                logger.error("[ai4trades] Failed to initialize Anthropic client: %s", e)
        else:
            logger.warning("[ai4trades] ANTHROPIC_API_KEY not set in environment or settings.")
    return _claude_client




# ─────────────────────────────────────────────────────────────
# Meta WhatsApp API Dispatcher
# ─────────────────────────────────────────────────────────────
def _meta_post_ai4trades(payload: dict, customer_obj=None, conv_obj=None, client_account_obj=None) -> bool:
    ai4trades_phone_id = getattr(settings, 'AI4TRADES_PHONE_NUMBER_ID', AI4TRADES_PHONE_NUMBER_ID)
    
    from CRM.models import ClientAccount, Customer, Conversation, Message
    if not client_account_obj:
        client_account_obj = ClientAccount.objects.filter(phone_number_id=ai4trades_phone_id).first()
    token = client_account_obj.access_token if (client_account_obj and client_account_obj.access_token) else getattr(settings, "META_PERMANENT_TOKEN", "")
    
    url = META_SEND_URL.format(phone_id=ai4trades_phone_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=12)
        if r.status_code not in (200, 201):
            logger.error("[ai4trades] Meta API error: %s - %s", r.status_code, r.text)
            return False

        logger.info("[ai4trades] Meta API success: %s", r.text)

        # Save outbound message to DB under the EXACT same conversation and customer
        try:
            resp_data = r.json()
            meta_id = ""
            if "messages" in resp_data and len(resp_data["messages"]) > 0:
                meta_id = resp_data["messages"][0].get("id", "")

            to_number = payload.get("to", "")
            msg_type = payload.get("type", "text")
            content = ""
            template_name = ""

            if msg_type == "text":
                content = payload.get("text", {}).get("body", "")
            elif msg_type == "template":
                template_name = payload.get("template", {}).get("name", "")
                content = f"[Template: {template_name}]"
            elif msg_type == "interactive":
                content = "[Interactive Message]"
            elif msg_type == "image":
                content = "[Image]"
            else:
                content = f"[{msg_type.capitalize()}]"

            if not customer_obj:
                # Find matching customer regardless of + prefix
                clean_phone = to_number.lstrip('+')
                customer_obj = Customer.objects.filter(phone__in=[clean_phone, f"+{clean_phone}"]).first()
                if not customer_obj:
                    customer_obj, _ = Customer.objects.get_or_create(phone=f"+{clean_phone}", defaults={'name': to_number})

            if not conv_obj:
                conv_obj = Conversation.objects.filter(
                    customer=customer_obj,
                    phone_number_id=ai4trades_phone_id
                ).first()
                if not conv_obj:
                    conv_obj, _ = Conversation.objects.get_or_create(
                        customer=customer_obj,
                        phone_number_id=ai4trades_phone_id,
                        defaults={'client': client_account_obj}
                    )

            if client_account_obj and not conv_obj.client:
                conv_obj.client = client_account_obj
                conv_obj.save(update_fields=['client'])

            db_msg_type = msg_type if msg_type in ['text', 'template', 'image', 'document', 'video'] else 'text'

            Message.objects.create(
                conversation=conv_obj,
                client=client_account_obj,
                customer=customer_obj,
                meta_message_id=meta_id,
                direction="outbound",
                message_type=db_msg_type,
                template_name=template_name,
                content=content,
                status='sent'
            )
        except Exception as e:
            logger.error("[ai4trades] Error saving outbound message: %s", e)

        return True
    except Exception as exc:
        logger.error("[ai4trades] Meta API exception: %s", exc)
        return False


def send_ai4trades_text(to: str, text: str, customer_obj=None, conv_obj=None, client_account_obj=None) -> bool:
    """Send normal text message."""
    to_clean = to.lstrip('+')
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_clean,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": text
        }
    }
    return _meta_post_ai4trades(payload, customer_obj=customer_obj, conv_obj=conv_obj, client_account_obj=client_account_obj)


def send_ai4trades_welcome_template(to: str, customer_obj=None, conv_obj=None, client_account_obj=None) -> bool:
    """Send Meta WhatsApp approved 'welcome' template."""
    return _meta_post_ai4trades({
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to.lstrip('+'),
        "type": "template",
        "template": {
            "name": "welcome",
            "language": {"code": "en"}
        }
    }, customer_obj=customer_obj, conv_obj=conv_obj, client_account_obj=client_account_obj)


def _upload_media_to_meta_ai4trades(file_obj, phone_number_id: str, access_token: str) -> str:
    """
    Uploads a media file directly to Meta's /media endpoint and returns the media_id.
    """
    import mimetypes
    url = f"https://graph.facebook.com/v22.0/{phone_number_id}/media"
    headers = {"Authorization": f"Bearer {access_token}"}
    content_type = getattr(file_obj, "content_type", None)
    if not content_type:
        content_type = mimetypes.guess_type(getattr(file_obj, "name", ""))[0] or "application/octet-stream"

    file_obj.seek(0)
    files = {"file": (getattr(file_obj, "name", "media.bin"), file_obj.read(), content_type)}
    data = {"messaging_product": "whatsapp"}

    try:
        resp = requests.post(url, headers=headers, data=data, files=files, timeout=60)
        resp_data = resp.json() if resp.text else {}
        if resp.status_code in (200, 201) and "id" in resp_data:
            return resp_data["id"]
        logger.error("[ai4trades] Meta media upload failed: %s", resp_data)
    except Exception as e:
        logger.error("[ai4trades] Meta media upload exception: %s", e)
    return ""


def build_ai4trades_template_components(
    components: list = None,
    header_type: str = None,
    header_text: str = None,
    header_variables: list = None,
    header_media_url: str = None,
    header_media_id: str = None,
    document_filename: str = None,
    variables=None,
    button_variables=None,
    otp_code: str = None,
) -> list:
    """
    Builds Meta WhatsApp Cloud API `components` array supporting:
      - Header: TEXT (with variables), IMAGE, VIDEO, DOCUMENT (via URL, media_id, or uploaded file)
      - Body: Static or dynamic variables ({{1}}, {{2}}, ...) as list or dict
      - Footer: Handled automatically by Meta template definition (no runtime params required by Meta)
      - Buttons: Dynamic URL buttons, Quick Reply payloads, or OTP / Copy Code buttons
      - Raw `components`: If caller passes full Meta components list directly, uses it as-is.
    """
    if components and isinstance(components, list):
        return components

    built_components = []

    # 1. HEADER COMPONENT (TEXT, IMAGE, VIDEO, DOCUMENT)
    h_type = (header_type or "").strip().upper()
    if not h_type:
        if header_media_url or header_media_id:
            url_lower = (header_media_url or "").lower().split("?")[0]
            if url_lower.endswith((".mp4", ".3gp", ".mov")):
                h_type = "VIDEO"
            elif url_lower.endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".csv")):
                h_type = "DOCUMENT"
            else:
                h_type = "IMAGE"
        elif header_text or header_variables:
            h_type = "TEXT"

    if h_type == "TEXT":
        h_params = []
        if header_variables:
            if isinstance(header_variables, list):
                for hv in header_variables:
                    h_params.append({"type": "text", "text": str(hv)})
            elif isinstance(header_variables, dict):
                for k in sorted(header_variables.keys(), key=lambda x: int(x) if str(x).isdigit() else str(x)):
                    h_params.append({"type": "text", "text": str(header_variables[k])})
            else:
                h_params.append({"type": "text", "text": str(header_variables)})
        elif header_text:
            h_params.append({"type": "text", "text": str(header_text)})

        if h_params:
            built_components.append({
                "type": "header",
                "parameters": h_params
            })

    elif h_type in ("IMAGE", "VIDEO", "DOCUMENT"):
        media_key = h_type.lower()
        media_obj = {}
        if header_media_id:
            media_obj["id"] = str(header_media_id).strip()
        elif header_media_url:
            media_obj["link"] = str(header_media_url).strip()

        if media_obj:
            if h_type == "DOCUMENT" and document_filename:
                media_obj["filename"] = str(document_filename).strip()
            built_components.append({
                "type": "header",
                "parameters": [
                    {
                        "type": media_key,
                        media_key: media_obj
                    }
                ]
            })

    # 2. BODY COMPONENT (variables / body_variables / otp_code)
    body_params = []
    if variables:
        if isinstance(variables, list):
            for v in variables:
                body_params.append({"type": "text", "text": str(v)})
        elif isinstance(variables, dict):
            for k in sorted(variables.keys(), key=lambda x: int(x) if str(x).isdigit() else str(x)):
                body_params.append({"type": "text", "text": str(variables[k])})
        else:
            body_params.append({"type": "text", "text": str(variables)})
    elif otp_code:
        body_params.append({"type": "text", "text": str(otp_code)})

    if body_params:
        built_components.append({
            "type": "body",
            "parameters": body_params
        })

    # 3. BUTTONS COMPONENT (Dynamic URL, Quick Reply, or OTP Copy Code)
    if otp_code and not button_variables:
        built_components.append({
            "type": "button",
            "sub_type": "url",
            "index": "0",
            "parameters": [{"type": "text", "text": str(otp_code)}]
        })
    elif button_variables:
        if isinstance(button_variables, list):
            for idx, b_val in enumerate(button_variables):
                if isinstance(b_val, dict):
                    sub_type = b_val.get("sub_type", "url").lower()
                    b_idx = str(b_val.get("index", idx))
                    param_val = b_val.get("text") or b_val.get("payload") or b_val.get("value") or ""
                    param_type = "payload" if sub_type == "quick_reply" else "text"
                    built_components.append({
                        "type": "button",
                        "sub_type": sub_type,
                        "index": b_idx,
                        "parameters": [{"type": param_type, param_type: str(param_val)}]
                    })
                else:
                    built_components.append({
                        "type": "button",
                        "sub_type": "url",
                        "index": str(idx),
                        "parameters": [{"type": "text", "text": str(b_val)}]
                    })
        elif isinstance(button_variables, dict):
            for idx_key, b_val in button_variables.items():
                built_components.append({
                    "type": "button",
                    "sub_type": "url",
                    "index": str(idx_key),
                    "parameters": [{"type": "text", "text": str(b_val)}]
                })

    return built_components


def send_ai4trades_template(
    to: str,
    template_name: str,
    language_code: str = "en",
    components: list = None,
    variables=None,
    header_type: str = None,
    header_text: str = None,
    header_variables=None,
    header_media_url: str = None,
    header_media_id: str = None,
    document_filename: str = None,
    button_variables=None,
    otp_code: str = None,
    customer_obj=None,
    conv_obj=None,
    client_account_obj=None
) -> tuple:
    """
    Send any Meta WhatsApp approved template to a phone number.
    Supports:
      - Plain templates (Body + Footer only)
      - Body with variables ({{1}}, {{2}}, ...)
      - Header with TEXT (and header variables)
      - Header with IMAGE, VIDEO, or DOCUMENT (via URL, media_id, or uploaded file)
      - Dynamic URL / Quick Reply / OTP buttons
    Returns (success: bool, response_data_or_error: dict/str)
    """
    ai4trades_phone_id = getattr(settings, 'AI4TRADES_PHONE_NUMBER_ID', AI4TRADES_PHONE_NUMBER_ID)
    from CRM.models import ClientAccount, Customer, Conversation, Message

    if not client_account_obj:
        client_account_obj = ClientAccount.objects.filter(phone_number_id=ai4trades_phone_id).first()
    token = client_account_obj.access_token if (client_account_obj and client_account_obj.access_token) else getattr(settings, "META_PERMANENT_TOKEN", "")

    to_clean = re.sub(r"[^\d]", "", str(to))
    if len(to_clean) == 10:
        to_clean = f"91{to_clean}"
    elif len(to_clean) == 11 and to_clean.startswith("0"):
        to_clean = f"91{to_clean[1:]}"

    if not to_clean or len(to_clean) < 10:
        return False, f"Invalid phone number: '{to}'"

    template_payload = {
        "name": template_name.strip(),
        "language": {"code": language_code or "en"},
    }

    final_components = build_ai4trades_template_components(
        components=components,
        header_type=header_type,
        header_text=header_text,
        header_variables=header_variables,
        header_media_url=header_media_url,
        header_media_id=header_media_id,
        document_filename=document_filename,
        variables=variables,
        button_variables=button_variables,
        otp_code=otp_code,
    )

    if final_components:
        template_payload["components"] = final_components

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_clean,
        "type": "template",
        "template": template_payload
    }

    url = META_SEND_URL.format(phone_id=ai4trades_phone_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        try:
            resp_json = r.json() if r.text else {}
        except ValueError:
            resp_json = {}
        if r.status_code not in (200, 201):
            logger.error("[ai4trades] Template send error (%s): %s - %s", template_name, r.status_code, r.text)
            err_msg = resp_json.get("error", {}).get("message") if isinstance(resp_json, dict) else None
            return False, err_msg or r.text or f"HTTP {r.status_code}"

        meta_id = ""
        if "messages" in resp_json and len(resp_json["messages"]) > 0:
            meta_id = resp_json["messages"][0].get("id", "")

        try:
            if not customer_obj:
                customer_obj = Customer.objects.filter(phone__in=[to_clean, f"+{to_clean}"]).first()
                if not customer_obj:
                    customer_obj, _ = Customer.objects.get_or_create(phone=f"+{to_clean}", defaults={'name': f"+{to_clean}"})

            if not conv_obj:
                conv_obj = Conversation.objects.filter(
                    customer=customer_obj,
                    phone_number_id=ai4trades_phone_id
                ).first()
                if not conv_obj:
                    conv_obj, _ = Conversation.objects.get_or_create(
                        customer=customer_obj,
                        phone_number_id=ai4trades_phone_id,
                        defaults={'client': client_account_obj}
                    )

            Message.objects.create(
                conversation=conv_obj,
                client=client_account_obj,
                customer=customer_obj,
                meta_message_id=meta_id,
                direction="outbound",
                message_type="template",
                template_name=template_name,
                content=f"[Template: {template_name}]",
                status="sent"
            )
        except Exception as db_err:
            logger.error("[ai4trades] Error saving outbound template message to DB: %s", db_err)

        return True, {"meta_message_id": meta_id, "to": to_clean, "template_name": template_name}
    except Exception as exc:
        logger.error("[ai4trades] Template send exception: %s", exc)
        return False, str(exc)


def send_ai4trades_otp_template(to: str, otp_code: str) -> bool:
    """
    Send Meta WhatsApp approved authentication template 'otp' with Copy Code button.
    Template: 'otp', category: AUTHENTICATION, language: 'en'
    """
    return _meta_post_ai4trades({
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to.lstrip('+'),
        "type": "template",
        "template": {
            "name": "otp",
            "language": {"code": "en"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {
                            "type": "text",
                            "text": str(otp_code)
                        }
                    ]
                },
                {
                    "type": "button",
                    "sub_type": "url",
                    "index": "0",
                    "parameters": [
                        {
                            "type": "text",
                            "text": str(otp_code)
                        }
                    ]
                }
            ]
        }
    })



# ─────────────────────────────────────────────────────────────
# Knowledgebase Document Fetcher & Parser
# ─────────────────────────────────────────────────────────────
def extract_text_from_file_bytes(file_bytes: bytes, filename: str) -> str:
    """Extracts text from txt, pdf, docx, csv, markdown, and xlsx/xls."""
    name_lower = filename.lower()
    text = ""
    try:
        if name_lower.endswith(".pdf") and PyPDF2:
            reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
        elif name_lower.endswith(".docx") and docx:
            doc = docx.Document(io.BytesIO(file_bytes))
            text = "\n".join([p.text for p in doc.paragraphs if p.text])
        elif (name_lower.endswith(".xlsx") or name_lower.endswith(".xls")) and openpyxl:
            wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
            sheet_texts = []
            for sheetname in wb.sheetnames:
                ws = wb[sheetname]
                sheet_lines = [f"--- Sheet: {sheetname} ---"]
                for row in ws.iter_rows(values_only=True):
                    # Filter out empty rows
                    row_vals = [str(cell).strip() for cell in row if cell is not None and str(cell).strip()]
                    if row_vals:
                        sheet_lines.append(" | ".join(row_vals))
                if len(sheet_lines) > 1:
                    sheet_texts.append("\n".join(sheet_lines))
            text = "\n\n".join(sheet_texts)
        else:
            # Fallback for txt, csv, md, json, html, or plain text
            text = file_bytes.decode("utf-8", errors="replace")
    except Exception as e:
        logger.error("[ai4trades] Document text extraction error: %s", e)
    return text.strip()


def update_ai4trades_knowledge_content(new_text: str):
    """Manually update or invalidate the in-memory cached KB text and rebuild RAG index."""
    global _CACHED_DOC_TEXT
    _CACHED_DOC_TEXT = new_text.strip()
    from .ai4trades_rag import build_or_reload_rag
    build_or_reload_rag(_CACHED_DOC_TEXT)
    logger.info("[ai4trades] Cache & RAG index updated with new KB text (%d characters)", len(_CACHED_DOC_TEXT))


def get_ai4trades_knowledge_content() -> str:
    """
    Fetches the Knowledgebase document:
    1. Returns in-memory cached text if available.
    2. Checks the latest Document in the database.
    3. Fetches from Blob URL if configured.
    """
    global _CACHED_DOC_TEXT, _CACHED_DOC_URL
    doc_url = AI4TRADES_KB_DOC_URL.strip()
    
    # Check cache if exists
    if _CACHED_DOC_TEXT:
        return _CACHED_DOC_TEXT

    # 1. Check Document model for uploaded Ai4Trades document or any latest doc
    try:
        from CRM.models import Document
        # Prioritize ai4trades document if tagged or latest document
        kb_doc = Document.objects.filter(name__icontains="ai4trades").order_by("-uploaded_at").first()
        if not kb_doc:
            kb_doc = Document.objects.order_by("-uploaded_at").first()
        if kb_doc and kb_doc.extracted_text:
            _CACHED_DOC_TEXT = kb_doc.extracted_text
            from .ai4trades_rag import build_or_reload_rag
            build_or_reload_rag(_CACHED_DOC_TEXT)
            logger.info("[ai4trades] KB loaded from Document model '%s' (%d chars) into RAG", kb_doc.name, len(_CACHED_DOC_TEXT))
            return _CACHED_DOC_TEXT
    except Exception as e:
        logger.warning("[ai4trades] Could not fetch KB from Document model: %s", e)

    # 2. Fetch from Blob / remote URL if provided
    if doc_url:
        try:
            logger.info("[ai4trades] Fetching knowledgebase document from URL: %s", doc_url)
            res = requests.get(doc_url, timeout=20)
            if res.status_code == 200:
                parsed_text = extract_text_from_file_bytes(res.content, doc_url.split("?")[0])
                if parsed_text:
                    _CACHED_DOC_TEXT = parsed_text
                    _CACHED_DOC_URL = doc_url
                    from .ai4trades_rag import build_or_reload_rag
                    build_or_reload_rag(_CACHED_DOC_TEXT)
                    logger.info("[ai4trades] KB document fetched from URL and loaded into RAG (%d characters)", len(parsed_text))
                    return _CACHED_DOC_TEXT
                else:
                    logger.warning("[ai4trades] Document fetched but text was empty")
            else:
                logger.error("[ai4trades] Failed to fetch doc URL: %s - status %d", doc_url, res.status_code)
        except Exception as e:
            logger.error("[ai4trades] Error fetching KB document from URL: %s", e)

    return _CACHED_DOC_TEXT


def ask_ai4trades_assistant(user_question: str, conversation_history: list = None) -> str:
    """
    Uses Anthropic Claude to answer user questions adhering to Ai4Trades guidelines.
    Uses RAG retrieval to fetch top matching chunks from the knowledge base.
    """
    client = get_claude_client()
    if not client:
        return "Thank you for reaching out to AI4Trades. Our AI assistant is currently updating. An executive will get back to you shortly."

    # Ensure KB is loaded
    kb_full_text = get_ai4trades_knowledge_content()

    # RAG Retrieval: fetch top matching context blocks
    from .ai4trades_rag import retrieve_ai4trades_context, get_rag_index, build_or_reload_rag
    rag_index = get_rag_index()
    if not rag_index.is_ready and kb_full_text:
        build_or_reload_rag(kb_full_text)

    rag_retrieved_context = retrieve_ai4trades_context(user_question, top_k=5)
    
    # Fallback to truncated full context if RAG yields empty
    final_context = rag_retrieved_context if rag_retrieved_context else (kb_full_text[:40000] if kb_full_text else "Standard Ai4Trades support.")

    system_prompt = f"""You are the official WhatsApp AI assistant for AI4Trades.

KNOWLEDGE BASE CONTEXT (Retrieved via RAG):
\"\"\"
{final_context}
\"\"\"

INSTRUCTIONS:
1. Answer the user's question clearly, helpfully, and politely using ONLY the Knowledge Base context provided above.
2. If the user asks something that is NOT found in the knowledge base, politely inform them that you do not have that specific information in your current records and offer to connect them with the AI4Trades support team.

STRICT COMPLIANCE RULES (MANDATORY):
- No investment advice, tips, stock recommendations, or targets. No promise of profit.
- Pricing: Say "free up to a usage limit" — NEVER say "free forever". No amount quoted unless verified in price list.
- Earning: Say "We have commercial arrangements with our partner brokers. Your brokerage is on the plan you agree with your broker."
- No claim of SEBI registration, exchange approval, or empanelment.
- NEVER ask for a password, PIN, broker password, or full bank/card details.
- Never read out balances, holdings, or profit/loss over WhatsApp.
- Answers should be short, friendly, clear, and formatted for WhatsApp:
  * Use *bold* (single asterisks) for emphasis. Do NOT use markdown **bold** or # headings.
  * Use bullet points with clean emojis.
  * Keep answers mobile-friendly and conversational.
"""

    messages = []
    if conversation_history:
        for msg in conversation_history:
            role = "assistant" if msg.get("role") == "assistant" else "user"
            content = msg.get("content", "")
            if content:
                messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": user_question})

    try:
        model = os.getenv("AI4TRADES_CLAUDE_MODEL") or "claude-haiku-4-5-20251001"
        response = client.messages.create(
            model=model,
            system=system_prompt,
            messages=messages,
            max_tokens=600
        )
        answer = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ).strip()
        
        # Format markdown for clean WhatsApp rendering
        # 1. Replace markdown headers ### or ## with *bold*
        answer = re.sub(r'^(?:#{1,6})\s*(.+)$', r'*\1*', answer, flags=re.MULTILINE)
        # 2. Replace markdown double asterisks **text** with single asterisks *text*
        answer = re.sub(r'\*\*(.*?)\*\*', r'*\1*', answer)
        # 3. Clean up awkward bullets like "· 📊 *Title*" or "* ·" into clean "• "
        answer = re.sub(r'^[ \t]*[•·\-*]\s*(?=[•·\-*])', '', answer, flags=re.MULTILINE)
        answer = re.sub(r'^[ \t]*[-*]\s+', r'• ', answer, flags=re.MULTILINE)
        # 4. Clean up consecutive blank lines
        answer = re.sub(r'\n{3,}', '\n\n', answer).strip()
        
        return answer
    except Exception as e:
        logger.error("[ai4trades] LLM error: %s", e)
        return "I apologize, but I am having trouble processing your query right now. Please try again in a moment."
