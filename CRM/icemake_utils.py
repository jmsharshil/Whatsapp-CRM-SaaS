import json
import logging
import os
import requests
import uuid
from django.conf import settings
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile

STATE_ENGINEER_MAPPING = {
    # North
    "delhi": {"name": "Mr Manjit", "phone": "919998857391"},
    "delhi ncr": {"name": "Mr Manjit", "phone": "919998857391"},
    "haryana": {"name": "Mr Manjit", "phone": "919998857391"},
    "uttar pradesh": {"name": "Mr Manjit", "phone": "919998857391"},
    "up": {"name": "Mr Manjit", "phone": "919998857391"},
    "uttarakhand": {"name": "Mr Manjit", "phone": "919998857391"},
    "hp": {"name": "Mr Manjit", "phone": "919998857391"},
    "himachal": {"name": "Mr Manjit", "phone": "919998857391"},
    "himachal pradesh": {"name": "Mr Manjit", "phone": "919998857391"},
    "punjab": {"name": "Mr Manjit", "phone": "919998857391"},
    "j & k": {"name": "Mr Manjit", "phone": "919998857391"},
    "jammu": {"name": "Mr Manjit", "phone": "919998857391"},
    "kashmir": {"name": "Mr Manjit", "phone": "919998857391"},
    "jammu & kashmir": {"name": "Mr Manjit", "phone": "919998857391"},
    "jammu and kashmir": {"name": "Mr Manjit", "phone": "919998857391"},
    "chandigarh": {"name": "Mr Manjit", "phone": "919998857391"},
    
    # East
    "kolkatta": {"name": "Mr Mahesh", "phone": "919512037115"},
    "kolkata": {"name": "Mr Mahesh", "phone": "919512037115"},
    "assam": {"name": "Mr Mahesh", "phone": "919512037115"},
    "bihar": {"name": "Mr Mahesh", "phone": "919512037115"},
    "chattisgarh": {"name": "Mr Mahesh", "phone": "919512037115"},
    "chhattisgarh": {"name": "Mr Mahesh", "phone": "919512037115"},
    "orissa": {"name": "Mr Mahesh", "phone": "919512037115"},
    "odisha": {"name": "Mr Mahesh", "phone": "919512037115"},
    "jharkhand": {"name": "Mr Mahesh", "phone": "919512037115"},
    "west bengal": {"name": "Mr Mahesh", "phone": "919512037115"},
    "arunachal pradesh": {"name": "Mr Mahesh", "phone": "919512037115"},
    "manipur": {"name": "Mr Mahesh", "phone": "919512037115"},
    "meghalaya": {"name": "Mr Mahesh", "phone": "919512037115"},
    "mizoram": {"name": "Mr Mahesh", "phone": "919512037115"},
    "nagaland": {"name": "Mr Mahesh", "phone": "919512037115"},
    "sikkim": {"name": "Mr Mahesh", "phone": "919512037115"},
    "tripura": {"name": "Mr Mahesh", "phone": "919512037115"},
        
    
    # West
    "maharashtra": {"name": "Mr Ashok", "phone": "919998701129"},
    "maharastra": {"name": "Mr Ashok", "phone": "919998701129"},
    "mh": {"name": "Mr Ashok", "phone": "919998701129"},
    "rajasthan": {"name": "Mr Ashok", "phone": "919998701129"},
    "madyapradesh": {"name": "Mr Ashok", "phone": "919998701129"},
    "madhya pradesh": {"name": "Mr Ashok", "phone": "919998701129"},
    "mp": {"name": "Mr Ashok", "phone": "919998701129"},
    "goa": {"name": "Mr Ashok", "phone": "919998701129"},
    
    # South
    "kerala": {"name": "Ms Vaidehi", "phone": "919512015593"},
    "tamil nadu": {"name": "Ms Vaidehi", "phone": "919512015593"},
    "tn": {"name": "Ms Vaidehi", "phone": "919512015593"},
    "telangana": {"name": "Ms Vaidehi", "phone": "919512015593"},
    "telungana": {"name": "Ms Vaidehi", "phone": "919512015593"},
    "karnataka": {"name": "Ms Vaidehi", "phone": "919512015593"},
    "andhra pradesh": {"name": "Ms Vaidehi", "phone": "919512015593"},
    "ap": {"name": "Ms Vaidehi", "phone": "919512015593"},
    
    # Gujarat
    "gujarat": {"name": "Mr Rutvik", "phone": "919727721447"},
    "gj": {"name": "Mr Rutvik", "phone": "919727721447"},
}

logger = logging.getLogger(__name__)

ICEMAKE_PHONE_NUMBER_ID = "1272077585997381"
META_SEND_URL = "https://graph.facebook.com/v22.0/{phone_id}/messages"

from CRM.models import Customer, Conversation, ClientAccount, Message

def _meta_post_icemake(payload: dict) -> bool:
    token = getattr(settings, "META_PERMANENT_TOKEN", "")
    url = META_SEND_URL.format(phone_id=ICEMAKE_PHONE_NUMBER_ID)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        
        # DEBUG LOGGING FOR META API
        try:
            with open(os.path.join(settings.BASE_DIR, "meta_debug.log"), "a", encoding="utf-8") as f:
                f.write(f"PAYLOAD: {json.dumps(payload)}\n")
                f.write(f"RESPONSE [{r.status_code}]: {r.text}\n\n")
        except Exception:
            pass

        if r.status_code not in (200, 201):
            logger.error("[IceMake] Meta API error: %s - %s", r.status_code, r.text)
            return False
            
        res_data = r.json()
        meta_msg_id = ""
        if "messages" in res_data and len(res_data["messages"]) > 0:
            meta_msg_id = res_data["messages"][0].get("id", "")
            
        logger.info("[IceMake] Meta API success")
        
        # Save outbound message to DB
        try:
            to_phone = payload.get("to")
            if to_phone:
                customer_obj, _ = Customer.objects.get_or_create(phone=to_phone, defaults={'name': to_phone})
                client_account_obj = ClientAccount.objects.filter(phone_number_id=ICEMAKE_PHONE_NUMBER_ID).first()
                conv_obj, _ = Conversation.objects.get_or_create(
                    customer=customer_obj,
                    phone_number_id=ICEMAKE_PHONE_NUMBER_ID,
                    defaults={'client': client_account_obj}
                )
                
                msg_type = payload.get("type", "text")
                content = ""
                if msg_type == "text":
                    content = payload.get("text", {}).get("body", "")
                elif msg_type == "template":
                    tpl_name = payload.get("template", {}).get("name", "")
                    components = payload.get("template", {}).get("components", [])
                    content = f"[Template: {tpl_name}]"
                    try:
                        from CRM.models import Template
                        template_obj = Template.objects.filter(name=tpl_name).first()
                        if template_obj and template_obj.body_text:
                            text = template_obj.body_text
                            for comp in components:
                                if comp.get("type") == "body":
                                    params = comp.get("parameters", [])
                                    for i, param in enumerate(params, start=1):
                                        val = param.get("text", "")
                                        text = text.replace(f"{{{{{i}}}}}", str(val))
                            header = template_obj.header_text + "\n" if template_obj.header_text else ""
                            content = f"{header}{text}\n\nTEMPLATE: {tpl_name.upper()}".strip()
                    except Exception as e:
                        logger.error("[IceMake] Template resolution error: %s", e)
                elif msg_type == "image":
                    content = f"[Image] {payload.get('image', {}).get('link', '')}"
                
                Message.objects.create(
                    conversation=conv_obj,
                    client=client_account_obj,
                    customer=customer_obj,
                    meta_message_id=meta_msg_id,
                    direction="outbound",
                    message_type=msg_type,
                    content=content,
                    status="sent"
                )
        except Exception as db_exc:
            logger.error("[IceMake] Failed to save outbound message: %s", db_exc)

        return True
    except Exception as exc:
        logger.error("[IceMake] Meta API exception: %s", exc)
        return False

def send_icemake_text(number: str, text: str):
    return _meta_post_icemake({
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": number,
        "type": "text",
        "text": {"preview_url": False, "body": text}
    })

def send_icemake_image(to: str, url: str, caption: str = ""):
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
    return _meta_post_icemake(payload)

def _icemake_template_payload(to: str, name: str, components: list = None) -> dict:
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

def tpl_ice_support_welcome(to: str):
    components = [
        {
            "type": "header",
            "parameters": [
                {
                    "type": "image",
                    "image": {
                        "link": "https://metacrm.blob.core.windows.net/media/icemake_media/icemake.jpeg"
                    }
                }
            ]
        }
    ]
    return _meta_post_icemake(_icemake_template_payload(to, "ice_support_welcome", components))

def tpl_icemake_ask_name(to: str):
    return _meta_post_icemake(_icemake_template_payload(to, "icemake_ask_name"))

def tpl_ice_support_ask_city(to: str):
    return _meta_post_icemake(_icemake_template_payload(to, "ice_support_ask_city"))

def tpl_ice_support_ask_state(to: str):
    return _meta_post_icemake(_icemake_template_payload(to, "ice_support_ask_state"))

def tpl_icemake_state_1(to: str):
    return _meta_post_icemake(_icemake_template_payload(to, "icemake_state_1"))

def tpl_icemake_state_2(to: str):
    return _meta_post_icemake(_icemake_template_payload(to, "icemake_state_2"))

def tpl_icemake_state_3(to: str):
    return _meta_post_icemake(_icemake_template_payload(to, "icemake_state_3"))

def tpl_ice_support_ask_pincode(to: str):
    return _meta_post_icemake(_icemake_template_payload(to, "ice_support_ask_pincode"))

def tpl_ice_ask_address(to: str):
    return _meta_post_icemake(_icemake_template_payload(to, "ice_ask_address"))

def tpl_ice_support_ask_number(to: str):
    return _meta_post_icemake(_icemake_template_payload(to, "ice_support_ask_number"))


def tpl_registered_number_confirmation(to: str, number: str):
    components = [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": number}
            ]
        }
    ]
    return _meta_post_icemake(_icemake_template_payload(to, "registered_number_confirmation", components))

def tpl_icemake_complaint(to: str):
    return _meta_post_icemake(_icemake_template_payload(to, "icemake_complaint"))


def tpl_ice_support_ask_issue(to: str):
    return _meta_post_icemake(_icemake_template_payload(to, "ice_support_ask_issue"))

def tpl_icemake_customer(to: str, ticket_no: str):
    components = [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": ticket_no}
            ]
        }
    ]
    return _meta_post_icemake(_icemake_template_payload(to, "icemake_customer_", components))

def tpl_icemake_serviceengineer(to: str, ticket: str, customer_name: str, customer_mobile: str, city_state: str, issue_type: str, description: str, assigned_engineer: str):
    components = [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": ticket},
                {"type": "text", "text": customer_name},
                {"type": "text", "text": customer_mobile},
                {"type": "text", "text": city_state},
                {"type": "text", "text": issue_type},
                {"type": "text", "text": description},
                {"type": "text", "text": assigned_engineer},
            ]
        }
    ]
    return _meta_post_icemake(_icemake_template_payload(to, "icemake_serviceengineer", components))

