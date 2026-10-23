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
    tpl_icemake_ask_name,
    tpl_ice_support_ask_city,
    tpl_ice_support_ask_state,
    tpl_icemake_state_1,
    tpl_icemake_state_2,
    tpl_icemake_state_3,
    tpl_ice_support_ask_pincode,
    tpl_ice_support_ask_number,
    tpl_ice_support_ask_issue,
    tpl_icemake_customer,
    tpl_icemake_serviceengineer,
    STATE_ENGINEER_MAPPING,
    tpl_registered_number_confirmation,
    tpl_icemake_complaint,
    tpl_icemake_session_expired,
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

def get_or_create_icemake_customer(raw_phone: str, default_name: str = ""):
    from CRM.META.webhook_views import _normalize_e164_phone
    from django.db.models import Q
    e164 = _normalize_e164_phone(raw_phone)
    raw = str(raw_phone or "").strip()
    digits = re.sub(r"[^\d]", "", raw)

    # Check both standardized e164 (+91...) and plain digits (91...) for existing records
    customer_obj = Customer.objects.filter(Q(phone=e164) | Q(phone=raw) | Q(phone=digits)).first()
    created = False
    if not customer_obj:
        customer_obj = Customer.objects.create(phone=e164 or raw, name=default_name or e164 or raw)
        created = True
    elif default_name and (customer_obj.name == customer_obj.phone or not customer_obj.name or customer_obj.name.startswith("91")):
        customer_obj.name = default_name
        customer_obj.save(update_fields=['name'])

    return customer_obj, created


def handle_icemake_message(msg: dict, contact: dict = None):
    number = msg.get("from", "")
    msg_id = msg.get("id", "")
    msg_type = msg.get("type", "text")
    
    profile_name = contact.get("profile", {}).get("name", "") if contact else ""

    customer_obj, created = get_or_create_icemake_customer(number, default_name=profile_name or number)
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
        body = "-"
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

    
    logger.info("[IceMake DEBUG] msg payload: %s", json.dumps(msg))
    logger.info("[IceMake] from=%s type=%s body=%r display=%r id=%s", number, msg_type, body, display_body, msg_id)

    conv_obj, conv_created = Conversation.objects.get_or_create(
        customer=customer_obj, 
        phone_number_id=ICEMAKE_PHONE_NUMBER_ID, 
        defaults={'client': client_account_obj}
    )
    inbound_msg = Message.objects.create(
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

    import datetime
    ist_tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    now_ist = datetime.datetime.now(ist_tz)
    today_ist_date = now_ist.date()

    # Check previous interaction date before this current inbound message
    prev_msg = conv_obj.messages.filter(id__lt=inbound_msg.id).order_by("-timestamp").first()

    session = ConversationSession(conv_obj)

    # Check if interaction crossed midnight (12:00 AM IST) compared to previous message
    session_expired = False
    if prev_msg and prev_msg.timestamp:
        prev_ist_date = prev_msg.timestamp.astimezone(ist_tz).date()
        if prev_ist_date < today_ist_date:
            session_expired = True

    # If midnight crossed, reset session and ALWAYS send session expired template first
    if session_expired:
        session.state = "SESSION_EXPIRED"
        session.ticket_data = {}
        session.save()
        tpl_icemake_session_expired(number)
        return

    # If user is in SESSION_EXPIRED state, they must send a greeting (hi/hello/hey/menu) to restart
    if session.state == "SESSION_EXPIRED":
        if bool(re.search(r'^(hi|hello|hey|menu)$', body.lower())):
            session.state = "AWAITING_WELCOME_CONTINUE"
            session.ticket_data = {}
            session.save()
            tpl_ice_support_welcome(number)
            return
        else:
            tpl_icemake_session_expired(number)
            return

    is_trigger = (session.state == "INIT") or bool(re.search(r'^(hi|hello|hey|menu)$', body.lower()))
    if is_trigger:
        session.state = "AWAITING_WELCOME_CONTINUE"
        session.ticket_data = {}
        session.save()
        tpl_ice_support_welcome(number)
        return

    state = session.state
    text = body.strip()

    if state == "AWAITING_WELCOME_CONTINUE":
        if msg_type not in ["interactive", "button"]:
            send_icemake_text(number, "Please click the Continue button.")
            return
        session.state = "AWAITING_NAME"
        session.save()
        tpl_icemake_ask_name(number)
        return

    elif state == "AWAITING_NAME":
        if msg_type != "text":
            send_icemake_text(number, "Please enter a valid name.")
            return
        session.ticket_data = {"name": text}
        session.state = "AWAITING_STATE"
        session.save()
        tpl_icemake_state_1(number)
    
    elif state == "AWAITING_STATE":
        if msg_type not in ["interactive", "button"]:
            send_icemake_text(number, "Please select a valid state from the list.")
            td = session.ticket_data or {}
            current_page = td.get("state_page", 1)
            if current_page == 1:
                tpl_icemake_state_1(number)
            elif current_page == 2:
                tpl_icemake_state_2(number)
            else:
                tpl_icemake_state_3(number)
            return
        selected_state_text = display_body.strip()
        if selected_state_text.lower() == "next":
            td = session.ticket_data or {}
            current_page = td.get("state_page", 1)
            if current_page == 1:
                td["state_page"] = 2
                session.ticket_data = td
                session.save()
                tpl_icemake_state_2(number)
            elif current_page == 2:
                td["state_page"] = 3
                session.ticket_data = td
                session.save()
                tpl_icemake_state_3(number)
            return

        td = session.ticket_data or {}
        td["state"] = selected_state_text
        td.pop("state_page", None)
        session.ticket_data = td
        session.state = "AWAITING_CITY"
        session.save()
        tpl_ice_support_ask_city(number)
        
    elif state == "AWAITING_CITY":
        if msg_type != "text":
            send_icemake_text(number, "Please enter a valid city.")
            return
        td = session.ticket_data or {}
        td["city"] = text
        session.ticket_data = td
        session.state = "AWAITING_PINCODE"
        session.save()
        tpl_ice_support_ask_pincode(number)
        
    elif state == "AWAITING_PINCODE":
        text_clean = text.replace(" ", "").replace("-", "")
        if msg_type != "text" or not text_clean.isdigit() or len(text_clean) != 6:
            send_icemake_text(number, "Please enter a valid 6-digit pincode (numbers only).")
            return
        td = session.ticket_data or {}
        td["pincode"] = text_clean
        session.ticket_data = td
        session.state = "AWAITING_NUMBER_INPUT"
        session.save()
        tpl_ice_support_ask_number(number)

    elif state == "AWAITING_NUMBER_INPUT":
        text_clean = text.replace(" ", "").replace("-", "").replace("+", "")
        if msg_type != "text" or not text_clean.isdigit() or len(text_clean) < 10:
            send_icemake_text(number, "Please enter a valid mobile number with at least 10 digits (numbers only).")
            return
        td = session.ticket_data or {}
        td["mobile"] = text_clean
        session.ticket_data = td
        session.state = "AWAITING_CONFIRM_NUMBER"
        session.save()
        tpl_registered_number_confirmation(number, text_clean)

    elif state == "AWAITING_CONFIRM_NUMBER":
        if msg_type not in ["interactive", "button"]:
            send_icemake_text(number, "Please select a valid option from the buttons.")
            td = session.ticket_data or {}
            tpl_registered_number_confirmation(number, td.get("mobile", ""))
            return
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
        if msg_type not in ["interactive", "button"]:
            send_icemake_text(number, "Please select a valid complaint type from the list.")
            tpl_icemake_complaint(number)
            return
        td = session.ticket_data or {}
        selected_complaint = display_body.strip()
        td["complaint_type"] = selected_complaint
        session.ticket_data = td
        session.state = "AWAITING_ISSUE"
        session.save()
        tpl_ice_support_ask_issue(number)

    elif state == "AWAITING_ISSUE":
        if msg_type != "text":
            send_icemake_text(number, "Please enter a valid issue description.")
            return
        td = session.ticket_data or {}
        td["issue_desc"] = text
        session.ticket_data = td
        
        # Generate ticket
        ticket_no = f"WC{datetime.now().strftime('%m%d%H%M%S')}"
        
        # Find engineer (with fuzzy match to handle spelling mistakes)
        user_state = td.get("state", "").lower().strip()
        
        # Default engineer → Gujarat (Mr Rutvik) as fallback
        engineer_info = STATE_ENGINEER_MAPPING.get("gujarat")
        
        if user_state:
            # Step 1: exact substring match
            for state_key, e_info in STATE_ENGINEER_MAPPING.items():
                if state_key in user_state:
                    engineer_info = e_info
                    break
            else:
                # Step 2: fuzzy match for spelling mistakes (cutoff=0.7 → 70% similarity)
                from difflib import get_close_matches
                all_keys = list(STATE_ENGINEER_MAPPING.keys())
                matches = get_close_matches(user_state, all_keys, n=1, cutoff=0.7)
                if matches:
                    engineer_info = STATE_ENGINEER_MAPPING[matches[0]]
                    logger.info("[IceMake] Fuzzy matched state '%s' → '%s'", user_state, matches[0])

        
        # Notify customer (Meta doesn't allow empty strings for template params)
        tpl_icemake_customer(number, ticket_no or "-")
        
        registered_mobile = td.get("mobile", "").strip()
        if registered_mobile:
            reg_num_clean = re.sub(r"[^\d]", "", registered_mobile)
            chat_num_clean = re.sub(r"[^\d]", "", number)
            # Compare last 10 digits to see if it's the exact same phone number
            is_same_number = (
                reg_num_clean == chat_num_clean or 
                (len(reg_num_clean) >= 10 and len(chat_num_clean) >= 10 and reg_num_clean[-10:] == chat_num_clean[-10:])
            )
            
            # Send to registered number ONLY if it is truly a different mobile number
            if not is_same_number and len(reg_num_clean) >= 10:
                target_reg_num = f"91{reg_num_clean[-10:]}"
                try:
                    tpl_icemake_customer(target_reg_num, ticket_no or "-")
                except Exception as e:
                    logger.warning("[IceMake] Failed to send ticket template to alt registered mobile: %s", e)
        
        # Notify engineer
        city_state = f"{td.get('city', '')} / {td.get('state', '')}".strip(" /")
        address_str = f"{td.get('address', '')}, Pincode: {td.get('pincode', '')}".strip(" ,")
        issue_type = td.get("complaint_type", "General Issue")
        
        tpl_icemake_serviceengineer(
            to=engineer_info["phone"],
            ticket=(ticket_no or "-").replace('\n', ' '),
            customer_name=(td.get("name", "Customer") or "Customer").replace('\n', ' '),
            customer_mobile=(td.get("mobile", number) or number).replace('\n', ' '),
            city_state=(city_state or "-").replace('\n', ' '),
            issue_type=(issue_type or "-").replace('\n', ' '),
            description=(td.get("issue_desc", "No description provided") or "No description provided").replace('\n', ' '),
            assigned_engineer=(engineer_info["name"] or "Support Team").replace('\n', ' ')
        )


        
        session.state = "TICKET_GENERATED"
        # Save ticket and engineer details back into the session data
        td["ticket_no"] = ticket_no
        td["engineer_name"] = engineer_info["name"] if engineer_info else ""
        td["engineer_phone"] = engineer_info["phone"] if engineer_info else ""
        session.ticket_data = td
        
        # Maintain history of all tickets generated in this conversation
        th = session.ticket_history or []
        if not isinstance(th, list):
            th = []
        if ticket_no and ticket_no not in th:
            th.append(ticket_no)
        session.ticket_history = th
        
        # Mark conversation as confirmed → CRM shows as Lead
        conv_obj.status = "confirmed"
        session.save()  # saves bot_state, bot_metadata, AND status together

        
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
            td = conv_obj.bot_metadata.get("ticket_data", {}) if isinstance(conv_obj.bot_metadata, dict) else {}
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

from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

class IceMakeDataAPIView(APIView):
    """
    GET /api/icemake/data/?token=<token>

    Returns all Ice Make conversation data:
    - Customer details (name, phone)
    - Bot state + ticket_data (name, mobile, city, state, pincode, complaint, issue)
    - Ticket number, assigned engineer
    - Full message history (inbound + outbound)
    - Conversation status (prospect/confirmed/lead)
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        try:
            from django.conf import settings
            from django.core.paginator import Paginator
            token = request.GET.get("token")

            if not token:
                return Response({"error": "token is required"}, status=400)

            if token != ICEMAKE_PHONE_NUMBER_ID:
                return Response({"error": "Invalid token"}, status=401)

            # Pagination params
            try:
                page_num = int(request.GET.get("page", 1))
            except (ValueError, TypeError):
                page_num = 1
                
            try:
                page_size = int(request.GET.get("page_size", 20))
            except (ValueError, TypeError):
                page_size = 20

            client_account = ClientAccount.objects.filter(phone_number_id=ICEMAKE_PHONE_NUMBER_ID).first()

            from django.db.models import Prefetch, Max

            base_qs = Conversation.objects.filter(phone_number_id=ICEMAKE_PHONE_NUMBER_ID)
            
            last_fetched = request.GET.get("last_fetched")
            if last_fetched:
                from django.utils.dateparse import parse_datetime
                dt = parse_datetime(last_fetched)
                if dt:
                    base_qs = base_qs.filter(messages__timestamp__gte=dt).distinct()

            total = base_qs.count()

            conversations = base_qs.annotate(
                latest_message_time=Max('messages__timestamp')
            ).select_related("customer", "chatbot_state").prefetch_related(
                Prefetch("messages", queryset=Message.objects.order_by("timestamp"))
            ).order_by("-latest_message_time", "-created_at")

            paginator = Paginator(conversations, page_size)
            page = paginator.get_page(page_num)

            base_url = request.build_absolute_uri(request.path)
            def make_url(p):
                if p is None:
                    return None
                params = request.GET.copy()
                params["page"] = p
                return f"{base_url}?{params.urlencode()}"

            import re
            import datetime
            ist_tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

            # Engineer identification: normalized 10-digit suffixes & 91-prefixed numbers, plus engineer names
            eng_phones_10 = set()
            engineer_phones = set()
            engineer_names = set()
            engineer_name_lookup = {}
            for info in STATE_ENGINEER_MAPPING.values():
                if isinstance(info, dict):
                    p_raw = re.sub(r"[^\d]", "", str(info.get("phone", "")))
                    if len(p_raw) >= 10:
                        p10 = p_raw[-10:]
                        eng_phones_10.add(p10)
                        engineer_phones.add(p10)
                        engineer_phones.add(f"91{p10}")
                        if info.get("name"):
                            engineer_name_lookup[p10] = info["name"]
                    nm = (info.get("name") or "").strip().lower()
                    if nm:
                        engineer_names.add(nm)

            # Deduplicate conversations by normalized phone number to merge +91 and 91 numbers
            seen_clean_phones = {}
            filtered_convs = []
            for conv in page.object_list:
                raw_p = (conv.customer.phone or "").strip() if conv.customer else ""
                clean_p = re.sub(r"[^\d]", "", raw_p)
                if len(clean_p) == 10:
                    clean_p = f"91{clean_p}"

                if clean_p and clean_p in seen_clean_phones:
                    # Append messages from secondary duplicate conversation into primary conversation
                    primary_conv = seen_clean_phones[clean_p]
                    if not hasattr(primary_conv, "_merged_messages"):
                        primary_conv._merged_messages = list(primary_conv.messages.all())
                    existing_msg_ids = {m.id for m in primary_conv._merged_messages}
                    for extra_msg in conv.messages.all():
                        if extra_msg.id not in existing_msg_ids:
                            primary_conv._merged_messages.append(extra_msg)
                            existing_msg_ids.add(extra_msg.id)
                    # Use ticket_data if primary didn't have one
                    c_td = conv.bot_metadata.get("ticket_data", {}) if isinstance(conv.bot_metadata, dict) else {}
                    p_td = primary_conv.bot_metadata.get("ticket_data", {}) if isinstance(primary_conv.bot_metadata, dict) else {}
                    if c_td and not p_td.get("ticket_no"):
                        primary_conv.bot_metadata = conv.bot_metadata
                else:
                    conv._merged_messages = list(conv.messages.all())
                    if clean_p:
                        seen_clean_phones[clean_p] = conv
                    filtered_convs.append(conv)

            conversations_data = []
            for conv in filtered_convs:
                bot_meta = conv.bot_metadata if isinstance(conv.bot_metadata, dict) else {}
                td = bot_meta.get("ticket_data", {}) or {}

                # Try ConversationState for stage
                stage = conv.bot_state
                try:
                    conv_state = conv.chatbot_state
                    stage = conv_state.stage
                except Exception:
                    pass

                ticket_no = td.get("ticket_no", "")
                assigned_engineer = td.get("engineer_name", "")
                registered_mobile = td.get("mobile", "")
                customer_name = td.get("name", "")

                cust_phone = (conv.customer.phone or "").strip() if conv.customer else ""
                clean_cust_phone = re.sub(r"[^\d]", "", cust_phone)
                cust_phone_10 = clean_cust_phone[-10:] if len(clean_cust_phone) >= 10 else clean_cust_phone
                cust_name_lower = (conv.customer.name or "").strip().lower() if conv.customer else ""

                is_engineer_conv = (
                    (clean_cust_phone in engineer_phones) or 
                    (cust_phone_10 in eng_phones_10) or
                    (cust_name_lower in engineer_names)
                )

                all_msgs = getattr(conv, "_merged_messages", list(conv.messages.all()))

                # Format common messages payload once
                formatted_messages = [
                    {
                        "id": msg.id,
                        "direction": msg.direction,
                        "type": msg.message_type,
                        "content": msg.content,
                        "status": msg.status,
                        "timestamp": msg.timestamp.astimezone(ist_tz).isoformat() if msg.timestamp else None,
                        "meta_message_id": msg.meta_message_id,
                    }
                    for msg in all_msgs
                ]

                # Helper to build consistent, simplified conv object
                def build_conv_item(item_id, item_ticket_no, t_info, created_iso, msgs_list=None):
                    t_desc = t_info.get("issue_desc") or ""
                    if t_desc.startswith("{{"):
                        t_desc = ""
                    t_eng = t_info.get("assigned_engineer") or ""
                    if t_eng.startswith("{{"):
                        t_eng = ""

                    final_ticket_no = "-"
                    if item_ticket_no and str(item_ticket_no).strip().upper().startswith("WC"):
                        final_ticket_no = str(item_ticket_no).strip()

                    return {
                        "id": item_id,
                        "conversation_id": conv.id,
                        "ticket_no": final_ticket_no,
                        "customer_name": t_info.get("name") or (conv.customer.name if conv.customer else ""),
                        "customer_phone": cust_phone,
                        "registered_mobile": t_info.get("registered_mobile") or registered_mobile or cust_phone,
                        "assigned_engineer": t_eng,
                        "engineer_phone": t_info.get("engineer_phone") or "",
                        "city": t_info.get("city") or "",
                        "state": t_info.get("state") or "",
                        "pincode": t_info.get("pincode") or "",
                        "complaint_type": t_info.get("complaint_type") or "",
                        "issue_desc": t_desc,
                        "status": conv.status,
                        "bot_state": conv.bot_state,
                        "stage": stage,
                        "is_engineer": is_engineer_conv,
                        "created_at": created_iso,
                        "customer": {
                            "name": conv.customer.name if conv.customer else "",
                            "whatsapp_number": cust_phone,
                        },
                        "ticket_data": {
                            "ticket_no": final_ticket_no,
                            "name": t_info.get("name") or customer_name or (conv.customer.name if conv.customer else ""),
                            "registered_mobile": t_info.get("registered_mobile") or registered_mobile or cust_phone,
                            "city": t_info.get("city") or "",
                            "state": t_info.get("state") or "",
                            "pincode": t_info.get("pincode") or "",
                            "complaint_type": t_info.get("complaint_type") or "",
                            "issue_desc": t_desc,
                            "assigned_engineer": t_eng,
                            "engineer_phone": t_info.get("engineer_phone") or "",
                        },
                        "messages": msgs_list if msgs_list is not None else formatted_messages
                    }

                conv_created_iso = conv.created_at.astimezone(ist_tz).isoformat() if conv.created_at else None

                # Case 1: Engineer conversation -> strictly ONE single row showing latest activity
                if is_engineer_conv:
                    eng_name = engineer_name_lookup.get(cust_phone_10) or (conv.customer.name if conv.customer else "") or assigned_engineer or "Service Engineer"
                    latest_ticket_data = {
                        "ticket_no": ticket_no,
                        "name": customer_name,
                        "registered_mobile": registered_mobile,
                        "city": td.get("city", ""),
                        "state": td.get("state", ""),
                        "pincode": td.get("pincode", ""),
                        "complaint_type": td.get("complaint_type", ""),
                        "issue_desc": td.get("issue_desc", ""),
                        "assigned_engineer": eng_name,
                        "engineer_phone": cust_phone,
                    }
                    # Parse latest ticket alert sent to this engineer
                    for msg in reversed(all_msgs):
                        c_str = msg.content or ""
                        t_match = re.search(r'Ticket\s*:\s*(WC\d+|C\d+)', c_str, re.IGNORECASE)
                        if t_match:
                            latest_ticket_data["ticket_no"] = t_match.group(1).strip()
                            c_name = re.search(r'Customer Name:\s*([^\n]+)', c_str)
                            if c_name: latest_ticket_data["name"] = c_name.group(1).strip()
                            c_mob = re.search(r'Customer Mobile:\s*([^\n]+)', c_str)
                            if c_mob: latest_ticket_data["registered_mobile"] = c_mob.group(1).strip()
                            cs_match = re.search(r'City / State:\s*([^\n]+)', c_str)
                            if cs_match:
                                parts = cs_match.group(1).split("/")
                                latest_ticket_data["city"] = parts[0].strip() if len(parts) > 0 else ""
                                latest_ticket_data["state"] = parts[1].strip() if len(parts) > 1 else ""
                            
                            # In templates:
                            # 🏠 Address: <Product Category / Address>
                            # ⚙️ Product (Name|Issue): <Product issue description / Name>
                            # 🛠️ Issue Type: <Assigned Engineer / Issue type>
                            # 📝 Description: <Description>
                            addr_match = re.search(r'🏠\s*Address:\s*([^\n]+)', c_str)
                            p_match = re.search(r'(?:⚙️\s*Product (?:Name|Issue)|Issue Type):\s*([^\n]+)', c_str)
                            d_match = re.search(r'(?:📝\s*Description|Issue Description):\s*([^\n]+)', c_str)

                            product_category = addr_match.group(1).strip() if addr_match else ""
                            product_issue = p_match.group(1).strip() if p_match else ""
                            raw_desc = d_match.group(1).strip() if d_match else ""
                            if raw_desc.startswith("{{"):
                                raw_desc = ""

                            if product_category and product_category.lower() not in ["none", "-"]:
                                latest_ticket_data["complaint_type"] = product_category
                                latest_ticket_data["issue_desc"] = product_issue or raw_desc
                            else:
                                latest_ticket_data["complaint_type"] = product_issue
                                latest_ticket_data["issue_desc"] = raw_desc

                            eng_match = re.search(r'(?:Assigned Engineer|🛠️\s*Issue Type):\s*([^\n]+)', c_str)
                            if eng_match:
                                eng_val = eng_match.group(1).strip()
                                if not eng_val.startswith("{{") and eng_val.lower().startswith("mr"):
                                    latest_ticket_data["assigned_engineer"] = eng_val
                            break

                    eng_conv_item = build_conv_item(
                        item_id=conv.id,
                        item_ticket_no=latest_ticket_data["ticket_no"],
                        t_info=latest_ticket_data,
                        created_iso=conv_created_iso
                    )
                    # For Engineer row: make sure chat participant reflects the Engineer
                    eng_title = f"{eng_name} (Service Engineer)"
                    eng_conv_item["customer_name"] = eng_title
                    eng_conv_item["customer_phone"] = cust_phone
                    eng_conv_item["registered_mobile"] = cust_phone
                    eng_conv_item["customer"] = {
                        "name": eng_title,
                        "whatsapp_number": cust_phone,
                    }
                    eng_conv_item["ticket_customer_name"] = latest_ticket_data.get("name") or customer_name
                    eng_conv_item["ticket_customer_phone"] = latest_ticket_data.get("registered_mobile") or registered_mobile

                    # Set ticket_data so that frontend dashboard DETAILS column renders the Engineer
                    if "ticket_data" in eng_conv_item:
                        eng_conv_item["ticket_data"]["name"] = eng_title
                        eng_conv_item["ticket_data"]["registered_mobile"] = cust_phone
                        eng_conv_item["ticket_data"]["customer_name"] = eng_conv_item["ticket_customer_name"]
                        eng_conv_item["ticket_data"]["customer_phone"] = eng_conv_item["ticket_customer_phone"]

                    conversations_data.append(eng_conv_item)
                    continue

                # Case 2: Regular Customer conversation -> extract distinct customer tickets
                tickets_detail_map = {}
                current_tid = td.get("ticket_no", "").strip()

                # Scan messages chronologically to extract tickets
                last_known_issue = ""
                for msg in all_msgs:
                    content_str = msg.content or ""
                    if msg.direction == "inbound" and len(content_str) > 10 and not content_str.lower().startswith(("hi", "hello", "yes", "no")):
                        last_known_issue = content_str

                    t_match = re.search(r'(?:Ticket\s*:\s*|Complaint Number:\s*\*?|(?:^|\b))(WC\d+|C\d+)', content_str, re.IGNORECASE)
                    if t_match:
                        tid = t_match.group(1).strip()
                        if tid not in tickets_detail_map:
                            tickets_detail_map[tid] = {
                                "ticket_no": tid,
                                "name": customer_name,
                                "registered_mobile": registered_mobile,
                                "city": "",
                                "state": "",
                                "pincode": "",
                                "complaint_type": "",
                                "issue_desc": last_known_issue,
                                "assigned_engineer": "",
                                "engineer_phone": "",
                                "created_at": msg.timestamp.astimezone(ist_tz).isoformat() if msg.timestamp else None,
                            }

                        if "TEMPLATE: ICEMAKE" in content_str.upper() or "Assigned Engineer:" in content_str or "Customer Name:" in content_str:
                            eng_match = re.search(r'Assigned Engineer:\s*([^\n]+)', content_str)
                            if eng_match:
                                eng_val = eng_match.group(1).strip()
                                if not eng_val.startswith("{{"):
                                    tickets_detail_map[tid]["assigned_engineer"] = eng_val

                            cust_match = re.search(r'Customer Name:\s*([^\n]+)', content_str)
                            if cust_match: tickets_detail_map[tid]["name"] = cust_match.group(1).strip()

                            mob_match = re.search(r'Customer Mobile:\s*([^\n]+)', content_str)
                            if mob_match: tickets_detail_map[tid]["registered_mobile"] = mob_match.group(1).strip()

                            cs_match = re.search(r'City / State:\s*([^\n]+)', content_str)
                            if cs_match:
                                parts = cs_match.group(1).strip().split("/")
                                tickets_detail_map[tid]["city"] = parts[0].strip() if len(parts) > 0 else ""
                                tickets_detail_map[tid]["state"] = parts[1].strip() if len(parts) > 1 else ""

                            iss_match = re.search(r'(?:⚙️\s*Product (?:Name|Issue)|Issue Type):\s*([^\n]+)', content_str)
                            if iss_match: tickets_detail_map[tid]["complaint_type"] = iss_match.group(1).strip()

                            desc_match = re.search(r'(?:📝\s*Description|Issue Description):\s*([^\n]+)', content_str)
                            if desc_match:
                                desc_val = desc_match.group(1).strip()
                                if not desc_val.startswith("{{"):
                                    tickets_detail_map[tid]["issue_desc"] = desc_val

                # Apply current ticket data from session to current_tid ONLY
                if current_tid:
                    if current_tid not in tickets_detail_map:
                        tickets_detail_map[current_tid] = {
                            "ticket_no": current_tid,
                            "name": customer_name,
                            "registered_mobile": registered_mobile,
                            "city": td.get("city", ""),
                            "state": td.get("state", ""),
                            "pincode": td.get("pincode", ""),
                            "complaint_type": td.get("complaint_type", ""),
                            "issue_desc": td.get("issue_desc", ""),
                            "assigned_engineer": assigned_engineer,
                            "engineer_phone": td.get("engineer_phone", ""),
                            "created_at": conv_created_iso,
                        }
                    else:
                        for k in ["name", "registered_mobile", "city", "state", "pincode", "complaint_type", "issue_desc", "assigned_engineer", "engineer_phone"]:
                            val = td.get(k, "")
                            if val:
                                tickets_detail_map[current_tid][k] = val

                # If tickets exist, create a separate row/entry for each distinct ticket
                if tickets_detail_map:
                    sorted_tickets = sorted(
                        tickets_detail_map.values(),
                        key=lambda x: (x.get("created_at") or "", x.get("ticket_no") or ""),
                        reverse=True
                    )
                    for item in sorted_tickets:
                        t_tid = item["ticket_no"]
                        if not item.get("assigned_engineer"):
                            st_key = (item.get("state") or td.get("state") or "").strip().lower()
                            if st_key in STATE_ENGINEER_MAPPING:
                                item["assigned_engineer"] = STATE_ENGINEER_MAPPING[st_key].get("name", "")
                                if not item.get("engineer_phone"):
                                    item["engineer_phone"] = STATE_ENGINEER_MAPPING[st_key].get("phone", "")
                            elif "gujarat" in st_key or "gj" in st_key:
                                item["assigned_engineer"] = "Mr Rutvik"
                                item["engineer_phone"] = "919727721447"

                        conversations_data.append(build_conv_item(
                            item_id=f"{conv.id}_{t_tid}",
                            item_ticket_no=t_tid,
                            t_info=item,
                            created_iso=item.get("created_at") or conv_created_iso
                        ))
                else:
                    # In-progress / prospect conversation without ticket yet.
                    # Group by session/day (midnight boundary) so each day's attempt is a separate row.
                    prospect_days_msgs = {}
                    for msg in all_msgs:
                        if msg.timestamp:
                            m_date = msg.timestamp.astimezone(ist_tz).date()
                            if m_date not in prospect_days_msgs:
                                prospect_days_msgs[m_date] = []
                            prospect_days_msgs[m_date].append(msg)

                    if prospect_days_msgs:
                        # Sort descending so today's prospect is on top
                        sorted_dates = sorted(prospect_days_msgs.keys(), reverse=True)
                        for d in sorted_dates:
                            day_msgs = prospect_days_msgs[d]
                            date_str = d.strftime("%Y%m%d")
                            first_msg_iso = day_msgs[0].timestamp.astimezone(ist_tz).isoformat() if (day_msgs and day_msgs[0].timestamp) else conv_created_iso

                            day_formatted_msgs = [
                                {
                                    "id": m.id,
                                    "direction": m.direction,
                                    "type": m.message_type,
                                    "content": m.content,
                                    "status": m.status,
                                    "timestamp": m.timestamp.astimezone(ist_tz).isoformat() if m.timestamp else None,
                                    "meta_message_id": m.meta_message_id,
                                }
                                for m in day_msgs
                            ]

                            # Extract prospect form answers from this specific day's messages
                            day_extracted_td = {}
                            for i, m in enumerate(day_msgs):
                                if m.direction == "inbound" and i > 0:
                                    prev_m = day_msgs[i - 1]
                                    prev_text = (prev_m.content or "").lower()
                                    curr_text = (m.content or "").strip()

                                    if "please provide your name" in prev_text or "enter a valid name" in prev_text:
                                        day_extracted_td["name"] = curr_text
                                    elif "please provide your city" in prev_text or "enter a valid city" in prev_text:
                                        day_extracted_td["city"] = curr_text
                                    elif "please enter your 6-digit postal pincode" in prev_text or "pincode" in prev_text:
                                        day_extracted_td["pincode"] = curr_text
                                    elif "registered 10-digit mobile number" in prev_text or "at least 10 digits" in prev_text:
                                        day_extracted_td["registered_mobile"] = curr_text
                                        day_extracted_td["mobile"] = curr_text
                                    elif "briefly describe the issue" in prev_text or "issue description" in prev_text:
                                        day_extracted_td["issue_desc"] = curr_text

                            # If this is the latest day, merge with active session memory
                            if d == sorted_dates[0] and td:
                                for k, v in td.items():
                                    if v and k not in ["ticket_no", "state_page"]:
                                        day_extracted_td[k] = v

                            conversations_data.append(build_conv_item(
                                item_id=f"{conv.id}_{date_str}",
                                item_ticket_no="",
                                t_info=day_extracted_td,
                                created_iso=first_msg_iso,
                                msgs_list=day_formatted_msgs
                            ))
                    else:
                        conversations_data.append(build_conv_item(
                            item_id=conv.id,
                            item_ticket_no="",
                            t_info=td,
                            created_iso=conv_created_iso,
                            msgs_list=formatted_messages
                        ))

            return Response({
                "client": {
                    "name": client_account.name if client_account else "Ice Make Refrigeration",
                    "phone_number_id": ICEMAKE_PHONE_NUMBER_ID,
                    "waba_id": client_account.waba_id if client_account else "",
                },
                "total": total,
                "page": page_num,
                "page_size": page_size,
                "total_pages": paginator.num_pages,
                "next": make_url(page.next_page_number() if page.has_next() else None),
                "previous": make_url(page.previous_page_number() if page.has_previous() else None),
                "conversations": conversations_data,
            })
        except Exception as e:
            logger.exception("[IceMake] Error in IceMakeDataAPIView: %s", e)
            return Response({"error": "Server error", "details": str(e)}, status=500)

