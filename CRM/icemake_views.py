import json
import logging
import re
from datetime import datetime
from django.conf import settings

from CRM.models import Customer, Conversation, Message, ClientAccount, ConversationState
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
import requests
import uuid
from .icemake_utils import (
    ICEMAKE_PHONE_NUMBER_ID, 
    send_icemake_text, 
    send_icemake_image,
    tpl_ice_support_welcome,
    tpl_ice_support_ask_city,
    tpl_ice_support_ask_state,
    tpl_ice_support_ask_pincode,
    tpl_ice_support_ask_number,
    tpl_ice_support_ask_issue,
    tpl_icemake_customer,
    tpl_icemake_serviceengineer,
    STATE_ENGINEER_MAPPING,
    tpl_registered_number_confirmation,
    tpl_icemake_complaint,
    tpl_other_complaint_type_
)



logger = logging.getLogger(__name__)

class ConversationSession:
    def __init__(self, conv):
        self._conv = conv

    def save(self):
        self._conv.save()

    @property
    def state(self):
        return self._conv.bot_state

    @state.setter
    def state(self, value):
        self._conv.bot_state = value

    def __getattr__(self, item):
        return self._conv.bot_metadata.get(item)

    def __setattr__(self, key, value):
        if key in ['_conv', 'state']:
            super().__setattr__(key, value)
        else:
            if not isinstance(self._conv.bot_metadata, dict):
                self._conv.bot_metadata = {}
            self._conv.bot_metadata[key] = value

def download_media_from_whatsapp(media_id: str, access_token: str) -> str:
    try:
        url = f"https://graph.facebook.com/v20.0/{media_id}"
        headers = {"Authorization": f"Bearer {access_token}"}
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code != 200:
            logger.error("[IceMake] Failed to get media URL for %s: %s", media_id, res.text)
            return ""
        media_url = res.json().get("url")
        if not media_url:
            return ""
        
        file_res = requests.get(media_url, headers=headers, timeout=15)
        if file_res.status_code != 200:
            logger.error("[IceMake] Failed to download media %s", media_id)
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
            "text/plain": "txt",
        }
        
        ext = ext_map.get(content_type)
        if not ext:
            ext = content_type.split("/")[-1]
            if not ext or len(ext) > 6 or not ext.isalnum():
                ext = "bin"
            
        file_name = f"icemake_media/{uuid.uuid4().hex}.{ext}"
        saved_path = default_storage.save(file_name, ContentFile(file_res.content))
        return default_storage.url(saved_path)
    except Exception as e:
        logger.error("[IceMake] Error downloading media: %s", e)
        return ""


def handle_icemake_message(msg: dict, contact: dict = None):
    number = msg.get("from", "")
    msg_id = msg.get("id", "")
    msg_type = msg.get("type", "text")
    
    profile_name = contact.get("profile", {}).get("name", "") if contact else ""

    customer_obj, created = Customer.objects.get_or_create(phone=number, defaults={'name': profile_name or number})
    if not created and profile_name and (customer_obj.name == number or not customer_obj.name or customer_obj.name.startswith("91")):
        customer_obj.name = profile_name
        customer_obj.save(update_fields=['name'])

    client_account_obj = ClientAccount.objects.filter(phone_number_id=ICEMAKE_PHONE_NUMBER_ID).first()

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
            icemake_phone_id = getattr(settings, 'ICEMAKE_PHONE_NUMBER_ID', ICEMAKE_PHONE_NUMBER_ID)
            client_account_obj = ClientAccount.objects.filter(phone_number_id=icemake_phone_id).first()
            token = client_account_obj.access_token if client_account_obj and client_account_obj.access_token else getattr(settings, "META_PERMANENT_TOKEN", "")
            dl_url = download_media_from_whatsapp(media_id, token)
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

    
    logger.info("[IceMake DEBUG] msg payload: %s", json.dumps(msg))
    logger.info("[IceMake] from=%s type=%s body=%r display=%r id=%s", number, msg_type, body, display_body, msg_id)

    conv_obj, conv_created = Conversation.objects.get_or_create(
        customer=customer_obj, 
        phone_number_id=ICEMAKE_PHONE_NUMBER_ID, 
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

    if not conv_obj.bot_state:
        conv_obj.bot_state = "INIT"
        conv_obj.save()

    session = ConversationSession(conv_obj)
    
    is_trigger = (session.state == "INIT") or bool(re.search(r'^(hi|hello|hey|menu)$', body.lower()))
    if is_trigger:
        session.state = "AWAITING_NAME"
        session.ticket_data = {}
        session.save()
        tpl_ice_support_welcome(number)
        return

    state = session.state
    text = body.strip()

    if state == "AWAITING_NAME":
        session.ticket_data = {"name": text}
        session.state = "AWAITING_STATE"
        session.save()
        tpl_ice_support_ask_state(number)
    
    elif state == "AWAITING_STATE":
        td = session.ticket_data or {}
        td["state"] = text
        session.ticket_data = td
        session.state = "AWAITING_CITY"
        session.save()
        tpl_ice_support_ask_city(number)
        
    elif state == "AWAITING_CITY":
        td = session.ticket_data or {}
        td["city"] = text
        session.ticket_data = td
        session.state = "AWAITING_PINCODE"
        session.save()
        tpl_ice_support_ask_pincode(number)
        
    elif state == "AWAITING_PINCODE":
        td = session.ticket_data or {}
        td["pincode"] = text
        session.ticket_data = td
        session.state = "AWAITING_NUMBER_INPUT"
        session.save()
        tpl_ice_support_ask_number(number)

    elif state == "AWAITING_NUMBER_INPUT":
        td = session.ticket_data or {}
        td["mobile"] = text
        session.ticket_data = td
        session.state = "AWAITING_CONFIRM_NUMBER"
        session.save()
        tpl_registered_number_confirmation(number, text)

    elif state == "AWAITING_CONFIRM_NUMBER":
        td = session.ticket_data or {}
        user_choice = display_body.strip().lower()
        if user_choice == "yes":
            session.state = "AWAITING_COMPLAINT"
            session.save()
            tpl_icemake_complaint(number)
        else:
            session.state = "AWAITING_NUMBER_INPUT"
            session.save()
            tpl_ice_support_ask_number(number)

    elif state == "AWAITING_COMPLAINT":
        td = session.ticket_data or {}
        selected_complaint = display_body.strip()
        
        if selected_complaint.lower() == "other":
            session.state = "AWAITING_OTHER_COMPLAINT_TYPE"
            session.save()
            tpl_other_complaint_type_(number)
        else:
            td["complaint_type"] = selected_complaint
            session.ticket_data = td
            session.state = "AWAITING_ISSUE"
            session.save()
            tpl_ice_support_ask_issue(number)
            
    elif state == "AWAITING_OTHER_COMPLAINT_TYPE":
        td = session.ticket_data or {}
        td["complaint_type"] = text
        session.ticket_data = td
        session.state = "AWAITING_ISSUE"
        session.save()
        tpl_ice_support_ask_issue(number)

    elif state == "AWAITING_ISSUE":
        td = session.ticket_data or {}
        td["issue_desc"] = text
        session.ticket_data = td
        
        # Generate ticket
        ticket_no = f"C{datetime.now().strftime('%m%d%H%M%S')}"
        
        # Find engineer
        user_state = td.get("state", "").lower().strip()
        
        # Default engineer if not found
        engineer_info = {"name": "Support Team", "phone": "919662933977"} # Fallback to Rutvik/Gujarat
        
        for state_key, e_info in STATE_ENGINEER_MAPPING.items():
            if state_key in user_state:
                engineer_info = e_info
                break
        
        # Notify customer (Meta doesn't allow empty strings for template params)
        tpl_icemake_customer(number, ticket_no or "-")
        
        registered_mobile = td.get("mobile", "").strip()
        if registered_mobile:
            reg_num_clean = "".join(filter(str.isdigit, registered_mobile))
            if len(reg_num_clean) == 10:
                reg_num_clean = "91" + reg_num_clean
            
            if reg_num_clean and reg_num_clean != number:
                try:
                    tpl_icemake_customer(reg_num_clean, ticket_no or "-")
                except Exception as e:
                    pass
        
        # Notify engineer
        city_state = f"{td.get('city', '')} / {td.get('state', '')}".strip(" /")
        address_str = f"{td.get('address', '')}, Pincode: {td.get('pincode', '')}".strip(" ,")
        issue_type = td.get("complaint_type", "General Issue")
        
        tpl_icemake_serviceengineer(
            to=engineer_info["phone"],
            ticket=ticket_no or "-",
            customer_name=td.get("name", "Customer") or "Customer",
            customer_mobile=td.get("mobile", number) or number,
            city_state=city_state or "-",
            issue_type=issue_type,
            description=td.get("issue_desc", "No description provided") or "No description provided",
            assigned_engineer=engineer_info["name"] or "Support Team"
        )
        
        session.state = "TICKET_GENERATED"
        session.save()
        
    else:
        # Fallback
        pass

    # --- Sync bot state to ConversationState for Leads/Prospects View (GKD pattern) ---
    org_obj = None
    if client_account_obj:
        org_obj = client_account_obj.tech_provider
    else:
        from CRM.models import WABAAccount
        waba = WABAAccount.objects.filter(phone_number_id=ICEMAKE_PHONE_NUMBER_ID).first()
        if waba:
            org_obj = waba.organization

    if org_obj:
        try:
            td = session.ticket_data or {}
            conv_state, created = ConversationState.objects.get_or_create(
                conversation=conv_obj,
                defaults={
                    "organization": org_obj,
                    "stage": "greeting",
                    "is_complete": False,
                    "collected_fields": {}
                }
            )
            if not created and conv_state.organization != org_obj:
                conv_state.organization = org_obj

            conv_state.stage = session.state or "greeting"
            conv_state.is_complete = (session.state == "TICKET_GENERATED")
            conv_state.collected_fields = {
                "Name": td.get("name", ""),
                "Mobile": td.get("mobile", ""),
                "City": td.get("city", ""),
                "State": td.get("state", ""),
                "Pincode": td.get("pincode", ""),
                "Complaint Type": td.get("complaint_type", ""),
                "Issue": td.get("issue_desc", ""),
            }
            conv_state.save()
        except Exception as e:
            logger.error("[IceMake] ConversationState sync error: %s", e)

