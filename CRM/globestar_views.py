import json
import logging
import os
import re
from datetime import datetime

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny

from CRM.models import Customer, Conversation, Message, ClientAccount, ConversationState
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from django.conf import settings
import requests
import uuid
from .globestar_utils import (
    GLOBESTAR_PHONE_NUMBER_ID,
    GLOBESTAR_PRODUCTS,
    tpl_gs_welcome,
    tpl_gs_main_menu,
    tpl_gs_product_list,
    tpl_gs_product_list_page2,
    send_gs_product_detail,
    send_gs_text,
    tpl_gs_talk_to_sales,
    tpl_gs_ask_capacity,
    tpl_gs_ask_head,
    tpl_gs_ask_application,
    tpl_gs_ask_pump_type,
    tpl_gs_ask_gravity,
    send_gs_document
)

logger = logging.getLogger(__name__)

class ConversationSession:
    def __init__(self, conv):
        self._conv = conv

    def save(self):
        self._conv.save()

    @property
    def mobile_number(self):
        return self._conv.customer.phone

    @property
    def state(self):
        return self._conv.bot_state

    @state.setter
    def state(self, value):
        self._conv.bot_state = value

    @property
    def updated_at(self):
        return self._conv.created_at

    def __getattr__(self, item):
        return self._conv.bot_metadata.get(item)

    def __setattr__(self, key, value):
        if key in ['_conv', 'state']:
            super().__setattr__(key, value)
        else:
            if not isinstance(self._conv.bot_metadata, dict):
                self._conv.bot_metadata = {}
            self._conv.bot_metadata[key] = value

def download_media_from_whatsapp(media_id: str, access_token: str) -> str:
    try:
        url = f"https://graph.facebook.com/v20.0/{media_id}"
        headers = {"Authorization": f"Bearer {access_token}"}
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200:
            logger.error("[GLOBESTAR] Failed to get media URL for %s: %s", media_id, res.text)
            return ""
        media_url = res.json().get("url")
        if not media_url:
            return ""
        
        file_res = requests.get(media_url, headers=headers, timeout=15)
        if file_res.status_code != 200:
            logger.error("[GLOBESTAR] Failed to download media %s", media_id)
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
            
        file_name = f"globestar_media/{uuid.uuid4().hex}.{ext}"
        saved_path = default_storage.save(file_name, ContentFile(file_res.content))
        return default_storage.url(saved_path)
    except Exception as e:
        logger.error("[GLOBESTAR] Error downloading media: %s", e)
        return ""

def handle_globestar_message(msg: dict):
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
            gs_phone_id = getattr(settings, 'GLOBESTAR_PHONE_NUMBER_ID', GLOBESTAR_PHONE_NUMBER_ID)
            client_account_obj = ClientAccount.objects.filter(phone_number_id=gs_phone_id).first()
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
        # Fallback for unhandled types (like template, unknown, system) or empty payloads (like OTP autofill missing text)
        body = f"[{msg_type}] " + json.dumps(msg.get(msg_type, msg))
        display_body = body


    logger.info("[GLOBESTAR DEBUG] msg payload: %s", json.dumps(msg))
    logger.info("[GLOBESTAR] from=%s type=%s body=%r display_body=%r id=%s", number, msg_type, body, display_body, msg_id)

    customer_obj, _ = Customer.objects.get_or_create(phone=number, defaults={'name': number})
    gs_phone_id = GLOBESTAR_PHONE_NUMBER_ID
    client_account_obj = ClientAccount.objects.filter(phone_number_id=gs_phone_id).first()

    conv_obj, conv_created = Conversation.objects.get_or_create(
        customer=customer_obj, 
        phone_number_id=gs_phone_id, 
        defaults={'client': client_account_obj}
    )
    Message.objects.create(
        conversation=conv_obj,
        client=client_account_obj,
        customer=customer_obj,
        meta_message_id=msg_id,
        direction="inbound",
        message_type=msg_type if msg_type in ['text', 'template', 'image', 'document', 'video'] else 'text',
        content=display_body,
        status='delivered'
    )

    if not conv_obj.bot_state:
        conv_obj.bot_state = "INIT"
        conv_obj.save()
        conv_created = True

    session = ConversationSession(conv_obj)
    
    # Reset flow ONLY on "hi", "hello", or "menu"
    is_trigger = bool(re.search(r'\b(hi|hello|menu)\b', body.lower()))
    
    if is_trigger:
        session.state = "GS_INIT"
        session.gs_selected_product = ""
        session.gs_capacity = ""
        session.gs_head = ""
        session.gs_application = ""
        session.gs_pump_type = ""
        session.gs_specific_gravity = ""
        session.save()

    state = session.state

    # Route based on state
    if state == "GS_INIT":
        import time
        tpl_gs_welcome(number)
        time.sleep(4)  # Delay to ensure WhatsApp delivers both messages
        tpl_gs_main_menu(number)
        session.state = "GS_MENU"
        session.save()

    elif state == "GS_MENU":
        if body in ["1", "view_products"]:
            tpl_gs_product_list(number)
            session.state = "GS_PRODUCTS"
            session.save()
        elif body in ["2", "talk_to_sales"]:
            tpl_gs_talk_to_sales(number)
            session.state = "GS_DONE"
            session.gs_selected_product = "Talk to Sales"
            session.save()
            logger.info("[GLOBESTAR] Lead generated: product=%s cap=%s head=%s app=%s type=%s sg=%s num=%s",
                        session.gs_selected_product, session.gs_capacity, session.gs_head, 
                        session.gs_application, session.gs_pump_type, session.gs_specific_gravity, number)
        elif body in ["3", "general_request"]:
            doc_url = "https://whatsappcrmsaasstorage.blob.core.windows.net/media/globestar/catalogue.pdf"
            send_gs_document(number, doc_url, "Globe_Star_Catalogue.pdf")
            session.state = "GS_DONE"
            session.gs_selected_product = "Catalogue"
            session.save()
            logger.info("[GLOBESTAR] Lead generated: product=%s cap=%s head=%s app=%s type=%s sg=%s num=%s",
                        session.gs_selected_product, session.gs_capacity, session.gs_head, 
                        session.gs_application, session.gs_pump_type, session.gs_specific_gravity, number)
        else:
            tpl_gs_main_menu(number)

    elif state == "GS_PRODUCTS":
        if body == "99" or body == "more_products":
            tpl_gs_product_list_page2(number)
            # Do not change state, they are still browsing products
        elif body.isdigit():
            product_id = body
            send_gs_product_detail(number, product_id)
            session.state = "GS_PRODUCT_DETAIL"
            session.gs_selected_product = product_id
            session.save()
        else:
            tpl_gs_product_list(number)

    elif state == "GS_PRODUCT_DETAIL":
        if body.isdigit() and body != "0":
            session.state = "GS_AWAIT_CAPACITY"
            session.gs_selected_product = body
            session.save()
            tpl_gs_ask_capacity(number)
        elif body in ["0", "back_to_products"]:
            tpl_gs_product_list(number)
            session.state = "GS_PRODUCTS"
            session.save()
        else:
            send_gs_product_detail(number, session.gs_selected_product)

    elif state == "GS_AWAIT_CAPACITY":
        session.gs_capacity = body
        session.state = "GS_AWAIT_HEAD"
        session.save()
        tpl_gs_ask_head(number)

    elif state == "GS_AWAIT_HEAD":
        session.gs_head = body
        session.state = "GS_AWAIT_APPLICATION"
        session.save()
        tpl_gs_ask_application(number)

    elif state == "GS_AWAIT_APPLICATION":
        session.gs_application = body
        session.state = "GS_DONE"
        session.save()
        msg = "Thank you for contacting Globe Star Engineers.\n You will receive a call shortly."
        send_gs_text(number, msg)
        logger.info("[GLOBESTAR] Lead generated: product=%s cap=%s head=%s app=%s type=%s sg=%s num=%s",
                    session.gs_selected_product, session.gs_capacity, session.gs_head, 
                    session.gs_application, session.gs_pump_type, session.gs_specific_gravity, number)

    elif state == "GS_DONE":
        if is_trigger:
            pass # already handled by trigger check above
        else:
            send_gs_text(number, "You have already completed the flow. Type 'hi' to restart.")

    # --- Sync WhatsAppSession to ConversationState for Leads/Prospects View ---
    org_obj = None
    if client_account_obj:
        org_obj = client_account_obj.tech_provider
    else:
        from CRM.models import WABAAccount
        waba = WABAAccount.objects.filter(phone_number_id=gs_phone_id).first()
        if waba:
            org_obj = waba.organization
            
    if org_obj:
        conv_state, created = ConversationState.objects.get_or_create(
            conversation=conv_obj,
            defaults={
                "organization": org_obj,
                "stage": "greeting", 
                "is_complete": False
            }
        )
        if not created and conv_state.organization != org_obj:
            conv_state.organization = org_obj
        
        # Map session state to meaningful stages for the frontend
        if session.gs_selected_product == "Talk to Sales":
            conv_state.stage = "Talk to Sales"
        elif session.gs_selected_product == "General Request":
            conv_state.stage = "Request Quotation"
        elif session.gs_selected_product:
            conv_state.stage = "Get Price"
        elif session.state == "GS_INIT":
            conv_state.stage = "greeting"
        else:
            conv_state.stage = "Exploring Menu"

        if session.state == "GS_DONE":
            conv_state.is_complete = True
        else:
            conv_state.is_complete = False
            
        conv_state.collected_fields = {
            "Product": session.gs_selected_product,
            "Capacity": session.gs_capacity,
            "Head": session.gs_head,
            "Application": session.gs_application,
            "Pump Type": session.gs_pump_type,
            "Specific Gravity": session.gs_specific_gravity
        }
        conv_state.save()
        
    # Set status
    if session.state == "GS_DONE":
        conv_obj.status = "confirmed"
    else:
        conv_obj.status = "prospect"
    conv_obj.save()

class GlobestarDataAPIView(APIView):
    """
    API to fetch all Globestar data, formatted similarly to Gigatel data export.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        phone_number_id = request.GET.get('phone_number_id')
        token = request.GET.get('token')

        if not phone_number_id or not token:
            return Response({"error": "phone_number_id and token are required in query params"}, status=400)

        client = ClientAccount.objects.filter(phone_number_id=phone_number_id).first()
        if not client:
            return Response({"error": "Invalid phone_number_id"}, status=401)
        
        if token != client.access_token and token != settings.META_PERMANENT_TOKEN:
            return Response({"error": "Invalid token"}, status=401)

        conversations = Conversation.objects.filter(
            phone_number_id=phone_number_id
        ).select_related('customer', 'chatbot_state').prefetch_related('messages').order_by('-created_at')

        export_data = {
            "client": {
                "name": client.name if client else "Globestar",
                "phone_number_id": client.phone_number_id if client else phone_number_id,
                "waba_id": client.waba_id if client else "",
            },
            "conversations": []
        }

        for conv in conversations:
            session_data = conv.chatbot_state.collected_fields if hasattr(conv, 'chatbot_state') else {}
            
            conv_data = {
                "id": conv.id,
                "customer_name": conv.customer.name,
                "customer_phone": conv.customer.phone,
                "status": conv.status,
                "created_at": conv.created_at.isoformat() if conv.created_at else None,
                "chatbot_state": session_data,
                "messages": []
            }
            
            for msg in conv.messages.all():
                conv_data["messages"].append({
                    "id": msg.id,
                    "direction": msg.direction,
                    "status": msg.status,
                    "type": msg.message_type,
                    "content": msg.content,
                    "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
                    "meta_message_id": msg.meta_message_id
                })
            
            export_data["conversations"].append(conv_data)

        return Response(export_data)


