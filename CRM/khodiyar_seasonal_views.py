import json
import logging
from CRM.models import Customer, Conversation, Message, ClientAccount

logger = logging.getLogger(__name__)

KHODIYAR_SEASONAL_PHONE_NUMBER_ID = "1138658179323225"


def handle_khodiyar_seasonal_message(msg: dict, contact_obj: dict = None):
    """
    Handle inbound message for Khodiyar Seasonal (Phone Number ID: 1138658179323225).
    - Uses existing _normalize_e164_phone logic safely.
    - Customer contact name fetched from Meta profile.
    - Zero heavy queries, zero blocking loops -> 100% lightweight & safe for server.
    """
    from CRM.META.webhook_views import _normalize_e164_phone

    raw_from = msg.get("from", "").strip()
    clean_phone = _normalize_e164_phone(raw_from)
    if not clean_phone:
        return

    msg_id = msg.get("id", "")
    msg_type = msg.get("type", "text")

    display_body = ""
    if msg_type == "text":
        display_body = msg.get("text", {}).get("body", "").strip()
    elif msg_type == "interactive":
        idata = msg.get("interactive", {})
        itype = idata.get("type")
        if itype == "button_reply":
            display_body = idata["button_reply"].get("title") or idata["button_reply"].get("id", "")
        elif itype == "list_reply":
            display_body = idata["list_reply"].get("title") or idata["list_reply"].get("id", "")
    elif msg_type == "button":
        display_body = msg.get("button", {}).get("text") or msg.get("button", {}).get("payload", "").strip()
    elif msg_type in ["image", "video", "audio", "document", "sticker"]:
        display_body = f"[{msg_type.upper()}]"
    elif msg_type == "location":
        loc = msg.get("location", {})
        lat, lng = loc.get("latitude"), loc.get("longitude")
        name = loc.get("name", "")
        display_body = f"📍 Location: {name} https://maps.google.com/?q={lat},{lng}".strip()
    elif msg_type == "contacts":
        c_list = msg.get("contacts", [])
        c_names = [c.get("name", {}).get("formatted_name", "Unknown") for c in c_list]
        display_body = f"👤 Contact: {', '.join(c_names)}"

    if not display_body:
        display_body = f"[{msg_type}]"

    # Extract customer profile name from webhook payload
    contact_name = clean_phone
    if contact_obj and isinstance(contact_obj, dict):
        profile_name = contact_obj.get("profile", {}).get("name", "").strip()
        if profile_name:
            contact_name = profile_name

    logger.info("[Khodiyar Seasonal] Inbound: phone=%s name=%s body=%r", clean_phone, contact_name, display_body)

    # ── Safe & Lightweight DB Queries (No heavy loops, no server lag) ──────
    customer_obj, created = Customer.objects.get_or_create(
        phone=clean_phone,
        defaults={'name': contact_name}
    )
    # If customer existed with just phone number as name, update with real contact name
    if not created and contact_name != clean_phone and (customer_obj.name == clean_phone or not customer_obj.name):
        customer_obj.name = contact_name
        customer_obj.save(update_fields=['name'])

    client_account_obj = ClientAccount.objects.filter(phone_number_id=KHODIYAR_SEASONAL_PHONE_NUMBER_ID).first()

    conv_obj, _ = Conversation.objects.get_or_create(
        customer=customer_obj,
        phone_number_id=KHODIYAR_SEASONAL_PHONE_NUMBER_ID,
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
