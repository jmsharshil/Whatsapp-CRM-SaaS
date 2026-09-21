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
        logger.info("[IceMake] Meta API success")
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

def tpl_ice_support_ask_product(to: str):
    return _meta_post_icemake(_icemake_template_payload(to, "ice_support_ask_product"))

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

def tpl_other_complaint_type(to: str):
    return _meta_post_icemake(_icemake_template_payload(to, "other_complaint_type"))

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

def tpl_icemake_serviceengineer(to: str, ticket: str, customer_name: str, customer_mobile: str, city_state: str, address: str, product_name: str, issue_type: str, description: str, assigned_engineer: str):
    components = [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": ticket},
                {"type": "text", "text": customer_name},
                {"type": "text", "text": customer_mobile},
                {"type": "text", "text": city_state},
                {"type": "text", "text": address},
                {"type": "text", "text": product_name},
                {"type": "text", "text": issue_type},
                {"type": "text", "text": description},
                {"type": "text", "text": assigned_engineer},
            ]
        }
    ]
    return _meta_post_icemake(_icemake_template_payload(to, "icemake_serviceengineer", components))

