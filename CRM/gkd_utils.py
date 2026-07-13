import json
import logging
import os
import requests
import uuid
from django.conf import settings
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)

GKD_PHONE_NUMBER_ID = "1251906908002889"
META_SEND_URL = "https://graph.facebook.com/v22.0/{phone_id}/messages"
def _meta_post_gkd(payload: dict) -> bool:
    token = getattr(settings, "META_PERMANENT_TOKEN", "")
    url = META_SEND_URL.format(phone_id=GKD_PHONE_NUMBER_ID)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        if r.status_code not in (200, 201):
            logger.error("[GKD] Meta API error: %s - %s", r.status_code, r.text)
            return False
        logger.info("[GKD] Meta API success")
        
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
            elif msg_type == "interactive":
                content = "[Interactive Message]"
            elif msg_type == "template":
                template_name = payload.get("template", {}).get("name", "")
                content = f"[Template: {template_name}]"
            elif msg_type == "image":
                content = "[Image]"
            else:
                content = f"[{msg_type.capitalize()}]"
                
            from CRM.models import Customer, Conversation, Message, ClientAccount
            customer_obj, _ = Customer.objects.get_or_create(phone=to_number, defaults={'name': to_number})
            client_account_obj = ClientAccount.objects.filter(phone_number_id=GKD_PHONE_NUMBER_ID).first()
            conv_obj, _ = Conversation.objects.get_or_create(
                customer=customer_obj, 
                phone_number_id=GKD_PHONE_NUMBER_ID, 
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
            logger.error("[GKD] Error saving outbound message: %s", e)

        return True
    except Exception as exc:
        logger.error("[GKD] Meta API exception: %s", exc)
        return False

def _gkd_template_payload(to: str, name: str, components: list = None) -> dict:
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": name,
            "language": {"code": "en"}
        }
    }
    if components:
        payload["template"]["components"] = components
    return payload

def send_gkd_text(number: str, text: str):
    return _meta_post_gkd({
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": number,
        "type": "text",
        "text": {"preview_url": False, "body": text}
    })

def send_gkd_image(to: str, url: str, caption: str = ""):
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "image",
        "image": {
            "link": url
        }
    }
    if caption:
        payload["image"]["caption"] = caption
    return _meta_post_gkd(payload)

def download_gkd_media_from_whatsapp(media_id: str, access_token: str, original_filename: str = None) -> str:
    try:
        url = f"https://graph.facebook.com/v20.0/{media_id}"
        headers = {"Authorization": f"Bearer {access_token}"}
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200:
            logger.error("[GKD] Failed to get media URL for %s: %s", media_id, res.text)
            return ""
        media_url = res.json().get("url")
        if not media_url:
            return ""
        
        file_res = requests.get(media_url, headers=headers, timeout=15)
        if file_res.status_code != 200:
            logger.error("[GKD] Failed to download media %s", media_id)
            return ""
            
        content_type = file_res.headers.get("Content-Type", "").split(';')[0].strip()
        
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
            if not ext or len(ext) > 5:
                ext = "bin"

        file_name = f"gkd_media/{uuid.uuid4().hex}.{ext}"
            
        saved_path = default_storage.save(file_name, ContentFile(file_res.content))
        return default_storage.url(saved_path)
    except Exception as e:
        logger.error("[GKD] Error downloading media: %s", e)
        return ""

# ================= TEMPLATE WRAPPER FUNCTIONS =================

def tpl_gkd_main_menu(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_main_menu"))

def tpl_gkd_budget_menu(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_budget_menu"))

def tpl_gkd_b1_q1(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b1_q1"))

def tpl_gkd_b1_q2(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b1_q2"))

def tpl_gkd_b1_q3(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b1_q3"))

def tpl_gkd_b1_q4(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b1_q4"))

def tpl_gkd_b1_q5(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b1_q5"))

def tpl_gkd_b1_q5_upload(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b1_q5_upload"))

def tpl_gkd_b1_q5_invalid(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b1_q5_invalid"))

def tpl_gkd_b1_q7(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b1_q7"))

def tpl_gkd_b1_q8(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b1_q8"))

def tpl_gkd_b3_q1(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b3_q1"))

def tpl_gkd_b3_q2(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b3_q2"))

def tpl_gkd_b5_q1(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b5_q1"))

def tpl_gkd_b5_q2(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b5_q2"))

def tpl_gkd_b5_q3(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b5_q3"))

def tpl_gkd_b6_q1(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b6_q1"))

def tpl_gkd_b6_q2(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b6_q2"))

def tpl_gkd_b6_q3(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b6_q3"))

def tpl_gkd_b6_q4(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b6_q4"))

def tpl_gkd_b7_q1(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b7_q1"))

def tpl_gkd_b7_q2(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b7_q2"))

def tpl_gkd_b7_q3(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b7_q3"))

def tpl_gkd_b7_q4(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b7_q4"))

def tpl_gkd_b8_q1(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b8_q1"))

def tpl_gkd_b8_q2(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b8_q2"))

def tpl_gkd_b8_q3(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b8_q3"))

def tpl_gkd_b8_q3_wait(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b8_q3_wait"))

def tpl_gkd_b9_talk(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_b9_talk"))

def tpl_gkd_handoff(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_handoff"))

def tpl_gkd_showroom_visit(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_showroom_visit"))

def tpl_gkd_showroom_confirm(to: str, display_body: str):
    components = [
        {
            "type": "body",
            "parameters": [{"type": "text", "text": display_body}]
        }
    ]
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_showroom_confirm", components))

def tpl_gkd_closing_name(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_closing_name"))

def tpl_gkd_closing_area(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_closing_area"))

def tpl_gkd_closing_time(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_closing_time"))

def tpl_gkd_closing_portfolio(to: str, name: str):
    components = [
        {
            "type": "body",
            "parameters": [{"type": "text", "text": name}]
        }
    ]
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_closing_portfolio", components))

def tpl_gkd_portfolio_link(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_portfolio_link"))

def tpl_gkd_done(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_done"))

def tpl_gkd_invalid_option(to: str):
    return _meta_post_gkd(_gkd_template_payload(to, "gkd_invalid_option"))
