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
# Authentication Helper (Requires AI4Trades Phone Number ID)
# ─────────────────────────────────────────────────────────────
def _verify_ai4trades_auth(request):
    """
    Strictly requires the AI4Trades phone_number_id (1357577430775484) to be passed via:
      - Header: `Authorization: Bearer 1357577430775484` (or `X-Phone-Number-Id: 1357577430775484`)
      - Query param: `?token=1357577430775484` or `?phone_number_id=1357577430775484`
      - Request Body: `{"token": "1357577430775484"}` or `{"phone_number_id": "1357577430775484"}`
    Returns None if valid, or a DRF Response (401) if missing/invalid.
    """
    expected_id = str(getattr(settings, 'AI4TRADES_PHONE_NUMBER_ID', AI4TRADES_PHONE_NUMBER_ID)).strip()

    token = (
        request.GET.get("token")
        or request.GET.get("phone_number_id")
        or request.headers.get("X-Phone-Number-Id")
        or ""
    ).strip()

    if not token:
        auth_header = request.headers.get("Authorization", "").strip()
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
        elif auth_header:
            token = auth_header

    if not token and hasattr(request, "data") and isinstance(request.data, dict):
        token = str(
            request.data.get("token")
            or request.data.get("phone_number_id")
            or ""
        ).strip()

    if not token:
        return Response(
            {
                "status": False,
                "error": "Authentication required. Provide phone_number_id via 'Authorization: Bearer <phone_number_id>' header or '?token=<phone_number_id>' query param."
            },
            status=401
        )

    if token != expected_id:
        return Response(
            {
                "status": False,
                "error": "Invalid authentication token / phone_number_id."
            },
            status=401
        )

    return None


# ─────────────────────────────────────────────────────────────
# Knowledge Base Upload & Update API
# ─────────────────────────────────────────────────────────────
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

class Ai4TradesKBUploadAPIView(APIView):
    """
    POST /api/ai4trades/kb/upload/
    Uploads a new Knowledgebase file (PDF, XLSX, CSV, DOCX, TXT) OR raw text.
    Extracts text, updates database Document model, and refreshes LLM cache.
    Requires AI4Trades phone_number_id authentication.
    """
    authentication_classes = []
    permission_classes = [AllowAny]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        auth_err = _verify_ai4trades_auth(request)
        if auth_err:
            return auth_err

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
        auth_err = _verify_ai4trades_auth(request)
        if auth_err:
            return auth_err

        from .ai4trades_utils import get_ai4trades_knowledge_content
        current_kb = get_ai4trades_knowledge_content()
        return Response({
            "status": True,
            "has_kb": bool(current_kb),
            "characters_count": len(current_kb) if current_kb else 0,
            "preview": current_kb[:400] + "..." if current_kb else "No KB content loaded."
        })


# ─────────────────────────────────────────────────────────────
# 1. All Conversations & Messages API
# ─────────────────────────────────────────────────────────────
class Ai4TradesMessagesAPIView(APIView):
    """
    GET /api/ai4trades/messages/
    Requires AI4Trades phone_number_id authentication (1357577430775484).
    Returns all conversations and their complete messages list (inbound + outbound) for AI4Trades.
    
    Optional Query Params:
      ?token=1357577430775484 (or Authorization: Bearer 1357577430775484)
      ?phone=919876543210 -> filter by specific customer phone number
      ?page=1&page_size=50 -> pagination
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            auth_err = _verify_ai4trades_auth(request)
            if auth_err:
                return auth_err

            from datetime import timezone, timedelta
            from django.core.paginator import Paginator

            ai4trades_phone_id = getattr(settings, 'AI4TRADES_PHONE_NUMBER_ID', AI4TRADES_PHONE_NUMBER_ID)
            phone_filter = str(request.GET.get("phone", "") or "").strip()

            try:
                page_num = max(1, int(request.GET.get("page", 1)))
            except (ValueError, TypeError):
                page_num = 1

            try:
                # Cap page_size at 200 conversations max per request to prevent heavy memory/DB spikes
                page_size = max(1, min(int(request.GET.get("page_size", 50)), 200))
            except (ValueError, TypeError):
                page_size = 50

            conv_qs = Conversation.objects.filter(
                phone_number_id=ai4trades_phone_id
            ).select_related("customer").order_by("-created_at")

            if phone_filter:
                clean_p = re.sub(r"[^\d]", "", phone_filter)
                if clean_p:
                    conv_qs = conv_qs.filter(customer__phone__icontains=clean_p)

            paginator = Paginator(conv_qs, page_size)
            page_obj = paginator.get_page(page_num)
            paged_convs = page_obj.object_list

            base_url = request.build_absolute_uri(request.path)
            def make_page_url(p):
                if p is None:
                    return None
                params = request.GET.copy()
                params["page"] = p
                return f"{base_url}?{params.urlencode()}"

            ist_tz = timezone(timedelta(hours=5, minutes=30))

            all_conversations = []
            total_messages_count = 0

            for conv in paged_convs:
                msgs = list(conv.messages.order_by("timestamp", "id"))
                total_messages_count += len(msgs)
                cust_phone = conv.customer.phone if conv.customer else ""
                cust_name = conv.customer.name if conv.customer else ""

                formatted_msgs = [
                    {
                        "id": m.id,
                        "direction": m.direction,
                        "type": m.message_type,
                        "content": m.content,
                        "template_name": m.template_name or "",
                        "status": m.status,
                        "timestamp": m.timestamp.astimezone(ist_tz).isoformat() if m.timestamp else None,
                        "meta_message_id": m.meta_message_id,
                    }
                    for m in msgs
                ]

                all_conversations.append({
                    "conversation_id": conv.id,
                    "customer_name": cust_name,
                    "customer_phone": cust_phone,
                    "bot_state": conv.bot_state,
                    "status": conv.status,
                    "created_at": conv.created_at.astimezone(ist_tz).isoformat() if conv.created_at else None,
                    "total_messages": len(formatted_msgs),
                    "messages": formatted_msgs
                })

            return Response({
                "status": True,
                "pagination": {
                    "current_page": page_obj.number,
                    "total_pages": paginator.num_pages,
                    "total_conversations": paginator.count,
                    "page_size": page_size,
                    "has_next": page_obj.has_next(),
                    "has_previous": page_obj.has_previous(),
                    "next": make_page_url(page_obj.next_page_number() if page_obj.has_next() else None),
                    "previous": make_page_url(page_obj.previous_page_number() if page_obj.has_previous() else None),
                },
                "total_conversations": len(all_conversations),
                "total_messages": total_messages_count,
                "conversations": all_conversations,
            }, status=200)
        except Exception as e:
            logger.error("[ai4trades] Error in Ai4TradesMessagesAPIView: %s", e, exc_info=True)
            return Response({
                "status": False,
                "error": "Failed to fetch conversations and messages.",
                "detail": str(e)
            }, status=500)


# ─────────────────────────────────────────────────────────────
# 2. Send Template Message API
# ─────────────────────────────────────────────────────────────
class Ai4TradesSendTemplateAPIView(APIView):
    """
    POST /api/ai4trades/send-template/
    Pass `template_name` and `phone_number` (or `phone_numbers` list) to send the template
    directly to that WhatsApp number via AI4Trades WABA.

    Example JSON Body:
    {
        "phone_number": "919876543210",
        "template_name": "welcome",
        "language": "en",
        "variables": ["Var1", "Var2"],      // optional body variables
        "components": [...]                 // optional raw Meta components
    }
    """
    authentication_classes = []
    permission_classes = [AllowAny]
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    def post(self, request):
        try:
            auth_err = _verify_ai4trades_auth(request)
            if auth_err:
                return auth_err

            from .ai4trades_utils import send_ai4trades_template, _upload_media_to_meta_ai4trades

            template_name = str(
                request.data.get("template_name")
                or request.data.get("template")
                or ""
            ).strip()
            phone_number = (
                request.data.get("phone_number")
                or request.data.get("phone")
                or request.data.get("to")
                or ""
            )
            phone_numbers_list = request.data.get("phone_numbers", [])
            language_code = str(request.data.get("language") or request.data.get("language_code") or "en").strip()

            # Body variables ({{1}}, {{2}}, ...)
            variables = request.data.get("variables") or request.data.get("body_variables")
            # Header options (TEXT, IMAGE, VIDEO, DOCUMENT)
            header_type = str(request.data.get("header_type") or "").strip()
            header_text = request.data.get("header_text")
            header_variables = request.data.get("header_variables")
            header_media_url = str(
                request.data.get("header_media_url")
                or request.data.get("media_url")
                or request.data.get("image_url")
                or request.data.get("video_url")
                or request.data.get("document_url")
                or ""
            ).strip()
            header_media_id = str(
                request.data.get("header_media_id")
                or request.data.get("media_id")
                or ""
            ).strip()
            document_filename = str(
                request.data.get("document_filename")
                or request.data.get("filename")
                or ""
            ).strip()

            # Auto-infer header_type if specific *_url key was used
            if not header_type:
                if request.data.get("image_url"):
                    header_type = "IMAGE"
                elif request.data.get("video_url"):
                    header_type = "VIDEO"
                elif request.data.get("document_url"):
                    header_type = "DOCUMENT"

            # Handle direct file upload in form-data (file / media_file / header_file)
            uploaded_media = (
                request.FILES.get("media_file")
                or request.FILES.get("header_file")
                or request.FILES.get("file")
            )
            if uploaded_media:
                if not document_filename:
                    document_filename = uploaded_media.name
                if not header_type:
                    ext = uploaded_media.name.split(".")[-1].lower() if "." in uploaded_media.name else ""
                    if ext in ("mp4", "3gp", "mov"):
                        header_type = "VIDEO"
                    elif ext in ("pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv"):
                        header_type = "DOCUMENT"
                    else:
                        header_type = "IMAGE"

                ai4trades_phone_id = getattr(settings, 'AI4TRADES_PHONE_NUMBER_ID', AI4TRADES_PHONE_NUMBER_ID)
                client_acc = ClientAccount.objects.filter(phone_number_id=ai4trades_phone_id).first()
                token = client_acc.access_token if (client_acc and client_acc.access_token) else getattr(settings, "META_PERMANENT_TOKEN", "")
                uploaded_id = _upload_media_to_meta_ai4trades(uploaded_media, ai4trades_phone_id, token)
                if uploaded_id:
                    header_media_id = uploaded_id
                else:
                    return Response({
                        "status": False,
                        "message": "Failed to upload header media file to Meta."
                    }, status=400)

            # Button / OTP variables
            button_variables = request.data.get("button_variables")
            otp_code = request.data.get("otp_code") or request.data.get("otp")
            components = request.data.get("components")

            # Parse JSON strings if sent via form-data
            if isinstance(variables, str):
                try:
                    variables = json.loads(variables)
                except Exception:
                    variables = [v.strip() for v in variables.split(",") if v.strip()]

            if isinstance(header_variables, str):
                try:
                    header_variables = json.loads(header_variables)
                except Exception:
                    header_variables = [v.strip() for v in header_variables.split(",") if v.strip()]

            if isinstance(button_variables, str):
                try:
                    button_variables = json.loads(button_variables)
                except Exception:
                    button_variables = [v.strip() for v in button_variables.split(",") if v.strip()]

            if isinstance(components, str):
                try:
                    components = json.loads(components)
                except Exception:
                    components = None

            if not template_name:
                return Response({
                    "status": False,
                    "message": "'template_name' is required."
                }, status=400)

            targets = []
            if phone_number:
                targets.append(str(phone_number).strip())
            if isinstance(phone_numbers_list, list):
                targets.extend([str(p).strip() for p in phone_numbers_list if str(p).strip()])
            elif isinstance(phone_numbers_list, str) and phone_numbers_list.strip():
                try:
                    parsed = json.loads(phone_numbers_list)
                    if isinstance(parsed, list):
                        targets.extend([str(p).strip() for p in parsed if str(p).strip()])
                    else:
                        targets.append(phone_numbers_list.strip())
                except Exception:
                    targets.extend([p.strip() for p in phone_numbers_list.split(",") if p.strip()])

            if not targets:
                return Response({
                    "status": False,
                    "message": "'phone_number' is required."
                }, status=400)

            common_kwargs = dict(
                template_name=template_name,
                language_code=language_code,
                components=components,
                variables=variables,
                header_type=header_type,
                header_text=header_text,
                header_variables=header_variables,
                header_media_url=header_media_url,
                header_media_id=header_media_id,
                document_filename=document_filename,
                button_variables=button_variables,
                otp_code=otp_code,
            )

            # Single recipient
            if len(targets) == 1:
                success, result = send_ai4trades_template(
                    to=targets[0],
                    **common_kwargs
                )
                if success:
                    return Response({
                        "status": True,
                        "message": f"Template '{template_name}' sent successfully to {result.get('to')}.",
                        "data": result
                    }, status=200)
                else:
                    return Response({
                        "status": False,
                        "message": f"Failed to send template '{template_name}'.",
                        "error": result
                    }, status=400)

            # Multiple recipients support if list passed (run concurrently with ThreadPoolExecutor, max 500 per request)
            if len(targets) > 500:
                return Response({
                    "status": False,
                    "message": "Maximum 500 phone numbers allowed per single request."
                }, status=400)

            import concurrent.futures
            from django.db import close_old_connections
            from CRM.models import ClientAccount

            ai4trades_phone_id = getattr(settings, 'AI4TRADES_PHONE_NUMBER_ID', AI4TRADES_PHONE_NUMBER_ID)
            shared_client_acc = ClientAccount.objects.filter(phone_number_id=ai4trades_phone_id).first()
            common_kwargs["client_account_obj"] = shared_client_acc

            def _send_to_target(target_num):
                close_old_connections()
                try:
                    ok, res = send_ai4trades_template(to=target_num, **common_kwargs)
                    return target_num, ok, res
                except Exception as exc:
                    return target_num, False, str(exc)
                finally:
                    close_old_connections()

            results = []
            sent_count = 0
            failed_count = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(_send_to_target, t) for t in targets]
                for future in concurrent.futures.as_completed(futures):
                    target, success, result = future.result()
                    if success:
                        sent_count += 1
                        results.append({"phone_number": target, "status": "sent", **result})
                    else:
                        failed_count += 1
                        results.append({"phone_number": target, "status": "failed", "error": result})

            return Response({
                "status": sent_count > 0,
                "template_name": template_name,
                "total": len(targets),
                "sent_count": sent_count,
                "failed_count": failed_count,
                "results": results
            }, status=200 if sent_count > 0 else 400)
        except Exception as e:
            logger.error("[ai4trades] Error in Ai4TradesSendTemplateAPIView: %s", e, exc_info=True)
            return Response({
                "status": False,
                "error": "An unexpected error occurred while sending template.",
                "detail": str(e)
            }, status=500)





