import json
import logging
import uuid
import re
import requests
from django.conf import settings
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny

from CRM.models import Customer, Conversation, Message, ClientAccount
from .ai4trades_utils import AI4TRADES_PHONE_NUMBER_ID

logger = logging.getLogger(__name__)

def download_media_from_whatsapp(media_id: str, access_token: str) -> str:
    try:
        url = f"https://graph.facebook.com/v20.0/{media_id}"
        headers = {"Authorization": f"Bearer {access_token}"}
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200:
            logger.error("[ai4trades] Failed to get media URL for %s: %s", media_id, res.text)
            return ""
        media_url = res.json().get("url")
        if not media_url:
            return ""
        
        file_res = requests.get(media_url, headers=headers, timeout=15)
        if file_res.status_code != 200:
            logger.error("[ai4trades] Failed to download media %s", media_id)
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
            
        file_name = f"ai4trades_media/{uuid.uuid4().hex}.{ext}"
        saved_path = default_storage.save(file_name, ContentFile(file_res.content))
        return default_storage.url(saved_path)
    except Exception as e:
        logger.error("[ai4trades] Error downloading media: %s", e)
        return ""

def handle_ai4trades_message(msg: dict, contact: dict = None):
    contact = contact or {}
    profile_name = contact.get("profile", {}).get("name", "")
    number   = msg.get("from", "")
    msg_id   = msg.get("id", "")
    msg_type = msg.get("type", "text")

    body = ""
    display_body = ""
    if msg_type == "text":
        body = msg.get("text", {}).get("body", "").strip()
        display_body = body
    elif msg_type == "interactive":
        idata = msg.get("interactive", {})
        itype = idata.get("type")
        if itype == "button_reply":
            body = idata["button_reply"]["id"]
            display_body = idata["button_reply"].get("title", body)
        elif itype == "list_reply":
            body = idata["list_reply"]["id"]
            display_body = idata["list_reply"].get("title", body)
    elif msg_type == "button":
        body = msg.get("button", {}).get("payload", "").strip()
        display_body = msg.get("button", {}).get("text", body).strip()
    elif msg_type in ["image", "video", "audio", "document", "sticker"]:
        media_id = msg.get(msg_type, {}).get("id")
        if media_id:
            ai4trades_phone_id = getattr(settings, 'AI4TRADES_PHONE_NUMBER_ID', AI4TRADES_PHONE_NUMBER_ID)
            client_account_obj = ClientAccount.objects.filter(phone_number_id=ai4trades_phone_id).first()
            token = client_account_obj.access_token if client_account_obj and client_account_obj.access_token else getattr(settings, "META_PERMANENT_TOKEN", "")
            dl_url = download_media_from_whatsapp(media_id, token)
            if dl_url:
                prefix = f"[{msg_type.upper()}]"
                body = f"{prefix} {dl_url}"
                display_body = body
            else:
                body = f"[{msg_type}]"
                display_body = f"[{msg_type}]"
        else:
            body = f"[{msg_type}]"
            display_body = f"[{msg_type}]"
            
    elif msg_type == "location":
        lat = msg.get("location", {}).get("latitude")
        lng = msg.get("location", {}).get("longitude")
        name = msg.get("location", {}).get("name", "")
        address = msg.get("location", {}).get("address", "")
        loc_str = []
        if name: loc_str.append(name)
        if address: loc_str.append(address)
        loc_str.append(f"https://maps.google.com/?q={lat},{lng}")
        
        body = f"📍 Location: " + " - ".join(loc_str)
        display_body = body

    elif msg_type == "contacts":
        contacts = msg.get("contacts", [])
        c_list = []
        for c in contacts:
            c_name = c.get("name", {}).get("formatted_name", "Unknown")
            phones = c.get("phones", [])
            c_phone = phones[0].get("phone", "") if phones else ""
            c_list.append(f"{c_name} ({c_phone})" if c_phone else c_name)
        
        body = "👤 Contact: " + ", ".join(c_list)
        display_body = body

    if not display_body:
        body = f"[{msg_type}] " + json.dumps(msg.get(msg_type, msg))
        display_body = body

    logger.info("[ai4trades DEBUG] msg payload: %s", json.dumps(msg))
    logger.info("[ai4trades] from=%s type=%s body=%r display_body=%r id=%s", number, msg_type, body, display_body, msg_id)

    customer_name = profile_name if profile_name else number
    customer_obj, _ = Customer.objects.get_or_create(phone=number, defaults={'name': customer_name})
    if profile_name and customer_obj.name == number:
        customer_obj.name = profile_name
        customer_obj.save(update_fields=['name'])
    ai4trades_phone_id = AI4TRADES_PHONE_NUMBER_ID
    client_account_obj = ClientAccount.objects.filter(phone_number_id=ai4trades_phone_id).first()

    conv_obj, _ = Conversation.objects.get_or_create(
        customer=customer_obj, 
        phone_number_id=ai4trades_phone_id, 
        defaults={'client': client_account_obj}
    )
    if client_account_obj and not conv_obj.client:
        conv_obj.client = client_account_obj
        conv_obj.save(update_fields=['client'])
    inbound_message = Message.objects.create(
        conversation=conv_obj,
        client=client_account_obj,
        customer=customer_obj,
        meta_message_id=msg_id,
        direction="inbound",
        message_type=msg_type if msg_type in ['text', 'template', 'image', 'document', 'video'] else 'text',
        content=display_body,
        status='delivered'
    )

    # ── Flow Execution: Welcome Template & Knowledgebase RAG QA ──
    from .ai4trades_utils import (
        send_ai4trades_welcome_template,
        send_ai4trades_text,
        ask_ai4trades_assistant
    )

    current_state = conv_obj.bot_state or "INIT"
    is_initial_interaction = (current_state == "INIT")

    # 1. First interaction: send Meta welcome template
    if is_initial_interaction:
        logger.info("[ai4trades] First message from %s. Sending welcome template...", number)
        sent = send_ai4trades_welcome_template(
            to=number,
            customer_obj=customer_obj,
            conv_obj=conv_obj,
            client_account_obj=client_account_obj
        )
        if not sent:
            logger.warning("[ai4trades] Welcome template failed to send. Sending fallback welcome text.")
            send_ai4trades_text(
                to=number,
                text="👋 *Welcome to AI4Trades!*\n\nHow can we help you today? Please feel free to ask any questions or queries regarding our services!",
                customer_obj=customer_obj,
                conv_obj=conv_obj,
                client_account_obj=client_account_obj
            )
        
        conv_obj.bot_state = "AWAITING_QUERY"
        conv_obj.save(update_fields=['bot_state'])
        return

    user_query = body if body else display_body
    if not user_query:
        return

    # Fetch last conversation messages for context
    past_messages = Message.objects.filter(
        conversation=conv_obj
    ).exclude(id=inbound_message.id).order_by('-timestamp')[:6]

    chat_history = []
    for pm in reversed(list(past_messages)):
        role = "assistant" if pm.direction == "outbound" else "user"
        if pm.content and not pm.content.startswith("["):
            chat_history.append({"role": role, "content": pm.content})

    logger.info("[ai4trades] Answering query from %s via RAG KB...", number)
    ai_answer = ask_ai4trades_assistant(
        user_question=user_query,
        conversation_history=chat_history
    )

    # Send the AI answer back to WhatsApp (saved to same conversation as outbound)
    send_ai4trades_text(
        to=number,
        text=ai_answer,
        customer_obj=customer_obj,
        conv_obj=conv_obj,
        client_account_obj=client_account_obj
    )


# ─────────────────────────────────────────────────────────────
# Knowledge Base Upload & Update API
# ─────────────────────────────────────────────────────────────
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

class Ai4TradesKBUploadAPIView(APIView):
    """
    POST /api/ai4trades/kb/upload/
    Uploads a new Knowledgebase file (PDF, XLSX, CSV, DOCX, TXT) OR raw text.
    Extracts text, updates database Document model, and refreshes LLM cache.
    """
    permission_classes = [AllowAny]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        from CRM.models import Document
        from .ai4trades_utils import extract_text_from_file_bytes, update_ai4trades_knowledge_content

        uploaded_file = request.FILES.get("file")
        raw_text = request.data.get("text", "").strip()
        doc_url = request.data.get("url", "").strip()

        extracted = ""
        filename = "ai4trades_knowledge.txt"
        file_type = "txt"

        saved_blob_url = ""
        if uploaded_file:
            safe_name = re.sub(r'[^a-zA-Z0-9_\.-]', '_', uploaded_file.name)
            filename = f"ai4trades_kb/{uuid.uuid4().hex[:8]}_{safe_name}"
            file_type = uploaded_file.name.split(".")[-1].lower() if "." in uploaded_file.name else "txt"
            file_bytes = uploaded_file.read()
            extracted = extract_text_from_file_bytes(file_bytes, uploaded_file.name)
            
            # Save file to blob / storage under ai4trades_kb/
            saved_path = default_storage.save(filename, ContentFile(file_bytes))
            saved_blob_url = default_storage.url(saved_path)

            saved_doc = Document.objects.create(
                name=f"ai4trades_{uploaded_file.name}",
                file=saved_path,
                file_type=file_type,
                extracted_text=extracted,
                char_count=len(extracted),
                is_indexed=True
            )
        elif raw_text:
            extracted = raw_text
            file_bytes = raw_text.encode('utf-8')
            filename = f"ai4trades_kb/{uuid.uuid4().hex[:8]}_ai4trades_text.txt"
            saved_path = default_storage.save(filename, ContentFile(file_bytes))
            saved_blob_url = default_storage.url(saved_path)

            saved_doc = Document.objects.create(
                name=f"ai4trades_text_update_{uuid.uuid4().hex[:6]}.txt",
                file=saved_path,
                file_type="txt",
                extracted_text=extracted,
                char_count=len(extracted),
                is_indexed=True
            )
        elif doc_url:
            try:
                res = requests.get(doc_url, timeout=20)
                if res.status_code == 200:
                    filename = doc_url.split("?")[0].split("/")[-1] or "ai4trades_kb.bin"
                    file_type = filename.split(".")[-1].lower() if "." in filename else "txt"
                    extracted = extract_text_from_file_bytes(res.content, filename)
                    saved_doc = Document.objects.create(
                        name=f"ai4trades_{filename}",
                        file_type=file_type,
                        extracted_text=extracted,
                        char_count=len(extracted),
                        is_indexed=True
                    )
                else:
                    return Response({"status": False, "message": f"Failed to download file from URL (status: {res.status_code})"}, status=400)
            except Exception as e:
                return Response({"status": False, "message": f"Error downloading from URL: {str(e)}"}, status=400)
        else:
            return Response(
                {"status": False, "message": "Please provide a 'file' upload, 'text', or 'url'."},
                status=400
            )

        if not extracted:
            return Response({
                "status": False,
                "message": "File was received, but no readable text could be extracted. Please check the file format."
            }, status=400)

        # Update in-memory cache and RAG index so LLM immediately uses new KB
        update_ai4trades_knowledge_content(extracted)
        from .ai4trades_rag import get_rag_index
        total_rag_chunks = get_rag_index().total_docs

        return Response({
            "status": True,
            "message": "Ai4Trades Knowledge Base updated & RAG indexed successfully!",
            "document_id": saved_doc.id,
            "filename": saved_doc.name,
            "storage_path": saved_path if 'saved_path' in locals() else "",
            "blob_url": saved_blob_url,
            "characters_count": len(extracted),
            "rag_chunks_indexed": total_rag_chunks,
            "preview": extracted[:300] + "..." if len(extracted) > 300 else extracted
        }, status=200)

    def get(self, request):
        """GET /api/ai4trades/kb/upload/ - Returns current active KB status."""
        from .ai4trades_utils import get_ai4trades_knowledge_content
        current_kb = get_ai4trades_knowledge_content()
        return Response({
            "status": True,
            "has_kb": bool(current_kb),
            "characters_count": len(current_kb) if current_kb else 0,
            "preview": current_kb[:400] + "..." if current_kb else "No KB content loaded."
        })





