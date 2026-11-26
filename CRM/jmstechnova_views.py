import json
import logging
import uuid
import requests
from django.conf import settings
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny

from CRM.models import Customer, Conversation, Message, ClientAccount
from CRM.META.webhook_views import _normalize_e164_phone
from .jmstechnova_utils import JMSTECHNOVA_PHONE_NUMBER_ID

logger = logging.getLogger(__name__)

def download_media_from_whatsapp(media_id: str, access_token: str) -> str:
    try:
        url = f"https://graph.facebook.com/v22.0/{media_id}"
        headers = {"Authorization": f"Bearer {access_token}"}
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200:
            logger.error("[jmstechnova] Failed to get media URL for %s: %s", media_id, res.text)
            return ""
        media_url = res.json().get("url")
        if not media_url:
            return ""
        
        file_res = requests.get(media_url, headers=headers, timeout=15)
        if file_res.status_code != 200:
            logger.error("[jmstechnova] Failed to download media %s", media_id)
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
            
        file_name = f"jmstechnova_media/{uuid.uuid4().hex}.{ext}"
        saved_path = default_storage.save(file_name, ContentFile(file_res.content))
        return default_storage.url(saved_path)
    except Exception as e:
        logger.error("[jmstechnova] Error downloading media: %s", e)
        return ""

def handle_jmstechnova_message(msg: dict, contact: dict = None):
    try:
        raw_from = msg.get("from", "")
        number = _normalize_e164_phone(raw_from)
        msg_id = msg.get("id", "")
        msg_type = msg.get("type", "text")

        contact = contact or {}
        profile_name = contact.get("profile", {}).get("name", "")

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
            caption = msg.get(msg_type, {}).get("caption", "").strip()
            if media_id:
                phone_id = getattr(settings, 'JMSTECHNOVA_PHONE_NUMBER_ID', JMSTECHNOVA_PHONE_NUMBER_ID)
                client_account_obj = ClientAccount.objects.filter(phone_number_id=phone_id).first()
                token = client_account_obj.access_token if client_account_obj and client_account_obj.access_token else getattr(settings, "META_PERMANENT_TOKEN", "")
                dl_url = download_media_from_whatsapp(media_id, token)
                prefix = f"[{msg_type.upper()}]"
                if dl_url:
                    body = f"{prefix} {dl_url}"
                    display_body = f"{body}\n{caption}" if caption else body
                else:
                    body = prefix
                    display_body = f"{body}\n{caption}" if caption else body
            else:
                body = f"[{msg_type}]"
                display_body = f"{body}\n{caption}" if caption else body
                
        elif msg_type == "location":
            lat = msg.get("location", {}).get("latitude")
            lng = msg.get("location", {}).get("longitude")
            name = msg.get("location", {}).get("name", "")
            address = msg.get("location", {}).get("address", "")
            loc_str = []
            if name: loc_str.append(name)
            if address: loc_str.append(address)
            loc_str.append(f"https://maps.google.com/?q={lat},{lng}")
            
            body = "📍 Location: " + " - ".join(loc_str)
            display_body = body

        elif msg_type == "contacts":
            contacts_data = msg.get("contacts", [])
            c_list = []
            for c in contacts_data:
                c_name = c.get("name", {}).get("formatted_name", "Unknown")
                phones = c.get("phones", [])
                c_phone = phones[0].get("phone", "") if phones else ""
                c_list.append(f"{c_name} ({c_phone})" if c_phone else c_name)
            
            body = "👤 Contact: " + ", ".join(c_list)
            display_body = body

        if not display_body:
            body = f"[{msg_type}] " + json.dumps(msg.get(msg_type, msg))
            display_body = body

        logger.info("[JMSTECHNOVA DEBUG] msg payload: %s", json.dumps(msg))
        logger.info("[JMSTECHNOVA] from=%s type=%s body=%r display_body=%r id=%s", number, msg_type, body, display_body, msg_id)

        customer_name = profile_name if profile_name else number
        customer_obj, _ = Customer.objects.get_or_create(phone=number, defaults={'name': customer_name})
        if profile_name and customer_obj.name == number:
            customer_obj.name = profile_name
            customer_obj.save(update_fields=['name'])

        phone_id = JMSTECHNOVA_PHONE_NUMBER_ID
        client_account_obj = ClientAccount.objects.filter(phone_number_id=phone_id).first()

        conv_obj, _ = Conversation.objects.get_or_create(
            customer=customer_obj, 
            phone_number_id=phone_id, 
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
    except Exception as e:
        logger.exception("[JMSTECHNOVA] Error handling message: %s", e)
