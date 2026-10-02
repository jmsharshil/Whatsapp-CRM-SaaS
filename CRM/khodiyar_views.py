import json
import logging
import requests
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny

from CRM.models import Customer, Conversation, Message, ClientAccount
from .khodiyar_utils import (
    KHODIYAR_PHONE_NUMBER_ID, 
    download_khodiyar_media_from_whatsapp,
    send_khodiyar_template
)

logger = logging.getLogger(__name__)

def handle_khodiyar_message(msg: dict):
    number   = msg.get("from", "")
    msg_id   = msg.get("id", "")
    msg_type = msg.get("type", "text")

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
        if media_id:
            client_account_obj = ClientAccount.objects.filter(phone_number_id=KHODIYAR_PHONE_NUMBER_ID).first()
            token = client_account_obj.access_token if client_account_obj and client_account_obj.access_token else getattr(settings, "META_PERMANENT_TOKEN", "")
            dl_url = download_khodiyar_media_from_whatsapp(media_id, token)
            if dl_url:
                prefix = f"[{msg_type.upper()}]"
                body = f"{prefix} {dl_url}"
                display_body = body
            else:
                body = f"[{msg_type}]"
                display_body = f"[{msg_type}]"
        else:
            body = f"[{msg_type}]"
            display_body = f"[{msg_type}]"
            
    elif msg_type == "location":
        lat = msg.get("location", {}).get("latitude")
        lng = msg.get("location", {}).get("longitude")
        name = msg.get("location", {}).get("name", "")
        address = msg.get("location", {}).get("address", "")
        loc_str = []
        if name: loc_str.append(name)
        if address: loc_str.append(address)
        loc_str.append(f"https://maps.google.com/?q={lat},{lng}")
        
        body = f"📍 Location: " + " - ".join(loc_str)
        display_body = body

    elif msg_type == "contacts":
        contacts = msg.get("contacts", [])
        c_list = []
        for c in contacts:
            c_name = c.get("name", {}).get("formatted_name", "Unknown")
            phones = c.get("phones", [])
            c_phone = phones[0].get("phone", "") if phones else ""
            c_list.append(f"{c_name} ({c_phone})" if c_phone else c_name)
        
        body = "👤 Contact: " + ", ".join(c_list)
        display_body = body

    if not display_body:
        body = f"[{msg_type}] " + json.dumps(msg.get(msg_type, msg))
        display_body = body

    logger.info("[KHODIYAR DEBUG] msg payload: %s", json.dumps(msg))
    logger.info("[KHODIYAR] from=%s type=%s body=%r display_body=%r id=%s", number, msg_type, body, display_body, msg_id)

    customer_obj, _ = Customer.objects.get_or_create(phone=number, defaults={'name': number})
    client_account_obj = ClientAccount.objects.filter(phone_number_id=KHODIYAR_PHONE_NUMBER_ID).first()

    conv_obj, _ = Conversation.objects.get_or_create(
        customer=customer_obj, 
        phone_number_id=KHODIYAR_PHONE_NUMBER_ID, 
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


class KhodiyarSendBillReceiptView(APIView):
    permission_classes = [AllowAny]
    
    def post(self, request, *args, **kwargs):
        """
        Expects either:
        1. bill_id and base_url to fetch from the billing API:
           {"bill_id": "123", "base_url": "https://api.example.com"}
        2. Or direct data payload:
           {"data": {"phone": "...", "template_name": "...", "variables": [...]}}
        """
        bill_id = request.data.get('bill_id')
        base_url = request.data.get('base_url', 'http://127.0.0.1:8000').rstrip('/')
        
        data = request.data.get('data')
        
        if bill_id and base_url:
            url = f"{base_url}/api/billing/{bill_id}/whatsapp_data/"
            try:
                resp = requests.get(url, timeout=15)
                if resp.status_code == 200:
                    resp_json = resp.json()
                    if resp_json.get("success"):
                        data = resp_json.get("data")
            except Exception as e:
                logger.error("[KHODIYAR] Failed to fetch billing API data: %s", e)
                
        if data:
            phone = data.get("phone")
            template_name = data.get("template_name")
            variables = data.get("variables", [])
            
            components = []
            if variables:
                components = [{
                    "type": "body",
                    "parameters": [{"type": "text", "text": str(v)} for v in variables]
                }]
            
            success = send_khodiyar_template(phone, template_name, components)
            if success:
                return Response({"success": True, "message": "WhatsApp template sent successfully."})
            else:
                return Response({"success": False, "message": "Failed to send Meta template."}, status=500)
                
        return Response({"success": False, "message": "Invalid parameters or failed to fetch data."}, status=400)
