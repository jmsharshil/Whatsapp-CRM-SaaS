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
    "delhi": {"name": "Mr Manjit", "phone": "917874566064"},
    "haryana": {"name": "Mr Manjit", "phone": "917874566064"},
    "uttar pradesh": {"name": "Mr Manjit", "phone": "917874566064"},
    "up": {"name": "Mr Manjit", "phone": "917874566064"},
    "uttarakhand": {"name": "Mr Manjit", "phone": "917874566064"},
    "hp": {"name": "Mr Manjit", "phone": "917874566064"},
    "himachal": {"name": "Mr Manjit", "phone": "917874566064"},
    "punjab": {"name": "Mr Manjit", "phone": "917874566064"},
    "j & k": {"name": "Mr Manjit", "phone": "917874566064"},
    "jammu": {"name": "Mr Manjit", "phone": "917874566064"},
    "kashmir": {"name": "Mr Manjit", "phone": "917874566064"},
    
    # East
    "kolkatta": {"name": "Mr Mahesh", "phone": "918460332759"},
    "kolkata": {"name": "Mr Mahesh", "phone": "918460332759"},
    "assam": {"name": "Mr Mahesh", "phone": "918460332759"},
    "bihar": {"name": "Mr Mahesh", "phone": "918460332759"},
    "chattisgarh": {"name": "Mr Mahesh", "phone": "918460332759"},
    "orissa": {"name": "Mr Mahesh", "phone": "918460332759"},
    "odisha": {"name": "Mr Mahesh", "phone": "918460332759"},
    "jharkhand": {"name": "Mr Mahesh", "phone": "918460332759"},
    
    # West
    "maharashtra": {"name": "Mr Ashok", "phone": "918733004773"},
    "maharastra": {"name": "Mr Ashok", "phone": "918733004773"},
    "mh": {"name": "Mr Ashok", "phone": "918733004773"},
    "rajasthan": {"name": "Mr Ashok", "phone": "918733004773"},
    "madyapradesh": {"name": "Mr Ashok", "phone": "918733004773"},
    "madhya pradesh": {"name": "Mr Ashok", "phone": "918733004773"},
    "mp": {"name": "Mr Ashok", "phone": "918733004773"},
    "goa": {"name": "Mr Ashok", "phone": "918733004773"},
    
    # South
    "kerala": {"name": "Ms Vaidehi", "phone": "919427285653"},
    "tamil nadu": {"name": "Ms Vaidehi", "phone": "919427285653"},
    "tn": {"name": "Ms Vaidehi", "phone": "919427285653"},
    "telangana": {"name": "Ms Vaidehi", "phone": "919427285653"},
    "telungana": {"name": "Ms Vaidehi", "phone": "919427285653"},
    "karnataka": {"name": "Ms Vaidehi", "phone": "919427285653"},
    "andhra pradesh": {"name": "Ms Vaidehi", "phone": "919427285653"},
    "ap": {"name": "Ms Vaidehi", "phone": "919427285653"},
    
    # Gujarat
    "gujarat": {"name": "Mr Rutvik", "phone": "919662933977"},
    "gj": {"name": "Mr Rutvik", "phone": "919662933977"},
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
                customer_obj = Customer.objects.filter(phone=to_phone).first()
                if customer_obj:
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
    return _meta_post_icemake(_icemake_template_payload(to, "ice_support_welcome"))

def tpl_ice_support_ask_city(to: str):
    return _meta_post_icemake(_icemake_template_payload(to, "ice_support_ask_city"))

def tpl_ice_support_ask_state(to: str):
    return _meta_post_icemake(_icemake_template_payload(to, "ice_support_ask_state"))

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

def tpl_other_complaint_type_(to: str):
    return _meta_post_icemake(_icemake_template_payload(to, "other_complaint_type_"))

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
    return _meta_post_icemake(_icemake_template_payload(to, "icemake_customer", components))

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

