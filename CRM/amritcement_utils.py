import json
import logging
import requests
import uuid
from django.conf import settings
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from CRM.models import ClientAccount, Customer, Conversation, Message

logger = logging.getLogger(__name__)

AMRITCEMENT_PHONE_NUMBER_ID = "1231612000038649"
META_SEND_URL = "https://graph.facebook.com/v20.0/{phone_id}/messages"

PRODUCT_MASTER = [
    {"id": "3318", "name": "OPC43-665", "code": "OPC43-665"},
    {"id": "3319", "name": "OPC43-NON MRP", "code": "OPC43-NON MRP"},
    {"id": "3320", "name": "OPC53-665", "code": "OPC53-665"},
    {"id": "3321", "name": "OPC53-NON MRP", "code": "OPC53-NON MRP"},
    {"id": "3322", "name": "PPC-650", "code": "PPC-650"},
    {"id": "3323", "name": "PPC-650 (HDPE)", "code": "PPC-650 (HDPE)"},
    {"id": "3324", "name": "PPC-NON MRP", "code": "PPC-NON MRP"},
    {"id": "4040", "name": "PPC-850 (HDPE)", "code": "PPC-850 (HDPE)"},
    {"id": "4053", "name": "PPC-850", "code": "PPC-850"},
]

def _meta_post_amritcement(payload: dict) -> bool:
    token = getattr(settings, "META_PERMANENT_TOKEN", "")
    client_account = ClientAccount.objects.filter(phone_number_id=AMRITCEMENT_PHONE_NUMBER_ID).first()
    if client_account and client_account.access_token:
        token = client_account.access_token
        
    url = META_SEND_URL.format(phone_id=AMRITCEMENT_PHONE_NUMBER_ID)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        if r.status_code not in (200, 201):
            logger.error("[Amritcement] Meta API error: %s - %s", r.status_code, r.text)
            return False
        
        try:
            resp_data = r.json()
            meta_id = ""
            if "messages" in resp_data and len(resp_data["messages"]) > 0:
                meta_id = resp_data["messages"][0].get("id", "")
                
            to_number = payload.get("to", "")
            msg_type = payload.get("type", "text")
            
            content = "[Interactive/Template]"
            if msg_type == "text":
                content = payload.get("text", {}).get("body", "")
            elif msg_type == "interactive":
                content = "[Interactive Menu]"
                
            customer, _ = Customer.objects.get_or_create(phone=to_number, defaults={"name": to_number})
            conv, _ = Conversation.objects.get_or_create(customer=customer, phone_number_id=AMRITCEMENT_PHONE_NUMBER_ID, defaults={'client': client_account})
            
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
            logger.exception("[Amritcement] Error saving outbound message: %s", ex)
            
        return True
    except Exception as e:
        logger.exception("[Amritcement] Request failed: %s", e)
        return False

def send_amritcement_text(to_number: str, text: str):
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "text",
        "text": {"preview_url": True, "body": text}
    }
    return _meta_post_amritcement(payload)

def send_amritcement_interactive(to_number: str, interactive_data: dict):
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "interactive",
        "interactive": interactive_data
    }
    return _meta_post_amritcement(payload)

def send_amritcement_template(to_number: str, template_name: str, language_code: str = "en", components: list = None):
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {
                "code": language_code
            }
        }
    }
    if components:
        payload["template"]["components"] = components
    return _meta_post_amritcement(payload)

def download_meta_media(media_id: str) -> str:
    token = getattr(settings, "META_PERMANENT_TOKEN", "")
    client_account = ClientAccount.objects.filter(phone_number_id=AMRITCEMENT_PHONE_NUMBER_ID).first()
    if client_account and client_account.access_token:
        token = client_account.access_token
        
    url = f"https://graph.facebook.com/v20.0/{media_id}"
    headers = {"Authorization": f"Bearer {token}"}
    
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        media_url = r.json().get("url")
        
        if media_url:
            media_r = requests.get(media_url, headers=headers, timeout=15)
            media_r.raise_for_status()
            
            ext = "jpg"
            if "image/png" in media_r.headers.get("Content-Type", ""):
                ext = "png"
                
            filename = f"claims/{uuid.uuid4().hex}.{ext}"
            file_path = default_storage.save(filename, ContentFile(media_r.content))
            
            # Construct URL
            file_url = default_storage.url(file_path)
            
            # If the storage URL is relative (local dev), prepend the domain
            if file_url.startswith('/'):
                domain = getattr(settings, "DOMAIN_URL", "http://127.0.0.1:8000").rstrip('/')
                image_url = f"{domain}{file_url}"
            else:
                image_url = file_url
                
            return image_url
    except Exception as e:
        logger.exception("[Amritcement] Failed to download media %s: %s", media_id, e)
    return ""


# --- external APIs ---

def amritcement_create_order(payload: dict, is_dealer: bool = True) -> dict:
    if is_dealer:
        # Purchase Order API for Dealer
        url = "https://supershop.bigbanginnovations.in/apilive/order/createOrder" 
    else:
        # Create Sales Order API for ASD
        url = "https://supershop.bigbanginnovations.in/apilive/order/createSalesOrder" # Replace with exact ASD API url when known
        
    headers = {}
    try:
        r = requests.post(url, data=payload, headers=headers, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.exception("[Amritcement] createOrder API failed: %s", e)
        return {"status": "0", "message": str(e)}

def amritcement_get_ledger(customer_code: str) -> dict:
    url = "https://supershop.bigbanginnovations.in/apilive/apis/getCustomerLedger"
    payload = {"customer_code": customer_code}
    try:
        r = requests.post(url, data=payload, timeout=15)
        r.raise_for_status()
        try:
            return r.json()
        except Exception as json_err:
            logger.error("[Amritcement] getCustomerLedger returned non-JSON. Status: %s, Text: '%s'", r.status_code, r.text)
            return None
    except Exception as e:
        logger.exception("[Amritcement] getCustomerLedger API request failed: %s", e)
        return None

def amritcement_add_claim_submission(payload: dict) -> dict:
    url = "https://supershop.bigbanginnovations.in/apilive/apis/addClaimSubmission"
    try:
        r = requests.post(url, data=payload, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.exception("[Amritcement] addClaimSubmission API failed: %s", e)
        return {"status": "0", "message": str(e)}

def amritcement_get_destinations(dealer_code: str) -> list:
    # Mock destination API
    return [
        {"code": "DEST001", "description": "Guwahati Hub"},
        {"code": "DEST002", "description": "Shillong Hub"}
    ]

def amritcement_verify_mobile_no(mobile_no: str) -> dict:
    # Strip the 91 country code if the number is 12 digits long
    if mobile_no.isdigit() and len(mobile_no) == 12 and mobile_no.startswith("91"):
        mobile_no = mobile_no[2:]

    url = "https://supersales.bigbanginnovations.in/development/Api/verifyMobileNo"
    payload = {"mobile_no": mobile_no}
    try:
        r = requests.post(url, data=payload, timeout=15)
        
        if r.status_code == 503:
            return {"success": False, "error_type": "SERVICE_UNAVAILABLE", "message": "Service Unavailable"}
            
        r.raise_for_status()
        
        try:
            resp_json = r.json()
            if not resp_json:
                return {"success": False, "error_type": "INVALID_RESPONSE", "message": "Empty response"}
                
            if resp_json.get("success") or str(resp_json.get("status", "")) == "1" or resp_json.get("customer_code"):
                # Handle missing fields in what appears to be a successful response
                customer_type = resp_json.get("data", {}).get("customer_type") or resp_json.get("customer_type")
                customer_code = resp_json.get("data", {}).get("customer_code") or resp_json.get("customer_code")
                
                if not customer_type:
                    logger.error("[Amritcement] verifyMobileNo API missing customer_type for %s", mobile_no)
                    return {"success": False, "error_type": "MISSING_CUSTOMER_TYPE", "message": "Missing Customer Type"}
                if not customer_code:
                    logger.error("[Amritcement] verifyMobileNo API missing customer_code for %s", mobile_no)
                    return {"success": False, "error_type": "MISSING_CUSTOMER_CODE", "message": "Missing Customer Code"}
                    
                return {"success": True, "data": resp_json.get("data", resp_json)}
            else:
                return {"success": False, "error_type": "NOT_REGISTERED", "message": "Not Registered"}

        except ValueError:
            logger.error("[Amritcement] verifyMobileNo API returned non-JSON. Status: %s, Text: '%s'", r.status_code, r.text)
            return {"success": False, "error_type": "INVALID_RESPONSE", "message": "Invalid API Response"}
            
    except requests.exceptions.Timeout:
        logger.exception("[Amritcement] verifyMobileNo API timeout")
        return {"success": False, "error_type": "TIMEOUT", "message": "Request Timeout"}
    except Exception as e:
        logger.exception("[Amritcement] verifyMobileNo API failed: %s", e)
        return {"success": False, "error_type": "API_FAILURE", "message": str(e)}
