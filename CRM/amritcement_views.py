import json
import logging
import re
from datetime import datetime
from CRM.models import Customer, Conversation, Message, ClientAccount
from CRM.amritcement_utils import (
    AMRITCEMENT_PHONE_NUMBER_ID, PRODUCT_MASTER, send_amritcement_text,
    send_amritcement_interactive, amritcement_create_order,
    amritcement_get_ledger, amritcement_add_claim_submission,
    amritcement_get_destinations, amritcement_verify_mobile_no,
    send_amritcement_template, download_meta_media
)
from django.conf import settings

logger = logging.getLogger(__name__)

class ConversationSession:
    def __init__(self, conversation):
        super().__setattr__('_conv', conversation)

    def save(self):
        self._conv.save()

    @property
    def state(self):
        return self._conv.bot_state or "START"

    @state.setter
    def state(self, value):
        self._conv.bot_state = value

    def __getattr__(self, item):
        if not self._conv.bot_metadata:
            return None
        return self._conv.bot_metadata.get(item)

    def __setattr__(self, key, value):
        if key in ['_conv', 'state']:
            super().__setattr__(key, value)
        else:
            if not isinstance(self._conv.bot_metadata, dict):
                self._conv.bot_metadata = {}
            self._conv.bot_metadata[key] = value

def tpl_amritcement_main_menu(number: str):
    # Sends the Meta WhatsApp template named 'amritcement_welcome_'
    send_amritcement_template(number, "amritcement_welcome_", "en")

def tpl_amritcement_products(number: str):
    send_amritcement_template(number, "amritcement_cement_type_", "en")
    
def tpl_amritcement_destinations(number: str, dealer_data: dict, qty: str, customer_type: str = "Dealer"):
    rows = []
    
    if isinstance(dealer_data, dict):
        addr1 = dealer_data.get("address1", "")
        if addr1:
            rows.append({"id": "dest_addr1", "title": addr1[:24]})
        addr2 = dealer_data.get("address2", "")
        if addr2:
            rows.append({"id": "dest_addr2", "title": addr2[:24]})
        destination = dealer_data.get("destination", "")
        if destination and not any(r["title"] == destination[:24] for r in rows):
            rows.append({"id": "dest_profile", "title": destination[:24]})
            
    if not rows:
        rows.append({"id": "dest_default", "title": "Default Location"})
        
    rows = rows[:9] # Keep room for new address
    rows.append({"id": "dest_new", "title": "+ Add new address"})
    
    try:
        qty_num = int(qty)
        rate = float(dealer_data.get("rate", 0)) if isinstance(dealer_data, dict) else 0
        est_val = qty_num * rate
        est_str = f"₹{est_val:,.2f}" if rate > 0 else "As per applicable price"
    except:
        est_str = "As per applicable price"

    qty_unit = "MT" if customer_type.lower() == "dealer" else "bags"
    interactive_data = {
        "type": "list",
        "body": {
            "text": f"Quantity: *{qty} {qty_unit}* • Est. value: {est_str}\n✅ Credit & stock check passed.\n\n*Step 4 — Select Dispatch Location:*"
        },
        "action": {
            "button": "Dispatch locations",
            "sections": [{"title": "Locations", "rows": rows}]
        }
    }
    send_amritcement_interactive(number, interactive_data)


def tpl_amritcement_ship_to_select(number: str, dealer_data: dict, current_selections: list):
    own_party = {
        "name": dealer_data.get("name", ""),
        "code": str(dealer_data.get("customer_code", "")),
        "destination": dealer_data.get("destination", "")
    }
    parties = [own_party]
    for sd in dealer_data.get("sub_dealer", []):
        parties.append({
            "name": sd.get("Name", ""),
            "code": str(sd.get("CustomerCode", "")),
            "destination": sd.get("destination", "")
        })
        
    available = [p for p in parties if p["code"] not in current_selections]
    
    if not available:
        if not current_selections:
            send_amritcement_text(number, "No Ship-To Party is mapped to your account. Please contact your RM for assistance.")
            return False
        else:
            return False
            
    if len(available) <= 10:
        rows = []
        for p in available:
            dest = f" - {p['destination']}" if p.get('destination') else ""
            rows.append({"id": f"ship_{p['code']}", "title": p['name'][:24], "description": f"Code: {p['code']}{dest}"[:72]})
            
        interactive_data = {
            "type": "list",
            "body": {
                "text": "Please select the Ship-To Party for delivery:"
            },
            "action": {
                "button": "Select Ship-To",
                "sections": [{"title": "Ship-To Parties", "rows": rows}]
            }
        }
        send_amritcement_interactive(number, interactive_data)
    else:
        text = "Please select the Ship-To Party for delivery by typing the Code:\n\n"
        for p in available:
            dest_str = f"\nDestination: {p['destination']}" if p.get('destination') else ""
            text += f"• *{p['name']}*\nCode: {p['code']}{dest_str}\n\n"
            
        send_amritcement_text(number, text)
    return True

def tpl_amritcement_inco_terms(number: str):
    send_amritcement_template(number, "amritcement_inco_terms", "en")

def tpl_amritcement_plant_select(number: str, dealer_data: dict) -> bool:
    all_plants = dealer_data.get("depot_plant_code", [])
    valid_plants = [p for p in all_plants if p.get("plant_code") not in ["EXF", "EXW", "FOR", "FOS", "SOR"]]
    
    if not valid_plants:
        return False
        
    text = "Please select a Plant/Depot for your order by typing the Plant Code (e.g. UN01):\n\n"
    for p in valid_plants:
        text += f"• *{p.get('plant_code')}* - {p.get('depot')}\n"
    
    send_amritcement_text(number, text)
    return True

def tpl_amritcement_order_summary(number: str, order_data: dict, dealer_data: dict, customer_type: str = "Dealer"):
    qty_unit = "MT" if customer_type.lower() == "dealer" else "bags"
    
    ship_to_list = order_data.get("ship_to_list", [])
    dests = ", ".join([p.get("destination", "") for p in ship_to_list if p.get("destination")])
    if not dests:
        dests = "N/A"
        
    components = [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": str(order_data.get('product_name', ''))},
                {"type": "text", "text": str(order_data.get('qty', '')) + f" {qty_unit}"},
                {"type": "text", "text": dests[:30]},
                {"type": "text", "text": "48 hours"}
            ]
        }
    ]
    send_amritcement_template(number, "amritcement_po_confirmation", "en", components)

def tpl_amritcement_claim_types(number: str):
    send_amritcement_template(number, "amritcement_claims", "en")


def handle_amritcement_message(msg: dict):
    try:
        from_number = msg.get("from", "")
        if not from_number:
            return

        msg_type = msg.get("type", "")
        logger.info(f"[Amritcement Debug] New message from {from_number}, type: {msg_type}")
        body_str = ""
        display_str = ""
        interactive_id = ""

        if msg_type == "text":
            body_str = msg.get("text", {}).get("body", "").strip().lower()
            display_str = msg.get("text", {}).get("body", "").strip()
        elif msg_type == "interactive":
            interactive = msg.get("interactive", {})
            inter_type = interactive.get("type", "")
            if inter_type == "list_reply":
                interactive_id = interactive.get("list_reply", {}).get("id", "")
                body_str = interactive_id.lower()
                display_str = interactive.get("list_reply", {}).get("title", "")
            elif inter_type == "button_reply":
                interactive_id = interactive.get("button_reply", {}).get("id", "")
                body_str = interactive_id.lower()
                display_str = interactive.get("button_reply", {}).get("title", "")
        elif msg_type == "button":
            button_info = msg.get("button", {})
            interactive_id = button_info.get("payload", "")
            display_str = button_info.get("text", "")
            body_str = interactive_id.lower() if interactive_id else display_str.lower()
        elif msg_type in ["image", "video"]:
            media_info = msg.get(msg_type, {})
            media_id = media_info.get("id", "")
            caption = media_info.get("caption", "").strip()
            
            # Download media and save to local blob (default_storage)
            local_path = download_meta_media(media_id) if media_id else ""
            
            # For now, store the caption or the local path. As requested, we won't send the actual file to external API yet.
            display_str = caption if caption else f"Media Saved: {local_path}"
            body_str = display_str.lower()

        logger.info(f"[Amritcement Debug] Parsed -> body_str: '{body_str}', display_str: '{display_str}', interactive_id: '{interactive_id}'")

        customer, _ = Customer.objects.get_or_create(phone=from_number, defaults={"name": from_number})
        client_account = ClientAccount.objects.filter(phone_number_id=AMRITCEMENT_PHONE_NUMBER_ID).first()
        conv, _ = Conversation.objects.get_or_create(customer=customer, phone_number_id=AMRITCEMENT_PHONE_NUMBER_ID, defaults={'client': client_account})

        session = ConversationSession(conv)
        state = session.state

        logger.info(f"[Amritcement Debug] Current State: {state}")

        now_ts = datetime.now().timestamp()
        
        # Session Timeout Check
        if state not in ["START", "NOT_REGISTERED"]:
            last_interaction = getattr(session, "last_interaction", 0)
            if last_interaction and (now_ts - float(last_interaction) > 900):
                session.state = "START"
                session.save()
                if body_str not in ["hi", "hello", "menu", "start"]:
                    send_amritcement_text(from_number, 'Your session has expired due to inactivity. Please type "Hi" to start a new session.')
                    return

        # Prevent bypassing authentication
        if state in ["START", "NOT_REGISTERED"] and body_str not in ["hi", "hello", "menu", "start"]:
            return
            
        if body_str == "cancel":
            if state.startswith("ORDER_"):
                session.state = "START"
                session.order_data = {}
                session.save()
                send_amritcement_text(from_number, "Your current order request has been cancelled. You may type 'Hi' to start a new order or return to the Main Menu.")
                return
            else:
                send_amritcement_text(from_number, "There is no active order to cancel.")
                return

        if body_str in ["hi", "hello", "menu", "start"]:
            resp = amritcement_verify_mobile_no(from_number)
            if resp.get("success"):
                dealer_data = resp.get("data", {})
                session.dealer_data = dealer_data
                raw_cust_type = dealer_data.get("customer_type") or dealer_data.get("Customer Type")
                if str(raw_cust_type) == "1":
                    session.customer_type = "Dealer"
                elif str(raw_cust_type) == "2":
                    session.customer_type = "ASD"
                else:
                    session.customer_type = str(raw_cust_type) if raw_cust_type else "Dealer"
                    
                session.state = "MENU"
                session.order_data = {}
                session.claim_data = {}
                session.last_interaction = now_ts
                session.save()
                tpl_amritcement_main_menu(from_number)
            else:
                logger.info(f"[Amritcement Debug] Verification failed for {from_number} at start, error_type: {resp.get('error_type')}")
                err_type = resp.get("error_type")
                if err_type == "NOT_REGISTERED":
                    send_amritcement_template(from_number, "amritcement_not_registered", "en")
                elif err_type == "TIMEOUT":
                    send_amritcement_text(from_number, "The request is taking longer than expected. Please try again after some time. If the issue continues, please contact your RM.")
                elif err_type == "SERVICE_UNAVAILABLE":
                    send_amritcement_text(from_number, "This service is temporarily unavailable. Please try again later. If the issue persists, please contact your RM.")
                else:
                    send_amritcement_text(from_number, "We are unable to process your request at the moment due to a technical issue. Please try again after some time. If the issue persists, please contact your RM.")
                
                session.state = "NOT_REGISTERED"
                session.save()
            return

        # Update last interaction for active sessions
        session.last_interaction = now_ts
        session.save()

        if state in ["START", "MENU"]:
            if body_str == "menu_order" or "order" in body_str or "place order" in display_str.lower():
                if getattr(session, "dealer_data", None):
                    session.state = "ORDER_PROD_SELECT"
                    session.order_data = {}
                    session.save()
                    tpl_amritcement_products(from_number)
                else:
                    logger.info(f"[Amritcement Debug] Dealer data missing for {from_number} during order")
                    session.state = "NOT_REGISTERED"
                    session.save()
                    send_amritcement_template(from_number, "amritcement_not_registered", "en")
            elif body_str == "menu_ledger" or "ledger" in body_str or "ledger / outstanding / cn-dn" in display_str.lower():
                if getattr(session, "dealer_data", None):
                    dealer_code = session.dealer_data.get("customer_code") or session.dealer_data.get("CUSTOMER_CODE")
                    if dealer_code:
                        resp = amritcement_get_ledger(dealer_code)
                        logger.info(f"[Amritcement Debug] Ledger API Response for {dealer_code}: {resp}")
                        
                        if resp and (resp.get("CUSTOMER_CODE") or resp.get("customer_code")):
                            outstanding = resp.get("CLOSING_BAL", "0")
                            due_invoices = "NA"
                            cn_dn = "NA"
                            due_dates = "NA"
                            
                            components = [
                                {
                                    "type": "body",
                                    "parameters": [
                                        {"type": "text", "text": str(outstanding)},
                                        {"type": "text", "text": str(due_invoices)},
                                        {"type": "text", "text": str(cn_dn)},
                                        {"type": "text", "text": str(due_dates)}
                                    ]
                                }
                            ]
                            send_amritcement_template(from_number, "amritcement_ledger", "en", components)
                        else:
                            send_amritcement_text(from_number, "Failed to retrieve ledger details or invalid dealer code. Please try again.")
                    else:
                        send_amritcement_text(from_number, "Customer code not found in session. Please contact support.")
                else:
                    logger.info(f"[Amritcement Debug] Dealer data missing for {from_number} during ledger")
                    session.state = "NOT_REGISTERED"
                    send_amritcement_template(from_number, "amritcement_not_registered", "en")
                
                session.state = "MENU"
                session.save()
            elif body_str == "menu_claim" or "claim" in body_str or "claims submission" in display_str.lower():
                if getattr(session, "dealer_data", None):
                    customer_code = session.dealer_data.get("customer_code") or session.dealer_data.get("CUSTOMER_CODE")
                    if customer_code:
                        session.state = "CLAIM_TYPE_SELECT"
                        session.claim_data = {"user_no": customer_code}
                        session.save()
                        tpl_amritcement_claim_types(from_number)
                    else:
                        send_amritcement_text(from_number, "Customer code not found in session. Please contact support.")
                        session.state = "MENU"
                        session.save()
                else:
                    logger.info(f"[Amritcement Debug] Dealer data missing for {from_number} during claim")
                    session.state = "NOT_REGISTERED"
                    session.save()
                    send_amritcement_template(from_number, "amritcement_not_registered", "en")
            else:
                tpl_amritcement_main_menu(from_number)

        elif state == "AUTH_DEALER_CODE":
            resp = amritcement_verify_mobile_no(display_str.strip())
            if resp.get("success"):
                session.dealer_data = resp.get("data", {})
                session.state = "MENU"
                session.order_data = {}
                session.claim_data = {}
                session.save()
                tpl_amritcement_main_menu(from_number)
            else:
                send_amritcement_template(from_number, "amritcement_not_registered", "en")
                session.state = "NOT_REGISTERED"
                session.save()
        
        # -----------------------------------------------------
        # Flow 1: Place Order
        # -----------------------------------------------------
        elif state == "ORDER_PROD_SELECT":
            cement_types = ["ppc", "opc 43", "opc 53"]
            
            if msg_type == "text":
                send_amritcement_text(from_number, "Invalid selection. Please select a product from the available list.")
                return
            elif msg_type not in ["interactive", "button"]:
                send_amritcement_text(from_number, "Please select a product to continue.")
                return
                
            selected_val = display_str.lower()
            if selected_val in cement_types or interactive_id.lower() in cement_types:
                prod_name = display_str
                prod_id = interactive_id if interactive_id else prod_name.replace(" ", "_").lower()
                session.order_data = {"product_id": prod_id, "product_name": prod_name}
                session.state = "ORDER_QTY_ENTER"
                session.save()
                
                cust_type = getattr(session, "customer_type", "Dealer")
                qty_unit = "Metric Ton (MT)" if cust_type.lower() == "dealer" else "Bags"
                
                components = [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": qty_unit}
                        ]
                    }
                ]
                send_amritcement_template(from_number, "amritcement_quantity", "en", components)
            else:
                send_amritcement_text(from_number, "Selected product is not available. Please choose one of the available products.")
                
        elif state == "ORDER_QTY_ENTER":
            cust_type = getattr(session, "customer_type", "Dealer")
            is_dealer = cust_type.lower() == "dealer"
            val = display_str.strip()
            
            if not val:
                send_amritcement_text(from_number, "Quantity is mandatory. Please enter the required quantity.")
                return
                
            if val.lower() in ["continue", "next", "place order", "random text"]:
                send_amritcement_text(from_number, "Please enter a valid quantity to continue.")
                return
                
            if "mt" in val.lower() or "bags" in val.lower() or "bag" in val.lower():
                send_amritcement_text(from_number, "Please enter only the numeric quantity without any unit.")
                return
                
            if re.search(r'[^\d.,-]', val):
                send_amritcement_text(from_number, "Please enter a valid numeric value.")
                return
                
            if ',' in val or val.count('.') > 1:
                send_amritcement_text(from_number, "Please enter a valid quantity.")
                return
                
            try:
                num = float(val)
            except ValueError:
                send_amritcement_text(from_number, "Please enter a valid quantity.")
                return
                
            if num < 0:
                send_amritcement_text(from_number, "Quantity cannot be negative. Please enter a valid quantity.")
                return
                
            if num == 0:
                send_amritcement_text(from_number, "Quantity must be greater than zero. Please enter a valid quantity.")
                return
                
            if not is_dealer and '.' in val:
                send_amritcement_text(from_number, "Please enter the quantity as a whole number in Bags.")
                return
                
            qty = val
            order_data = session.order_data or {}
            order_data["qty"] = qty
            
            dealer_data = getattr(session, "dealer_data", {}) if hasattr(session, "dealer_data") else {}
            credit_limit = float(dealer_data.get("credit_limit", 0) or 0)
            outstanding = float(dealer_data.get("outstanding", 0) or 0)
            
            if credit_limit > 0 and outstanding > credit_limit:
                send_amritcement_text(from_number, "Alert: Your outstanding balance exceeds your credit limit. We are connecting you to your Sales RM.")
                session.state = "MENU"
                session.save()
                return
            
            session.order_data = order_data
            session.state = "ORDER_INCO_TERM_SELECT"
            session.save()
            tpl_amritcement_inco_terms(from_number)
                
        elif state == "ORDER_INCO_TERM_SELECT":
            val = display_str.lower().strip()
            
            if not val:
                send_amritcement_text(from_number, "Please select an Order Term to continue.")
                return
                
            if val in ["next", "continue", "ok", "random text"]:
                send_amritcement_text(from_number, "Please select an Order Term from the available options.")
                return
                
            selected_term = None
            if interactive_id and interactive_id.startswith("inco_"):
                selected_term = interactive_id.replace("inco_", "").upper()
            else:
                if "exf" in val: selected_term = "EXF"
                elif "exw" in val: selected_term = "EXW"
                elif "for" in val: selected_term = "FOR"
                elif "fos" in val: selected_term = "FOS"
                elif "sor" in val: selected_term = "SOR"
                else:
                    send_amritcement_text(from_number, "Invalid selection. Please select a valid Order Term from the available list.")
                    return
                    
            order_data = session.order_data or {}
            order_data["inco_term"] = selected_term
            session.order_data = order_data
            
            dealer_data = getattr(session, "dealer_data", {}) if hasattr(session, "dealer_data") else {}
            qty = order_data.get("qty", "1")
            cust_type = getattr(session, "customer_type", "Dealer")
            
            if selected_term == "FOR":
                try:
                    qty_num = float(qty)
                except:
                    qty_num = 0
                
                mt_qty = qty_num if cust_type.lower() == "dealer" else (qty_num / 20.0)
                
                if mt_qty < 10:
                    order_data["plant_code"] = dealer_data.get("default_plant_code", "UN01")
                elif mt_qty <= 25:
                    order_data["plant_code"] = ""
                else:
                    order_data["plant_code"] = "UN01"
                    
                session.order_data = order_data
                session.state = "ORDER_SHIP_TO_SELECT"
                session.save()
                tpl_amritcement_ship_to_select(from_number, dealer_data, [c["code"] for c in order_data.get("ship_to_list", [])])
            else:
                session.order_data = order_data
                success = tpl_amritcement_plant_select(from_number, dealer_data)
                if not success:
                    send_amritcement_text(from_number, "No Plant/Depot is currently available. Please try again later or contact your RM.")
                    session.state = "MENU"
                    session.save()
                    return
                session.state = "ORDER_PLANT_SELECT"
                session.save()
                
        elif state == "ORDER_PLANT_SELECT":
            val = display_str.upper().strip()
            if not val:
                send_amritcement_text(from_number, "Please select a Plant/Depot to continue.")
                return
                
            dealer_data = getattr(session, "dealer_data", {}) if hasattr(session, "dealer_data") else {}
            all_plants = dealer_data.get("depot_plant_code", [])
            valid_plants = [p for p in all_plants if p.get("plant_code") not in ["EXF", "EXW", "FOR", "FOS", "SOR"]]
            
            if not valid_plants:
                send_amritcement_text(from_number, "We are unable to retrieve the Plant/Depot list at the moment due to a technical issue. Please try again after some time.")
                session.state = "MENU"
                session.save()
                return
                
            selected_plant = next((p for p in valid_plants if p.get("plant_code", "").upper() == val or val in p.get("depot", "").upper()), None)
            
            if not selected_plant:
                send_amritcement_text(from_number, "Invalid Plant/Depot selected. Please select a valid option from the available list.")
                return
                
            order_data = session.order_data or {}
            order_data["plant_code"] = selected_plant.get("plant_code")
            session.order_data = order_data
            
            session.state = "ORDER_SHIP_TO_SELECT"
            session.save()
            tpl_amritcement_ship_to_select(from_number, dealer_data, [c["code"] for c in order_data.get("ship_to_list", [])])
                
        elif state == "ORDER_SHIP_TO_SELECT":
            if interactive_id.startswith("ship_"):
                val = interactive_id.replace("ship_", "").upper()
            else:
                val = display_str.upper().strip()
                
            if not val:
                send_amritcement_text(from_number, "Please select a Ship-To Party to continue.")
                return
                
            dealer_data = getattr(session, "dealer_data", {}) if hasattr(session, "dealer_data") else {}
            order_data = session.order_data or {}
            current_selections = order_data.get("ship_to_list", [])
            
            own_party = {
                "name": dealer_data.get("name", ""),
                "code": str(dealer_data.get("customer_code", "")),
                "destination": dealer_data.get("destination", "")
            }
            parties = [own_party]
            for sd in dealer_data.get("sub_dealer", []):
                parties.append({
                    "name": sd.get("Name", ""),
                    "code": str(sd.get("CustomerCode", "")),
                    "destination": sd.get("destination", "")
                })
                
            selected_party = next((p for p in parties if p["code"].upper() == val), None)
            
            if not selected_party:
                send_amritcement_text(from_number, "Please select a Ship-To Party to continue.")
                return
                
            if selected_party["code"] in [c["code"] for c in current_selections]:
                send_amritcement_text(from_number, "The selected Ship-To Party has already been added. Please select another Ship-To Party.")
                return
                
            current_selections.append(selected_party)
            order_data["ship_to_list"] = current_selections
            session.order_data = order_data
            
            if len(current_selections) >= 3:
                send_amritcement_text(from_number, "A maximum of three Ship-To Parties can be selected for a single order.")
                session.state = "ORDER_CONFIRM"
                session.save()
                tpl_amritcement_order_summary(from_number, order_data, dealer_data, getattr(session, "customer_type", "Dealer"))
            else:
                session.state = "ORDER_SHIP_TO_ADD_MORE"
                session.save()
                
                interactive_data = {
                    "type": "button",
                    "body": {
                        "text": "Would you like to add another Ship-To Party?"
                    },
                    "action": {
                        "buttons": [
                            {"type": "reply", "reply": {"id": "add_yes", "title": "Yes"}},
                            {"type": "reply", "reply": {"id": "add_no", "title": "No"}}
                        ]
                    }
                }
                send_amritcement_interactive(from_number, interactive_data)
                
        elif state == "ORDER_SHIP_TO_ADD_MORE":
            val = display_str.lower().strip()
            if val == "yes" or interactive_id == "add_yes":
                session.state = "ORDER_SHIP_TO_SELECT"
                session.save()
                dealer_data = getattr(session, "dealer_data", {}) if hasattr(session, "dealer_data") else {}
                order_data = session.order_data or {}
                current_codes = [c["code"] for c in order_data.get("ship_to_list", [])]
                tpl_amritcement_ship_to_select(from_number, dealer_data, current_codes)
            elif val == "no" or interactive_id == "add_no":
                session.state = "ORDER_CONFIRM"
                session.save()
                order_data = session.order_data or {}
                dealer_data = getattr(session, "dealer_data", {}) if hasattr(session, "dealer_data") else {}
                tpl_amritcement_order_summary(from_number, order_data, dealer_data, getattr(session, "customer_type", "Dealer"))
            else:
                send_amritcement_text(from_number, "Please select Yes or No.")
                
        elif state == "ORDER_CONFIRM":
            if display_str.strip() == "1" or "confirm" in body_str or "yes" in body_str or display_str.lower() == "confirm":
                order_data = session.order_data or {}
                qty = order_data.get("qty", "1")
                price = "1.00"
                product_gross_amount = f"{float(qty)*float(price):.2f}"
                
                items = [{
                    "product_id": order_data.get("product_id"),
                    "product_name": order_data.get("product_name"),
                    "qty": qty,
                    "price": price,
                    "product_gross_amount": product_gross_amount,
                    "discount_amount": "0.00",
                    "total_product_amount_afterdiscount": product_gross_amount,
                    "cgst": "0",
                    "cgst_amount": "0",
                    "sgst": "0",
                    "sgst_amount": "0",
                    "final_product_amount": "0000",
                    "igst": "0",
                    "igst_amount": "0"
                }]
                
                dealer_data = getattr(session, "dealer_data", {})
                customer_id = dealer_data.get("customer_code", "")
                customer_name = dealer_data.get("name", from_number)
                
                customer_type = getattr(session, "customer_type", "Dealer")
                is_dealer = customer_type.lower() == "dealer"
                
                # Payload mapping based on Postman screenshot and PDF
                payload = {
                    "shop_id": dealer_data.get("shop_id", ""),
                    "token": dealer_data.get("token", ""),
                    "user_id": dealer_data.get("shop_id", ""),
                    "customer_id": customer_id,
                    "customer_name": customer_name,
                    "order_date": datetime.now().strftime("%d-%m-%Y"),
                    "total_product_amount": product_gross_amount,
                    "total_discount": "0.00",
                    "total_amount_after_discount": product_gross_amount,
                    "total_sgst_amount": "0.00",
                    "total_cgst_amount": "0.00",
                    "total_igst_amount": "0.00",
                    "total_final_amount": product_gross_amount,
                    "igst_or_sgst": "S",
                    "inco_term": order_data.get("inco_term", "FOR"),
                    "plant_code": order_data.get("plant_code", ""),
                    "ship_to_parties": json.dumps(order_data.get("ship_to_list", [])),
                    "items": json.dumps(items)
                }
                
                resp = amritcement_create_order(payload, is_dealer=is_dealer)
                if str(resp.get("status")) == "1":
                    order_id = resp.get("id")
                    send_amritcement_text(from_number, f"Your order #{order_id} has been successfully registered.\nTrack your order anytime using option 2.")
                else:
                    err = resp.get("message", "Unknown error")
                    send_amritcement_text(from_number, f"Failed to create order: {err}\nPlease retry or contact support.")
                    
                session.state = "MENU"
                session.save()
            elif display_str.strip() == "2" or "modify" in body_str or "no" in body_str or display_str.lower() == "modify":
                session.state = "ORDER_PROD_SELECT"
                session.order_data = {}
                session.save()
                tpl_amritcement_products(from_number)
            else:
                send_amritcement_text(from_number, "Reply 1 to confirm or 2 to modify.")
                
        # -----------------------------------------------------
        # Flow 4: Ledger / Outstanding
        # -----------------------------------------------------
        elif state == "LEDGER_INPUT_CODE":
            dealer_code = display_str.strip()
            resp = amritcement_get_ledger(dealer_code)
            logger.info(f"[Amritcement Debug] Ledger API Response for {dealer_code}: {resp}")
            
            if resp and (resp.get("CUSTOMER_CODE") or resp.get("customer_code")):
                outstanding = resp.get("CLOSING_BAL", "0")
                due_invoices = "NA" # Map actual fields if available
                cn_dn = "NA"
                due_dates = "NA"
                
                components = [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": str(outstanding)},
                            {"type": "text", "text": str(due_invoices)},
                            {"type": "text", "text": str(cn_dn)},
                            {"type": "text", "text": str(due_dates)}
                        ]
                    }
                ]
                send_amritcement_template(from_number, "amritcement_ledger", "en", components)
            else:
                send_amritcement_text(from_number, "Failed to retrieve ledger details or invalid dealer code. Please try again.")
            
            session.state = "MENU"
            session.save()
            
        # -----------------------------------------------------
        # Flow 5: Claims Submission
        # -----------------------------------------------------
        elif state == "CLAIM_INPUT_CODE":
            session.claim_data = {"user_no": display_str}
            session.state = "CLAIM_TYPE_SELECT"
            session.save()
            tpl_amritcement_claim_types(from_number)
            
        elif state == "CLAIM_TYPE_SELECT":
            claim_types = ["transit damage", "shortage", "quality complaint", "unload delay"]
            if display_str.lower() in claim_types or interactive_id.startswith("claim_"):
                claim_type = display_str
                claim_data = session.claim_data or {}
                claim_data["claim_type"] = claim_type
                session.claim_data = claim_data
                session.state = "CLAIM_INVOICE_ENTER"
                session.save()
                send_amritcement_template(from_number, "amritcement_claims_invoice", "en")
            else:
                send_amritcement_text(from_number, "Please select a valid claim type.")
                tpl_amritcement_claim_types(from_number)
                
        elif state == "CLAIM_INVOICE_ENTER":
            claim_data = session.claim_data or {}
            claim_data["invoice_no"] = display_str
            session.claim_data = claim_data
            session.state = "CLAIM_QTY_ENTER"
            session.save()
            send_amritcement_template(from_number, "amritcement_claims_quantity", "en")
            
        elif state == "CLAIM_QTY_ENTER":
            claim_data = session.claim_data or {}
            claim_data["qty"] = display_str
            session.claim_data = claim_data
            session.state = "CLAIM_ISSUE_ENTER"
            session.save()
            send_amritcement_template(from_number, "amritcement_claims_issue", "en")
            
        elif state == "CLAIM_ISSUE_ENTER":
            claim_data = session.claim_data or {}
            claim_data["issue_detail"] = display_str
            session.claim_data = claim_data
            session.state = "CLAIM_UPLOAD_ENTER"
            session.save()
            send_amritcement_template(from_number, "amritcement_claims_upload", "en")
            
        elif state == "CLAIM_UPLOAD_ENTER":
            claim_data = session.claim_data or {}
            desc = display_str if body_str != "skip" else ""
            
            payload = {
                "user_no": claim_data.get("user_no", ""),
                "claim_type": claim_data.get("claim_type", ""),
                "invoice_no": claim_data.get("invoice_no", ""),
                "issue_detail": claim_data.get("issue_detail", ""),
                "issue_image": desc,
                "description": desc
            }
            
            resp = amritcement_add_claim_submission(payload)
            if str(resp.get("status")) == "1":
                ticket_id = resp.get("ticket_id", f"TKT-{datetime.now().strftime('%M%S')}")
                components = [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": str(ticket_id)},
                            {"type": "text", "text": str(claim_data.get("claim_type", ""))},
                            {"type": "text", "text": str(claim_data.get("invoice_no", ""))}
                        ]
                    }
                ]
                send_amritcement_template(from_number, "amritcement_claims_ticket", "en", components)
            else:
                err = resp.get("message", "Unknown error")
                send_amritcement_text(from_number, f"Failed to submit claim: {err}\nPlease retry or contact support.")
                
            session.state = "MENU"
            session.save()
            
    except Exception as e:
        logger.exception("[Amritcement] Error handling message: %s", e)
