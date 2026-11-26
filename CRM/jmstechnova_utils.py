import json
import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

JMSTECHNOVA_PHONE_NUMBER_ID = "1472208769305154"
META_SEND_URL = "https://graph.facebook.com/v22.0/{phone_id}/messages"

def _meta_post_jmstechnova(payload: dict) -> bool:
    phone_id = JMSTECHNOVA_PHONE_NUMBER_ID
    if not phone_id:
        logger.error("[jmstechnova] JMSTECHNOVA_PHONE_NUMBER_ID is not set")
        return False
        
    token = getattr(settings, "META_PERMANENT_TOKEN", "")
    url = META_SEND_URL.format(phone_id=phone_id)
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        if r.status_code not in (200, 201):
            logger.error("[jmstechnova] Meta API error: %s - %s", r.status_code, r.text)
            return False
        logger.info("[jmstechnova] Meta API success: %s", r.text)
        return True
    except Exception as e:
        logger.error("[jmstechnova] Error sending message to Meta API: %s", e)
        return False

def send_jmstechnova_text(to: str, text: str) -> bool:
    return _meta_post_jmstechnova({
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": text
        }
    })
