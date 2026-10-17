import json
import logging
import os
import requests
import io
from django.conf import settings
from openai import AzureOpenAI

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
# Azure OpenAI Client Helper
# ─────────────────────────────────────────────────────────────
_azure_client = None

def get_azure_openai_client():
    global _azure_client
    if _azure_client is None:
        api_key = os.getenv("OPENAI_API_KEY") or getattr(settings, "OPENAI_API_KEY", "")
        endpoint = os.getenv("ENDPOINT_URL") or getattr(settings, "ENDPOINT_URL", "")
        api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-05-01-preview")
        if api_key and endpoint:
            try:
                _azure_client = AzureOpenAI(
                    api_key=api_key,
                    api_version=api_version,
                    azure_endpoint=endpoint
                )
            except Exception as e:
                logger.error("[ai4trades] Failed to initialize AzureOpenAI: %s", e)
        else:
            logger.warning("[ai4trades] OPENAI_API_KEY or ENDPOINT_URL not set in environment.")
    return _azure_client




# ─────────────────────────────────────────────────────────────
# Meta WhatsApp API Dispatcher
# ─────────────────────────────────────────────────────────────
def _meta_post_ai4trades(payload: dict) -> bool:
    ai4trades_phone_id = getattr(settings, 'AI4TRADES_PHONE_NUMBER_ID', AI4TRADES_PHONE_NUMBER_ID)
    
    from CRM.models import ClientAccount
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

        # Save outbound message to DB
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

            from CRM.models import Customer, Conversation, Message
            customer_obj, _ = Customer.objects.get_or_create(phone=to_number, defaults={'name': to_number})
            conv_obj, _ = Conversation.objects.get_or_create(
                customer=customer_obj,
                phone_number_id=ai4trades_phone_id,
                defaults={'client': client_account_obj}
            )

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


def send_ai4trades_text(to: str, text: str) -> bool:
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
    return _meta_post_ai4trades(payload)


def send_ai4trades_welcome_template(to: str) -> bool:
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
    })


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
    Uses Azure OpenAI gpt-4o-mini to answer user questions adhering to Ai4Trades guidelines.
    Uses RAG retrieval to fetch top matching chunks from the knowledge base.
    """
    client = get_azure_openai_client()
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

    messages = [{"role": "system", "content": system_prompt}]
    
    if conversation_history:
        messages.extend(conversation_history)
        
    messages.append({"role": "user", "content": user_question})

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=600,
            temperature=0.3
        )
        answer = response.choices[0].message.content.strip()
        return answer
    except Exception as e:
        logger.error("[ai4trades] LLM error: %s", e)
        return "I apologize, but I am having trouble processing your query right now. Please try again in a moment."
