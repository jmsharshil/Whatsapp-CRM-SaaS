import json
import logging
import requests
import uuid
from django.conf import settings
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from CRM.models import ClientAccount, Customer, Conversation, Message

logger = logging.getLogger(__name__)

KHODIYAR_PHONE_NUMBER_ID = "1138658179323225"
META_SEND_URL = "https://graph.facebook.com/v20.0/{phone_id}/messages"

def _meta_post_khodiyar(payload: dict) -> bool:
    token = getattr(settings, "META_PERMANENT_TOKEN", "")
    client_account = ClientAccount.objects.filter(phone_number_id=KHODIYAR_PHONE_NUMBER_ID).first()
    if client_account and client_account.access_token:
        token = client_account.access_token
        
    url = META_SEND_URL.format(phone_id=KHODIYAR_PHONE_NUMBER_ID)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        if r.status_code not in (200, 201):
            logger.error("[KHODIYAR] Meta API error: %s - %s", r.status_code, r.text)
            return False
        logger.info("[KHODIYAR] Meta API success")
        
        try:
            resp_data = r.json()
            meta_id = ""
            if "messages" in resp_data and len(resp_data["messages"]) > 0:
                meta_id = resp_data["messages"][0].get("id", "")
                
            to_number = payload.get("to", "")
            msg_type = payload.get("type", "text")
            
            content = "[Template]"
            template_name = ""
            if msg_type == "text":
                content = payload.get("text", {}).get("body", "")
            elif msg_type == "template":
                template_name = payload.get("template", {}).get("name", "")
                content = f"[Template: {template_name}]"
                
            customer, _ = Customer.objects.get_or_create(phone=to_number, defaults={"name": to_number})
            conv, _ = Conversation.objects.get_or_create(customer=customer, phone_number_id=KHODIYAR_PHONE_NUMBER_ID, defaults={'client': client_account})
            
            Message.objects.create(
                conversation=conv,
                client=client_account,
                customer=customer,
                meta_message_id=meta_id,
                direction="outbound",
                message_type=msg_type,
                template_name=template_name,
                content=content,
                status="sent"
            )
        except Exception as ex:
            logger.exception("[KHODIYAR] Error saving outbound message: %s", ex)
            
        return True
    except Exception as e:
        logger.exception("[KHODIYAR] Request failed: %s", e)
        return False

def send_khodiyar_text(to_number: str, text: str):
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "text",
        "text": {"preview_url": True, "body": text}
    }
    return _meta_post_khodiyar(payload)

def send_khodiyar_template(to_number: str, template_name: str, components: list = None):
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
    return _meta_post_khodiyar(payload)

def download_khodiyar_media_from_whatsapp(media_id: str, access_token: str) -> str:
    try:
        url = f"https://graph.facebook.com/v20.0/{media_id}"
        headers = {"Authorization": f"Bearer {access_token}"}
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200:
            logger.error("[KHODIYAR] Failed to get media URL for %s: %s", media_id, res.text)
            return ""
        media_url = res.json().get("url")
        if not media_url:
            return ""
        
        file_res = requests.get(media_url, headers=headers, timeout=15)
        if file_res.status_code != 200:
            logger.error("[KHODIYAR] Failed to download media %s", media_id)
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
            
        file_name = f"khodiyar_media/{uuid.uuid4().hex}.{ext}"
        saved_path = default_storage.save(file_name, ContentFile(file_res.content))
        return default_storage.url(saved_path)
    except Exception as e:
        logger.error("[KHODIYAR] Error downloading media: %s", e)
        return ""

def tpl_bill_invoice_receipt(
    to_number: str, 
    customer_name: str, 
    store_name: str, 
    bill_no: str, 
    date_time: str, 
    branch: str, 
    cashier: str, 
    customer: str, 
    phone: str, 
    items_list: str, 
    subtotal: str, 
    tax: str, 
    round_off: str, 
    grand_total: str, 
    paid_by: str
):
    """
    Sends the bill_invoice_receipt template.
    Template Variables:
    {{1}} Customer Name
    {{2}} Store Name
    {{3}} Bill No
    {{4}} Date Time
    {{5}} Branch
    {{6}} Cashier
    {{7}} Customer
    {{8}} Phone
    {{9}} Items List
    {{10}} Subtotal
    {{11}} Tax (GST)
    {{12}} Round Off
    {{13}} Grand Total
    {{14}} Paid By
    """
    components = [{
        "type": "body",
        "parameters": [
            {"type": "text", "text": str(customer_name)},
            {"type": "text", "text": str(store_name)},
            {"type": "text", "text": str(bill_no)},
            {"type": "text", "text": str(date_time)},
            {"type": "text", "text": str(branch)},
            {"type": "text", "text": str(cashier)},
            {"type": "text", "text": str(customer)},
            {"type": "text", "text": str(phone)},
            {"type": "text", "text": str(items_list)},
            {"type": "text", "text": str(subtotal)},
            {"type": "text", "text": str(tax)},
            {"type": "text", "text": str(round_off)},
            {"type": "text", "text": str(grand_total)},
            {"type": "text", "text": str(paid_by)}
        ]
    }]
    return send_khodiyar_template(to_number, "bill_invoice_receipt", components)

