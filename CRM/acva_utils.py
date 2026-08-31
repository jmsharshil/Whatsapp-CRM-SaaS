import json
import logging
import requests
import uuid
import csv
import io
from django.conf import settings
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from CRM.models import ClientAccount, Customer, Conversation, Message

logger = logging.getLogger(__name__)

ACVA_PHONE_NUMBER_ID = "104557535819026" # Actual ACVA phone number ID
META_SEND_URL = "https://graph.facebook.com/v20.0/{phone_id}/messages"

def _meta_post_acva(payload: dict) -> bool:
    token = getattr(settings, "META_PERMANENT_TOKEN", "")
    client_account = ClientAccount.objects.filter(phone_number_id=ACVA_PHONE_NUMBER_ID).first()
    if client_account and client_account.access_token:
        token = client_account.access_token
        
    url = META_SEND_URL.format(phone_id=ACVA_PHONE_NUMBER_ID)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        if r.status_code not in (200, 201):
            logger.error("[ACVA] Meta API error: %s - %s", r.status_code, r.text)
            return False
        logger.info("[ACVA] Meta API success")
        
        try:
            resp_data = r.json()
            meta_id = ""
            if "messages" in resp_data and len(resp_data["messages"]) > 0:
                meta_id = resp_data["messages"][0].get("id", "")
                
            to_number = payload.get("to", "")
            msg_type = payload.get("type", "text")
            
            content = "[Template]"
            if msg_type == "text":
                content = payload.get("text", {}).get("body", "")
            elif msg_type == "template":
                template_name = payload.get("template", {}).get("name", "")
                content = f"[Template: {template_name}]"
                
            customer, _ = Customer.objects.get_or_create(phone=to_number, defaults={"name": to_number})
            conv, _ = Conversation.objects.get_or_create(customer=customer, phone_number_id=ACVA_PHONE_NUMBER_ID, defaults={'client': client_account})
            
            Message.objects.create(
                conversation=conv,
                client=client_account,
                customer=customer,
                meta_message_id=meta_id,
                direction="outbound",
                message_type=msg_type,
                content=content,
                status="sent"
            )
        except Exception as ex:
            logger.exception("[ACVA] Error saving outbound message: %s", ex)
            
        return True
    except Exception as e:
        logger.exception("[ACVA] Request failed: %s", e)
        return False

def send_acva_text(to_number: str, text: str):
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "text",
        "text": {"preview_url": True, "body": text}
    }
    return _meta_post_acva(payload)

def send_acva_template(to_number: str, template_name: str, components: list = None):
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": "en"},
            "components": components or []
        }
    }
    return _meta_post_acva(payload)

def download_acva_media_from_whatsapp(media_id: str, access_token: str) -> str:
    try:
        url = f"https://graph.facebook.com/v20.0/{media_id}"
        headers = {"Authorization": f"Bearer {access_token}"}
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200:
            logger.error("[ACVA] Failed to get media URL for %s: %s", media_id, res.text)
            return ""
        media_url = res.json().get("url")
        if not media_url:
            return ""
        
        file_res = requests.get(media_url, headers=headers, timeout=15)
        if file_res.status_code != 200:
            logger.error("[ACVA] Failed to download media %s", media_id)
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
            
        file_name = f"acva_media/{uuid.uuid4().hex}.{ext}"
        saved_path = default_storage.save(file_name, ContentFile(file_res.content))
        return default_storage.url(saved_path)
    except Exception as e:
        logger.error("[ACVA] Error downloading media: %s", e)
        return ""

def fetch_member_data(identifier: str, required_columns: list) -> dict:
    url = "https://docs.google.com/spreadsheets/d/1Mu57Qlx9Bo1u2QOffSV7htLaUS29d6phr-WMxvgRo5Y/export?format=csv&gid=2028800605"
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            logger.error("[ACVA] Failed to fetch google sheet data")
            return None
        r.encoding = 'utf-8'
        reader = csv.DictReader(io.StringIO(r.text))
        if reader.fieldnames:
            reader.fieldnames = [f.strip() for f in reader.fieldnames]
        
        is_email = "@" in identifier
        for row in reader:
            match = False
            if is_email:
                if row.get("Email ID", "").strip().lower() == identifier.lower().strip():
                    match = True
            else:
                if row.get("Member ID", "").strip() == identifier.strip():
                    match = True
                    
            if match:
                res = {}
                for col in required_columns:
                    val = row.get(col.strip())
                    if not val or not str(val).strip():
                        val = "NA"
                    res[col] = str(val).strip()
                return res
        return None
    except Exception as e:
        logger.error(f"[ACVA] Error fetching member data: {e}")
        return None

# === Template Wrappers ===

def tpl_acva_main_menu(to_number: str):
    return send_acva_template(to_number, "acva_main_menu")

def tpl_acva_menu_learn(to_number: str):
    return send_acva_template(to_number, "acva_menu_learn")

def tpl_acva_opt1_learn(to_number: str):
    return send_acva_template(to_number, "acva_opt1_learn")

def tpl_acva_opt2_eligibility(to_number: str):
    return send_acva_template(to_number, "acva_opt2_eligibility")

def tpl_acva_opt2_eligibility_other(to_number: str):
    return send_acva_template(to_number, "acva_opt2_eligibility_other")

def tpl_acva_opt2_eligibility_res(to_number: str):
    return send_acva_template(to_number, "acva_opt2_eligibility_res")

def tpl_acva_opt3_admission(to_number: str):
    return send_acva_template(to_number, "acva_opt3_admission")

def tpl_acva_opt4_fees(to_number: str):
    return send_acva_template(to_number, "acva_opt4_fees")

def tpl_acva_opt5_curriculum(to_number: str):
    return send_acva_template(to_number, "acva_opt5_curriculum")

def tpl_acva_opt5_all(to_number: str):
    return send_acva_template(to_number, "acva_opt5_all")

def tpl_acva_opt6_call_name(to_number: str):
    return send_acva_template(to_number, "acva_opt6_call_name")

def tpl_acva_opt6_call_email(to_number: str):
    return send_acva_template(to_number, "acva_opt6_call_email")

def tpl_acva_opt6_call_number(to_number: str):
    return send_acva_template(to_number, "acva_opt6_call_number")

def tpl_acva_opt6_call_profession(to_number: str):
    return send_acva_template(to_number, "acva_opt6_call_profession")

def tpl_acva_opt6_call_city(to_number: str):
    return send_acva_template(to_number, "acva_opt6_call_city")

def tpl_acva_opt6_call_option(to_number: str):
    return send_acva_template(to_number, "acva_opt6_call_option")

def tpl_acva_opt6_call_time(to_number: str):
    return send_acva_template(to_number, "acva_opt6_call_time")

def tpl_acva_opt6_call_confirmtime(to_number: str):
    return send_acva_template(to_number, "acva_opt6_call_confirmtime")

def tpl_acva_opt7_support(to_number: str):
    return send_acva_template(to_number, "acva_opt7_support")

def tpl_acva_opt7_all(to_number: str):
    return send_acva_template(to_number, "acva_opt7_all")

def tpl_acva_opt8_speak(to_number: str):
    return send_acva_template(to_number, "acva_opt8_speak_")

def tpl_acva_handoff(to_number: str):
    return send_acva_template(to_number, "acva_handoff")

def tpl_key_benefits(to_number: str):
    return send_acva_template(to_number, "key_benefits")

def tpl_acva_res_membership(to_number: str, name: str, member_id: str, email: str, aff_date: str, exp_date: str):
    components = [{
        "type": "body",
        "parameters": [
            {"type": "text", "text": name},
            {"type": "text", "text": member_id},
            {"type": "text", "text": email},
            {"type": "text", "text": aff_date},
            {"type": "text", "text": exp_date}
        ]
    }]
    return send_acva_template(to_number, "acva_res_membership", components)

def tpl_acva_res_lms(to_number: str, name: str, member_id: str, email: str, aff_date: str, status: str):
    components = [{
        "type": "body",
        "parameters": [
            {"type": "text", "text": name},
            {"type": "text", "text": member_id},
            {"type": "text", "text": email},
            {"type": "text", "text": aff_date},
            {"type": "text", "text": status}
        ]
    }]
    return send_acva_template(to_number, "acva_res_lms", components)

def tpl_acva_res_exam(to_number: str, name: str, member_id: str, email: str, aff_date: str, status: str):
    components = [{
        "type": "body",
        "parameters": [
            {"type": "text", "text": name},
            {"type": "text", "text": member_id},
            {"type": "text", "text": email},
            {"type": "text", "text": aff_date},
            {"type": "text", "text": status}
        ]
    }]
    return send_acva_template(to_number, "acva_res_exam", components)

def tpl_acva_res_case_study(to_number: str, name: str, member_id: str, email: str, aff_date: str, status: str):
    components = [{
        "type": "body",
        "parameters": [
            {"type": "text", "text": name},
            {"type": "text", "text": member_id},
            {"type": "text", "text": email},
            {"type": "text", "text": aff_date},
            {"type": "text", "text": status}
        ]
    }]
    return send_acva_template(to_number, "acva_res_case_study", components)

def tpl_acva_res_due_date(to_number: str, name: str, member_id: str, email: str, aff_date: str, date: str):
    components = [{
        "type": "body",
        "parameters": [
            {"type": "text", "text": name},
            {"type": "text", "text": member_id},
            {"type": "text", "text": email},
            {"type": "text", "text": aff_date},
            {"type": "text", "text": date}
        ]
    }]
    return send_acva_template(to_number, "acva_res_due_date", components)

def tpl_acva_res_certificate(to_number: str, name: str, member_id: str, email: str, aff_date: str, status: str):
    components = [{
        "type": "body",
        "parameters": [
            {"type": "text", "text": name},
            {"type": "text", "text": member_id},
            {"type": "text", "text": email},
            {"type": "text", "text": aff_date},
            {"type": "text", "text": status}
        ]
    }]
    return send_acva_template(to_number, "acva_res_certificate", components)
