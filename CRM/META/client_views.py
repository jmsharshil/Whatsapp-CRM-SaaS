"""
CRM/META/client_views.py

Multi-Tenant Client Bot Flow
==============================

Each ClientAccount onboarded via Embedded Signup gets its own bot flow.
Messages are routed here by webhook_views.py based on phone_number_id.

Bot stages (uses ConversationState model):
    greeting  →  qualifying  →  complete  →  human_handoff

Each client has ISOLATED conversations:
    Customer 9999 + Client A = Conversation #101
    Customer 9999 + Client B = Conversation #102  (completely separate)

All messages are sent via client_utils.py using the CLIENT's own
access_token + phone_number_id — never JMS TechNova's credentials.
"""

import json
import logging
import re

from django.db import transaction
from django.utils import timezone

from CRM.models import (
    ClientAccount,
    Conversation,
    ConversationState,
    Customer,
    Message,
)
from CRM.META.client_utils import (
    client_send_text,
    client_send_buttons,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Customer + Conversation helpers (multi-tenant)
# ─────────────────────────────────────────────────────────────────────────────

def _upsert_customer(phone: str, profile_name: str = "") -> Customer:
    """
    Get or create a Customer by phone.
    Customers are global — same person can talk to multiple clients.
    """
    customer, created = Customer.objects.get_or_create(
        phone=phone,
        defaults={"name": profile_name or phone},
    )
    if not created and profile_name and customer.name in (customer.phone, "Unknown"):
        customer.name = profile_name
        customer.save(update_fields=["name"])
    return customer


def _get_or_create_conversation(
    customer: Customer, client: ClientAccount
) -> Conversation:
    """
    Each client has ISOLATED conversations.
    Customer 9999 messaging Client A and Client B = 2 separate conversations.
    """
    conv = (
        Conversation.objects
        .filter(customer=customer, client=client, status="prospect")
        .order_by("-created_at")
        .first()
    )
    if not conv:
        conv = Conversation.objects.create(
            customer=customer,
            client=client,
            client_name=client.name,
            status="prospect",
        )
        logger.info(
            "[ClientBot] New conversation id=%s customer=%s client=%s",
            conv.id, customer.phone, client.name,
        )
    return conv


def _get_or_create_state(
    conversation: Conversation, client: ClientAccount
) -> ConversationState:
    """Get or create ConversationState for this conversation."""
    try:
        return conversation.chatbot_state
    except ConversationState.DoesNotExist:
        return ConversationState.objects.create(
            conversation=conversation,
            organization=client.tech_provider,
            stage="greeting",
            collected_fields={},
        )


def _save_inbound_message(
    conversation: Conversation,
    client: ClientAccount,
    customer: Customer,
    body: str,
    wa_msg_id: str = "",
    msg_type: str = "text",
    timestamp=None,
) -> Message:
    """Save an inbound customer message to DB."""
    return Message.objects.create(
        conversation=conversation,
        client=client,
        customer=customer,
        content=body,
        direction="inbound",
        message_type=msg_type,
        meta_message_id=wa_msg_id,
        status="received",
        client_name=client.name,
    )


def _save_outbound_message(
    conversation: Conversation,
    client: ClientAccount,
    customer: Customer,
    body: str,
) -> Message:
    """Save an outbound bot reply to DB."""
    return Message.objects.create(
        conversation=conversation,
        client=client,
        customer=customer,
        content=body,
        direction="outbound",
        message_type="text",
        status="sent",
        client_name=client.name,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Text extraction from Meta payload
# ─────────────────────────────────────────────────────────────────────────────

def _extract_text(msg_payload: dict) -> str:
    """Extract user text from various Meta message types."""
    msg_type = msg_payload.get("type", "text")

    if msg_type == "text":
        return msg_payload.get("text", {}).get("body", "").strip()

    if msg_type == "interactive":
        interactive = msg_payload.get("interactive", {})
        itype = interactive.get("type", "")
        if itype == "button_reply":
            return interactive.get("button_reply", {}).get("title", "").strip()
        if itype == "list_reply":
            return interactive.get("list_reply", {}).get("title", "").strip()
        return ""

    if msg_type == "button":
        return msg_payload.get("button", {}).get("text", "").strip()

    if msg_type == "image":
        return msg_payload.get("image", {}).get("caption", "").strip()

    if msg_type == "document":
        return msg_payload.get("document", {}).get("caption", "").strip()

    if msg_type == "location":
        loc = msg_payload.get("location", {})
        return f"[Location: {loc.get('latitude', '')}, {loc.get('longitude', '')}]"

    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Bot reply helper
# ─────────────────────────────────────────────────────────────────────────────

def _bot_reply(client: ClientAccount, phone: str, text: str,
               conversation: Conversation = None,
               customer: Customer = None):
    """Send bot reply + save to DB."""
    client_send_text(client, phone, text)
    if conversation and customer:
        _save_outbound_message(conversation, client, customer, text)


# ─────────────────────────────────────────────────────────────────────────────
# Stage handlers
# ─────────────────────────────────────────────────────────────────────────────

def _handle_greeting(client, customer, conversation, state, phone, text):
    """
    First message — send welcome + ask for name.
    """
    welcome = (
        f"👋 Welcome! Thank you for contacting *{client.name}*.\n\n"
        f"I'm here to help you. Let me quickly understand your needs.\n\n"
        f"Could you please share your *name*?"
    )
    _bot_reply(client, phone, welcome, conversation, customer)

    state.stage = "qualifying"
    state.collected_fields = {}
    state.message_count = 1
    state.last_bot_message = welcome
    state.save()


def _handle_qualifying(client, customer, conversation, state, phone, text):
    """
    Collect qualifying fields one by one:
      1. name
      2. service/need (what they're looking for)
      3. email or preferred contact

    Once all fields collected → move to complete.
    """
    fields = state.collected_fields or {}
    state.message_count = (state.message_count or 0) + 1

    # ── Collect name ──────────────────────────────────────────────────────
    if "name" not in fields:
        fields["name"] = text.strip()
        state.collected_fields = fields
        state.save()

        # Update customer name if it was just a phone number
        if customer.name in (customer.phone, "Unknown", ""):
            customer.name = text.strip()
            customer.save(update_fields=["name"])

        reply = (
            f"Nice to meet you, *{fields['name']}*! 😊\n\n"
            f"What are you looking for? Please describe your requirement briefly."
        )
        _bot_reply(client, phone, reply, conversation, customer)
        state.last_bot_message = reply
        state.save()
        return

    # ── Collect requirement ───────────────────────────────────────────────
    if "requirement" not in fields:
        fields["requirement"] = text.strip()
        state.collected_fields = fields
        state.save()

        reply = (
            f"Got it! ✅\n\n"
            f"Could you share your *email address* so our team can reach out to you?"
        )
        _bot_reply(client, phone, reply, conversation, customer)
        state.last_bot_message = reply
        state.save()
        return

    # ── Collect email ─────────────────────────────────────────────────────
    if "email" not in fields:
        # Basic email validation — accept anything with @ or skip
        email_text = text.strip()
        if "@" in email_text:
            fields["email"] = email_text
        else:
            # If they didn't give email, store as "not provided" and move on
            fields["email"] = email_text or "not provided"

        state.collected_fields = fields
        state.is_complete = True
        state.stage = "complete"
        state.save()

        # ── Lead captured — send confirmation ─────────────────────────────
        summary = (
            f"🎉 Thank you, *{fields['name']}*!\n\n"
            f"Here's what I've noted:\n"
            f"📝 *Requirement:* {fields['requirement']}\n"
            f"📧 *Email:* {fields['email']}\n\n"
            f"Our team at *{client.name}* will get in touch with you shortly. "
            f"Feel free to send any additional details in the meantime!"
        )
        _bot_reply(client, phone, summary, conversation, customer)

        # Update conversation status to confirmed (lead)
        conversation.status = "confirmed"
        conversation.save(update_fields=["status"])

        state.last_bot_message = summary
        state.save()
        return


def _handle_complete(client, customer, conversation, state, phone, text):
    """
    Lead already captured. Additional messages are saved and acknowledged.
    Team will see them in CRM inbox.
    """
    reply = (
        f"Thank you for the additional details! ✅\n"
        f"Our team at *{client.name}* has been notified and will respond soon."
    )
    _bot_reply(client, phone, reply, conversation, customer)

    state.message_count = (state.message_count or 0) + 1
    state.last_bot_message = reply
    state.save()


def _handle_human_handoff(client, customer, conversation, state, phone, text):
    """
    Human handoff active — bot is suppressed.
    Messages are still saved to DB for the CRM inbox.
    No bot reply is sent.
    """
    logger.info(
        "[ClientBot] Human handoff active — message saved only. "
        "client=%s phone=%s",
        client.name, phone,
    )
    state.message_count = (state.message_count or 0) + 1
    state.save()


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point — called by webhook_views.py
# ─────────────────────────────────────────────────────────────────────────────

def handle_client_message(
    client: ClientAccount,
    msg_payload: dict,
    contact: dict,
):
    """
    Main entry point for client bot flow.
    Called by WhatsAppWebhookView when a ClientAccount is matched.

    Args:
        client:      The matched ClientAccount
        msg_payload: Meta's message object (from webhook payload)
        contact:     Meta's contact object (profile name etc.)
    """
    phone = msg_payload.get("from", "")
    wa_msg_id = msg_payload.get("id", "")
    msg_type = msg_payload.get("type", "text")
    profile_name = contact.get("profile", {}).get("name", "") if contact else ""

    if not phone:
        return

    # Normalize phone
    if not phone.startswith("+"):
        phone = f"+{phone}"

    # Extract text
    text = _extract_text(msg_payload)
    if not text:
        logger.debug(
            "[ClientBot] Non-text message from %s (type=%s) client=%s — skipping bot",
            phone, msg_type, client.name,
        )
        # Still save non-text message to DB
        customer = _upsert_customer(phone, profile_name)
        conversation = _get_or_create_conversation(customer, client)
        body_desc = {
            "image": "[Image]", "audio": "[Audio]", "video": "[Video]",
            "document": "[Document]", "sticker": "[Sticker]",
            "location": _extract_text(msg_payload) or "[Location]",
        }.get(msg_type, f"[{msg_type}]")
        _save_inbound_message(
            conversation, client, customer,
            body=body_desc, wa_msg_id=wa_msg_id, msg_type=msg_type,
        )
        return

    logger.info(
        "[ClientBot] phone=%s client=%s text=%s",
        phone, client.name, text[:50],
    )

    # ── DB operations ─────────────────────────────────────────────────────
    customer = _upsert_customer(phone, profile_name)
    conversation = _get_or_create_conversation(customer, client)

    # Deduplicate by wa_msg_id
    if wa_msg_id and Message.objects.filter(meta_message_id=wa_msg_id).exists():
        logger.debug("[ClientBot] Duplicate wamid=%s — skipping", wa_msg_id)
        return

    # Save inbound message
    _save_inbound_message(
        conversation, client, customer,
        body=text, wa_msg_id=wa_msg_id, msg_type=msg_type,
    )

    # ── Bot state machine ─────────────────────────────────────────────────
    state = _get_or_create_state(conversation, client)

    stage = state.stage
    logger.info(
        "[ClientBot] stage=%s client=%s phone=%s",
        stage, client.name, phone,
    )

    if stage == "greeting":
        _handle_greeting(client, customer, conversation, state, phone, text)

    elif stage == "qualifying":
        _handle_qualifying(client, customer, conversation, state, phone, text)

    elif stage == "complete":
        _handle_complete(client, customer, conversation, state, phone, text)

    elif stage == "human_handoff":
        _handle_human_handoff(client, customer, conversation, state, phone, text)

    else:
        # Unknown stage — reset to greeting
        logger.warning("[ClientBot] Unknown stage=%s — resetting", stage)
        _handle_greeting(client, customer, conversation, state, phone, text)


# ─────────────────────────────────────────────────────────────────────────────
# Human handoff toggle — can be called from CRM API
# ─────────────────────────────────────────────────────────────────────────────

def set_client_handoff(conversation_id: int, handoff: bool) -> bool:
    """
    Toggle human handoff for a client conversation.
    When handoff=True, bot stops responding; messages are still saved.
    When handoff=False, bot resumes from 'complete' stage.
    """
    try:
        state = ConversationState.objects.get(conversation_id=conversation_id)
        if handoff:
            state.stage = "human_handoff"
            state.human_handoff = True
        else:
            state.stage = "complete"
            state.human_handoff = False
        state.save()
        logger.info(
            "[ClientBot] Handoff=%s for conversation=%s",
            handoff, conversation_id,
        )
        return True
    except ConversationState.DoesNotExist:
        logger.warning(
            "[ClientBot] ConversationState not found for conversation=%s",
            conversation_id,
        )
        return False
