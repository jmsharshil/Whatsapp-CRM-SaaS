import json
import logging
import re
from CRM.models import Customer, Conversation, Message, ClientAccount, ConversationState
from CRM.acva_utils import (
    ACVA_PHONE_NUMBER_ID, send_acva_text, download_acva_media_from_whatsapp,
    tpl_acva_main_menu, tpl_acva_opt1_learn, tpl_acva_menu_learn, tpl_acva_opt2_eligibility,
    tpl_acva_opt2_eligibility_res, tpl_acva_opt3_admission, tpl_acva_opt4_fees,
    tpl_acva_opt5_curriculum, tpl_acva_opt7_support,
    tpl_acva_opt8_speak, tpl_acva_handoff, tpl_key_benefits,
    tpl_acva_opt2_eligibility_other, tpl_acva_opt5_all,
    tpl_acva_opt6_call_name, tpl_acva_opt6_call_email,
    tpl_acva_opt6_call_number, tpl_acva_opt6_call_profession,
    tpl_acva_opt6_call_city, tpl_acva_opt6_call_option,
    tpl_acva_opt6_call_time, tpl_acva_opt6_call_confirmtime,
    tpl_acva_opt7_all
)
from django.conf import settings

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

def handle_acva_message(msg: dict):
    try:
        _handle_acva_message_internal(msg)
    except Exception as e:
        logger.exception("[ACVA] Error processing message: %s", e)

def _handle_acva_message_internal(msg: dict):
    number = msg.get("from", "")
    msg_id = msg.get("id", "")
    msg_type = msg.get("type", "text")

    customer_obj, _ = Customer.objects.get_or_create(phone=number, defaults={'name': number})
    client_account_obj = ClientAccount.objects.filter(phone_number_id=ACVA_PHONE_NUMBER_ID).first()

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
        media_info = msg.get(msg_type, {})
        media_id = media_info.get("id")
        caption = media_info.get("caption", "").strip()
        if media_id:
            token = client_account_obj.access_token if client_account_obj and client_account_obj.access_token else getattr(settings, "META_PERMANENT_TOKEN", "")
            dl_url = download_acva_media_from_whatsapp(media_id, token)
            if dl_url:
                prefix = f"[{msg_type.upper()}]"
                body = f"{prefix} {dl_url}"
                if caption:
                    body += f"\n{caption}"
                display_body = body
            else:
                body = f"[{msg_type}]" + (f"\n{caption}" if caption else "")
                display_body = body
        else:
            body = f"[{msg_type}]" + (f"\n{caption}" if caption else "")
            display_body = body
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

    print(f"========== ACVA MESSAGE RECVD: number={number} body={body} msg_type={msg_type} ==========")
    logger.info("[ACVA] from=%s type=%s body=%r display=%r id=%s", number, msg_type, body, display_body, msg_id)

    conv_obj, conv_created = Conversation.objects.get_or_create(
        customer=customer_obj, 
        phone_number_id=ACVA_PHONE_NUMBER_ID, 
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
    
    is_trigger = bool(re.search(r'^(hi|hello|hey|menu)$', body.lower()))
    if is_trigger:
        session.state = "INIT"
        session.collected_info = {}
        session.save()
        
    state = session.state
    body_str = body.lower()
    display_str = display_body.lower()
    
    def goto_handoff():
        session.state = "COLLECT_NAME"
        session.save()
        tpl_acva_opt6_call_name(number)

    # Flow Logic
    if state == "INIT":
        tpl_acva_main_menu(number)
        session.state = "MENU_SELECTION"
        session.save()

    elif state == "MENU_SELECTION":
        session.collected_info = {}
        if "1" in body_str or "learn" in display_str:
            session.state = "MENU_SELECTION"
            session.save()
            tpl_acva_menu_learn(number)
        elif "2" in body_str or "eligibility" in display_str:
            session.state = "OPT2_ELIGIBILITY"
            session.save()
            tpl_acva_opt2_eligibility(number)
        elif "3" in body_str or "admission" in display_str:
            session.state = "OPT3_ADMISSION"
            session.save()
            tpl_acva_opt3_admission(number)
        elif "4" in body_str or "fees" in display_str:
            session.state = "OPT4_FEES"
            session.save()
            tpl_acva_opt4_fees(number)
        elif "5" in body_str or "curriculum" in display_str:
            session.state = "OPT5_CURRICULUM"
            session.save()
            tpl_acva_opt5_curriculum(number)
        elif "6" in body_str or "counselling" in display_str:
            goto_handoff()
        elif "7" in body_str or "support" in display_str or "existing" in display_str:
            session.state = "OPT7_SUPPORT"
            session.save()
            tpl_acva_opt7_support(number)
        elif "all options" in display_str or "options" in display_str:
            session.state = "OPT1_LEARN"
            session.save()
            tpl_acva_opt1_learn(number)
        elif "8" in body_str or "speak" in display_str:
            session.state = "OPT8_SPEAK"
            session.save()
            tpl_acva_opt8_speak(number)
        else:
            send_acva_text(number, "Invalid option. Please reply with 'Hi' or 'Menu' to start again.")

    elif state == "OPT1_LEARN":
        if "benefits" in display_str or "1" in body_str:
            session.state = "OPT1_LEARN"
            session.save()
            tpl_key_benefits(number)
        elif "menu" in display_str or "back" in display_str or "3" in body_str:
            session.state = "MENU_SELECTION"
            session.save()
            tpl_acva_main_menu(number)
        else:
            session.state = "MENU_SELECTION"
            session.save()
            tpl_acva_main_menu(number)

    elif state == "OPT2_ELIGIBILITY":
        if "other" in display_str or "8" in body_str:
            session.state = "OPT2_ELIGIBILITY_OTHER"
            session.save()
            tpl_acva_opt2_eligibility_other(number)
        else:
            session.collected_info = {**session.collected_info, "Profession": display_body}
            session.state = "OPT2_ELIGIBILITY_RES"
            session.save()
            tpl_acva_opt2_eligibility_res(number)

    elif state == "OPT2_ELIGIBILITY_OTHER":
        session.collected_info = {**session.collected_info, "Profession": display_body}
        session.state = "OPT2_ELIGIBILITY_RES"
        session.save()
        tpl_acva_opt2_eligibility_res(number)

    elif state == "OPT2_ELIGIBILITY_RES" or state == "OPT3_ADMISSION" or state == "OPT4_FEES":
        if "counsellor" in display_str or "team" in display_str:
            goto_handoff()
        else:
            session.state = "MENU_SELECTION"
            session.save()
            tpl_acva_main_menu(number)

    elif state == "OPT5_CURRICULUM":
        if "1" in body_str or "pattern" in display_str:
            session.state = "OPT5_EXAM"
            session.save()
            tpl_acva_opt5_all(number)
        else:
            # Fallback or answer other FAQs
            send_acva_text(number, "For more details, please request a counselling call or reply 'Menu'.")

    elif state == "OPT5_EXAM":
        if "team" in display_str:
            goto_handoff()
        else:
            session.state = "MENU_SELECTION"
            session.save()
            tpl_acva_main_menu(number)

    elif state == "OPT7_SUPPORT":
        if "speak" in display_str or "team" in display_str:
            session.state = "OPT8_SPEAK"
            session.save()
            tpl_acva_opt8_speak(number)
        elif "other" in display_str or "8" in body_str:
            session.state = "OPT7_OTHER"
            session.save()
            tpl_acva_opt2_eligibility_other(number)
        else:
            session.state = "OPT7_DETAILS"
            session.collected_info = {**session.collected_info, "SupportType": display_body}
            session.save()
            tpl_acva_opt7_all(number)

    elif state == "OPT7_OTHER":
        session.collected_info = {**session.collected_info, "SupportType": display_body}
        session.state = "OPT7_DETAILS"
        session.save()
        tpl_acva_opt7_all(number)

    elif state == "OPT7_DETAILS":
        session.collected_info = {**session.collected_info, "SupportIdentifier": display_body}
        session.state = "DONE"
        session.save()
        send_acva_text(number, "Our team will verify your details and contact you shortly.")

    elif state == "OPT8_SPEAK":
        if "email" in display_str:
            send_acva_text(number, "You can reach us at admissions@acvaindia.com")
            session.state = "DONE"
            session.save()
        elif "whatsapp" in display_str:
            send_acva_text(number, "You can contact our support team on WhatsApp at +91 9016728639")
            session.state = "DONE"
            session.save()
        else:
            goto_handoff()

    # --- Lead Collection Flow ---
    elif state == "COLLECT_NAME":
        session.collected_info = {**session.collected_info, "Name": display_body}
        customer_obj.name = display_body
        customer_obj.save()
        session.state = "COLLECT_EMAIL"
        session.save()
        tpl_acva_opt6_call_email(number)
        
    elif state == "COLLECT_EMAIL":
        session.collected_info = {**session.collected_info, "Email": display_body}
        session.state = "COLLECT_MOBILE"
        session.save()
        tpl_acva_opt6_call_number(number)
        
    elif state == "COLLECT_MOBILE":
        session.collected_info = {**session.collected_info, "Mobile": display_body}
        session.state = "COLLECT_PROFESSION"
        session.save()
        tpl_acva_opt6_call_profession(number)
        
    elif state == "COLLECT_PROFESSION":
        session.collected_info = {**session.collected_info, "Profession": display_body}
        session.state = "COLLECT_CITY"
        session.save()
        tpl_acva_opt6_call_city(number)
        
    elif state == "COLLECT_CITY":
        session.collected_info = {**session.collected_info, "City": display_body}
        session.state = "COLLECT_SOURCE"
        session.save()
        tpl_acva_opt6_call_option(number)
        
    elif state == "COLLECT_SOURCE":
        session.collected_info = {**session.collected_info, "Source": display_body}
        session.state = "COLLECT_TIME"
        session.save()
        tpl_acva_opt6_call_time(number)
        
    elif state == "COLLECT_TIME":
        session.collected_info = {**session.collected_info, "PreferredTime": display_body}
        session.state = "DONE"
        session.save()
        tpl_acva_opt6_call_confirmtime(number)
        
    # --- Sync WhatsAppSession to ConversationState ---
    org_obj = None
    if client_account_obj:
        org_obj = client_account_obj.tech_provider
            
    if org_obj:
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
        
        if session.state in ["DONE"]:
            conv_state.is_complete = True
            conv_state.stage = "Lead Collected"
        else:
            conv_state.is_complete = False
            conv_state.stage = session.state
            
        conv_state.collected_fields = session.collected_info or {}
        conv_state.save()
        
    if session.state in ["DONE"]:
        conv_obj.status = "confirmed"
        
    conv_obj.save()
