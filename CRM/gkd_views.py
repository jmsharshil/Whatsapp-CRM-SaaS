import json
import logging
import re
import time
from datetime import datetime
from django.conf import settings

from CRM.models import Customer, Conversation, Message, ClientAccount, ConversationState
from .gkd_utils import (
    GKD_PHONE_NUMBER_ID,
    download_gkd_media_from_whatsapp,
    send_gkd_text,
    send_gkd_image,
    tpl_gkd_main_menu,
    tpl_gkd_budget_menu,
    tpl_gkd_b1_q1,
    tpl_gkd_b1_q2,
    tpl_gkd_b1_q3,
    tpl_gkd_b1_q4,
    tpl_gkd_b1_q5,
    tpl_gkd_b1_q5_upload,
    tpl_gkd_b1_q5_invalid,
    tpl_gkd_b1_q7,
    tpl_gkd_b1_q8,
    tpl_gkd_b3_q1,
    tpl_gkd_b3_q2,
    tpl_gkd_b5_q1,
    tpl_gkd_b5_q2,
    tpl_gkd_b5_q3,
    tpl_gkd_b6_q1,
    tpl_gkd_b6_q2,
    tpl_gkd_b6_q3,
    tpl_gkd_b6_q4,
    tpl_gkd_b7_q1,
    tpl_gkd_b7_q2,
    tpl_gkd_b7_q3,
    tpl_gkd_b7_q4,
    tpl_gkd_b8_q1,
    tpl_gkd_b8_q2,
    tpl_gkd_b8_q3,
    tpl_gkd_b8_q3_wait,
    tpl_gkd_b9_talk,
    tpl_gkd_handoff,
    tpl_gkd_showroom_visit,
    tpl_gkd_showroom_confirm,
    tpl_gkd_closing_name,
    tpl_gkd_closing_area,
    tpl_gkd_closing_time,
    tpl_gkd_closing_portfolio,
    tpl_gkd_portfolio_link,
    tpl_gkd_done,
    tpl_gkd_invalid_option
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

def handle_gkd_message(msg: dict):
    number = msg.get("from", "")
    msg_id = msg.get("id", "")
    msg_type = msg.get("type", "text")

    customer_obj, _ = Customer.objects.get_or_create(phone=number, defaults={'name': number})
    client_account_obj = ClientAccount.objects.filter(phone_number_id=GKD_PHONE_NUMBER_ID).first()

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
        filename = msg.get(msg_type, {}).get("filename")
        if media_id:
            token = client_account_obj.access_token if client_account_obj and client_account_obj.access_token else getattr(settings, "META_PERMANENT_TOKEN", "")
            media_url = download_gkd_media_from_whatsapp(media_id, token, filename)
            
            if media_url:
                prefix = f"[{msg_type.upper()}]"
                body = f"{prefix} {media_url}"
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

    logger.info("[GKD DEBUG] msg payload: %s", json.dumps(msg))
    logger.info("[GKD] from=%s type=%s body=%r display=%r id=%s", number, msg_type, body, display_body, msg_id)

    conv_obj, conv_created = Conversation.objects.get_or_create(
        customer=customer_obj, 
        phone_number_id=GKD_PHONE_NUMBER_ID, 
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
        session.gkd_branch = ""
        session.tags = []
        session.save()
    else:
        # 5. Human Handoff Rules
        handoff_triggers = [r'\btalk to someone\b', r'\bcall me\b', r'\bhuman\b', r'\bagent\b']
        if any(re.search(trigger, body.lower()) for trigger in handoff_triggers) or session.state == "HANDOFF":
            if session.state != "HANDOFF":
                session.state = "HANDOFF"
                session.save()
                tpl_gkd_handoff(number)
            return

    state = session.state

    # Enforce button selection for predefined choice states
    interactive_states = [
        "B1_Q1", "B1_Q2", "B1_Q3", "B1_Q4", "B1_Q5", "B1_Q6", "B1_Q7", "B1_Q8",
        "B3_Q1", "B3_Q2",
        "B5_Q1", "B5_Q2", "B5_Q3",
        "B6_Q1", "B6_Q2", "B6_Q3", "B6_Q4",
        "B7_Q1", "B7_Q2", "B7_Q3", "B7_Q4",
        "B8_Q1", "B8_Q2", "B8_Q3",
        "CLOSING_TIME",
        "CLOSING_PORTFOLIO"
    ]
    if state in interactive_states and msg_type not in ["interactive", "button"]:
        tpl_gkd_invalid_option(number)
        return


    def add_tag(tag: str):
        tags = session.tags or []
        if tag not in tags:
            tags.append(tag)
        session.tags = tags

    def goto_closing():
        session.state = "CLOSING_NAME"
        session.save()
        tpl_gkd_closing_name(number)

    body_str = body.lower()
    display_str = display_body.lower()

    if state == "INIT":
        tpl_gkd_main_menu(number)
        session.state = "MENU_SELECTION"
        session.save()

    elif state == "MENU_SELECTION":
        session.collected_info = {}
        
        if body_str in ["b1", "b2", "b4"] or any(k in display_str for k in ["kitchen", "wardrobe", "office"]):
            session.gkd_branch = "kitchen_wardrobe_office"
            session.state = "B1_Q1"
            session.save()
            tpl_gkd_b1_q1(number)
        elif body_str == "b3" or "living" in display_str:
            session.gkd_branch = "living_room"
            session.state = "B3_Q1"
            session.save()
            tpl_gkd_b3_q1(number)
        elif body_str == "b5" or "full home" in display_str or "turnkey" in display_str:
            session.gkd_branch = "full_home"
            session.state = "B5_Q1"
            session.save()
            tpl_gkd_b5_q1(number)
        elif body_str == "b6" or "hotel" in display_str:
            session.gkd_branch = "hotel"
            add_tag("Hot")
            add_tag("B2B")
            session.state = "B6_Q1"
            session.save()
            tpl_gkd_b6_q1(number)
        elif body_str == "b7" or "hospital" in display_str:
            session.gkd_branch = "hospital"
            add_tag("Hot")
            add_tag("B2B")
            session.state = "B7_Q1"
            session.save()
            tpl_gkd_b7_q1(number)
        elif body_str == "b8" or "imported" in display_str:
            session.gkd_branch = "imported"
            session.state = "B8_Q1"
            session.save()
            tpl_gkd_b8_q1(number)
        elif body_str == "b9" or "talk" in display_str or "something else" in display_str:
            session.gkd_branch = "talk_to_us"
            session.state = "HANDOFF"
            session.save()
            tpl_gkd_b9_talk(number)
        else:
            tpl_gkd_invalid_option(number)

    # ================= BRANCH 1, 2, 4 (Kitchen / Wardrobe / Office) =================
    elif state == "B1_Q1":
        session.collected_info = {**session.collected_info, "Q1_WorkType": display_body}
        session.state = "B1_Q2"
        session.save()
        tpl_gkd_b1_q2(number)

    elif state == "B1_Q2":
        session.collected_info = {**session.collected_info, "Q2_HiredArchitect": display_body}
        if body_str == "yes" or "yes" in display_str:
            add_tag("Trade Lead")
        session.state = "B1_Q3"
        session.save()
        tpl_gkd_b1_q3(number)

    elif state == "B1_Q3":
        session.collected_info = {**session.collected_info, "Q3_LayoutReady": display_body}
        if body_str == "yes" or "yes" in display_str:
            add_tag("Hot")
        session.state = "B1_Q4"
        session.save()
        tpl_gkd_b1_q4(number)

    elif state == "B1_Q4":
        session.collected_info = {**session.collected_info, "Q4_Need3D": display_body}
        
        q3_answer = session.collected_info.get("Q3_LayoutReady", "").lower()
        if q3_answer == "yes":
            session.state = "B1_Q5"
            session.save()
            tpl_gkd_b1_q5(number)
        else:
            session.state = "B1_Q6"
            session.save()
            tpl_gkd_budget_menu(number)

    elif state == "B1_Q5":
        session.collected_info = {**session.collected_info, "Q5_PrepareQuote": display_body}
        if body_str == "yes" or "yes" in display_str:
            add_tag("Hot")
            session.state = "B1_Q5_UPLOAD"
            session.save()
            tpl_gkd_b1_q5_upload(number)
        else:
            session.state = "B1_Q6"
            session.save()
            tpl_gkd_budget_menu(number)

    elif state == "B1_Q5_UPLOAD":
        if msg_type in ["image", "document"]:
            session.collected_info = {**session.collected_info, "Q5_MediaReceived": display_body}
            session.state = "B1_Q6"
            session.save()
            tpl_gkd_budget_menu(number)
        else:
            tpl_gkd_b1_q5_invalid(number)

    elif state == "B1_Q6":
        session.collected_info = {**session.collected_info, "Q6_Budget": display_body}
        if body_str != "not_sure" and "not sure" not in display_str:
            add_tag("Hot")
        
        # Cold lead logic
        if session.collected_info.get("Q3_LayoutReady") == "no" and (body_str == "not_sure" or "not sure" in display_str):
            add_tag("Cold")

        session.state = "B1_Q7"
        session.save()
        tpl_gkd_b1_q7(number)

    elif state == "B1_Q7":
        session.collected_info = {**session.collected_info, "Q7_Visit": display_body}
        if body_str == "yes" or "yes" in display_str:
            session.state = "SHOWROOM_VISIT"
            session.save()
            tpl_gkd_showroom_visit(number)
        else:
            session.state = "B1_Q8"
            session.save()
            tpl_gkd_b1_q8(number)

    elif state == "SHOWROOM_VISIT":
        session.collected_info = {**session.collected_info, "ShowroomVisitSlot": display_body}
        tpl_gkd_showroom_confirm(number, display_body)
        time.sleep(3)  # Increased delay to ensure correct delivery order on WhatsApp
        goto_closing()

    elif state == "B1_Q8":
        session.collected_info = {**session.collected_info, "Q8_SpeakToExpert": display_body}
        if body_str == "yes" or "yes" in display_str:
            add_tag("Hot")
            session.state = "HANDOFF"
            session.save()
            tpl_gkd_handoff(number)
        else:
            goto_closing()

    # ================= BRANCH 3 (Living Room) =================
    elif state == "B3_Q1":
        session.collected_info = {**session.collected_info, "Q1_LookingFor": display_body}
        session.state = "B3_Q2"
        session.save()
        tpl_gkd_b3_q2(number)

    elif state == "B3_Q2":
        session.collected_info = {**session.collected_info, "Q2_RoomSize": display_body}
        goto_closing()

    # ================= BRANCH 5 (Full Home Interior) =================
    elif state == "B5_Q1":
        session.collected_info = {**session.collected_info, "Q1_Scope": display_body}
        session.state = "B5_Q2"
        session.save()
        tpl_gkd_b5_q2(number)

    elif state == "B5_Q2":
        session.collected_info = {**session.collected_info, "Q2_HomeSize": display_body}
        session.state = "B5_Q3"
        session.save()
        tpl_gkd_b5_q3(number)

    elif state == "B5_Q3":
        session.collected_info = {**session.collected_info, "Q3_SiteReady": display_body}
        goto_closing()

    # ================= BRANCH 6 (Hotel) =================
    elif state == "B6_Q1":
        session.collected_info = {**session.collected_info, "Q1_Type": display_body}
        session.state = "B6_Q2"
        session.save()
        tpl_gkd_b6_q2(number)

    elif state == "B6_Q2":
        session.collected_info = {**session.collected_info, "Q2_Rooms": display_body}
        session.state = "B6_Q3"
        session.save()
        tpl_gkd_b6_q3(number)

    elif state == "B6_Q3":
        session.collected_info = {**session.collected_info, "Q3_Scope": display_body}
        session.state = "B6_Q4"
        session.save()
        tpl_gkd_b6_q4(number)

    elif state == "B6_Q4":
        session.collected_info = {**session.collected_info, "Q4_Timeline": display_body}
        goto_closing()

    # ================= BRANCH 7 (Hospital) =================
    elif state == "B7_Q1":
        session.collected_info = {**session.collected_info, "Q1_Type": display_body}
        session.state = "B7_Q2"
        session.save()
        tpl_gkd_b7_q2(number)

    elif state == "B7_Q2":
        session.collected_info = {**session.collected_info, "Q2_Needs": display_body}
        session.state = "B7_Q3"
        session.save()
        tpl_gkd_b7_q3(number)

    elif state == "B7_Q3":
        session.collected_info = {**session.collected_info, "Q3_Size": display_body}
        session.state = "B7_Q4"
        session.save()
        tpl_gkd_b7_q4(number)

    elif state == "B7_Q4":
        session.collected_info = {**session.collected_info, "Q4_Timeline": display_body}
        goto_closing()

    # ================= BRANCH 8 (Imported Furniture) =================
    elif state == "B8_Q1":
        session.collected_info = {**session.collected_info, "Q1_LookingFor": display_body}
        session.state = "B8_Q2"
        session.save()
        tpl_gkd_b8_q2(number)

    elif state == "B8_Q2":
        session.collected_info = {**session.collected_info, "Q2_IsFor": display_body}
        session.state = "B8_Q3"
        session.save()
        tpl_gkd_b8_q3(number)

    elif state == "B8_Q3":
        if body_str == "type" or "type" in display_str:
            session.state = "B8_Q3_WAIT_TEXT"
            session.save()
            tpl_gkd_b8_q3_wait(number)
        else:
            session.collected_info = {**session.collected_info, "Q3_BrandStyle": "Recommend for me"}
            goto_closing()

    elif state == "B8_Q3_WAIT_TEXT":
        session.collected_info = {**session.collected_info, "Q3_BrandStyle": display_body}
        goto_closing()

    elif state == "CLOSING_NAME":
        session.collected_info = {**session.collected_info, "Name": display_body}
            
        session.state = "CLOSING_AREA"
        session.save()
        tpl_gkd_closing_area(number)

    elif state == "CLOSING_AREA":
        session.collected_info = {**session.collected_info, "Area": display_body}
        session.state = "CLOSING_TIME"
        session.save()
        tpl_gkd_closing_time(number)

    elif state == "CLOSING_TIME":
        session.collected_info = {**session.collected_info, "BestTime": display_body}
        session.state = "CLOSING_PORTFOLIO"
        session.save()
        name = session.collected_info.get("Name", "")
        tpl_gkd_closing_portfolio(number, name)

    elif state == "CLOSING_PORTFOLIO":
        if display_body == "Yes, show me":
            tpl_gkd_portfolio_link(number)
            
        session.state = "DONE"
        session.save()
        tpl_gkd_done(number)


    # --- Sync WhatsAppSession to ConversationState for Leads/Prospects View ---
    org_obj = None
    if client_account_obj:
        org_obj = client_account_obj.tech_provider
    else:
        from CRM.models import WABAAccount
        waba = WABAAccount.objects.filter(phone_number_id=GKD_PHONE_NUMBER_ID).first()
        if waba:
            org_obj = waba.organization
            
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
        
        if session.state in ["DONE", "HANDOFF"]:
            conv_state.is_complete = True
            if session.tags:
                conv_state.stage = ", ".join(session.tags)
            else:
                conv_state.stage = session.state
        else:
            conv_state.is_complete = False
            conv_state.stage = session.state
            
        # Update fields with tags and branches
        full_fields = session.collected_info or {}
        full_fields["Branch"] = session.gkd_branch
        if session.tags:
            full_fields["Tags"] = ", ".join(session.tags)
            
        conv_state.collected_fields = full_fields
        conv_state.save()
        
    conv_obj.status = "prospect"
    conv_obj.save()
