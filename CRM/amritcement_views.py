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
    # Sends the Meta WhatsApp template named 'amritcement_welcome'
    send_amritcement_template(number, "amritcement_welcome", "en")

def tpl_amritcement_products(number: str):
    send_amritcement_template(number, "amritcement_cement_type", "en")
    
def tpl_amritcement_destinations(number: str, dealer_data: dict, qty: str):
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

    interactive_data = {
        "type": "list",
        "body": {
            "text": f"Quantity: *{qty} bags* • Est. value: {est_str}\n✅ Credit & stock check passed.\n\n*Step 4 — Select Dispatch Location:*"
        },
        "action": {
            "button": "Dispatch locations",
            "sections": [{"title": "Locations", "rows": rows}]
        }
    }
    send_amritcement_interactive(number, interactive_data)

def tpl_amritcement_packing(number: str):
    send_amritcement_template(number, "amritcement_select_packing", "en")

def tpl_amritcement_order_summary(number: str, order_data: dict, dealer_data: dict):
    components = [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": str(order_data.get('product_name', ''))},
                {"type": "text", "text": str(order_data.get('qty', '')) + " bags/MT"},
                {"type": "text", "text": str(order_data.get('destination', ''))},
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

        if body_str in ["hi", "hello", "menu", "start"]:
            resp = amritcement_verify_mobile_no(from_number)
            if resp.get("success"):
                session.dealer_data = resp.get("data", {})
                session.state = "MENU"
                session.order_data = {}
                session.claim_data = {}
                session.save()
                tpl_amritcement_main_menu(from_number)
            else:
                logger.info(f"[Amritcement Debug] Verification failed for {from_number} at start, going to AUTH_DEALER_CODE")
                session.state = "AUTH_DEALER_CODE"
                session.save()
                send_amritcement_text(from_number, "Please share Dealer Code or Registered Mobile No.")
            return

        if state in ["START", "MENU"]:
            if body_str == "menu_order" or "order" in body_str or "place order" in display_str.lower():
                if getattr(session, "dealer_data", None):
                    session.state = "ORDER_PROD_SELECT"
                    session.order_data = {}
                    session.save()
                    tpl_amritcement_products(from_number)
                else:
                    logger.info(f"[Amritcement Debug] Dealer data missing for {from_number} during order")
                    session.state = "AUTH_DEALER_CODE"
                    session.save()
                    send_amritcement_text(from_number, "Please share Dealer Code or Registered Mobile No.")
            elif body_str == "menu_ledger" or "ledger" in body_str or "ledger / outstanding / cn-dn" in display_str.lower():
                session.state = "LEDGER_INPUT_CODE"
                session.save()
                send_amritcement_text(from_number, "Please enter your Dealer/Customer Code to view ledger details:")
            elif body_str == "menu_claim" or "claim" in body_str or "claims submission" in display_str.lower():
                session.state = "CLAIM_INPUT_CODE"
                session.claim_data = {}
                session.save()
                send_amritcement_text(from_number, "Please enter your User Number to proceed with claim submission:")
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
                send_amritcement_text(from_number, "Invalid Code/Number. Please share Dealer Code or Registered Mobile No. or type 'menu' to go back.")
        
        # -----------------------------------------------------
        # Flow 1: Place Order
        # -----------------------------------------------------
        elif state == "ORDER_PROD_SELECT":
            cement_types = ["opc 53", "opc 43", "ppc", "psc", "premium variant (if applicable)"]
            logger.info(f"[Amritcement Debug] In ORDER_PROD_SELECT. Checking if '{display_str.lower()}' in {cement_types} OR '{interactive_id}'.startswith('prod_')")
            if display_str.lower() in cement_types or interactive_id.startswith("prod_"):
                # Use display_str as product_name and a slugified version as id
                prod_name = display_str
                prod_id = interactive_id.replace("prod_", "") if interactive_id else prod_name.replace(" ", "_").lower()
                session.order_data = {"product_id": prod_id, "product_name": prod_name}
                session.state = "ORDER_PACKING_SELECT"
                session.save()
                tpl_amritcement_packing(from_number)
            else:
                send_amritcement_text(from_number, "Please select a valid product from the menu.")
                tpl_amritcement_products(from_number)
                
        elif state == "ORDER_PACKING_SELECT":
            packing_types = ["50 kg bag", "jumbo bag", "bulk order (for big projects)"]
            if display_str.lower() in packing_types or interactive_id.startswith("pack_"):
                pack_type = display_str
                order_data = session.order_data or {}
                order_data["packing"] = pack_type
                session.order_data = order_data
                session.state = "ORDER_QTY_ENTER"
                session.save()
                send_amritcement_template(from_number, "amritcement_quantity", "en")
            else:
                send_amritcement_text(from_number, "Please select a valid packing type.")
                tpl_amritcement_packing(from_number)
                
        elif state == "ORDER_QTY_ENTER":
            if display_str.replace('.', '', 1).isdigit():
                qty = display_str
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
                session.state = "ORDER_DEST_SELECT"
                session.save()
                tpl_amritcement_destinations(from_number, dealer_data, qty)
            else:
                send_amritcement_text(from_number, "Invalid quantity. Please enter quantity in bags or MT.")
                
        elif state == "ORDER_DEST_SELECT":
            if interactive_id.startswith("dest_"):
                if interactive_id == "dest_new":
                    session.state = "ORDER_NEW_DEST_ENTER"
                    session.save()
                    send_amritcement_text(from_number, "Please enter your new address for dispatch location:")
                else:
                    order_data = session.order_data or {}
                    dealer_data = getattr(session, "dealer_data", {}) if hasattr(session, "dealer_data") else {}
                    if interactive_id == "dest_addr1":
                        dest_name = dealer_data.get("address1", display_str)
                    elif interactive_id == "dest_addr2":
                        dest_name = dealer_data.get("address2", display_str)
                    elif interactive_id == "dest_profile":
                        dest_name = dealer_data.get("destination", display_str)
                    else:
                        dest_name = display_str
                        
                    order_data["destination"] = dest_name
                    session.order_data = order_data
                    session.state = "ORDER_CONFIRM"
                    session.save()
                    tpl_amritcement_order_summary(from_number, order_data, dealer_data)
            else:
                send_amritcement_text(from_number, "Please select a valid destination from the list.")
                dealer_data = getattr(session, "dealer_data", {}) if hasattr(session, "dealer_data") else {}
                qty = session.order_data.get("qty", "1")
                tpl_amritcement_destinations(from_number, dealer_data, qty)

        elif state == "ORDER_NEW_DEST_ENTER":
            order_data = session.order_data or {}
            order_data["destination"] = display_str
            session.order_data = order_data
            
            dealer_data = getattr(session, "dealer_data", {}) if hasattr(session, "dealer_data") else {}
            tpl_amritcement_order_summary(from_number, order_data, dealer_data)
            session.state = "ORDER_CONFIRM"
            session.save()
                
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
                
                # Payload mapping based on Postman screenshot and PDF
                payload = {
                    "shop_id": "423",
                    "token": "4b9d9b3b5ff69eb5234bc55f425c78c1",
                    "user_id": "423",
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
                    "items": json.dumps(items)
                }
                
                resp = amritcement_create_order(payload)
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
