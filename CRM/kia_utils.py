import json
import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

kia_PHONE_NUMBER_ID = "1326499830547655"
META_SEND_URL = "https://graph.facebook.com/v20.0/{phone_id}/messages"

def _meta_post_kia(payload: dict) -> bool:
    phone_id = kia_PHONE_NUMBER_ID
    if not phone_id or phone_id == "YOUR_KIA_PHONE_ID_HERE":
        logger.error("[kia] kia_PHONE_NUMBER_ID is not set in code")
        return False
        
    token = settings.META_PERMANENT_TOKEN
    url = META_SEND_URL.format(phone_id=phone_id)
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        if r.status_code not in (200, 201):
            logger.error("[kia] Meta API error: %s - %s", r.status_code, r.text)
            return False
        logger.info("[kia] Meta API success: %s", r.text)
        
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
                
            from CRM.models import Customer, Conversation, Message, ClientAccount
            customer_obj, _ = Customer.objects.get_or_create(phone=to_number, defaults={'name': to_number})
            client_account_obj = ClientAccount.objects.filter(phone_number_id=phone_id).first()
            conv_obj, _ = Conversation.objects.get_or_create(
                customer=customer_obj, 
                phone_number_id=phone_id, 
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
            logger.error("[kia] Error saving outbound message: %s", e)

        return True
    except Exception as exc:
        logger.error("[kia] Meta API exception: %s", exc)
        return False

def send_kia_text(to: str, text: str) -> bool:
    return _meta_post_kia({
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": text
        }
    })
