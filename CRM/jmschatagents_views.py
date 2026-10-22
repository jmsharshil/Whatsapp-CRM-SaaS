"""
views.py — unified webhook + all three bots in a single file.

Bot routing (by first trigger word / active session):
  • "hi" / "hello" / "hey"  → Industry / Technova AI bot
  • "whatsapp"              → WhatsApp-API assistant bot
  • "jms"                   → JMS-Tech website-analysis bot
"""

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from io import BytesIO
from threading import Lock

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.files.base import File
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Count, Max, Q, Subquery, OuterRef
from django.db.models.functions import TruncDate
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from openai import AzureOpenAI
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

# ── Shared send utilities (single import point for all bots) ──────────────────
from .jmschatagents_utils import (
    send_text,
    send_template,
    send_buttons,
    send_url_button,
    send_list,
    send_interactive_list,
    send_image,
    send_document,
    send_location,
    send_contact,
)

# ── DB models ─────────────────────────────────────────────────────────────────
from .models import *

# ── Industry bot — LLM callers & SSE streams ─────────────────────────────────
from CRM.jms_llms.llm import process_text_web_stream as travel_stream, ask_assistant as _travel_ask
from CRM.jms_llms.sales_llm import process_text_sales_web_stream as sales_stream, ask_insurance as _insurance_ask
from CRM.jms_llms.health_llm import process_text_healthcare_stream as health_stream, ask_assistant as _healthcare_ask
from CRM.jms_llms.edu_llm import process_text_edu_web_stream as education_stream, ask_assistant as _edu_ask
from CRM.jms_llms.estate_llm import process_text_realestate_web_stream as estate_stream, ask_assistant as _estate_ask
from CRM.jms_llms.hospitallity_llm import process_text_hospitality_stream as hospitality_stream, ask_assistant as _hospitality_ask
from CRM.jms_llms.business_llm import process_text_business_stream as business_stream, ask_assistant as _business_ask
from CRM.jms_llms.recruitment_llm import process_text_recruitment_stream as recruitment_stream, ask_assistant as _recruitment_ask
from CRM.jms_llms.customer_service_llm import process_text_customer_service_stream as customer_stream, ask_assistant as _customer_ask, save_session as _cs_save_session
from CRM.jms_llms.sales_marketing_llm import process_text_sales_marketing_stream as salesmarketing_stream, ask_assistant as _marketing_ask
from CRM.jms_llms.entrepreneurship_llm import process_text_entrepreneurship_stream as entrepreneurship_stream, ask_assistant as _entrepreneur_ask
from CRM.jms_llms.eye_llm import process_text_eye_stream as eye_stream
from CRM.jms_llms.mf_llm import ask_mf_assistant

from CRM.jms_llms.views import get_client as _startup_client, AIC_REFERENCE as _AIC_REF
from CRM.jms_llms.retriever import get_context as _get_context

logger = logging.getLogger(__name__)
# print("settings.OPENAI_API_KEY =", getattr(settings, "OPENAI_API_KEY", None))
# print("settings.ENDPOINT_URL =", getattr(settings, "ENDPOINT_URL", None))
# ── Azure OpenAI client (JMS-Tech bot & website analysis) ────────────────────
openai_client = AzureOpenAI(
    api_key=settings.OPENAI_API_KEY,
    azure_endpoint=settings.ENDPOINT_URL,
    api_version="2024-05-01-preview",
)

bot_base_url = "https://jmswhatsappbot-eqhbfhcsaggee5d6.centralindia-01.azurewebsites.net/"

# Tracks whether this is the first GPT report response per server lifetime
is_first_response = True


# ═════════════════════════════════════════════════════════════════════════════
# SHARED UTILITIES
# ═════════════════════════════════════════════════════════════════════════════

def _send(phone: str, text: str) -> None:
    """Fire-and-forget send — never reads or writes any session cache."""
    send_text(phone, text)


def markdown_to_whatsapp(text: str) -> str:
    """Convert Markdown formatting to WhatsApp-native formatting."""
    text = re.sub(r"\*\*(.*?)\*\*",                 r"*\1*",    text)
    text = re.sub(r"__(.*?)__",                      r"*\1*",    text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.*?)\*(?<!\*)", r"_\1_",    text)
    text = re.sub(r"_(.*?)_",                        r"_\1_",    text)
    text = re.sub(r"~~(.*?)~~",                      r"~\1~",    text)
    text = re.sub(r"`([^`]*)`",                      r"`\1`",    text)
    text = re.sub(r"^#{1,6}\s*", "",                 text, flags=re.MULTILINE)
    text = re.sub(r"\[(.*?)\]\((.*?)\)",             r"\1 (\2)", text)
    return text.strip()


def markdown_to_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", text)
    text = re.sub(r"___(.+?)___",        r"<b><i>\1</i></b>", text)
    text = re.sub(r"\*\*(.+?)\*\*",     r"<b>\1</b>",         text)
    text = re.sub(r"__(.+?)__",          r"<b>\1</b>",         text)
    text = re.sub(r"\*(.+?)\*",          r"<i>\1</i>",         text)
    text = re.sub(r"_(.+?)_",            r"<i>\1</i>",         text)
    text = text.replace("\n", "<br>")
    text = re.sub(r"<script.*?>.*?</script>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style.*?>.*?</style>",   "", text, flags=re.IGNORECASE | re.DOTALL)
    return text


# ═════════════════════════════════════════════════════════════════════════════
# SAFE CACHE  — shared base class, instantiated once per bot with its own prefix
# ═════════════════════════════════════════════════════════════════════════════

class SafeCache:
    """
    Tries Django cache; falls back to a thread-safe in-process dict if Redis
    is unavailable.  Each bot gets its own instance with a distinct prefix.
    """

    def __init__(self, prefix: str = "sess:", default_ttl: int = 3600):
        self.prefix        = prefix
        self.default_ttl   = default_ttl
        self._fallback     = {}
        self._lock         = Lock()
        self._use_fallback = False
        try:
            cache.set(f"__smoke_{prefix}__", "ok", 5)
            if cache.get(f"__smoke_{prefix}__") != "ok":
                self._use_fallback = True
                logger.error("SafeCache[%s] smoke test failed; using fallback dict", prefix)
            else:
                logger.info("SafeCache[%s] — cache backend OK", prefix)
        except Exception as e:
            self._use_fallback = True
            logger.error("SafeCache[%s] backend error; fallback dict: %s", prefix, e)

    def _k(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def get(self, key: str, default=None):
        k = self._k(key)
        if self._use_fallback:
            with self._lock:
                return self._fallback.get(k, default)
        try:
            return cache.get(k, default=default)
        except Exception as e:
            logger.error("Cache get failed; fallback: %s", e)
            self._use_fallback = True
            with self._lock:
                return self._fallback.get(k, default)

    def set(self, key: str, value, timeout=None):
        k       = self._k(key)
        timeout = timeout if timeout is not None else self.default_ttl
        if self._use_fallback:
            with self._lock:
                self._fallback[k] = value
            return
        try:
            cache.set(k, value, timeout=timeout)
        except Exception as e:
            logger.error("Cache set failed; fallback: %s", e)
            self._use_fallback = True
            with self._lock:
                self._fallback[k] = value

    def delete(self, key: str):
        k = self._k(key)
        if self._use_fallback:
            with self._lock:
                self._fallback.pop(k, None)
            return
        try:
            cache.delete(k)
        except Exception as e:
            logger.error("Cache delete failed; fallback: %s", e)
            self._use_fallback = True
            with self._lock:
                self._fallback.pop(k, None)


# ── Per-bot session stores (separate key prefixes, no cross-contamination) ────
jms_sessions = SafeCache(prefix="jms_sess:",  default_ttl=60 * 60)   # JMS-Tech bot
wa_sessions  = SafeCache(prefix="wa_sess:",   default_ttl=60 * 15)   # WhatsApp-API bot
ind_sessions = SafeCache(prefix="ind_sess:",  default_ttl=60 * 60)   # Industry bot
shopify_sessions = SafeCache(prefix="shopify_sess:", default_ttl=60 * 15) # Shopify bot
hotel_sessions = SafeCache(prefix="hotel_sess:", default_ttl=60 * 30)     # Hotel service bot
mody_sessions  = SafeCache(prefix="mody_sess:",  default_ttl=60 * 30)     # Mody Toyota bot

JMS_SESSION_TTL = 60 * 60
WA_SESSION_TTL  = 60 * 15
IND_SESSION_TTL = 60 * 60
SHOPIFY_SESSION_TTL = 60 * 15
HOTEL_SESSION_TTL = 60 * 30
MODY_SESSION_TTL  = 60 * 30
SHOPIFY_BOT_TRIGGER = "shopify"
HOTEL_BOT_TRIGGER = "service"
MODY_BOT_TRIGGER = "mody"
HOTEL_NOTIFY_NUMBERS = ["919898271321", "917874566064"]
HOTEL_ADMIN_TEMPLATE = "hotel_service_request"

# ═════════════════════════════════════════════════════════════════════════════
# SHARED DB HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def save_message(phone, content, reply_of=None, client_name=None, client_obj=None, phone_number_id=None):
    """Create or update Customer + Conversation + Message rows."""
    try:
        name_value = (client_name or "").strip() or "Unknown"
        with transaction.atomic():
            customer, _ = Customer.objects.get_or_create(
                phone=phone,
                defaults={"name": name_value},
            )
            if client_name and customer.name != client_name:
                customer.name = client_name
                customer.save(update_fields=["name"])
            
            # Use phone_number_id for conversation isolation, fallback to client_obj's ID
            pid = phone_number_id
            if not pid and client_obj:
                pid = client_obj.phone_number_id
                
            conversation, _ = Conversation.objects.get_or_create(
                customer=customer, 
                phone_number_id=pid,
                defaults={'client': client_obj}
            )
            
            msg = Message.objects.create(
                customer=customer,
                client=client_obj,
                conversation=conversation,
                content=content,
                reply_of=reply_of,
                client_name=client_name,
                direction='inbound',
            )
        return msg
    except Exception as e:
        logger.exception("Error saving message: %s", e)
        return None


def _save_reply(msg_id, text):
    """Create a new outbound Message row for the bot reply."""
    if not msg_id:
        return
    try:
        inbound_msg = Message.objects.filter(id=msg_id).first()
        if not inbound_msg:
            return
        
        # Create a new outbound message for the bot reply
        Message.objects.create(
            conversation=inbound_msg.conversation,
            client=inbound_msg.client,
            customer=inbound_msg.customer,
            direction="outbound",
            message_type="text",
            content=text,
            status="sent"
        )
    except Exception as e:
        logger.exception("_save_reply failed: %s", e)


def admin_reply_and_record(phone: str, text: str, msg_id: int | None = None) -> None:
    """Send admin reply without touching any bot session."""
    send_text(phone, text)
    if not msg_id:
        customer = Customer.objects.filter(phone=phone).first()
        if not customer:
            return
        conversation = Conversation.objects.filter(customer=customer).first()
        if not conversation:
            return
        last_user_msg = (
            Message.objects
            .filter(conversation=conversation)
            .exclude(content="")
            .order_by("-timestamp")
            .first()
        )
        if not last_user_msg:
            return
        msg_id = last_user_msg.id
    _save_reply(msg_id, text)


# ═════════════════════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────
#  BOT 1 — JMS-TECH WEBSITE ANALYSIS BOT  (trigger: "jms")
# ─────────────────────────────────────────────────────────────────────────────
# ═════════════════════════════════════════════════════════════════════════════

PHONE_LIKE             = re.compile(r"^\+?\d{7,15}$")
URL_REGEX              = re.compile(r"https?://(?:www\.)?[^\s/$.?#].[^\s]*", re.IGNORECASE)
ALL_ANALYSIS           = ["Website Analysis", "AI Capability Analysis", "SEO Analysis"]
SESSION_RESET_KEYWORDS = {"jms"}


def normalize_website_url(raw: str) -> str:
    raw = raw.strip().lower()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
        if not rest.startswith("www."):
            rest = "www." + rest
        raw = scheme + "://" + rest
    return raw


def is_trigger_message(text: str) -> bool:
    return any(k.lower() in text.lower() for k in SESSION_RESET_KEYWORDS)


# ── JMS session helpers ───────────────────────────────────────────────────────

def _jms_sess_key(phone: str) -> str:
    return phone


def jms_get_session(phone: str) -> dict:
    key  = _jms_sess_key(phone)
    sess = jms_sessions.get(key)
    if not sess:
        sess = {
            "previous_reply": None,
            "started": True,
            "stage": "greeting",
            "phone": phone,
        }
        jms_sessions.set(key, sess, timeout=JMS_SESSION_TTL)
    return sess


def jms_save_session(session: dict) -> None:
    jms_sessions.set(_jms_sess_key(session["phone"]), session, timeout=JMS_SESSION_TTL)


def jms_clear_session(phone: str) -> None:
    jms_sessions.delete(_jms_sess_key(phone))


# ── JMS reply helper ──────────────────────────────────────────────────────────

def jms_reply_and_record(phone: str, text: str, msg_id: int | None = None) -> None:
    print(f"[JMS REPLY] phone={phone!r} text={text[:50]!r}")
    send_text(phone, text)
    if not msg_id:
        sess   = jms_get_session(phone)
        msg_id = sess.get("last_msg_id")
    if not msg_id:
        logger.warning("jms_reply_and_record: no msg_id for phone=%s", phone)
        return
    _save_reply(msg_id, text)
    sess = jms_get_session(phone)
    sess["previous_reply"] = text
    jms_save_session(sess)


def jms_button_reply_and_record(phone: str, text: str, buttons: list, msg_id: int | None = None) -> None:
    print(f"[JMS BUTTON REPLY] phone={phone!r} text={text[:50]!r}")
    send_buttons(phone, text, buttons)
    if not msg_id:
        sess   = jms_get_session(phone)
        msg_id = sess.get("last_msg_id")
    if not msg_id:
        logger.warning("jms_button_reply_and_record: no msg_id for phone=%s", phone)
        return
    _save_reply(msg_id, text)
    sess = jms_get_session(phone)
    sess["previous_reply"] = text
    jms_save_session(sess)


def jms_url_button_reply_and_record(phone: str, text: str, button_text: str, url: str, msg_id: int | None = None) -> None:
    print(f"[JMS URL BUTTON REPLY] phone={phone!r} text={text[:50]!r}")
    send_url_button(phone, text, button_text, url)
    if not msg_id:
        sess   = jms_get_session(phone)
        msg_id = sess.get("last_msg_id")
    if not msg_id:
        logger.warning("jms_url_button_reply_and_record: no msg_id for phone=%s", phone)
        return
    _save_reply(msg_id, text)
    sess = jms_get_session(phone)
    sess["previous_reply"] = text
    jms_save_session(sess)


def jms_template_reply_and_record(phone: str, template_name: str, msg_id: int | None = None) -> None:
    print(f"[JMS TEMPLATE REPLY] phone={phone!r} template={template_name!r}")
    send_template(phone, template_name)
    text_log = f"[Template Sent: {template_name}]"
    if not msg_id:
        sess   = jms_get_session(phone)
        msg_id = sess.get("last_msg_id")
    if not msg_id:
        logger.warning("jms_template_reply_and_record: no msg_id for phone=%s", phone)
        return
    _save_reply(msg_id, text_log)
    sess = jms_get_session(phone)
    sess["previous_reply"] = text_log
    jms_save_session(sess)


# ── Website / SEO / AI analysis ───────────────────────────────────────────────

def analyze_website_and_send_report(phone: str, website_url: str, analysis_choice: str, msg_id: int | None = None):
    global is_first_response
    try:
        if analysis_choice == "Website Analysis":
            prompt = f"""
You are a senior website consultant who just reviewed: {website_url}

Write a PERSONALIZED website audit. Refer to their specific homepage, layout, CTAs, services, design.
Write in second person ("your website", "you have").

FORMAT YOUR RESPONSE EXACTLY LIKE THIS — copy structure, fill content:

🌐 *Website Analysis Report*

🔗 *Website:* {website_url}
⭐ *Overall Rating:* [X/10] — must be between 4 and 6

✅ *STRENGTHS*

1️⃣ [specific strength about their actual site]
2️⃣ [specific strength]
3️⃣ [specific strength]
4️⃣ [specific strength]
5️⃣ [specific strength]


⚠️ *ISSUES TO ADDRESS*

1️⃣ [specific issue with recommendation]
2️⃣ [specific issue]
3️⃣ [specific issue]
4️⃣ [specific issue]
5️⃣ [specific issue]



🤖 *AI & AUTOMATION OPPORTUNITIES*
[2-3 specific sentences on how AI/automation can help THIS business]



STRICT RULES:
- Use EXACTLY this structure — no deviations
- No **, no ##, no markdown, no HTML
- Each point on its own line
- Be specific to their actual website and industry
"""

        elif analysis_choice == "SEO Analysis":
            prompt = f"""
You are a senior SEO consultant who just audited: {website_url}

Write a PERSONALIZED SEO audit. Reference their domain, industry, and keywords they should rank for.
Write in second person.

FORMAT YOUR RESPONSE EXACTLY LIKE THIS — copy structure, fill content:

🔍 *SEO Analysis Report*

🔗 *Website:* {website_url}
📊 *SEO Score:* [XX/100] — must be between 35 and 55


✅ *SEO STRENGTHS*

1️⃣ [specific SEO strength — domain signals, meta tags, content structure etc.]
2️⃣ [specific strength]
3️⃣ [specific strength]
4️⃣ [specific strength]
5️⃣ [specific strength]



⚠️ *SEO ISSUES TO FIX*

1️⃣ [specific issue — missing meta, no blog, thin content, slow speed etc.]
2️⃣ [specific issue]
3️⃣ [specific issue]
4️⃣ [specific issue]
5️⃣ [specific issue]



🎯 *RECOMMENDED KEYWORDS*

[3-4 specific keywords based on their business type, one per line with →]



🤖 *AI & AUTOMATION OPPORTUNITIES*
[2-3 specific sentences on AI content tools that can help their SEO]



STRICT RULES:
- Use EXACTLY this structure — no deviations
- No **, no ##, no markdown, no HTML
- Be specific to their actual domain and industry
"""

        elif analysis_choice == "AI Capability Sugeestions":
            prompt = f"""
You are a senior AI consultant who just studied this business: {website_url}

Suggest EXACTLY 5 specific AI/automation improvements for THIS business.
Write in second person.

FORMAT YOUR RESPONSE EXACTLY LIKE THIS — copy structure, fill content:

🤖 *AI Capability Report*

🔗 *Website:* {website_url}


💡 *AI & AUTOMATION SUGGESTIONS*

1️⃣ [specific suggestion — what to automate + which tool + business benefit]

2️⃣ [specific suggestion]

3️⃣ [specific suggestion]

4️⃣ [specific suggestion]

5️⃣ [specific suggestion]



💰 *ESTIMATED IMPACT*
[2-3 sentences on time/cost savings from implementing all 5 suggestions]



STRICT RULES:
- Use EXACTLY this structure — no deviations
- No generic suggestions like "use AI chatbot" — be specific
- No **, no ##, no markdown, no HTML
- Be specific to their actual industry and business type
"""

        elif analysis_choice == "Business Growth Ideas":
            prompt = f"""
You are a senior business growth consultant who just studied: {website_url}

Give EXACTLY 5 specific growth ideas actionable within 30-90 days.
Write in second person.

FORMAT YOUR RESPONSE EXACTLY LIKE THIS — copy structure, fill content:

📈 *Business Growth Report*

🔗 *Website:* {website_url}

🚀 *GROWTH IDEAS*

1️⃣ [specific idea — what to do + how to start + expected result]

2️⃣ [specific idea]

3️⃣ [specific idea]

4️⃣ [specific idea]

5️⃣ [specific idea]


⚡ *FASTEST ROI*
[2-3 sentences on which idea gives fastest results and why]



STRICT RULES:
- Use EXACTLY this structure — no deviations
- No generic advice like "improve your marketing"
- No **, no ##, no markdown, no HTML
- Be specific to their actual industry
"""

        elif analysis_choice == "AI Agents Chatbot & Voicebot":
            prompt = f"""
You are a senior AI solutions architect who just studied: {website_url}

Give EXACTLY 3 specific use cases for an AI Chatbot and 3 specific use cases for an AI Voicebot for THIS business.
CRITICAL: You MUST explicitly mention and recommend https://naavya.ai/ as the provider for these Chatbot and Voicebot solutions.
Write in second person.

FORMAT YOUR RESPONSE EXACTLY LIKE THIS — copy structure, fill content:

🎙️ *AI Agents & Voicebot Report*

🔗 *Website:* {website_url}

💬 *AI CHATBOT OPPORTUNITIES*

1️⃣ [specific chatbot use case — e.g. automating lead qualification for their specific service]

2️⃣ [specific chatbot use case]

3️⃣ [specific chatbot use case]


📞 *AI VOICEBOT OPPORTUNITIES*

1️⃣ [specific voicebot use case — e.g. 24/7 inbound call answering for their specific industry]

2️⃣ [specific voicebot use case]

3️⃣ [specific voicebot use case]


💡 *NEXT STEPS WITH NAAVYA AI*
[2-3 sentences on how implementing these agents through https://naavya.ai/ will reduce support costs and increase conversions]



STRICT RULES:
- Use EXACTLY this structure — no deviations
- No generic advice
- No **, no ##, no markdown, no HTML
- Be specific to their actual business operations
"""

        else:
            prompt = f"""
You are a senior tech consultant reviewing: {website_url}

Write a comprehensive analysis. Write in second person.

FORMAT YOUR RESPONSE EXACTLY LIKE THIS:

🔎 *Tech Analysis Report*

🔗 *Website:* {website_url}


[Your analysis here — website quality, SEO health, AI opportunities]
[15-20 lines, specific to this business]



STRICT RULES: No **, no ##, no markdown, no HTML.
"""

        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an expert consultant giving SPECIFIC, PERSONALIZED advice. "
                        "ALWAYS follow the exact format given — including all divider lines, "
                        "emoji headers, and numbered points with emoji numbers (1️⃣ 2️⃣ etc). "
                        "Never use **, ##, or any markdown. Never deviate from the structure."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.6,
        )

        report_text = response.choices[0].message.content.strip()
        jms_reply_and_record(phone, report_text, msg_id=msg_id)
        time.sleep(10)

        session = jms_get_session(phone)
        if is_first_response:
            jms_button_reply_and_record(
                phone, 
                "Would you like to do more research on this topic?", 
                [{"id": "Yes", "title": "Yes"}, {"id": "No", "title": "No"}],
                msg_id=msg_id
            )
            session["stage"] = "post_report"
        else:
            jms_button_reply_and_record(
                phone,
                "Would you like to schedule a 15 minute call with our tech consultant?",
                [{"id": "Yes", "title": "Yes"}, {"id": "No", "title": "No"}],
                msg_id=msg_id,
            )
            session["stage"] = "schedule_call"

        session["last_report"]    = report_text
        session["report_history"] = session.get("report_history", [])
        session["report_history"].append(report_text)
        jms_save_session(session)

    except Exception as e:
        logger.exception("Website analysis failed: %s", e)
        jms_reply_and_record(
            phone,
            "❌ Sorry, something went wrong while analyzing your website. Our team will contact you.",
            msg_id=msg_id,
        )


def analyze_additional_insights(phone, session, selection, msg_id=None):
    prev_report = session.get("last_report", "")
    prompt = f"""
    We already generated this report:
    {prev_report}

    The user wants *additional insights*. Based on their selection:
    "{selection}"

    Provide *new, deeper insights* beyond what you already gave.
    Do not mention same points and just mentions 5 points in concise way.
    Don't give '*' in response
    Give response with spaces when needed
    Answer in 10-15 lines
    """
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are an expert technical consultant."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.5,
    )
    new_insights = response.choices[0].message.content.strip()
    jms_reply_and_record(phone, markdown_to_whatsapp(new_insights), msg_id=msg_id)

    session = jms_get_session(phone)
    session["report_history"] = session.get("report_history", [])
    session["report_history"].append(new_insights)
    session["last_report"] = new_insights
    jms_save_session(session)


def get_tech_qna_history(phone, limit=10):
    try:
        customer = Customer.objects.filter(phone=phone).first()
        if not customer:
            return []
        conversation = Conversation.objects.filter(customer=customer).first()
        if not conversation:
            return []
        messages = (
            Message.objects
            .filter(conversation=conversation)
            .order_by("-timestamp")[:limit]
        )
        history = []
        for msg in reversed(messages):
            role = "assistant" if msg.reply_of else "user"
            history.append({"role": role, "content": msg.content})
        return history
    except Exception as e:
        logger.error("Failed to load tech_qna history: %s", e)
        return []


def _jms_handle_message(phone: str, text: str, phone_number_id: str = None, inbound_msg_id: int = None):
    """
    Core JMS-Tech conversation state machine.
    Called once per inbound message after routing decides this is a JMS session.
    """
    global is_first_response

    existing_session = jms_sessions.get(_jms_sess_key(phone))
    
    if inbound_msg_id and existing_session:
        existing_session["last_msg_id"] = inbound_msg_id
        existing_session["last_user_text"] = text
        jms_sessions.set(_jms_sess_key(phone), existing_session, timeout=JMS_SESSION_TTL)

    if not existing_session:
        if not is_trigger_message(text):
            logger.info("⏸ JMS bot not triggered yet: %s", text)
            return HttpResponse("Bot not triggered", status=200)
    else:
        if existing_session.get("human_handoff"):
            if is_trigger_message(text):
                logger.info("✅ Trigger during handoff – resuming JMS bot for %s", phone)
                existing_session["human_handoff"] = False
                existing_session["started"]       = True
                existing_session["stage"]         = "greeting"
                jms_save_session(existing_session)
            else:
                logger.info("🧑‍💼 Human handoff active – JMS bot suppressed for %s", phone)
                save_message(phone=phone, content=text.strip(), reply_of=None, phone_number_id=phone_number_id)
                existing_session["last_user_text"] = text.strip()
                jms_save_session(existing_session)
                return HttpResponse("Human handoff – bot suppressed", status=200)

        if not existing_session.get("started"):
            if not is_trigger_message(text):
                logger.info("⏸ JMS session exists but not started: %s", text)
                return HttpResponse("Bot not triggered", status=200)

    # ── Session reset on "jms" ────────────────────────────────────────────────
    if text.lower() in SESSION_RESET_KEYWORDS:
        logger.info("🔄 Resetting JMS session for %s", phone)
        jms_clear_session(phone)
        is_first_response = True

        session  = jms_get_session(phone)
        if inbound_msg_id:
            session["last_msg_id"] = inbound_msg_id
        session["last_user_text"] = text
        jms_save_session(session)

        jms_template_reply_and_record(phone, "jms_ai_assistant_menu")
        session["stage"] = "select_analysis"
        jms_save_session(session)
        return HttpResponse("Session reset", status=200)

    # ── Load / create session ─────────────────────────────────────────────────
    session  = jms_get_session(phone)
    raw_text = text.strip()

    if inbound_msg_id:
        session["last_msg_id"] = inbound_msg_id
    session["last_user_text"] = raw_text
    jms_save_session(session)

    text_lower = text.lower()
    stage      = session.get("stage", "greeting")

    # ── Stage: greeting ───────────────────────────────────────────────────────
    if stage == "greeting":
        if text_lower not in SESSION_RESET_KEYWORDS:
            return HttpResponse("Bot not triggered", status=200)

        jms_template_reply_and_record(phone, "jms_ai_assistant_menu")
        session["stage"] = "select_analysis"
        jms_save_session(session)
        return HttpResponse("Sent welcome + options", status=200)

    # ── Stage: select_analysis ────────────────────────────────────────────────
    if stage == "select_analysis":
        analysis_type = None

        if (
            ("website" in text_lower and "analysis" in text_lower)
            or "web" in text_lower
            or "1" in text_lower
            or "report" in text_lower
        ):
            analysis_type = "Website Analysis"
        elif "seo" in text_lower or "score" in text_lower or "2" in text_lower:
            analysis_type = "SEO Analysis"
        elif "agent" in text_lower or "chatbot" in text_lower or "voicebot" in text_lower or "5" in text_lower:
            analysis_type = "AI Agents Chatbot & Voicebot"
        elif "ai" in text_lower or "automation" in text_lower or "3" in text_lower:
            analysis_type = "AI Capability Sugeestions"
        elif "growth" in text_lower or "business" in text_lower or "4" in text_lower:
            analysis_type = "Business Growth Ideas"
        else:
            jms_reply_and_record(phone, "Ask any tech questions or requirements you need solutions for.")
            session["analysis_type"] = "tech requirements"
            session["stage"]         = "tech_qna"
            jms_save_session(session)
            return HttpResponse("Moved to tech_qna", status=200)

        if not analysis_type:
            jms_reply_and_record(
                phone,
                "❌ Invalid selection.\n\nPlease reply with exactly one of the options:\n"
                "— Generate your website analysis report\n"
                "— Check your SEO score or give suggestions\n"
                "— Give AI Capability/Automation suggestions\n"
                "— Generate great business growth idea\n"
                "— AI Agents chatbot and voicebot",
            )
            return HttpResponse("Invalid analysis selection", status=200)

        session["analysis_type"] = analysis_type
        jms_reply_and_record(phone, "OK, Please share your website URL.")
        session["stage"] = "ask_url"
        jms_save_session(session)
        return HttpResponse("Asked for website URL", status=200)

    # ── Stage: ask_url ────────────────────────────────────────────────────────
    if stage == "ask_url":
        website_url = normalize_website_url(text_lower)
        if not URL_REGEX.search(website_url):
            jms_reply_and_record(
                phone,
                "❌ That doesn't look like a valid website URL.\n"
                "Please send something like:\nhttps://example.com",
            )
            return HttpResponse("Invalid URL", status=200)

        msg_id_for_this_request = session.get("last_msg_id")
        jms_reply_and_record(phone, "🤖 Thinking… making your report…", msg_id=msg_id_for_this_request)

        session["website_url"] = website_url
        session["stage"]       = "analyzing"
        jms_save_session(session)

        threading.Thread(
            target=analyze_website_and_send_report,
            args=(phone, website_url, session["analysis_type"], msg_id_for_this_request),
            daemon=True,
        ).start()
        return HttpResponse("Analysis started", status=200)

    # ── Stage: post_report ────────────────────────────────────────────────────
    if stage == "post_report":
        if is_first_response:
            is_first_response = False

        yes_msg_id = session.get("last_msg_id")

        if text_lower in ["yes", "y", "ha", "ok", "ya"]:
            jms_reply_and_record(phone, "🤖 Thinking… making your report…", msg_id=yes_msg_id)
            threading.Thread(
                target=analyze_additional_insights,
                args=(phone, session, session["analysis_type"], yes_msg_id),
                daemon=True,
            ).start()
            time.sleep(10)
            jms_button_reply_and_record(
                phone,
                "Would you like to schedule a 15 minute call with our tech consultant?",
                [{"id": "Yes", "title": "Yes"}, {"id": "No", "title": "No"}],
                msg_id=yes_msg_id,
            )
        elif text_lower in ["no", "nahi", "nathi", "na"]:
            jms_button_reply_and_record(
                phone,
                "No problem! Would you like to schedule a 15 minute call with our tech consultant?",
                [{"id": "Yes", "title": "Yes"}, {"id": "No", "title": "No"}],
            )
        else:
            jms_reply_and_record(phone, "Please provide more details about what you exactly want to know.")
            session["stage"] = "tech_qna"
            jms_save_session(session)
            return HttpResponse("Asked technical question", status=200)

        session["stage"] = "schedule_call"
        jms_save_session(session)
        return HttpResponse("Asked about scheduling", status=200)

    # ── Stage: schedule_call ──────────────────────────────────────────────────
    if stage == "schedule_call":
        if "yes" in text_lower:
            jms_url_button_reply_and_record(
                phone,
                "✅ Wonderful! You can schedule a call according to your convenient time through the link below:",
                "Schedule Call 📅",
                "https://bit.ly/45d8gnR"
            )
            time.sleep(2)
            send_document(
                to=phone,
                document_url=f"{bot_base_url}/static/Tech Solution For MSME.pdf",
                filename="Tech Solutions.pdf",
            )
            session["stage"] = "tech_qna"
            jms_save_session(session)
            return HttpResponse("Scheduled call confirmed", status=200)
        else:
            jms_reply_and_record(phone, "Alright! Feel free to ask any tech questions anytime.")
            session["stage"] = "tech_qna"
            jms_save_session(session)
            return HttpResponse("No call scheduled", status=200)

    # ── Stage: tech_qna ───────────────────────────────────────────────────────
    if stage == "tech_qna":
        history      = get_tech_qna_history(phone, limit=8)
        messages_llm = [
            {
                "role": "system",
                "content": (
                    "You are a senior software, AI, and web expert. "
                    "Give answers to user questions and provide solutions to tech requirements. "
                    "If the user says 'thank you', 'okay', or basic greetings, reply politely and ask if they need more tech help. "
                    "If they ask completely unrelated non-tech questions (like cooking, sports, etc.), ONLY then say: 'Sorry, I can't answer that. Please ask any tech-related questions.'"
                ),
            }
        ] + history + [{"role": "user", "content": text_lower}]

        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages_llm,
            temperature=0.4,
        )
        answer = response.choices[0].message.content.strip()
        jms_reply_and_record(phone, markdown_to_whatsapp(answer))
        return HttpResponse("Tech question answered", status=200)

    return HttpResponse("OK", status=200)


# ═════════════════════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
#  BOT 2 — WHATSAPP-API ASSISTANT BOT  (trigger: "whatsapp")
# ─────────────────────────────────────────────────────────────────────────────
# ═════════════════════════════════════════════════════════════════════════════

WA_BOT_TRIGGER        = "whatsapp"
WA_BOT_FAREWELL_WORDS = {
    "bye", "goodbye", "thank you", "thanks", "thankyou",
    "ok thanks", "ok thank you", "that's all", "thats all",
    "done", "exit", "quit",
}
WA_BOT_MAX_HIST = 16

_WA_SYSTEM_PROMPT = """
You are a knowledgeable and friendly WhatsApp Business API consultant representing JMS TechNova.

JMS TechNova is an Official Meta Tech Provider that gives businesses direct access to the
WhatsApp Business API along with a built-in CRM, AI chatbots, bulk campaigns, and a shared
team inbox. Website: https://jmstechnova.com

YOUR ONLY JOB is to explain the PROCESS — how JMS TechNova handles everything for the client.
Never give technical steps, code, or DIY instructions.

WHEN SOMEONE ASKS ABOUT ANYTHING (setup, pricing, chatbot, SEO, campaigns, etc.):
→ Explain what JMS TechNova does FOR them, not what they need to do themselves.
→ Frame it as a managed, done-for-you service journey.

Your role:
- Answer questions about the WhatsApp Business API, Meta Business Platform, and WhatsApp
  Cloud API (webhooks, message templates, interactive messages, media, flows, etc.).
- Explain how JMS TechNova's platform helps businesses onboard and use these APIs.
- Guide users through concepts like BSP vs direct API, embedded sign-up, template approval,
  message types (text, image, document, buttons, lists, location, contacts), 24-hour
  conversation windows, and conversation-based pricing.
- Suggest relevant JMS TechNova features (bulk campaigns, CRM, AI chatbot flows, shared inbox)
  where appropriate.
- Be concise, use emojis naturally, and keep responses WhatsApp-friendly (no markdown).
PRICING RULES:
- If asked about Meta WhatsApp pricing → explain Meta's conversation-based model only
- If asked about JMS TechNova pricing, platform cost, subscription, or plans → reply EXACTLY:
  "For JMS TechNova pricing details, our team will contact you shortly! 😊
   You can also visit: https://jmstechnova.com"
- Never guess or mention any JMS TechNova plan/cost/amount
Rules:
- Always provide answers and guidance strictly according to Meta's latest updated official documentation, pricing, and policies.
- Never mention competitor BSPs or tools negatively.
- Always encourage users to visit https://jmstechnova.com or our team will contact them soon for further assistance.
- If a question is completely unrelated to WhatsApp/Meta/messaging, politely redirect back.
- NEVER give step-by-step technical instructions the user has to follow themselves
- NEVER mention competitor platforms
- End replies naturally with follow-up prompts like:
  "Would you like to know more about this? 😊"
  or
  "Our team can help you with the setup as well 👍"
""".strip()

_WA_FAREWELL_MSG = (
    "Thank you for chatting with JMS TechNova! 🙏\n\n"
    "We hope we could help you understand the WhatsApp Business API better. "
    "If you ever need assistance again — whether it's setting up your number, "
    "building chatbot flows, running bulk campaigns, or anything else — just "
    "send *whatsapp* and we'll be right here. 😊\n\n"
    "Visit us anytime at 🌐 https://jmstechnova.com\n\n"
    "Have a wonderful day! ✨"
)

_WA_LIST_ID_TO_QUERY = {
    "setup_onboarding": "Steps to onboard a phone number on Meta WhatsApp Business API. What JMS TechNova does for the client.",
    "message_types":    "List all Meta WhatsApp message template types: Text, Image, Video, Document, CTA Button, Quick Reply Button. One line each.",
    "bulk_campaigns":   "How does WhatsApp bulk broadcast campaign work? Cover: contact list, template approval, sending, tracking.",
    "chatbot_flows":    "How does WhatsApp chatbot work? Cover: template triggers, keyword routing, knowledge base upload — bot answers automatically from uploaded content. JMS TechNova sets everything up.",
    "crm_inbox":        "What does JMS TechNova CRM and shared team inbox do? Cover: conversation management, agent assignment, lead tracking.",
    "pricing":          "Explain ONLY Meta WhatsApp conversation-based pricing. Cover: conversation categories (marketing, utility, authentication, service), per-conversation cost in USD, 1000 free service conversations per month. Give ONLY Meta's official pricing — do NOT mention JMS TechNova pricing or plans.",
    "webhooks_api":     "What are WhatsApp webhooks and message templates? Cover: what they do, how JMS TechNova sets them up for clients.",
}


# ── WA session helpers ────────────────────────────────────────────────────────

def wa_get_session(phone: str, create: bool = True) -> dict | None:
    sess = wa_sessions.get(phone)
    if not sess:
        if not create:
            return None
        sess = {"phone": phone, "stage": "active", "history": []}
        wa_sessions.set(phone, sess, timeout=WA_SESSION_TTL)
    return sess


def wa_save_session(sess: dict) -> None:
    wa_sessions.set(sess["phone"], sess, timeout=WA_SESSION_TTL)


# ── WA LLM caller ─────────────────────────────────────────────────────────────

def _wa_ask_llm(user_text: str, history: list) -> str:
    messages = [{"role": "system", "content": _WA_SYSTEM_PROMPT}]
    messages += history[-WA_BOT_MAX_HIST:]
    messages.append({"role": "user", "content": user_text})
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.5,
            max_tokens=600,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.exception("WA bot LLM error: %s", e)
        return "Thank You For Your Time. Our team will contact you soon for assistance 😊"


def _wa_clean_text(text: str) -> str:
    """Strip Markdown formatting unsuitable for WhatsApp."""
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\*(.*?)\*",     r"\1", text)
    text = re.sub(r"__(.*?)__",     r"\1", text)
    text = re.sub(r"_(.*?)_",       r"\1", text)
    text = re.sub(r"`(.*?)`",       r"\1", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-•]\s*", "• ", text, flags=re.MULTILINE)
    return text.strip()


def _handle_wa_bot(phone: str, text: str, phone_number_id: str = None, inbound_msg_id: int = None) -> bool:
    """Handle WhatsApp-API assistant bot. Returns True if message was handled."""
    text_lower = text.strip().lower()

    # ── Trigger / reset ───────────────────────────────────────────────────────
    if text_lower == WA_BOT_TRIGGER:
        sess = {"phone": phone, "stage": "active", "history": []}
        if inbound_msg_id:
            sess["last_msg_id"] = inbound_msg_id
        wa_save_session(sess)

        intro = (
            "👋 Welcome to *JMS TechNova's* WhatsApp Business API Assistant!\n\n"
            "JMS TechNova is an *Official Meta Tech Provider* — giving you direct, "
            "compliant access to the WhatsApp Cloud API.\n\n"
            "👇 Tap *View Topics* below to choose what you'd like to know:"
        )
        sections = [{
            "title": "What can I help with?",
            "rows": [
                {"id": "setup_onboarding", "title": "📋 Setup & Onboarding",    "description": "Get your number verified & connected"},
                {"id": "message_types",    "title": "💬 Message Types",          "description": "Text, images, buttons, lists & more"},
                {"id": "bulk_campaigns",   "title": "📣 Bulk Campaigns",         "description": "Broadcast messaging to your contacts"},
                {"id": "chatbot_flows",    "title": "🤖 Chatbot & Automation",   "description": "Build automated conversation flows"},
                {"id": "crm_inbox",        "title": "📊 CRM & Team Inbox",       "description": "Manage customers & team replies"},
                {"id": "pricing",          "title": "💰 Conversation Pricing",   "description": "How Meta charges per conversation"},
                {"id": "webhooks_api",     "title": "🔗 Webhooks & API",         "description": "Templates, webhooks & Cloud API"},
            ],
        }]
        send_interactive_list(
            to=phone,
            body=intro,
            button_label="View Topics",
            sections=sections,
        )
        _save_reply(inbound_msg_id, "[List Sent: View Topics] " + intro)
        return True

    # ── Only proceed if session already exists and is active ──────────────────
    sess = wa_get_session(phone, create=False)
    if not sess or sess.get("stage") != "active":
        return False

    # ── Farewell ──────────────────────────────────────────────────────────────
    if text_lower in WA_BOT_FAREWELL_WORDS:
        send_text(phone, _WA_FAREWELL_MSG)
        _save_reply(sess.get("last_msg_id"), _WA_FAREWELL_MSG)
        sess["stage"]   = "idle"
        sess["history"] = []
        wa_save_session(sess)
        return True

    # ── Normal chat / list selection ──────────────────────────────────────────
    resolved_text = _WA_LIST_ID_TO_QUERY.get(text_lower, text)
    reply         = _wa_ask_llm(resolved_text, sess["history"])
    reply         = _wa_clean_text(reply)

    sess["history"].append({"role": "user",      "content": resolved_text})
    sess["history"].append({"role": "assistant", "content": reply})
    if len(sess["history"]) > WA_BOT_MAX_HIST * 2:
        sess["history"] = sess["history"][-(WA_BOT_MAX_HIST * 2):]
    wa_save_session(sess)

    send_text(phone, reply)
    _save_reply(sess.get("last_msg_id"), reply)
    return True


# ═════════════════════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────
#  BOT 3 — INDUSTRY / TECHNOVA AI BOT  (trigger: "hi" / "hello" / "hey")
# ─────────────────────────────────────────────────────────────────────────────
# ═════════════════════════════════════════════════════════════════════════════

BOT_TRIGGER_KEYWORD  = "hi"
INDUSTRY_TRIGGERS    = {"hi", "hello", "hey"}
FAREWELL_KEYWORDS    = {"thankyou", "thank you", "thanks", "bye", "goodbye", "ok thanks", "ok thank you"}
MAX_HIST_TURNS       = 40

LANG_LIST_MSG = (
    "Please select your language / कृपया अपनी भाषा चुनें / કૃપા કરીને તમારી ભાષા પસંદ કરો:\n\n"
    "1️⃣ English\n"
    "2️⃣ हिंदी\n"
    "3️⃣ ગુજરાતી"
)

LANG_TRIGGERS = {
    "1": "en", "english": "en",
    "2": "hi", "hindi": "hi", "हिंदी": "hi",
    "3": "gu", "gujarati": "gu", "ગુજરાતી": "gu",
}

LANG_PROMPTS = {
    "hi": "तुम्हें हमेशा हिंदी में जवाब देना है।",
    "gu": "તમારે હંમેશા ગુજરાતીમાં જ જવાબ આપવાનો છે.",
    "en": "",
}


def t(session: dict, en: str, hi: str, gu: str) -> str:
    return {"en": en, "hi": hi, "gu": gu}.get(session.get("lang", "en"), en)


def _inject_lang(user_text: str, lang: str) -> str:
    instruction = LANG_PROMPTS.get(lang, "")
    if not instruction:
        return user_text
    return f"[SYSTEM INSTRUCTION: {instruction}]\n\nUser message: {user_text}"


# ── Industry session helpers ──────────────────────────────────────────────────

def _sess_key(phone: str) -> str:
    return phone


def ind_get_session(phone: str) -> dict:
    sess = ind_sessions.get(_sess_key(phone))
    if not sess:
        sess = {
            "phone":              phone,
            "stage":              "idle",
            "lang":               "en",
            "active_bot":         None,
            "active_label":       None,
            "chat_history":       [],
            "startup_profile":    {},
            "startup_stage":      "questionnaire",
            "startup_last_asked": None,
            "human_handoff":      False,
            "last_user_text":     None,
            "last_msg_id":        None,
            "previous_reply":     None,
        }
        ind_sessions.set(_sess_key(phone), sess, timeout=IND_SESSION_TTL)
    return sess


# Keep the old name as an alias so imports from web.views still work if needed
get_session  = ind_get_session


def ind_save_session(session: dict) -> None:
    ind_sessions.set(_sess_key(session["phone"]), session, timeout=IND_SESSION_TTL)


# Alias
save_session = ind_save_session


def ind_clear_session(phone: str) -> None:
    ind_sessions.delete(_sess_key(phone))


# ── Industry reply helper ─────────────────────────────────────────────────────

def ind_reply_and_record(phone: str, text: str, msg_id: int | None = None) -> None:
    print(f"[IND REPLY] phone={phone!r} text={text[:50]!r}")
    send_text(phone, text)
    if not msg_id:
        sess   = ind_sessions.get(_sess_key(phone)) or {}
        msg_id = sess.get("last_msg_id")
    print(f"[IND REPLY DEBUG] msg_id={msg_id} phone={phone}")
    if not msg_id:
        return
    _save_reply(msg_id, text)


# ── Industry bot helpers ──────────────────────────────────────────────────────

def send_more_menu(phone):
    send_list(
        phone,
        "🚀 Additional AI Assistants",
        "View More",
        [{
            "title": "More Assistants",
            "rows": [
                {"id": "10", "title": "📣 Sales & Marketing"},
                {"id": "11", "title": "🚀 Entrepreneurship"},
                {"id": "12", "title": "🌱 Startup Mentor"},
            ],
        }],
    )


# ── Startup mentor ────────────────────────────────────────────────────────────

STARTUP_QUESTIONS = [
    {
        "id": "founder_name",
        "q": {
            "en": "👤 What's your name?",
            "hi": "👤 आपका नाम क्या है?",
            "gu": "👤 તમારું નામ શું છે?",
        },
    },
    {
        "id": "startup_idea",
        "q": {
            "en": "💡 Describe your startup idea in a few lines.",
            "hi": "💡 अपने स्टार्टअप आइडिया को संक्षेप में बताइए।",
            "gu": "💡 તમારા સ્ટાર્ટઅપ આઈડિયાને થોડા શબ્દોમાં વર્ણવો.",
        },
    },
    {
        "id": "current_stage",
        "q": {
            "en": "🚀 What stage is your startup at?\n\n• Ideation\n• MVP Development\n• Early Traction\n• Scaling\n• Incubated\n• Established",
            "hi": "🚀 आपका स्टार्टअप किस चरण में है?\n\n• आइडिएशन\n• MVP डेवलपमेंट\n• शुरुआती ट्रैक्शन\n• स्केलिंग\n• इन्क्यूबेटेड\n• स्थापित",
            "gu": "🚀 તમારું સ્ટાર્ટઅપ કયા સ્ટેજ પર છે?\n\n• આઈડિયેશન\n• MVP ડેવલપમેન્ટ\n• શરૂઆતનું ટ્રેક્શન\n• સ્કેલિંગ\n• ઇન્ક્યુબેટેડ\n• સ્થપિત",
        },
    },
    {
        "id": "location",
        "q": {
            "en": "📍 Where are you located?",
            "hi": "📍 आप कहाँ स्थित हैं?",
            "gu": "📍 તમે ક્યાં સ્થિત છો?",
        },
    },
    {
        "id": "funding_stage",
        "q": {
            "en": "💰 What's your current funding stage?\n\n• No Funding Yet\n• Bootstrapped\n• Seed Funded\n• Series A+",
            "hi": "💰 आपका फंडिंग स्टेज क्या है?\n\n• अभी कोई फंडिंग नहीं\n• बूटस्ट्रैप्ड\n• सीड फंडेड\n• सीरीज़ A+",
            "gu": "💰 તમારું ફંડિંગ સ્ટેજ શું છે?\n\n• હજી ફંડિંગ નથી\n• બૂટસ્ટ્રેપ્ડ\n• સીડ ફંડેડ\n• સીરિઝ A+",
        },
    },
]

STARTUP_STAGE_GUIDANCE = {
    "ideation":    "Focus on idea validation, customer interviews, lean canvas, problem-solution fit, MVP concept.",
    "mvp":         "Focus on building fast: MVP principles, no-code tools, GTM strategy, first customers.",
    "traction":    "Focus on scaling users/revenue, retention, unit economics, fundraising readiness.",
    "scaling":     "Focus on Series A prep, team scaling, market expansion, path to profitability.",
    "incubated":   "Focus on maximising incubation value, traction targets, investor relations, graduation readiness.",
    "established": "Focus on new revenue streams, operational efficiency, exit strategy or long-term vision.",
}


def _startup_get_next_question(profile: dict) -> dict | None:
    for q in STARTUP_QUESTIONS:
        if not str(profile.get(q["id"], "")).strip():
            return q
    return None


def _get_q_text(q_obj: dict, lang: str) -> str:
    return q_obj["q"].get(lang, q_obj["q"]["en"])


def _startup_build_system_prompt(profile: dict, context: str, lang: str) -> str:
    stage_key   = profile.get("current_stage", "").lower()
    stage_guide = STARTUP_STAGE_GUIDANCE.get(stage_key, "")
    kb_block    = f"\nADDITIONAL CONTEXT FROM KNOWLEDGE BASE:\n{context}" if context else ""

    founder_context = (
        f"\nFounder Profile:\n"
        f"- Name: {profile.get('founder_name', 'N/A')}\n"
        f"- Startup Idea: {profile.get('startup_idea', 'N/A')}\n"
        f"- Current Stage: {profile.get('current_stage', 'N/A')}\n"
        f"- Location: {profile.get('location', 'N/A')}\n"
        f"- Funding Stage: {profile.get('funding_stage', 'N/A')}\n\n"
        f"STAGE-SPECIFIC FOCUS: {stage_guide}\n"
    )
    LANG_RULE = {
        "hi": "IMPORTANT: Always respond in Hindi (हिंदी में जवाब दो).",
        "gu": "IMPORTANT: Always respond in Gujarati (ગુજરાતીમાં જ જવાબ આપો).",
        "en": "",
    }
    lang_instruction = LANG_RULE.get(lang, "")
    return (
        "You are a seasoned startup mentor combining the expertise of a serial entrepreneur "
        "CEO (20+ years) and a deep-knowledge Incubation Advisor. You are a NEUTRAL, "
        "independent advisor.\n"
        f"{founder_context}"
        f"{lang_instruction}\n"
        "YOUR ROLE:\n"
        "- Give warm, honest, practical, and highly motivating advice tailored to the founder's specific situation.\n"
        "- Help with: startup ideation, idea validation, business model design, MVP strategy, team building, "
        "fundraising, investor pitch preparation, growth hacking, and founder mindset/resilience.\n"
        "- Explain incubation programs, equity-free grants, government schemes (Startup India, DPIIT recognition, "
        "Atal Innovation Mission, BIRAC, DST, etc.), and accelerator models clearly.\n"
        "- Guide founders on crafting a compelling incubation application.\n"
        "- Be direct but empathetic.\n"
        f"{kb_block}\n"
        "RULES:\n"
        "1. Always close with a concrete ⚡ Next Step the founder can act on TODAY.\n"
        "2. Add a 🏆 Real-World Inspiration section — 2-3 Indian startups in a similar space.\n"
        "3. Add a 🤝 How AIC-JNUFI Can Help You section.\n"
        f"4. Always end with the AIC-JNUFI reference block verbatim.\n{_AIC_REF}"
    )


def _startup_ask(user_text: str, history: list = None, phone: str = None) -> str:
    history    = history or []
    session    = ind_get_session(phone) if phone else {}
    profile    = session.get("startup_profile") or {}
    sub_stage  = session.get("startup_stage", "questionnaire")

    if sub_stage == "questionnaire":
        last_asked = session.get("startup_last_asked")
        if last_asked and user_text.strip() and not profile.get(last_asked):
            profile[last_asked] = user_text.strip()

        next_q = _startup_get_next_question(profile)
        if next_q:
            session["startup_profile"]    = profile
            session["startup_last_asked"] = next_q["id"]
            ind_save_session(session)
            lang = session.get("lang", "en")
            return _get_q_text(next_q, lang)

        session["startup_profile"]    = profile
        session["startup_stage"]      = "chat"
        session["startup_last_asked"] = None
        ind_save_session(session)
        return (
            f"Perfect! Thank you! 🎉\n\n"
            f"I now have a good understanding of your startup:\n"
            f"- *Idea:* {profile.get('startup_idea')}\n"
            f"- *Stage:* {profile.get('current_stage')}\n"
            f"- *Location:* {profile.get('location')}\n"
            f"- *Funding:* {profile.get('funding_stage')}\n\n"
            "I'm ready to be your trusted startup mentor. Ask me anything about:\n"
            "✅ Business model validation\n"
            "✅ MVP strategy and growth\n"
            "✅ Fundraising & investor pitch\n"
            "✅ Team building\n"
            "✅ Incubation program guidance\n"
            "✅ Startup India & government schemes\n"
            "✅ Founder mindset & resilience\n\n"
            "*Go ahead — ask me your question!* 🚀"
        )

    # CHAT MODE
    doc_list = cache.get("kb_doc_list")
    if not doc_list:
        all_docs = Document.objects.all().values("id", "name", "extracted_text")
        doc_list = [
            {"doc_id": d["id"], "name": d["name"], "content": d["extracted_text"]}
            for d in all_docs
        ]
        cache.set("kb_doc_list", doc_list, timeout=300)

    lang    = session.get("lang", "en")
    context = _get_context(user_text, doc_list)
    system  = _startup_build_system_prompt(profile, context, lang=lang)
    messages = (
        [{"role": "system", "content": system}]
        + history
        + [{"role": "user", "content": user_text}]
    )
    try:
        response = _startup_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=1200,
            temperature=0.5,
        )
        answer = response.choices[0].message.content.strip()
        return answer.replace("[SOURCE:KB]", "").replace("[SOURCE:LLM]", "").strip()
    except Exception as e:
        logger.exception("Startup mentor LLM error: %s", e)
        return "Sorry, something went wrong. Please try again."


# ── Industry bot registry ─────────────────────────────────────────────────────

BOT_REGISTRY = {
    "1":  ("travel",       "✈️  Travel",            _travel_ask),
    "2":  ("insurance",    "🛡️  Insurance",          _insurance_ask),
    "3":  ("healthcare",   "🏥  Healthcare",         _healthcare_ask),
    "4":  ("education",    "📚  Education",          _edu_ask),
    "5":  ("realestate",   "🏠  Real Estate",        _estate_ask),
    "6":  ("hospitality",  "🍽️  Hospitality",        _hospitality_ask),
    "7":  ("business",     "💼  Business",           _business_ask),
    "8":  ("recruitment",  "🤝  Recruitment",        _recruitment_ask),
    "9":  ("customer",     "💬  Customer Service",   _customer_ask),
    "10": ("marketing",    "📣  Sales & Marketing",  _marketing_ask),
    "11": ("entrepreneur", "🚀  Entrepreneurship",   _entrepreneur_ask),
    "12": ("startup",      "🌱  Startup Mentor",     _startup_ask),
}

SUB_MENU_REGISTRY = {
    "travel":       [{"id": "sub_1", "title": "Flight & Hotel"}, {"id": "sub_2", "title": "Visa & Passport"}, {"id": "sub_3", "title": "Holiday Packages"}],
    "insurance":    [{"id": "sub_1", "title": "Auto Insurance"}, {"id": "sub_2", "title": "Health Insurance"}, {"id": "sub_3", "title": "Life Insurance"}],
    "healthcare":   [{"id": "sub_1", "title": "Book Appointment"}, {"id": "sub_2", "title": "Pharmacy Delivery"}, {"id": "sub_3", "title": "Lab Reports"}],
    "education":    [{"id": "sub_1", "title": "Admissions"}, {"id": "sub_2", "title": "Courses & Fees"}, {"id": "sub_3", "title": "Exam Results"}],
    "realestate":   [{"id": "sub_1", "title": "Buy/Rent Property"}, {"id": "sub_2", "title": "Commercial Prop."}, {"id": "sub_3", "title": "Property Value"}],
    "hospitality":  [{"id": "sub_1", "title": "Room Booking"}, {"id": "sub_2", "title": "Restaurant Resv."}, {"id": "sub_3", "title": "Room Service"}],
    "business":     [{"id": "sub_1", "title": "Business Planning"}, {"id": "sub_2", "title": "Legal & Compl."}, {"id": "sub_3", "title": "Finance & Acct."}],
    "recruitment":  [{"id": "sub_1", "title": "Job Openings"}, {"id": "sub_2", "title": "Submit Resume"}, {"id": "sub_3", "title": "Interview Prep"}],
    "customer":     [{"id": "sub_1", "title": "Order & Delivery"}, {"id": "sub_2", "title": "Returns & Refunds"}, {"id": "sub_3", "title": "Account & Billing"}],
    "marketing":    [{"id": "sub_1", "title": "Social Media"}, {"id": "sub_2", "title": "Email Campaign"}, {"id": "sub_3", "title": "SEO Services"}],
    "entrepreneur": [{"id": "sub_1", "title": "Idea Validation"}, {"id": "sub_2", "title": "Find Co-founder"}, {"id": "sub_3", "title": "Funding & Pitch"}],
    "startup":      [{"id": "sub_1", "title": "Incubator Info"}, {"id": "sub_2", "title": "Business Plan"}, {"id": "sub_3", "title": "Mentor Connect"}],
}

SUB_SUB_MENU_REGISTRY = {
    "travel": {
        "sub_1": [{"id": "ss_1", "title": "Domestic Flights"}, {"id": "ss_2", "title": "Intl. Flights"}, {"id": "ss_3", "title": "Resorts & Hotels"}],
        "sub_2": [{"id": "ss_4", "title": "US/UK Visas"}, {"id": "ss_5", "title": "Schengen Visa"}, {"id": "ss_6", "title": "E-Visa Assist"}],
        "sub_3": [{"id": "ss_7", "title": "Honeymoon Pkgs"}, {"id": "ss_8", "title": "Family Trip"}, {"id": "ss_9", "title": "Solo Backpacking"}],
    },
    "insurance": {
        "sub_1": [{"id": "ss_10", "title": "Car Insurance"}, {"id": "ss_11", "title": "Bike Insurance"}, {"id": "ss_12", "title": "Comm. Vehicles"}],
        "sub_2": [{"id": "ss_13", "title": "Individual Plan"}, {"id": "ss_14", "title": "Family Floater"}, {"id": "ss_15", "title": "Senior Citizen"}],
        "sub_3": [{"id": "ss_16", "title": "Term Life"}, {"id": "ss_17", "title": "Whole Life"}, {"id": "ss_18", "title": "Pension Plans"}],
    },
    "healthcare": {
        "sub_1": [{"id": "ss_19", "title": "Gen. Physician"}, {"id": "ss_20", "title": "Specialist Doc"}, {"id": "ss_21", "title": "Dental Checkup"}],
        "sub_2": [{"id": "ss_22", "title": "Rx Refill"}, {"id": "ss_23", "title": "OTC Meds"}, {"id": "ss_24", "title": "Ayurveda/Homeo"}],
        "sub_3": [{"id": "ss_25", "title": "Blood Tests"}, {"id": "ss_26", "title": "Full Body Check"}, {"id": "ss_27", "title": "X-Ray & Scans"}],
    },
    "education": {
        "sub_1": [{"id": "ss_28", "title": "School Admission"}, {"id": "ss_29", "title": "College Admission"}, {"id": "ss_30", "title": "Overseas Edu."}],
        "sub_2": [{"id": "ss_31", "title": "Science/Tech"}, {"id": "ss_32", "title": "Arts & Commerce"}, {"id": "ss_33", "title": "Vocational Tr."}],
        "sub_3": [{"id": "ss_34", "title": "Board Exams"}, {"id": "ss_35", "title": "University Sem"}, {"id": "ss_36", "title": "Comp. Exams"}],
    },
    "realestate": {
        "sub_1": [{"id": "ss_37", "title": "Flats/Apartments"}, {"id": "ss_38", "title": "Villas & Houses"}, {"id": "ss_39", "title": "Plots & Land"}],
        "sub_2": [{"id": "ss_40", "title": "Office Space"}, {"id": "ss_41", "title": "Retail Shops"}, {"id": "ss_42", "title": "Warehouses"}],
        "sub_3": [{"id": "ss_43", "title": "Prop. Appraisal"}, {"id": "ss_44", "title": "Title Check"}, {"id": "ss_45", "title": "Market Trends"}],
    },
    "hospitality": {
        "sub_1": [{"id": "ss_46", "title": "Standard Rooms"}, {"id": "ss_47", "title": "Luxury Suites"}, {"id": "ss_48", "title": "Hostels & B&B"}],
        "sub_2": [{"id": "ss_49", "title": "Fine Dining"}, {"id": "ss_50", "title": "Casual Dining"}, {"id": "ss_51", "title": "Buffet Options"}],
        "sub_3": [{"id": "ss_52", "title": "Food Order"}, {"id": "ss_53", "title": "Laundry/Cleaning"}, {"id": "ss_54", "title": "Spa & Wellness"}],
    },
    "business": {
        "sub_1": [{"id": "ss_55", "title": "Startup Strategy"}, {"id": "ss_56", "title": "Growth Planning"}, {"id": "ss_57", "title": "Exit Strategy"}],
        "sub_2": [{"id": "ss_58", "title": "Co. Registration"}, {"id": "ss_59", "title": "Tax Compliance"}, {"id": "ss_60", "title": "Contracts"}],
        "sub_3": [{"id": "ss_61", "title": "Bookkeeping"}, {"id": "ss_62", "title": "Audits"}, {"id": "ss_63", "title": "Fundraising"}],
    },
    "recruitment": {
        "sub_1": [{"id": "ss_64", "title": "IT & Software"}, {"id": "ss_65", "title": "Sales & Mktg."}, {"id": "ss_66", "title": "HR & Admin"}],
        "sub_2": [{"id": "ss_67", "title": "Upload CV"}, {"id": "ss_68", "title": "Video Profile"}, {"id": "ss_69", "title": "Cover Letter"}],
        "sub_3": [{"id": "ss_70", "title": "Mock Interviews"}, {"id": "ss_71", "title": "Aptitude Tests"}, {"id": "ss_72", "title": "Salary Negot."}],
    },
    "customer": {
        "sub_1": [{"id": "ss_73", "title": "Track Order"}, {"id": "ss_74", "title": "Delayed Delivery"}, {"id": "ss_75", "title": "Wrong Item"}],
        "sub_2": [{"id": "ss_76", "title": "Return Policy"}, {"id": "ss_77", "title": "Refund Status"}, {"id": "ss_78", "title": "Exchange Item"}],
        "sub_3": [{"id": "ss_79", "title": "Login Issues"}, {"id": "ss_80", "title": "Update Billing"}, {"id": "ss_81", "title": "Delete Account"}],
    },
    "marketing": {
        "sub_1": [{"id": "ss_82", "title": "Insta & FB"}, {"id": "ss_83", "title": "LinkedIn Ads"}, {"id": "ss_84", "title": "Content Creation"}],
        "sub_2": [{"id": "ss_85", "title": "Newsletters"}, {"id": "ss_86", "title": "Drip Campaigns"}, {"id": "ss_87", "title": "Lead Nurturing"}],
        "sub_3": [{"id": "ss_88", "title": "On-page SEO"}, {"id": "ss_89", "title": "Backlink Bldg."}, {"id": "ss_90", "title": "Local SEO"}],
    },
    "entrepreneur": {
        "sub_1": [{"id": "ss_91", "title": "Market Research"}, {"id": "ss_92", "title": "Competitor Anal."}, {"id": "ss_93", "title": "MVP Testing"}],
        "sub_2": [{"id": "ss_94", "title": "Tech Co-founder"}, {"id": "ss_95", "title": "Sales Co-founder"}, {"id": "ss_96", "title": "Networking"}],
        "sub_3": [{"id": "ss_97", "title": "Seed Funding"}, {"id": "ss_98", "title": "Angel Investors"}, {"id": "ss_99", "title": "Pitch Deck Prep"}],
    },
    "startup": {
        "sub_1": [{"id": "ss_100", "title": "Y-Combinator"}, {"id": "ss_101", "title": "Govt. Grants"}, {"id": "ss_102", "title": "Local Hubs"}],
        "sub_2": [{"id": "ss_103", "title": "Financial Model"}, {"id": "ss_104", "title": "Go-to-Market"}, {"id": "ss_105", "title": "Revenue Streams"}],
        "sub_3": [{"id": "ss_106", "title": "Industry Experts"}, {"id": "ss_107", "title": "Alumni Network"}, {"id": "ss_108", "title": "1-on-1 Sessions"}],
    },
}

def _build_menu(lang: str = "en") -> str:
    lines = ["🤖 *Welcome to Technova AI!*\n", "Please select a domain:\n"]
    for num, (_, label, _) in BOT_REGISTRY.items():
        lines.append(f"{num}️⃣  {label}")
    lines.append("\nReply with the number of your choice.")
    return "\n".join(lines)


# ── Industry message router ───────────────────────────────────────────────────

def _route_message(phone: str, user_text: str, sess: dict, msg_id: int | None):
    stage      = sess.get("stage", "idle")
    text_lower = user_text.strip().lower()
    print(f"[ROUTE DEBUG] stage={stage!r} text_lower={text_lower!r}")

    # ── 0. Trigger keyword ────────────────────────────────────────────────────
    if text_lower in INDUSTRY_TRIGGERS:
        sess.update({
            "stage":              "lang_select",
            "active_bot":         None,
            "active_label":       None,
            "chat_history":       [],
            "startup_profile":    {},
            "startup_stage":      "questionnaire",
            "startup_last_asked": None,
        })
        ind_save_session(sess)
        send_buttons(
            phone,
            "🌐 Please select your language",
            [
                {"id": "1", "title": "English"},
                {"id": "2", "title": "Hindi"},
                {"id": "3", "title": "Gujarati"},
            ],
        )
        _save_reply(msg_id, "🌐 Please select your language [buttons sent]")
        return

    # ── 1. Idle ───────────────────────────────────────────────────────────────
    if stage == "idle":
        _send(phone, f"👋 Send *{BOT_TRIGGER_KEYWORD}* to start the Technova AI assistant.")
        return

    # ── 2. Language selection ─────────────────────────────────────────────────
    if stage == "lang_select":
        lang = LANG_TRIGGERS.get(text_lower)
        if lang:
            sess["lang"]  = lang
            sess["stage"] = "menu"
            ind_save_session(sess)
            send_list(
                phone,
                "🤖 *Welcome to Technova AI!*\n\n✨ Select an AI Assistant",
                "🚀 Open Menu",
                [{
                    "title": "🤖 AI Assistants",
                    "rows": [
                        {"id": "1",    "title": "✈️ Travel"},
                        {"id": "2",    "title": "🛡️ Insurance"},
                        {"id": "3",    "title": "🏥 Healthcare"},
                        {"id": "4",    "title": "📚 Education"},
                        {"id": "5",    "title": "🏠 Real Estate"},
                        {"id": "6",    "title": "🍽️ Hospitality"},
                        {"id": "7",    "title": "💼 Business"},
                        {"id": "8",    "title": "🤝 Recruitment"},
                        {"id": "9",    "title": "💬 Customer Service"},
                        {"id": "more", "title": "✨ More Options"},
                    ],
                }],
            )
            _save_reply(msg_id, "🤖 AI Assistant menu sent")
        else:
            send_buttons(
                phone,
                "🌐 Please select your language",
                [
                    {"id": "1", "title": "English"},
                    {"id": "2", "title": "Hindi"},
                    {"id": "3", "title": "Gujarati"},
                ],
            )
            _save_reply(msg_id, "🌐 Please select your language [buttons sent]")
        return

    # ── 3. Menu ───────────────────────────────────────────────────────────────
    if stage == "menu":
        if text_lower == "more":
            send_more_menu(phone)
            return

        entry = BOT_REGISTRY.get(text_lower)
        if not entry:
            # Fallback if text_lower is the human-readable title or bot_key
            entry = next((v for k, v in BOT_REGISTRY.items() if text_lower in {k, v[0].lower(), v[1].lower()}), None)
            
        if entry:
            bot_key, label, _ = entry
            sess["active_bot"]   = bot_key
            sess["active_label"] = label
            
            # Transition to sub_menu
            sess["stage"] = "sub_menu"
            ind_save_session(sess)
            
            sub_options = SUB_MENU_REGISTRY.get(bot_key, [])
            if sub_options:
                text_msg = t(
                    sess,
                    f"Great! You've selected *{label}*. Please choose a specific topic:",
                    f"बढ़िया! आपने *{label}* चुना है। कृपया एक विशिष्ट विषय चुनें:",
                    f"સરસ! તમે *{label}* પસંદ કર્યો. કૃપા કરીને ચોક્કસ વિષય પસંદ કરો:",
                )
                send_buttons(phone, text_msg, sub_options)
                _save_reply(msg_id, f"[Sub-menu Sent: {label}]")
            else:
                # Fallback
                sess["stage"] = "chat"
                sess["chat_history"] = []
                ind_save_session(sess)
                greeting = t(
                    sess,
                    f"Great! You've selected *{label}*. How can I help you today?",
                    f"बढ़िया! आपने *{label}* चुना है। मैं आपकी कैसे मदद कर सकता हूँ?",
                    f"સરસ! તમે *{label}* પસંદ કર્યો. હું તમને કેવી રીતે મદદ કરી શકું?",
                )
                ind_reply_and_record(phone, greeting, msg_id)
        else:
            ind_reply_and_record(phone, _build_menu(sess.get("lang", "en")), msg_id)
        return

    # ── 3.5 Sub-Menu ──────────────────────────────────────────────────────────
    if stage == "sub_menu":
        bot_key = sess.get("active_bot")
        sub_options = SUB_MENU_REGISTRY.get(bot_key, [])
        
        # Check if user typed one of the sub-option ids or titles
        selected_sub = next((opt for opt in sub_options if text_lower in {opt["id"].lower(), opt["title"].lower()}), None)
        
        if selected_sub:
            sub_id = selected_sub["id"]
            sess["active_sub_bot"] = selected_sub["title"]
            sess["active_sub_id"] = sub_id
            
            ss_options = SUB_SUB_MENU_REGISTRY.get(bot_key, {}).get(sub_id, [])
            
            if ss_options:
                sess["stage"] = "sub_sub_menu"
                ind_save_session(sess)
                
                label = sess.get("active_label", "")
                sub_title = selected_sub["title"]
                text_msg = t(
                    sess,
                    f"You chose *{sub_title}*. Please select a specific area:",
                    f"आपने *{sub_title}* चुना है। कृपया एक विशिष्ट क्षेत्र चुनें:",
                    f"તમે *{sub_title}* પસંદ કર્યું. કૃપા કરીને ચોક્કસ વિસ્તાર પસંદ કરો:",
                )
                send_buttons(phone, text_msg, ss_options)
                _save_reply(msg_id, f"[Sub-Sub-Menu Sent: {sub_title}]")
            else:
                # Fallback directly to chat
                sess["stage"] = "chat"
                sess["chat_history"] = []
                
                if bot_key == "startup":
                    sess["startup_stage"]      = "questionnaire"
                    sess["startup_last_asked"] = None
                    sess["startup_profile"]    = {}
                    
                ind_save_session(sess)
                
                label = sess.get("active_label", "")
                sub_title = selected_sub["title"]
                greeting = t(
                    sess,
                    f"You chose *{sub_title}* under *{label}*. How can I help you with this today?",
                    f"आपने *{label}* के तहत *{sub_title}* चुना है। मैं इसमें आपकी कैसे मदद कर सकता हूँ?",
                    f"તમે *{label}* હેઠળ *{sub_title}* પસંદ કર્યું. હું તમને આમાં કેવી રીતે મદદ કરી શકું?",
                )
                ind_reply_and_record(phone, greeting, msg_id)
        else:
            if sub_options:
                text_msg = t(
                    sess,
                    "Please select one of the specific topics using the buttons below:",
                    "कृपया नीचे दिए गए बटन का उपयोग करके किसी विशिष्ट विषय का चयन करें:",
                    "કૃપા કરીને નીચેના બટનોનો ઉપયોગ કરીને ચોક્કસ વિષય પસંદ કરો:",
                )
                send_buttons(phone, text_msg, sub_options)
                _save_reply(msg_id, "[Sub-menu Re-sent]")
        return

    # ── 3.7 Sub-Sub-Menu ──────────────────────────────────────────────────────
    if stage == "sub_sub_menu":
        bot_key = sess.get("active_bot")
        sub_id = sess.get("active_sub_id")
        
        ss_options = SUB_SUB_MENU_REGISTRY.get(bot_key, {}).get(sub_id, [])
        
        selected_ss = next((opt for opt in ss_options if text_lower in {opt["id"].lower(), opt["title"].lower()}), None)
        
        if selected_ss:
            sess["active_ss_bot"] = selected_ss["title"]
            sess["stage"] = "chat"
            sess["chat_history"] = []
            
            if bot_key == "startup":
                sess["startup_stage"]      = "questionnaire"
                sess["startup_last_asked"] = None
                sess["startup_profile"]    = {}
                
            ind_save_session(sess)
            
            label = sess.get("active_label", "")
            sub_title = sess.get("active_sub_bot", "")
            ss_title = selected_ss["title"]
            greeting = t(
                sess,
                f"You selected *{ss_title}* in *{sub_title}*. How can I assist you with this?",
                f"आपने *{sub_title}* में *{ss_title}* चुना है। मैं इसमें आपकी कैसे मदद कर सकता हूँ?",
                f"તમે *{sub_title}* માં *{ss_title}* પસંદ કર્યું. હું આમાં તમારી કેવી રીતે મદદ કરી શકું?",
            )
            ind_reply_and_record(phone, greeting, msg_id)
        else:
            if ss_options:
                text_msg = t(
                    sess,
                    "Please select one of the specific areas using the buttons below:",
                    "कृपया नीचे दिए गए बटन का उपयोग करके किसी विशिष्ट क्षेत्र का चयन करें:",
                    "કૃપા કરીને નીચેના બટનોનો ઉપયોગ કરીને ચોક્કસ વિસ્તાર પસંદ કરો:",
                )
                send_buttons(phone, text_msg, ss_options)
                _save_reply(msg_id, "[Sub-Sub-Menu Re-sent]")
        return

    # ── 4. Active chat ────────────────────────────────────────────────────────
    if stage == "chat":
        if text_lower in {"menu", "back", "0", "मेनू", "મેনૂ"}:
            sess["stage"]      = "menu"
            sess["active_bot"] = None
            ind_save_session(sess)
            send_list(
                phone,
                "🤖 *Welcome to Technova AI!*\n\n✨ Select an AI Assistant",
                "🚀 Open Menu",
                [{
                    "title": "🤖 AI Assistants",
                    "rows": [
                        {"id": "1",    "title": "✈️ Travel"},
                        {"id": "2",    "title": "🛡️ Insurance"},
                        {"id": "3",    "title": "🏥 Healthcare"},
                        {"id": "4",    "title": "📚 Education"},
                        {"id": "5",    "title": "🏠 Real Estate"},
                        {"id": "6",    "title": "🍽️ Hospitality"},
                        {"id": "7",    "title": "💼 Business"},
                        {"id": "8",    "title": "🤝 Recruitment"},
                        {"id": "9",    "title": "💬 Customer Service"},
                        {"id": "more", "title": "✨ More Options"},
                    ],
                }],
            )
            _save_reply(msg_id, "🤖 AI Assistant menu sent")
            return

        if text_lower in FAREWELL_KEYWORDS:
            farewell = t(
                sess,
                f"Thank you for using Technova AI! Have a great day. 😊\n\nSend *{BOT_TRIGGER_KEYWORD}* anytime to start again.",
                f"Technova AI का उपयोग करने के लिए धन्यवाद! शुभ दिन। 😊\n\nफिर से शुरू करने के लिए *{BOT_TRIGGER_KEYWORD}* भेजें।",
                f"Technova AI નો ઉપયોગ કરવા બદલ આભાર! સારો દિવસ. 😊\n\nફરીથી શરૂ કરવા *{BOT_TRIGGER_KEYWORD}* મોકલો.",
            )
            ind_reply_and_record(phone, farewell, msg_id)
            sess["stage"] = "idle"
            ind_save_session(sess)
            return

        bot_key = sess.get("active_bot")
        entry   = next(((k, v) for k, v in BOT_REGISTRY.items() if v[0] == bot_key), None)
        if not entry:
            sess["stage"] = "menu"
            ind_save_session(sess)
            ind_reply_and_record(phone, _build_menu(sess.get("lang", "en")), msg_id)
            return

        _, (_, label, ask_fn) = entry
        history = sess.get("chat_history", [])
        lang    = sess.get("lang", "en")

        try:
            if bot_key == "startup":
                raw_reply = _startup_ask(user_text, history=history, phone=phone)
                sess      = ind_get_session(phone)   # re-fetch; _startup_ask may mutate it
            else:
                sub_bot = sess.get("active_sub_bot")
                ss_bot = sess.get("active_ss_bot")
                ctx_text = user_text
                if sub_bot and ss_bot:
                    ctx_text = f"[FOCUS: {sub_bot} -> {ss_bot}]\nUser message: {user_text}"
                elif sub_bot:
                    ctx_text = f"[FOCUS ON SPECIFIC SUB-TOPIC: {sub_bot}]\nUser message: {user_text}"
                raw_reply = ask_fn(_inject_lang(ctx_text, lang), history=history)
        except Exception as e:
            logger.exception("LLM error bot=%s phone=%s: %s", bot_key, phone, e)
            raw_reply = "Sorry, something went wrong. Please try again."

        if isinstance(raw_reply, list):
            raw_reply = raw_reply[0] if raw_reply else ""
        if not isinstance(raw_reply, str):
            raw_reply = str(raw_reply)

        reply_text = markdown_to_whatsapp(raw_reply)
        ind_reply_and_record(phone, reply_text, msg_id)

        history.append({"role": "user",      "content": user_text})
        history.append({"role": "assistant", "content": raw_reply})
        if len(history) > MAX_HIST_TURNS * 2:
            history = history[-(MAX_HIST_TURNS * 2):]
        sess["chat_history"] = history
        ind_save_session(sess)
        return

    # ── Fallback ──────────────────────────────────────────────────────────────
    _send(phone, f"Send *{BOT_TRIGGER_KEYWORD}* to start.")


def _process_meta_message(msg: dict, value: dict):
    """Entry point called by the unified webhook for industry-bot messages."""
    print(f"[IND PM] called — type={msg.get('type')}, from={msg.get('from')}")

    phone = msg.get("from")
    if not phone:
        return
    if not phone.startswith("+"):
        phone = f"+{phone}"

    # Deduplication
    msg_id_wa = msg.get("id", "")
    if msg_id_wa:
        dedup_key = f"wamid:{msg_id_wa}"
        if cache.get(dedup_key):
            print(f"[IND PM] Duplicate wamid {msg_id_wa} — skipping")
            return
        cache.set(dedup_key, "1", timeout=300)

    user_text = _extract_text_from_meta_message(msg)
    if not user_text:
        logger.debug("Non-text message from %s (type=%s), skipping.", phone, msg.get("type"))
        return

    sess   = ind_get_session(phone)
    msg_id = sess.get("last_msg_id")   # already saved by unified webhook
    sess["last_user_text"] = user_text
    ind_save_session(sess)

    print(f"[IND PM DEBUG] phone={phone} msg_id from session={msg_id}")

    if sess.get("human_handoff"):
        logger.info("Human handoff active for %s, ignoring.", phone)
        return

    _route_message(phone, user_text, sess, msg_id)


def _extract_text_from_meta_message(msg: dict) -> str | None:
    msg_type = msg.get("type")
    if msg_type == "text":
        return msg.get("text", {}).get("body", "").strip()
    if msg_type == "interactive":
        interactive = msg.get("interactive", {})
        if interactive.get("type") == "button_reply":
            return interactive["button_reply"].get("id", "").strip()
        if interactive.get("type") == "list_reply":
            return (
                interactive["list_reply"].get("id", "").strip()
                or interactive["list_reply"].get("title", "").strip()
            )
    if msg_type == "button":
        return msg.get("button", {}).get("text", "").strip()
    return None


# ── Human handoff toggle ──────────────────────────────────────────────────────

@csrf_exempt
def set_human_handoff(request):
    """POST {"phone": "91...", "handoff": true/false}"""
    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)
    try:
        data = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    phone   = (data.get("phone") or "").strip()
    handoff = bool(data.get("handoff", False))
    if not phone:
        return JsonResponse({"error": "phone required"}, status=400)

    sess = ind_get_session(phone)
    sess["human_handoff"] = handoff
    ind_save_session(sess)
    return JsonResponse({"status": "ok", "phone": phone, "human_handoff": handoff})



# ═════════════════════════════════════════════════════════════════════════════
# NOTE: Webhook has been moved to CRM/META/webhook_views.py
# All bot routing (industry/wa/jms-tech + client bots) is now handled by
# WhatsAppWebhookView with multi-tenant phone_number_id routing.
# ═════════════════════════════════════════════════════════════════════════════



# ═════════════════════════════════════════════════════════════════════════════
# HOME VIEW
# ═════════════════════════════════════════════════════════════════════════════

# def home(request):
#     return render(request, "home.html")


# ── SSE helper + stream endpoint factory ─────────────────────────────────────

def _make_sse_response(process_func, session_id: str, message: str):
    def event_stream():
        try:
            result = process_func(session_id, message)
            if result["type"] == "instant":
                for reply in result["replies"]:
                    yield "data: " + json.dumps({"type": "instant", "text": reply}) + "\n\n"
                yield "data: " + json.dumps({"type": "done"}) + "\n\n"
            elif result["type"] == "stream":
                for ev in result["generator"]:
                    if isinstance(ev, str):
                        yield "data: " + json.dumps({"type": "chunk", "text": ev}) + "\n\n"
                        continue
                    if isinstance(ev, dict):
                        if ev.get("type") == "delta":
                            yield "data: " + json.dumps({"type": "chunk", "text": ev.get("text", "")}) + "\n\n"
                            continue
                        if ev.get("type") == "final":
                            for reply in ev.get("replies", []):
                                yield "data: " + json.dumps({"type": "instant", "text": reply}) + "\n\n"
                            continue
                yield "data: " + json.dumps({"type": "done"}) + "\n\n"
        except Exception as e:
            logger.error(f"Stream error: {e}", exc_info=True)
            yield "data: " + json.dumps({"type": "error", "text": str(e)}) + "\n\n"

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"]     = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


def _stream_view(process_func):
    @csrf_exempt
    def view(request):
        if request.method != "POST":
            return JsonResponse({"error": "POST only"}, status=405)
        try:
            data = json.loads(request.body.decode("utf-8"))
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)
        session_id = (data.get("session_id") or "").strip() or str(uuid.uuid4())
        message    = (data.get("message") or "").strip()
        if not message:
            return JsonResponse({"error": "Message required"}, status=400)
        return _make_sse_response(process_func, session_id, message)
    return view


travel_send_message_stream           = _stream_view(travel_stream)
health_send_message_stream           = _stream_view(health_stream)
education_send_message_stream        = _stream_view(education_stream)
customer_send_message_stream         = _stream_view(customer_stream)
business_send_message_stream         = _stream_view(business_stream)
recruitment_send_message_stream      = _stream_view(recruitment_stream)
BFSI_send_message_stream             = _stream_view(sales_stream)
estate_send_message_stream           = _stream_view(estate_stream)
hospitality_send_message_stream      = _stream_view(hospitality_stream)
marketing_send_message_stream        = _stream_view(salesmarketing_stream)
eye_send_message_stream              = _stream_view(eye_stream)
entrepreneurship_send_message_stream = _stream_view(entrepreneurship_stream)


# ═════════════════════════════════════════════════════════════════════════════
# PDF HELPERS  (JMS-Tech bot — generate & upload report)
# ═════════════════════════════════════════════════════════════════════════════

def wrap_text(text, font_name, font_size, max_width):
    words        = text.split(" ")
    lines        = []
    current_line = ""
    for word in words:
        test_line = f"{current_line} {word}".strip()
        if stringWidth(test_line, font_name, font_size) <= max_width:
            current_line = test_line
        else:
            if current_line:
                lines.append(current_line)
            current_line = word
    if current_line:
        lines.append(current_line)
    return lines


def generate_pdf(report_text: str, website_url: str) -> BytesIO:
    buffer       = BytesIO()
    c            = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    margin_x      = 40
    margin_y      = 50
    usable_width  = width - (2 * margin_x)
    y             = height - margin_y

    c.setFont("Helvetica-Bold", 16)
    c.drawString(margin_x, y, "Website Analysis Report")
    y -= 25
    c.setFont("Helvetica", 10)
    c.drawString(margin_x, y, f"Website: {website_url}")
    c.drawString(margin_x, y - 15, f"Generated on: {datetime.utcnow().strftime('%d %b %Y')}")
    y -= 40

    for raw_line in report_text.split("\n"):
        raw_line = raw_line.strip()
        if y < margin_y:
            c.showPage()
            c.setFont("Helvetica", 11)
            y = height - margin_y
        if raw_line.endswith(":"):
            c.setFont("Helvetica-Bold", 12)
            for line in wrap_text(raw_line, "Helvetica-Bold", 12, usable_width):
                c.drawString(margin_x, y, line)
                y -= 18
            c.setFont("Helvetica", 11)
            y -= 5
        else:
            for line in wrap_text(raw_line, "Helvetica", 11, usable_width):
                c.drawString(margin_x + 10, y, line)
                y -= 15

    c.save()
    buffer.seek(0)
    return buffer


def upload_pdf_and_get_url(local_path: str) -> str:
    file_name = f"media/reports/{os.path.basename(local_path)}"
    with open(local_path, "rb") as f:
        default_storage.save(file_name, File(f))
    return "https://e2095a0d4237.ngrok-free.app" + settings.MEDIA_URL + file_name


# ═════════════════════════════════════════════════════════════════════════════
# DASHBOARD / CRM APIs
# ═════════════════════════════════════════════════════════════════════════════

def _auth(request) -> bool:
    return True   # swap for real auth when ready


def _json(data, status=200):
    return JsonResponse(data, status=status, safe=isinstance(data, dict))


def _forbidden():
    return _json({"error": "Unauthorized. Provide a valid X-API-Key header."}, 403)


def _parse_date_range(request):
    from_str = request.GET.get("from")
    to_str   = request.GET.get("to")
    from_dt  = to_dt = None
    try:
        if from_str:
            from_dt = timezone.make_aware(timezone.datetime.strptime(from_str, "%Y-%m-%d"))
        if to_str:
            to_dt = timezone.make_aware(
                timezone.datetime.strptime(to_str, "%Y-%m-%d") + timedelta(days=1)
            )
    except ValueError:
        pass
    return from_dt, to_dt


@csrf_exempt
def dashboard_summary(request):
    from rest_framework_simplejwt.authentication import JWTAuthentication
    from rest_framework.exceptions import AuthenticationFailed
    
    auth = JWTAuthentication()
    try:
        user_auth_tuple = auth.authenticate(request)
        if not user_auth_tuple:
            return _forbidden()
        user, token = user_auth_tuple
    except AuthenticationFailed:
        return _forbidden()

    # Get TechProvider's phone_number_id
    phone_number_id = None
    org = getattr(user, "organization", None)
    if not org and hasattr(user, "membership"):
        org = user.membership.organization
    
    if org:
        try:
            phone_number_id = org.waba_account.phone_number_id
        except Exception:
            pass

    now   = timezone.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if phone_number_id:
        customer_qs = Customer.objects.filter(
            messages__direction="inbound", conversations__phone_number_id=phone_number_id,
            
        ).distinct()
        conversation_qs = Conversation.objects.filter(phone_number_id=phone_number_id)
        message_qs = Message.objects.filter(conversation__phone_number_id=phone_number_id)
    else:
        customer_qs = Customer.objects.none()
        conversation_qs = Conversation.objects.none()
        message_qs = Message.objects.none()

    total_customers     = customer_qs.count()
    total_conversations = conversation_qs.count()
    total_messages      = message_qs.count()
    
    # Active today: Conversations that had an inbound reply today
    active_today = conversation_qs.filter(
        messages__direction="inbound", messages__timestamp__gte=today,
        
    ).distinct().count()
    
    # New today: Customers created today who have replied
    new_today = customer_qs.filter(
        messages__direction="inbound", conversations__created_at__gte=today,
        
    ).distinct().count()

    status_qs               = conversation_qs.values("status").annotate(count=Count("id"))
    conversations_by_status = {row["status"]: row["count"] for row in status_qs}

    daily_qs = (
        message_qs
        .annotate(day=TruncDate("timestamp"))
        .values("day")
        .annotate(count=Count("id"))
        .order_by("-day")[:7]
    )
    
    # Reverse to show chronologically in the chart
    daily_list = list(daily_qs)
    daily_list.reverse()
    
    messages_last_7_days = [{"date": str(row["day"]), "count": row["count"]} for row in daily_list]

    return _json({
        "total_customers":         total_customers,
        "total_conversations":     total_conversations,
        "total_messages":          total_messages,
        "active_today":            active_today,
        "new_today":               new_today,
        "conversations_by_status": conversations_by_status,
        "messages_last_7_days":    messages_last_7_days,
    })


@csrf_exempt
def customer_list(request):
    # Parse JWT Token manually since this isn't a DRF APIView
    from rest_framework_simplejwt.authentication import JWTAuthentication
    from rest_framework.exceptions import AuthenticationFailed
    
    auth = JWTAuthentication()
    try:
        user_auth_tuple = auth.authenticate(request)
        if not user_auth_tuple:
            return _forbidden()
        user, token = user_auth_tuple
    except AuthenticationFailed:
        return _forbidden()

    # Get TechProvider's phone_number_id
    phone_number_id = None
    org = getattr(user, "organization", None)
    if not org and hasattr(user, "membership"):
        org = user.membership.organization
    
    if org:
        try:
            phone_number_id = org.waba_account.phone_number_id
        except Exception:
            pass

    search    = request.GET.get("search", "").strip()
    status    = request.GET.get("status", "").strip()
    page      = max(int(request.GET.get("page", 1)), 1)
    page_size = min(int(request.GET.get("page_size", 100)), 100)

    first_msg_subq = Message.objects.filter(
        customer=OuterRef("pk")
    ).order_by("timestamp").values("content")[:1]
    
    first_seen_subq = Message.objects.filter(
        customer=OuterRef("pk")
    ).order_by("timestamp").values("timestamp")[:1]
    
    from django.db.models import Q
    base_qs = Customer.objects.all()
    if phone_number_id:
        base_qs = base_qs.filter(
            Q(conversations__client__phone_number_id=phone_number_id) |
            Q(conversations__phone_number_id=phone_number_id),
            messages__direction="inbound"
        ).distinct()
    else:
        # If no phone_number_id is found for TechProvider, do not leak clients
        base_qs = Customer.objects.none()

    qs = base_qs.annotate(
        total_conversations=Count("conversations", distinct=True),
        total_messages=Count("messages", distinct=True),
        last_seen=Max("messages__timestamp"),
        created_at=Subquery(first_seen_subq),
        closing_count=Count(
            "messages",
            filter=Q(messages__direction="inbound") & (
                Q(messages__content__icontains="thank you") |
                Q(messages__content__icontains="thanks") |
                Q(messages__content__iexact="tq") |
                Q(messages__content__iexact="thx")
            ),
            distinct=True,
        ),
        first_message_content=Subquery(first_msg_subq),
        last_hi_id=Max(
            "messages__id",
            filter=Q(messages__content__in=["hi", "Hi", "HI", "hello", "Hello", "hey", "Hey"]),
        ),
        last_jms_id=Max(
            "messages__id",
            filter=Q(messages__content__iexact="jms"),
        ),
        last_wa_id=Max(
            "messages__id",
            filter=Q(messages__content__iexact="whatsapp"),
        ),
        last_mf_id=Max(
            "messages__id",
            filter=Q(messages__content__iexact="mutual funds") | Q(messages__content__iexact="mf"),
        ),
        last_hotel_id=Max(
            "messages__id",
            filter=Q(messages__direction="inbound") & Q(messages__content__iexact="service"),
        ),
        last_mody_id=Max(
            "messages__id",
            filter=Q(messages__direction="inbound") & Q(messages__content__iexact="mody"),
        ),
    ).order_by("-id")

    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(phone__icontains=search))

    all_customers  = list(qs)
    total          = len(all_customers)
    lead_count     = sum(1 for c in all_customers if c.closing_count > 0)
    prospect_count = total - lead_count

    results = []
    for c in all_customers:
        customer_status = "lead" if c.closing_count > 0 else "prospect"
        if status and customer_status != status:
            continue

        # Determine latest bot based on max message ID of triggers
        bots = []
        if c.last_hi_id:
            bots.append((c.last_hi_id, "industry"))
        if c.last_jms_id:
            bots.append((c.last_jms_id, "jms"))
        if c.last_wa_id:
            bots.append((c.last_wa_id, "whatsapp"))
        if c.last_mf_id:
            bots.append((c.last_mf_id, "mutual_funds"))
        if c.last_hotel_id:
            bots.append((c.last_hotel_id, "hotel"))
        if c.last_mody_id:
            bots.append((c.last_mody_id, "mody"))
        
        if bots:
            bot_source = max(bots, key=lambda x: x[0])[1]
        else:
            bot_source = "unknown"

        results.append({
            "id":                  c.id,
            "name":                c.name,
            "phone":               c.phone,
            "total_conversations": c.total_conversations,
            "total_messages":      c.total_messages,
            "last_seen":           c.last_seen.isoformat() if c.last_seen else None,
            "created_at":          c.created_at.isoformat() if c.created_at else None,
            "status":              customer_status,
            "bot_source":          bot_source,
        })

    filtered_total = len(results)
    start          = (page - 1) * page_size
    paged          = results[start : start + page_size]

    return _json({
        "count":          filtered_total,
        "total_count":    total,
        "lead_count":     lead_count,
        "prospect_count": prospect_count,
        "page":           page,
        "page_size":      page_size,
        "results":        paged,
    })


@csrf_exempt
def customer_detail(request, phone):
    if not _auth(request):
        return _forbidden()

    customer = Customer.objects.filter(Q(phone=phone) | Q(phone=phone.lstrip("+"))).first()
    if not customer:
        return _json({"error": "Customer not found"}, 404)

    conversations = []
    for conv in customer.conversations.order_by("-created_at"):
        msgs = [
            {
                "id": m.id, "content": m.content, "reply_of": m.reply_of,
                "timestamp": m.timestamp.isoformat(), "client_name": m.client_name,
            }
            for m in conv.messages.order_by("timestamp")
        ]
        conversations.append({
            "id": conv.id, "status": conv.status,
            "created_at": conv.created_at.isoformat(), "messages": msgs,
        })

    return _json({
        "id": customer.id, "name": customer.name,
        "phone": customer.phone, "conversations": conversations,
    })


@csrf_exempt
def conversation_list(request):
    from rest_framework_simplejwt.authentication import JWTAuthentication
    from rest_framework.exceptions import AuthenticationFailed
    from django.db.models import Q
    
    auth = JWTAuthentication()
    try:
        user_auth_tuple = auth.authenticate(request)
        if not user_auth_tuple:
            return _forbidden()
        user, token = user_auth_tuple
    except AuthenticationFailed:
        return _forbidden()

    # Get TechProvider's phone_number_id
    phone_number_id = None
    org = getattr(user, "organization", None)
    if not org and hasattr(user, "membership"):
        org = user.membership.organization
    
    if org:
        try:
            phone_number_id = org.waba_account.phone_number_id
        except Exception:
            pass

    status_filter  = request.GET.get("status", "").strip()
    phone_filter   = request.GET.get("phone",  "").strip()
    page           = max(int(request.GET.get("page", 1)), 1)
    page_size      = min(int(request.GET.get("page_size", 100)), 100)
    from_dt, to_dt = _parse_date_range(request)

    qs = Conversation.objects.select_related("customer").annotate(
        message_count=Count("messages"),
        last_msg_time=Max("messages__timestamp")
    ).order_by("-last_msg_time", "-created_at")

    if phone_number_id:
        qs = qs.filter(
            Q(client__phone_number_id=phone_number_id) |
            Q(phone_number_id=phone_number_id)
        )
    else:
        qs = qs.none()

    if status_filter: qs = qs.filter(status=status_filter)
    if phone_filter:  qs = qs.filter(customer__phone__icontains=phone_filter)
    if from_dt:       qs = qs.filter(created_at__gte=from_dt)
    if to_dt:         qs = qs.filter(created_at__lt=to_dt)

    total = qs.count()
    convs = qs[(page - 1) * page_size : page * page_size]

    results = []
    for conv in convs:
        last_msg   = conv.messages.order_by("-timestamp").first()
        # Lead if there's any inbound message with a closing sentence
        has_closing = conv.messages.filter(
            Q(direction="inbound") & (
                Q(content__icontains="thank you") |
                Q(content__icontains="thanks") |
                Q(content__iexact="tq") |
                Q(content__iexact="thx")
            )
        ).exists()
        auto_status = "lead" if has_closing else "prospect"
        phone = conv.customer.phone
        if phone and len(phone) == 10 and phone.isdigit():
            phone = f"91{phone}"
            
        results.append({
            "id":                conv.id,
            "customer_name":     conv.customer.name,
            "customer_phone":    phone,
            "status":            auto_status,
            "created_at":        conv.created_at.isoformat(),
            "message_count":     conv.message_count,
            "last_message":      last_msg.content[:120] if last_msg else None,
            "last_message_time": last_msg.timestamp.isoformat() if last_msg else None,
        })

    return _json({"count": total, "page": page, "page_size": page_size, "results": results})


@csrf_exempt
def conversation_messages(request, conversation_id):
    if not _auth(request):
        return _forbidden()

    try:
        conv = Conversation.objects.select_related("customer").get(id=conversation_id)
    except Conversation.DoesNotExist:
        return _json({"error": "Conversation not found"}, 404)

    messages = [
        {
            "id": m.id, "content": m.content, "reply_of": m.reply_of,
            "timestamp": m.timestamp.isoformat(), "client_name": m.client_name,
        }
        for m in conv.messages.order_by("timestamp")
    ]
    cust_phone = conv.customer.phone
    if cust_phone and len(cust_phone) == 10 and cust_phone.isdigit():
        cust_phone = f"91{cust_phone}"

    return _json({
        "conversation_id": conv.id,
        "customer_name":   conv.customer.name,
        "customer_phone":  cust_phone,
        "status":          conv.status,
        "messages":        messages,
    })


@csrf_exempt
def recent_messages(request):
    if not _auth(request):
        return _forbidden()

    limit     = min(int(request.GET.get("limit", 50)), 200)
    since_str = request.GET.get("since", "")
    since_dt  = None
    if since_str:
        try:
            since_dt = timezone.datetime.fromisoformat(since_str.replace("Z", "+00:00"))
        except ValueError:
            pass

    qs = Message.objects.select_related("customer", "conversation").order_by("-timestamp")
    if since_dt:
        qs = qs.filter(timestamp__gt=since_dt)

    messages = [
        {
            "id": m.id, "customer_name": m.customer.name, "customer_phone": m.customer.phone,
            "conversation_id": m.conversation_id, "content": m.content,
            "reply_of": m.reply_of, "timestamp": m.timestamp.isoformat(),
        }
        for m in qs[:limit]
    ]
    return _json({"count": len(messages), "messages": messages})


@csrf_exempt
def analytics_messages(request):
    if not _auth(request):
        return _forbidden()

    from_dt, to_dt = _parse_date_range(request)
    group_by       = request.GET.get("group_by", "day")

    qs = Message.objects.all()
    if from_dt: qs = qs.filter(timestamp__gte=from_dt)
    if to_dt:   qs = qs.filter(timestamp__lt=to_dt)

    trunc_sql = (
        "DATE(timestamp - INTERVAL (DAYOFWEEK(timestamp)-2) DAY)"
        if group_by == "week"
        else "DATE(timestamp)"
    )
    rows = (
        qs.extra(select={"period": trunc_sql})
        .values("period")
        .annotate(
            total=Count("id"),
            with_reply=Count("id", filter=Q(reply_of__isnull=False) & ~Q(reply_of="")),
        )
        .order_by("period")
    )
    data = [
        {
            "period":     str(r["period"]),
            "total":      r["total"],
            "with_reply": r["with_reply"],
            "no_reply":   r["total"] - r["with_reply"],
        }
        for r in rows
    ]
    return _json({
        "from":     str(from_dt.date()) if from_dt else None,
        "to":       str(to_dt.date()) if to_dt else None,
        "group_by": group_by,
        "data":     data,
    })


@csrf_exempt
def analytics_customers(request):
    if not _auth(request):
        return _forbidden()

    from_dt, to_dt = _parse_date_range(request)
    qs = Conversation.objects.all()
    if from_dt: qs = qs.filter(created_at__gte=from_dt)
    if to_dt:   qs = qs.filter(created_at__lt=to_dt)

    rows = (
        qs.extra(select={"date": "DATE(created_at)"})
        .values("date")
        .annotate(new_customers=Count("customer", distinct=True))
        .order_by("date")
    )
    return _json({
        "data": [{"date": str(r["date"]), "new_customers": r["new_customers"]} for r in rows]
    })


@csrf_exempt
def global_search(request):
    if not _auth(request):
        return _forbidden()

    q = request.GET.get("q", "").strip()
    if not q:
        return _json({"customers": [], "messages": []})

    customers = [
        {"id": c.id, "name": c.name, "phone": c.phone}
        for c in Customer.objects.filter(Q(name__icontains=q) | Q(phone__icontains=q))[:10]
    ]
    messages = [
        {
            "id": m.id, "customer_name": m.customer.name, "customer_phone": m.customer.phone,
            "conversation_id": m.conversation_id, "content": m.content[:200],
            "timestamp": m.timestamp.isoformat(),
        }
        for m in Message.objects.select_related("customer").filter(
            Q(content__icontains=q) | Q(reply_of__icontains=q)
        ).order_by("-timestamp")[:20]
    ]
    return _json({"customers": customers, "messages": messages})

# ═════════════════════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────
#  BOT 4 — MUTUAL FUNDS BOT  (trigger: "mutual funds")
# ─────────────────────────────────────────────────────────────────────────────
# ═════════════════════════════════════════════════════════════════════════════

MF_BOT_TRIGGER = ("mutual funds", "mf")
MF_SESSION_TTL = 60 * 60 * 24

class SafeCache:
    def __init__(self, prefix: str, default_ttl: int):
        self.prefix = prefix
        self.default_ttl = default_ttl
    def get(self, key: str):
        from django.core.cache import cache
        return cache.get(self.prefix + key)
    def set(self, key: str, value, timeout: int = None):
        from django.core.cache import cache
        cache.set(self.prefix + key, value, timeout or self.default_ttl)
    def delete(self, key: str):
        from django.core.cache import cache
        cache.delete(self.prefix + key)

mf_sessions = SafeCache(prefix="mf_sess:", default_ttl=MF_SESSION_TTL)

def mf_get_session(phone: str, create: bool = True) -> dict | None:
    sess = mf_sessions.get(phone)
    if not sess:
        if not create:
            return None
        sess = {"phone": phone, "stage": "active", "history": []}
        mf_sessions.set(phone, sess, timeout=MF_SESSION_TTL)
    return sess

def mf_save_session(sess: dict) -> None:
    mf_sessions.set(sess["phone"], sess, timeout=MF_SESSION_TTL)

def _handle_mf_bot(phone: str, text: str, phone_number_id: str = None, inbound_msg_id: int = None, raw_msg: dict = None):
    text_lower = text.lower().strip()
    session = mf_get_session(phone, create=True)
    stage = session.get("stage", "active")
    company = session.get("company", "")
    
    # Parse Interactive ID
    interactive_id = None
    if raw_msg and raw_msg.get("type") == "interactive":
        interactive = raw_msg.get("interactive", {})
        itype = interactive.get("type")
        if itype == "list_reply":
            interactive_id = interactive.get("list_reply", {}).get("id", "")
        elif itype == "button_reply":
            interactive_id = interactive.get("button_reply", {}).get("id", "")

    # Exit / Farewell
    farewell_triggers = ["thank you", "thanks", "thankyou", "bye", "goodbye", "exit", "quit", "done", "ok thanks"]
    if any(trigger == text_lower for trigger in farewell_triggers) or \
       (text_lower.startswith("ok") and "thank" in text_lower) or \
       (text_lower.startswith("okay") and "thank" in text_lower):
        mf_sessions.delete(phone)
        bye_text = "Thank you for consulting with me! I'm glad I could help with your mutual fund queries. If you need any more information on Indian mutual funds in the future, just type *mutual funds* anytime to start again. Have a great day! 👋"
        send_text(phone, bye_text)
        _save_reply(inbound_msg_id, bye_text)
        return

    # Send Another NAV (mf_restart)
    if interactive_id == "mf_restart":
        last_query = session.get("last_query")
        if last_query:
            session["stage"] = "active"
            mf_save_session(session)
            _mf_search_and_send(phone, last_query, inbound_msg_id)
            return
        else:
            # Fallback to welcome menu if no last query is available
            text_lower = "mutual funds"  # Just pick the primary one to force fallback

    # Welcome Menu
    if text_lower in MF_BOT_TRIGGER:
        session["stage"] = "active"
        session["company"] = ""
        session["last_scheme_context"] = None
        session["history"] = []
        session["compare_mode"] = False
        session["compare_step"] = 0
        session["compare_scheme_1"] = None
        mf_save_session(session)
        
        welcome_text = (
            "📈 *Welcome to the Mutual Funds Bot!*\n\n"
            "How can I help you today? If you know the mutual fund name then write it, or else select an option from below."
        )
        
        buttons = [
            {"id": "btn_select_company", "title": "Select Company"},
            {"id": "btn_compare_schemes", "title": "Compare Schemes"}
        ]
        send_buttons(phone, welcome_text, buttons)
        _save_reply(inbound_msg_id, welcome_text)
        return

    if interactive_id == "btn_select_company":
        _send_amc_page(phone, 0, "Please select a company:", inbound_msg_id)
        return

    if interactive_id == "btn_compare_schemes":
        session["stage"] = "active"
        session["company"] = ""
        session["compare_mode"] = True
        session["compare_step"] = 1
        session["compare_scheme_1"] = None
        mf_save_session(session)
        
        msg = "🔄 *Compare Mutual Funds*\n\nLet's select the FIRST scheme. Please select a company:"
        _send_amc_page(phone, 0, msg, inbound_msg_id)
        return

    if interactive_id and interactive_id.startswith("amcpage_"):
        page = int(interactive_id.split("_")[1])
        _send_amc_page(phone, page, "Select Company (Continued):", inbound_msg_id)
        return

    if interactive_id and interactive_id.startswith("schemepage_"):
        page = int(interactive_id.split("_")[1])
        _mf_search_and_send(phone, session.get("last_query", ""), inbound_msg_id, page=page)
        return

    # "Know More" (LLM context)
    if interactive_id == "mf_know_more":
        session["stage"] = "awaiting_llm_query"
        mf_save_session(session)
        msg = "🤖 What would you like to know about this scheme?"
        send_text(phone, msg)
        _save_reply(inbound_msg_id, msg)
        return

    # Handle LLM Query
    if stage == "awaiting_llm_query":
        context_str = session.get("last_scheme_context", "No context available.")
        answer = ask_mf_assistant(text, context_str)
        send_text(phone, answer)
        _save_reply(inbound_msg_id, answer)
        return

    # Handle Company Selection from List
    if interactive_id and interactive_id.startswith("company_"):
        selected_company = interactive_id.replace("company_", "")
        session["stage"] = "awaiting_category"
        session["company"] = selected_company
        mf_save_session(session)
        
        msg = f"You selected *{selected_company}*. Please choose a category:"
        cats = ["Equity", "Debt", "Liquid", "Other"]
        items = []
        for cat in cats:
            items.append({
                "id": f"category_{cat}",
                "title": cat,
                "description": f"{cat} schemes"
            })
        sections = [{"title": "Select Category", "rows": items}]
        send_interactive_list(
            to=phone, 
            body_text=msg, 
            button_text="Select Category", 
            sections=sections
        )
        _save_reply(inbound_msg_id, msg)
        return

    # Handle Category Selection
    if interactive_id and interactive_id.startswith("category_"):
        selected_category = interactive_id.replace("category_", "")
        
        if selected_category.lower() == "other":
            session["stage"] = "active"
            mf_save_session(session)
            msg = "Please type the specific mutual fund name or category you are looking for:"
            send_text(phone, msg)
            _save_reply(inbound_msg_id, msg)
            return

        query = f"{company} {selected_category}".strip()
        
        session["stage"] = "active" # Reset stage so direct typing falls back to search
        session["last_query"] = query
        mf_save_session(session)
        
        _mf_search_and_send(phone, query, inbound_msg_id)
        return

    # Handle Scheme Selection (Scheme Code is always numeric)
    if interactive_id and interactive_id.isdigit():
        scheme_code = interactive_id
        
        # Check compare mode
        if session.get("compare_mode"):
            if session.get("compare_step") == 1:
                session["compare_scheme_1"] = scheme_code
                session["compare_step"] = 2
                mf_save_session(session)
                msg = "✅ First scheme selected.\n\nNow, let's select the SECOND scheme to compare against. Please select a company:"
                _send_amc_page(phone, 0, msg, inbound_msg_id)
                return
            elif session.get("compare_step") == 2:
                scheme1 = session.get("compare_scheme_1")
                scheme2 = scheme_code
                session["compare_mode"] = False
                session["compare_step"] = 0
                session["compare_scheme_1"] = None
                mf_save_session(session)
                
                send_text(phone, "🔄 Comparing schemes, please wait a moment...")
                _send_mutual_fund_comparison(phone, scheme1, scheme2, inbound_msg_id)
                return
        try:
            resp = requests.get(f"https://api.mfapi.in/mf/{scheme_code}/latest", timeout=60)
            if resp.status_code == 200:
                result = resp.json()
                if result.get("status") == "SUCCESS":
                    meta = result.get("meta", {})
                    nav_data = result.get("data", [])
                    if nav_data:
                        latest = nav_data[0]
                        reply = (
                            f"📊 *Scheme:* {meta.get('scheme_name', 'N/A')}\n"
                            f"🏢 *Fund House:* {meta.get('fund_house', 'N/A')}\n"
                            f"📁 *Category:* {meta.get('scheme_category', 'N/A')}\n\n"
                            f"📈 *Latest NAV:* ₹{latest.get('nav', 'N/A')}\n"
                            f"📅 *Date:* {latest.get('date', 'N/A')}"
                        )
                        # Save context for LLM
                        session["last_scheme_context"] = reply
                        session["stage"] = "active"
                        mf_save_session(session)
                        
                        # Send NAV text
                        send_text(phone, reply)
                        _save_reply(inbound_msg_id, reply)
                        
                        # Send Post-NAV Buttons
                        buttons = [
                            {"id": "mf_restart", "title": "Send Another NAV"},
                            {"id": "mf_know_more", "title": "Know More"}
                        ]
                        send_buttons(phone, "What would you like to do next?", buttons)
                    else:
                        send_text(phone, "No NAV data available for this scheme.")
                else:
                    send_text(phone, "Failed to fetch NAV data.")
            else:
                send_text(phone, "Failed to fetch NAV data.")
        except Exception as e:
            send_text(phone, f"Error: {str(e)}")
        return

    # Direct Search (Fallback for any unhandled text/interactive)

    if text_lower and not interactive_id:
        session["last_query"] = text_lower
        mf_save_session(session)
        _mf_search_and_send(phone, text_lower, inbound_msg_id)
        return

    # Fallback
    fallback_msg = "I didn't understand. Please use the menu options or send 'exit' to quit."
    send_text(phone, fallback_msg)
    _save_reply(inbound_msg_id, fallback_msg)

def _mf_search_and_send(phone: str, query: str, inbound_msg_id: int = None, page: int = 0):
    try:
        resp = requests.get(f"https://api.mfapi.in/mf/search?q={query}", timeout=60)
        if resp.status_code == 200:
            data = resp.json()
            if not data:
                send_text(phone, "No schemes found for that query. Try another one.")
            else:
                # Filter for Regular Plan - Growth / IDCW
                filtered_data = []
                for item in data:
                    name_lower = item["schemeName"].lower()
                    
                    # Exclude Direct, Institutional, Premium, Wholesale, Bonus, Retail
                    is_excluded = any(x in name_lower for x in ["direct", "institutional", "premium", "wholesale", "bonus", "retail", "institutional plus"])
                    
                    # Include Growth or IDCW (or Dividend, which is the old name for IDCW)
                    is_growth_or_idcw = any(x in name_lower for x in ["growth", " gro", "-gro", "idcw", "div", "dividend"])
                    
                    if not is_excluded and is_growth_or_idcw:
                        filtered_data.append(item)
                        
                if not filtered_data:
                    send_text(phone, "No 'Regular Plan - Growth' or 'IDCW' schemes found for this category.")
                    return
                    
                items_per_page = 9
                start_idx = page * items_per_page
                end_idx = start_idx + items_per_page
                
                current_data = filtered_data[start_idx:end_idx]
                
                items = []
                seen_titles = set()
                for item in current_data:
                    raw_title = item["schemeName"][:24].strip()
                    title = raw_title
                    counter = 1
                    while title in seen_titles:
                        suffix = f" {counter}"
                        base = raw_title[:24 - len(suffix)].strip()
                        title = base + suffix
                        counter += 1
                    seen_titles.add(title)
                    
                    items.append({
                        "id": str(item["schemeCode"]),
                        "title": title,
                        "description": item["schemeName"][:72].strip()
                    })
                    
                has_next = end_idx < len(filtered_data)
                if has_next:
                    items.append({
                        "id": f"schemepage_{page+1}",
                        "title": "More Schemes ➡️",
                        "description": "View more results"
                    })
                        
                sections = [{"title": "Search Results", "rows": items}]
                send_interactive_list(
                    to=phone, 
                    body_text=f"Search Results for '{query}' (Page {page+1})", 
                    button_text="Select Scheme", 
                    sections=sections
                )
                if inbound_msg_id:
                    _save_reply(inbound_msg_id, f"Sent search results for {query}")
        else:
            send_text(phone, "Search failed.")
    except Exception as e:
        send_text(phone, f"Error: {str(e)}")

def _send_amc_page(phone: str, page: int, body_text: str, inbound_msg_id: int = None):
    amcs = [
        "SBI", "ICICI", "HDFC", "Nippon", "Kotak", "UTI", "Aditya Birla", "DSP", 
        "Franklin Templeton", "Axis", "Mirae Asset", "Tata", "L&T", "Edelweiss", "Motilal Oswal", 
        "Parag Parikh", "Quant", "Canara Robeco", "Bandhan", "Navi", "Mahindra Manulife", 
        "Invesco", "Sundaram", "PGIM India", "WhiteOak", "Baroda BNP", "HSBC", "LIC", 
        "Union", "Bank of India"
    ]
    items_per_page = 9
    start_idx = page * items_per_page
    end_idx = start_idx + items_per_page
    
    current_amcs = amcs[start_idx:end_idx]
    
    items = []
    for amc in current_amcs:
        items.append({
            "id": f"company_{amc}",
            "title": amc[:24]
        })
        
    has_next = end_idx < len(amcs)
    if has_next:
        items.append({
            "id": f"amcpage_{page+1}",
            "title": "More Companies ➡️",
            "description": "View more AMCs"
        })
        
    sections = []
    sections.append({
        "title": "Select Company",
        "rows": items
    })
    
    send_interactive_list(
        to=phone, 
        body_text=body_text, 
        button_text="Select Company", 
        sections=sections
    )
    if inbound_msg_id:
        _save_reply(inbound_msg_id, body_text)

def _get_historical_nav(nav_data: list, target_days_ago: int):
    from datetime import datetime, timedelta
    
    if not nav_data:
        return "N/A"
        
    latest_date_str = nav_data[0].get("date")
    if not latest_date_str:
        return "N/A"
        
    try:
        latest_date = datetime.strptime(latest_date_str, "%d-%m-%Y")
    except:
        return "N/A"
        
    target_date = latest_date - timedelta(days=target_days_ago)
    
    # Find closest date
    closest_nav = nav_data[0].get("nav", "N/A")
    min_diff = 99999
    
    for item in nav_data:
        try:
            item_date = datetime.strptime(item["date"], "%d-%m-%Y")
            diff = abs((item_date - target_date).days)
            if diff < min_diff:
                min_diff = diff
                closest_nav = item["nav"]
        except:
            continue
            
    return closest_nav

def _send_mutual_fund_comparison(phone: str, scheme1: str, scheme2: str, inbound_msg_id: int = None):
    try:
        import requests
        
        # Fetch Scheme 1 - Get full history!
        r1 = requests.get(f"https://api.mfapi.in/mf/{scheme1}", timeout=60).json()
        # Fetch Scheme 2 - Get full history!
        r2 = requests.get(f"https://api.mfapi.in/mf/{scheme2}", timeout=60).json()
        
        s1_meta = r1.get("meta", {})
        s1_data = r1.get("data", [])
        
        s2_meta = r2.get("meta", {})
        s2_data = r2.get("data", [])
        
        name1 = s1_meta.get("scheme_name", f"Scheme {scheme1}")
        name2 = s2_meta.get("scheme_name", f"Scheme {scheme2}")
        
        def format_navs(data):
            if not data:
                return ["N/A"] * 5
                
            curr_str = data[0].get("nav", "0")
            try:
                curr = float(curr_str)
            except:
                curr = 0.0
                
            def get_with_pct(days):
                old_str = _get_historical_nav(data, days)
                if old_str == "N/A" or curr == 0.0:
                    return old_str
                try:
                    old_f = float(old_str)
                    if old_f == 0:
                        return old_str
                    pct = ((curr - old_f) / old_f) * 100
                    sign = "+" if pct >= 0 else ""
                    return f"₹{old_str} ({sign}{pct:.2f}%)"
                except:
                    return old_str
                    
            curr_fmt = f"{curr}" if curr != 0.0 else "N/A"
            nav_1m = get_with_pct(30)
            nav_3m = get_with_pct(90)
            nav_6m = get_with_pct(180)
            nav_1y = get_with_pct(365)
            
            return curr_fmt, nav_1m, nav_3m, nav_6m, nav_1y
            
        c1, m1_1, m3_1, m6_1, y1_1 = format_navs(s1_data)
        c2, m1_2, m3_2, m6_2, y1_2 = format_navs(s2_data)
        
        msg = f"📊 *Mutual Fund Comparison*\n\n"
        msg += f"🔹 *{name1}*\n"
        msg += f"• Current NAV: ₹{c1}\n"
        msg += f"• 1 Month: {m1_1}\n"
        msg += f"• 3 Months: {m3_1}\n"
        msg += f"• 6 Months: {m6_1}\n"
        msg += f"• 1 Year: {y1_1}\n\n"
        
        msg += f"🔹 *{name2}*\n"
        msg += f"• Current NAV: ₹{c2}\n"
        msg += f"• 1 Month: {m1_2}\n"
        msg += f"• 3 Months: {m3_2}\n"
        msg += f"• 6 Months: {m6_2}\n"
        msg += f"• 1 Year: {y1_2}\n"
        
        send_text(phone, msg)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, msg)
            
        # Post-Comparison buttons
        buttons = [
            {"id": "mf_restart", "title": "Start Over"}
        ]
        send_buttons(phone, "What would you like to do next?", buttons)
        
    except Exception as e:
        send_text(phone, f"Failed to compare schemes: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────
#  BOT 5 — SHOPIFY BOT (trigger: "shopify")
# ─────────────────────────────────────────────────────────────────────────────
# ═════════════════════════════════════════════════════════════════════════════


# ── Shopify API Helpers ────────────────────────────────────────────────────────
def get_shopify_headers():
    # Hardcoded Shopify Credentials as requested
    token = getattr(settings, "SHOPIFY_ACCESS_TOKEN", "")
    client_id = "7829c1c932b2e6ab8d952d2c4ffa2767"
    return {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json"
    }

def get_shopify_url(endpoint: str) -> str:
    # Hardcoded domain as requested
    domain = "znyysp-kk.myshopify.com"
    version = os.getenv("SHOPIFY_API_VERSION", "2024-01")
    domain = domain.replace("https://", "").replace("http://", "")
    return f"https://{domain}/admin/api/{version}/{endpoint}"

def search_customer_by_phone(phone: str):
    url = get_shopify_url("customers/search.json")
    clean_phone = phone.replace('+', '')
    params = {"query": f"phone:*{clean_phone}*"}
    try:
        response = requests.get(url, headers=get_shopify_headers(), params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        customers = data.get("customers", [])
        if customers:
            return customers[0]
        return None
    except Exception as e:
        logger.error(f"Error searching Shopify customer: {e}")
        return None

def create_customer(phone: str, first_name: str, last_name: str = ""):
    url = get_shopify_url("customers.json")
    if not phone.startswith("+"):
        phone = f"+{phone}"
    payload = {
        "customer": {
            "first_name": first_name,
            "last_name": last_name,
            "phone": phone,
        }
    }
    try:
        response = requests.post(url, headers=get_shopify_headers(), json=payload, timeout=10)
        response.raise_for_status()
        return response.json().get("customer")
    except Exception as e:
        logger.error(f"Error creating Shopify customer: {e}")
        return None

def create_draft_order(customer_id, variant_id, quantity: int = 1):
    import logging
    logger = logging.getLogger(__name__)
    try:
        if isinstance(variant_id, str) and "gid://" in variant_id:
            variant_id = variant_id.split("/")[-1]
        if isinstance(customer_id, str) and "gid://" in customer_id:
            customer_id = customer_id.split("/")[-1]
            
        url = get_shopify_url("draft_orders.json")
        payload = {
            "draft_order": {
                "line_items": [
                    {
                        "variant_id": int(variant_id),
                        "quantity": int(quantity)
                    }
                ],
                "customer": {
                    "id": int(customer_id)
                },
                "use_customer_default_address": True
            }
        }
        
        import requests
        response = requests.post(url, headers=get_shopify_headers(), json=payload, timeout=10)
        if not response.ok:
            with open(r'c:\Users\pranj\vscode\JMS_\scratch\shopify_error.txt', 'w') as f:
                f.write(f"Status: {response.status_code}\nText: {response.text}")
            logger.error(f"Shopify Error: {response.status_code} - {response.text}")
        response.raise_for_status()
        return response.json().get("draft_order", {}).get("id")
    except Exception as e:
        with open(r'c:\Users\pranj\vscode\JMS_\scratch\shopify_error.txt', 'w') as f:
            f.write(f"Exception: {str(e)}")
        logger.error(f"Error creating Shopify order: {e}")
        return None

def get_products():
    url = get_shopify_url("products.json")
    params = {"status": "active", "limit": 10}
    try:
        response = requests.get(url, headers=get_shopify_headers(), params=params, timeout=10)
        response.raise_for_status()
        return response.json().get("products", [])
    except Exception as e:
        logger.error(f"Error fetching Shopify products: {e}")
        return []

def get_product_by_id(product_id: str):
    url = get_shopify_url(f"products/{product_id}.json")
    try:
        response = requests.get(url, headers=get_shopify_headers(), timeout=10)
        response.raise_for_status()
        return response.json().get("product")
    except Exception as e:
        logger.error(f"Error fetching product {product_id}: {e}")
        return None


    except Exception as e:
        logger.error(f"Error creating Shopify order: {e}")
        return None


def _handle_shopify_bot(phone: str, text: str, phone_number_id: str, inbound_msg_id: int = None, raw_msg: dict = None):
    text_lower = text.lower().strip()
    session = shopify_sessions.get(phone)
    
    if not session or text_lower == SHOPIFY_BOT_TRIGGER:
        session = {"stage": "idle", "phone": phone}
        shopify_sessions.set(phone, session, timeout=SHOPIFY_SESSION_TTL)

    stage = session.get("stage")
    
    if stage == "idle":
        # Check if customer exists in Shopify
        customer = search_customer_by_phone(phone)
        if customer:
            session["stage"] = "browsing_brands"
            session["shopify_customer_id"] = customer["id"]
            shopify_sessions.set(phone, session, timeout=SHOPIFY_SESSION_TTL)
            
            msg = f"Welcome back to our Shopify store, {customer.get('first_name', 'Customer')}! 👋"
            _send_shopify_brands(phone, inbound_msg_id, welcome_msg=msg)
        else:
            session["stage"] = "registration"
            shopify_sessions.set(phone, session, timeout=SHOPIFY_SESSION_TTL)
            
            msg = "Welcome to our Shopify store! 👋 It looks like you're new here.\n\nPlease reply with your *First Name* to get started:"
            send_text(phone, msg)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, msg)
        return

    if stage == "registration":
        first_name = text.strip()
        customer = create_customer(phone, first_name)
        if customer:
            session["stage"] = "browsing_brands"
            session["shopify_customer_id"] = customer["id"]
            shopify_sessions.set(phone, session, timeout=SHOPIFY_SESSION_TTL)
            
            msg = f"Thanks, {first_name}! Your account has been created. 🎉"
            _send_shopify_brands(phone, inbound_msg_id, welcome_msg=msg)
        else:
            msg = "Sorry, there was an issue creating your account. Please try again later."
            send_text(phone, msg)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, msg)
        return
        
    interactive_id = ""
    if raw_msg and raw_msg.get("type") == "interactive":
        interactive = raw_msg.get("interactive", {})
        itype = interactive.get("type", "")
        if itype == "list_reply":
            interactive_id = interactive.get("list_reply", {}).get("id", "")
        elif itype == "button_reply":
            interactive_id = interactive.get("button_reply", {}).get("id", "")

    if stage == "browsing_brands":
        if interactive_id and interactive_id.startswith("brand_"):
            brand = interactive_id.replace("brand_", "")
            session["stage"] = "browsing_products"
            session["selected_brand"] = brand
            shopify_sessions.set(phone, session, timeout=SHOPIFY_SESSION_TTL)
            _send_shopify_shoes(phone, inbound_msg_id, brand)
        return

    if stage == "browsing_products":
        if interactive_id and interactive_id.startswith("prod_"):
            prod_id = interactive_id.replace("prod_", "")
            product = get_product_by_id(prod_id)
            if product:
                variants = product.get("variants", [])
                if variants:
                    session["stage"] = "quantity_selection"
                    session["selected_product_id"] = prod_id
                    # Default to first variant
                    session["selected_variant_id"] = str(variants[0]["id"])
                    shopify_sessions.set(phone, session, timeout=SHOPIFY_SESSION_TTL)
                    
                    # Send Product Details Image first
                    image_url = ""
                    if product.get("image") and product.get("image").get("src"):
                        image_url = product["image"]["src"]
                    elif product.get("images") and len(product["images"]) > 0:
                        image_url = product["images"][0].get("src")
                        
                    desc = product.get("body_html", "")
                    if desc:
                        import re
                        desc = re.sub('<[^<]+>', '', desc).strip()
                        
                    price = variants[0].get("price", "0.00")
                    caption = f"*{product.get('title')}*\nPrice: ₹{price}\n\n{desc}"
                    
                    # Merge image and quantity buttons into one message
                    buttons = [
                        {"id": "qty_1", "title": "Qty: 1"},
                        {"id": "qty_2", "title": "Qty: 2"},
                        {"id": "qty_3", "title": "Qty: 3"}
                    ]
                    send_buttons(phone, caption, buttons, header_image_url=image_url)
                    if inbound_msg_id:
                        _save_reply(inbound_msg_id, "Sent product details and quantity buttons")
            else:
                send_text(phone, "Product not found. Please try again.")
        return
        
    if stage == "variant_selection":
        if interactive_id and interactive_id.startswith("var_"):
            var_id = interactive_id.replace("var_", "")
            session["stage"] = "quantity_selection"
            session["selected_variant_id"] = var_id
            shopify_sessions.set(phone, session, timeout=SHOPIFY_SESSION_TTL)
            
            buttons = [
                {"id": "qty_1", "title": "1"},
                {"id": "qty_2", "title": "2"},
                {"id": "qty_3", "title": "3"}
            ]
            send_buttons(phone, "Great! Please select the quantity:", buttons)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, "Requested quantity")
        return
        
    if stage == "quantity_selection":
        if interactive_id and interactive_id.startswith("qty_"):
            qty = int(interactive_id.replace("qty_", ""))
            
            session["stage"] = "order_confirmation"
            session["qty"] = qty
            shopify_sessions.set(phone, session, timeout=SHOPIFY_SESSION_TTL)
            
            prod_id = session.get("selected_product_id")
            var_id = session.get("selected_variant_id")
            product = get_product_by_id(prod_id)
            
            if product:
                variant = next((v for v in product.get("variants", []) if str(v.get("id")) == var_id), None)
                if variant:
                    price = float(variant.get("price", "0.00"))
                    total = price * qty
                    
                    summary = f"🛒 *Order Summary*\n"
                    summary += f"Product: {product.get('title')}\n"
                    summary += f"Variant: {variant.get('title')}\n"
                    summary += f"Quantity: {qty}\n"
                    summary += f"Total: ₹{total:.2f}"
                    
                    # Merge summary and buttons into one message
                    buttons = [
                        {"id": "confirm_order", "title": "Confirm Order"},
                        {"id": "cancel_order", "title": "Cancel"}
                    ]
                    send_buttons(phone, summary, buttons)
                    if inbound_msg_id:
                        _save_reply(inbound_msg_id, "Sent order summary and confirmation")
                else:
                    send_text(phone, "Error finding variant. Please try again.")
            else:
                send_text(phone, "Error finding product. Please try again.")
        return

    if stage == "order_confirmation":
        if interactive_id == "confirm_order":
            send_text(phone, "⏳ Placing your order, please wait...")
            
            customer_id = session.get("shopify_customer_id")
            variant_id = session.get("selected_variant_id")
            qty = session.get("qty", 1)
            
            order_id = create_draft_order(customer_id, variant_id, qty)
            if order_id:
                msg = f"✅ Order successfully placed!\nYour Order ID is: #{order_id}\n\nThank you for shopping with us!"
            else:
                msg = "❌ There was an issue placing your order. Please try again later."
                
            send_text(phone, msg)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, msg)
                
            shopify_sessions.delete(phone)
            
        elif interactive_id == "cancel_order":
            msg = "❌ Your order has been cancelled. Send 'shopify' anytime to start a new order."
            send_text(phone, msg)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, "Order cancelled")
            shopify_sessions.delete(phone)
            
        return

def _send_shopify_brands(phone: str, inbound_msg_id: int, welcome_msg: str = ""):
    products = get_products()
    brands = set()
    for p in products:
        vendor = p.get("vendor")
        if vendor:
            brands.add(vendor)
            
    items = []
    for b in list(brands)[:10]:
        items.append({
            "id": f"brand_{b}",
            "title": b,
            "description": "Tap to view shoes"
        })
        
    if not items:
        send_text(phone, "No brands available right now.")
        return
        
    sections = [{"title": "Select a Brand", "rows": items}]
    body = f"{welcome_msg}\n\nBrowse our latest shoes collection by Brand:" if welcome_msg else "Browse our latest shoes collection by Brand:"
    send_interactive_list(
        to=phone,
        body_text=body,
        button_text="Select Brand",
        sections=sections
    )
    if inbound_msg_id:
        _save_reply(inbound_msg_id, "Sent brands list")

def _send_shopify_shoes(phone: str, inbound_msg_id: int, brand: str):
    products = get_products()
    items = []
    for p in products:
        if p.get("vendor") != brand:
            continue
        title = p.get("title", "")[:24]
        price = p.get("variants", [{}])[0].get("price", "0.00") if p.get("variants") else "0.00"
        items.append({
            "id": f"prod_{p['id']}",
            "title": title,
            "description": f"Price: ₹{price}"
        })
    
    if not items:
        send_text(phone, f"No shoes available for {brand} right now.")
        return
        
    sections = [{"title": f"{brand} Shoes", "rows": items[:10]}]
    send_interactive_list(
        to=phone,
        body_text=f"Here are the latest shoes from *{brand}*:",
        button_text="View Shoes",
        sections=sections
    )
    if inbound_msg_id:
        _save_reply(inbound_msg_id, f"Sent {brand} shoes list")


# ═════════════════════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────
#  BOT 6 — HOTEL SERVICE BOT (trigger: "service")
# ─────────────────────────────────────────────────────────────────────────────
# ═════════════════════════════════════════════════════════════════════════════

def _send_hotel_admin_notification(customer_phone: str, customer_name: str, service_type: str, details_summary: str, description: str, phone_number_id: str = None, inbound_msg_id: int = None):
    """
    Send customer service request details to all admin numbers in HOTEL_NOTIFY_NUMBERS.
    Tries WhatsApp template first; also sends structured text.
    Records the notification in CRM under the customer's existing conversation.
    Wrapped in try-except so admin notification failures NEVER fail or disrupt customer flow.
    """
    try:
        clean_phone = (customer_phone or "").lstrip("+")
        c_name = customer_name or "Guest"
        stype = service_type or "General Service"
        sdetails = details_summary or "Standard Request"
        sdesc = (description or "").strip() or "No special notes"

        # 1. Template components
        components = [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": clean_phone},
                    {"type": "text", "text": c_name},
                    {"type": "text", "text": stype},
                    {"type": "text", "text": sdetails},
                    {"type": "text", "text": sdesc},
                ]
            }
        ]

        # 2. Formatted text message
        admin_text = (
            "🛎️ *New Hotel Service Request Received!*\n\n"
            f"📱 *Customer Phone:* +{clean_phone}\n"
            f"👤 *Customer Name:* {c_name}\n"
            f"🛎️ *Service Type:* {stype}\n"
            f"📋 *Details:* {sdetails}\n"
            f"📝 *Description / Preference:* {sdesc}\n\n"
            "⚡ Please assign a staff member promptly."
        )

        # 3. Send to all configured admin numbers (Template first; Text ONLY as fallback)
        for notify_num in HOTEL_NOTIFY_NUMBERS:
            template_sent = False
            try:
                send_template(
                    to=notify_num,
                    template_name=HOTEL_ADMIN_TEMPLATE,
                    language_code="en",
                    components=components
                )
                template_sent = True
            except Exception as e:
                logger.warning("Failed to send hotel admin template to %s: %s", notify_num, e)

            # Only send plain text if template failed, preventing duplicate messages
            if not template_sent:
                try:
                    send_text(notify_num, admin_text)
                except Exception as e:
                    logger.warning("Failed to send hotel admin fallback text to %s: %s", notify_num, e)

        # 4. Save notification in CRM database under ADMIN's conversation (not customer's chat)
        for notify_num in HOTEL_NOTIFY_NUMBERS:
            clean_notify = notify_num.lstrip("+")
            try:
                # Find or create admin Customer record
                admin_cust = Customer.objects.filter(
                    Q(phone=clean_notify) | Q(phone=f"+{clean_notify}") | Q(phone=clean_notify[-10:])
                ).first()
                if not admin_cust:
                    admin_cust = Customer.objects.create(
                        phone=f"+{clean_notify}",
                        name=f"Admin ({clean_notify[-4:]})"
                    )

                # Get or create admin conversation with system phone_number_id
                admin_conv, _ = Conversation.objects.get_or_create(
                    customer=admin_cust,
                    phone_number_id=phone_number_id
                )

                # Store as outbound template message in Admin's chat
                Message.objects.create(
                    customer=admin_cust,
                    conversation=admin_conv,
                    content=f"[Template: {HOTEL_ADMIN_TEMPLATE}]\n\n{admin_text}",
                    direction="outbound",
                    message_type="template",
                    template_name=HOTEL_ADMIN_TEMPLATE,
                    status="sent",
                    client_name="Hotel Service Bot"
                )
            except Exception as e:
                logger.warning("Could not record admin notification under admin %s: %s", notify_num, e)

    except Exception as e:
        logger.exception("Error in _send_hotel_admin_notification: %s", e)



def _get_dynamic_room_number(phone: str) -> str:
    """Generate a consistent dynamic room number for the guest based on their phone number."""
    digits = "".join(filter(str.isdigit, phone or ""))
    if len(digits) >= 3:
        # e.g., 200 + (last 2 digits % 80 + 1) -> 201 - 280
        last_digits = int(digits[-2:]) if digits[-2:] else 10
        room_num = 200 + (last_digits % 80 + 1)
    else:
        room_num = 204
    return str(room_num)


def _handle_hotel_bot(phone: str, text: str, phone_number_id: str = None, inbound_msg_id: int = None, raw_msg: dict = None, client_name: str = ""):
    """
    Enhanced state machine for Hotel Guest Service Bot.
    Triggers on 'service' or continues through interactive buttons / lists / text.
    Supports reset / back to menu at any time ('menu', 'back', 'service', 'reset').
    """
    text_clean = (text or "").strip()
    text_lower = text_clean.lower()
    session = hotel_sessions.get(phone)

    # Extract interactive ID and Title if sent via button or list reply
    interactive_id = ""
    interactive_title = ""
    if raw_msg and raw_msg.get("type") == "interactive":
        interactive = raw_msg.get("interactive", {})
        itype = interactive.get("type", "")
        if itype == "list_reply":
            interactive_id = interactive.get("list_reply", {}).get("id", "")
            interactive_title = interactive.get("list_reply", {}).get("title", "")
        elif itype == "button_reply":
            interactive_id = interactive.get("button_reply", {}).get("id", "")
            interactive_title = interactive.get("button_reply", {}).get("title", "")

    # Quick exit / cancel check
    if text_lower in ["cancel", "exit", "quit"]:
        hotel_sessions.delete(phone)
        cancel_msg = "Your request has been cancelled. Send *service* whenever you need anything! 😊"
        send_text(phone, cancel_msg)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, cancel_msg)
        return

    # Back to Main Menu check
    is_back_to_menu = (
        interactive_id in ["btn_main_menu", "btn_back_menu"] or
        text_lower in ["back to menu", "main menu", "menu", "back", HOTEL_BOT_TRIGGER, "start", "reset"]
    )

    # Trigger, reset, or Back to Menu
    if is_back_to_menu or not session:
        guest_name = client_name or "Guest"
        room_number = (session and session.get("room_number")) or _get_dynamic_room_number(phone)
        session = {
            "stage": "welcome",
            "phone": phone,
            "client_name": guest_name,
            "room_number": room_number,
            "service_type": None,
            "room_service_type": None,
            "food_venue": None,
            "meal_type": None,
            "dish_option": None,
            "restaurant_name": None,
            "pax": None,
            "timing": None,
            "description": None,
        }
        hotel_sessions.set(phone, session, timeout=HOTEL_SESSION_TTL)

        welcome_text = (
            f"🌟 *A Warm Welcome to Our Hotel, {guest_name}!* 🏨\n\n"
            f"We are delighted to have you stay with us in *Room {room_number}*.\n\n"
            "Your 24/7 Digital Concierge is here to make your stay effortless and memorable. How may we assist you today?\n\n"
            "👇 *Please select a service:*"
        )
        buttons = [
            {"id": "svc_room_service", "title": "Room Service"},
            {"id": "svc_food", "title": "Food & Dining"},
        ]
        send_buttons(phone, welcome_text, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, welcome_text)
        return

    stage = session.get("stage")
    room_number = session.get("room_number") or _get_dynamic_room_number(phone)
    session["room_number"] = room_number

    # ── Stage 1: Welcome Options ──────────────────────────────────────────────
    if stage == "welcome":
        selected = interactive_id or text_lower
        if selected in ["svc_room_service", "room service"] or "room service" in text_lower:
            session["service_type"] = "Room Service"
            session["stage"] = "room_service_categories"
            hotel_sessions.set(phone, session, timeout=HOTEL_SESSION_TTL)

            body_msg = (
                f"🛎️ *Room Service & Care (Room {room_number})*\n\n"
                "Please choose a service below:"
            )
            buttons = [
                {"id": "cat_cleaning", "title": "Room Cleaning"},
                {"id": "cat_laundry", "title": "Laundry & Iron"},
                {"id": "cat_more_services", "title": "More Services"},
            ]
            send_buttons(phone, body_msg, buttons)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, body_msg)
            return

        elif selected in ["svc_food", "food", "food & dining"] or "food" in text_lower or "dining" in text_lower:
            session["service_type"] = "Food & Dining"
            session["stage"] = "food_location"
            hotel_sessions.set(phone, session, timeout=HOTEL_SESSION_TTL)

            body_msg = (
                f"🍽️ *Food & Dining Services (Room {room_number})*\n\n"
                "Where would you prefer to enjoy your culinary experience?"
            )
            buttons = [
                {"id": "food_in_room", "title": "In Room"},
                {"id": "food_in_restaurant", "title": "In Restaurant"},
                {"id": "btn_main_menu", "title": "Back to Menu"},
            ]
            send_buttons(phone, body_msg, buttons)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, body_msg)
            return

        else:
            buttons = [
                {"id": "svc_room_service", "title": "Room Service"},
                {"id": "svc_food", "title": "Food & Dining"},
            ]
            send_buttons(phone, f"Please select an option below for Room {room_number}:", buttons)
            return

    # ── Stage 2A: Room Service Category Chosen -> Show Specific Sub-Menu ──────
    if stage == "room_service_categories":
        cat_id = interactive_id or text_clean.lower()

        # If user clicks "More Services" from the initial 2 categories -> Show remaining categories
        if cat_id in ["cat_more_services", "more services"] or "more service" in text_lower:
            body_msg = f"🛎️ *Additional Room Services (Room {room_number})*\n\nSelect an option below:"
            buttons = [
                {"id": "cat_amenities", "title": "Extra Amenities"},
                {"id": "cat_repairs", "title": "Repairs & Tech"},
                {"id": "cat_other", "title": "Other Help"},
            ]
            send_buttons(phone, body_msg, buttons)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, body_msg)
            return

        # If user picked "Other Help" directly from categories -> Ask description
        if cat_id in ["cat_other", "other", "other help"] or "other" in text_lower or "custom" in text_lower:
            session["room_service_category"] = "Room Service"
            session["room_service_item"] = "Custom Request"
            session["stage"] = "room_service_desc"
            hotel_sessions.set(phone, session, timeout=HOTEL_SESSION_TTL)

            prompt_msg = (
                f"📝 *Custom Request for Room {room_number}*\n\n"
                "Please describe what you need (e.g., *extra blankets, ice bucket, late checkout help*):"
            )
            send_text(phone, prompt_msg)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, prompt_msg)
            return

        # Category 1: Room Cleaning
        if cat_id in ["cat_cleaning", "clean"] or "clean" in text_lower:
            session["room_service_category"] = "Room Cleaning"
            session["stage"] = "room_service_submenu"
            hotel_sessions.set(phone, session, timeout=HOTEL_SESSION_TTL)

            body_msg = f"🧹 *Room Cleaning Services (Room {room_number})*\n\nSelect an option below:"
            buttons = [
                {"id": "sub_clean_full", "title": "Full Cleaning"},
                {"id": "sub_clean_bed", "title": "Bed & Linen"},
                {"id": "sub_clean_more", "title": "Other Options"},
            ]
            send_buttons(phone, body_msg, buttons)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, body_msg)
            return

        # Category 2: Laundry & Ironing
        elif cat_id in ["cat_laundry", "laundry", "iron"] or "laundry" in text_lower or "iron" in text_lower:
            session["room_service_category"] = "Laundry & Ironing"
            session["stage"] = "room_service_submenu"
            hotel_sessions.set(phone, session, timeout=HOTEL_SESSION_TTL)

            body_msg = f"🧺 *Laundry & Ironing Services (Room {room_number})*\n\nSelect an option below:"
            buttons = [
                {"id": "sub_laund_express", "title": "Express Laundry"},
                {"id": "sub_laund_iron", "title": "Steam Ironing"},
                {"id": "sub_laund_more", "title": "Other Options"},
            ]
            send_buttons(phone, body_msg, buttons)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, body_msg)
            return

        # Category 3: Extra Amenities
        elif cat_id in ["cat_amenities", "amenit", "towel"] or "amenit" in text_lower or "towel" in text_lower:
            session["room_service_category"] = "Extra Amenities"
            session["stage"] = "room_service_submenu"
            hotel_sessions.set(phone, session, timeout=HOTEL_SESSION_TTL)

            body_msg = f"🧴 *Guest Amenities (Room {room_number})*\n\nSelect an option below:"
            buttons = [
                {"id": "sub_amen_towels", "title": "Fresh Towels"},
                {"id": "sub_amen_water", "title": "Water Bottles"},
                {"id": "sub_amen_more", "title": "Other Options"},
            ]
            send_buttons(phone, body_msg, buttons)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, body_msg)
            return

        # Category 4: Repairs & Maintenance
        else:
            session["room_service_category"] = "Repairs & Tech"
            session["stage"] = "room_service_submenu"
            hotel_sessions.set(phone, session, timeout=HOTEL_SESSION_TTL)

            body_msg = f"🔧 *Repairs & Assistance (Room {room_number})*\n\nSelect an option below:"
            buttons = [
                {"id": "sub_rep_ac", "title": "AC Assistance"},
                {"id": "sub_rep_tv", "title": "TV / WiFi Help"},
                {"id": "sub_rep_more", "title": "Other Options"},
            ]
            send_buttons(phone, body_msg, buttons)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, body_msg)
            return

    # ── Stage 2B: Room Service Sub-menu Item Chosen ───────────────────────────
    if stage == "room_service_submenu":
        chosen_id = interactive_id or text_clean.lower()
        category = session.get("room_service_category", "Room Service")

        # More / Other button clicked in sub-menu -> Reveal the rest of the options
        if chosen_id in ["sub_clean_more", "sub_laund_more", "sub_amen_more", "sub_rep_more"]:
            if category == "Room Cleaning":
                body_msg = f"🧹 *Room Cleaning - More Options (Room {room_number})*:"
                buttons = [
                    {"id": "sub_clean_trash", "title": "Trash & Dusting"},
                    {"id": "sub_rs_other", "title": "Other Request"},
                    {"id": "btn_main_menu", "title": "Back to Menu"},
                ]
            elif category == "Laundry & Ironing":
                body_msg = f"🧺 *Laundry - More Options (Room {room_number})*:"
                buttons = [
                    {"id": "sub_laund_dry", "title": "Dry Cleaning"},
                    {"id": "sub_rs_other", "title": "Other Request"},
                    {"id": "btn_main_menu", "title": "Back to Menu"},
                ]
            elif category == "Extra Amenities":
                body_msg = f"🧴 *Amenities - More Options (Room {room_number})*:"
                buttons = [
                    {"id": "sub_amen_dental", "title": "Dental/Shave Kit"},
                    {"id": "sub_rs_other", "title": "Other Request"},
                    {"id": "btn_main_menu", "title": "Back to Menu"},
                ]
            else: # Repairs & Tech
                body_msg = f"🔧 *Repairs - More Options (Room {room_number})*:"
                buttons = [
                    {"id": "sub_rep_plumbing", "title": "Plumbing/Geyser"},
                    {"id": "sub_rs_other", "title": "Other Request"},
                    {"id": "btn_main_menu", "title": "Back to Menu"},
                ]
            send_buttons(phone, body_msg, buttons)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, body_msg)
            return

        # If user selected ANY "Other" option in the sub-menu -> Ask description
        if chosen_id == "sub_rs_other" or "other" in text_lower or "custom" in text_lower:
            session["room_service_item"] = f"{category} - Custom Request"
            session["stage"] = "room_service_desc"
            hotel_sessions.set(phone, session, timeout=HOTEL_SESSION_TTL)

            prompt_msg = (
                f"📝 *{category} - Custom Instructions (Room {room_number})*\n\n"
                "Please describe your specific requirement:"
            )
            send_text(phone, prompt_msg)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, prompt_msg)
            return

        # Standard option selected from sub-menu -> Confirm directly without asking description!
        item_title_map = {
            "sub_clean_full": "Full Room Cleaning",
            "sub_clean_bed": "Bed & Linen Refresh",
            "sub_clean_trash": "Trash & Dusting Only",
            "sub_laund_express": "Express Laundry & Wash",
            "sub_laund_iron": "Steam Ironing / Press",
            "sub_laund_dry": "Dry Cleaning Service",
            "sub_amen_towels": "Fresh Towels Set",
            "sub_amen_water": "Packaged Water Bottles",
            "sub_amen_dental": "Dental & Shaving Kit",
            "sub_rep_ac": "AC Cooling / Remote Help",
            "sub_rep_tv": "TV / WiFi Tech Assist",
            "sub_rep_plumbing": "Geyser & Plumbing",
        }
        item_name = item_title_map.get(chosen_id) or interactive_title or text_clean
        session["room_service_item"] = item_name
        session["description"] = f"Standard request for {item_name}"

        stype = category
        details = f"{item_name} | Room: {room_number}"

        # Notify Admin & Record in CRM
        _send_hotel_admin_notification(
            customer_phone=phone,
            customer_name=session.get("client_name") or client_name,
            service_type=stype,
            details_summary=details,
            description=session["description"],
            phone_number_id=phone_number_id,
            inbound_msg_id=inbound_msg_id
        )

        thank_you_msg = (
            f"✅ *Thank You! Your Request Has Been Logged.* 🌟\n\n"
            f"🔑 *Room:* Room {room_number}\n"
            f"🛎️ *Service Requested:* {item_name}\n\n"
            "Our staff has been notified and dispatched to your room. "
            "Send *service* anytime if you need further assistance!"
        )
        send_text(phone, thank_you_msg)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, thank_you_msg)

        hotel_sessions.delete(phone)
        return

    # ── Stage 2C: Room Service Description (Only reached when 'Other' chosen) ──
    if stage == "room_service_desc":
        description = text_clean
        session["description"] = description
        category = session.get("room_service_category", "Room Service")

        stype = category
        details = f"Custom Request | Room: {room_number}"

        # Notify Admin & Record in CRM
        _send_hotel_admin_notification(
            customer_phone=phone,
            customer_name=session.get("client_name") or client_name,
            service_type=stype,
            details_summary=details,
            description=description,
            phone_number_id=phone_number_id,
            inbound_msg_id=inbound_msg_id
        )

        thank_you_msg = (
            f"✅ *Thank You! Your Request Has Been Logged.* 🌟\n\n"
            f"🔑 *Room:* Room {room_number}\n"
            f"📋 *Service:* {category}\n"
            f"📝 *Instructions:* {description}\n\n"
            "Our team has received your instructions and will attend to your room shortly. "
            "Send *service* anytime if you need further assistance!"
        )
        send_text(phone, thank_you_msg)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, thank_you_msg)

        hotel_sessions.delete(phone)
        return

    # ── Stage 3: Food Venue (In Room vs In Restaurant) ─────────────────────────
    if stage == "food_location":
        selected = interactive_id or text_lower
        if selected in ["food_in_room", "in room"] or "room" in text_lower:
            session["food_venue"] = "In Room"
            session["stage"] = "room_meal_type"
            hotel_sessions.set(phone, session, timeout=HOTEL_SESSION_TTL)

            body_msg = f"🍳 What meal would you like delivered to Room {room_number}?"
            buttons = [
                {"id": "meal_breakfast", "title": "Breakfast"},
                {"id": "meal_lunch", "title": "Lunch"},
                {"id": "meal_dinner", "title": "Dinner"},
            ]
            send_buttons(phone, body_msg, buttons)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, body_msg)
            return

        elif selected in ["food_in_restaurant", "in restaurant"] or "restaurant" in text_lower:
            session["food_venue"] = "In Restaurant"
            session["stage"] = "restaurant_selection"
            hotel_sessions.set(phone, session, timeout=HOTEL_SESSION_TTL)

            body_msg = (
                f"🍽️ *Choose Your Dining Venue (Room {room_number})*\n\n"
                "Please select from our featured hotel restaurants:"
            )
            buttons = [
                {"id": "rest_royal_dine", "title": "The Royal Spice"},
                {"id": "rest_sky_lounge", "title": "Skyview Rooftop"},
                {"id": "rest_more", "title": "More Venues"},
            ]
            send_buttons(phone, body_msg, buttons)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, body_msg)
            return

        else:
            buttons = [
                {"id": "food_in_room", "title": "In Room"},
                {"id": "food_in_restaurant", "title": "In Restaurant"},
                {"id": "btn_main_menu", "title": "Back to Menu"},
            ]
            send_buttons(phone, "Please select where you would like your food:", buttons)
            return

    # ── Stage 4A (Room Food): Meal Type (Breakfast, Lunch, Dinner) ────────────
    if stage == "room_meal_type":
        meal_map = {
            "meal_breakfast": "Breakfast",
            "meal_lunch": "Lunch",
            "meal_dinner": "Dinner",
        }
        chosen_meal = meal_map.get(interactive_id) or interactive_title or text_clean.capitalize()
        session["meal_type"] = chosen_meal
        session["stage"] = "room_dish_options"
        hotel_sessions.set(phone, session, timeout=HOTEL_SESSION_TTL)

        if "Breakfast" in chosen_meal:
            buttons = [
                {"id": "dish_continental", "title": "Continental Platter"},
                {"id": "dish_indian_bf", "title": "Indian Thali"},
                {"id": "dish_more_bf", "title": "Other Options"},
            ]
        elif "Lunch" in chosen_meal:
            buttons = [
                {"id": "dish_deluxe_thali", "title": "Deluxe Thali"},
                {"id": "dish_pasta_pizza", "title": "Pasta & Pizza"},
                {"id": "dish_more_lunch", "title": "Other Options"},
            ]
        else: # Dinner
            buttons = [
                {"id": "dish_chef_special", "title": "Chef Special"},
                {"id": "dish_sizzler", "title": "Sizzler Platter"},
                {"id": "dish_more_dinner", "title": "Other Options"},
            ]

        body_msg = (
            f"🍴 *{chosen_meal} Menu (Room {room_number})*\n\n"
            "Please select a dish below:"
        )
        send_buttons(phone, body_msg, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body_msg)
        return

    # ── Stage 4B (Room Food): Dish Selected from Sub-Menu ─────────────────────
    if stage == "room_dish_options":
        dish_id = interactive_id or text_clean.lower()
        chosen_meal_val = session.get("meal_type", "Meal")

        # More / Other clicked from initial 2 dish buttons -> Show remaining options
        if dish_id in ["dish_more_bf", "dish_more_lunch", "dish_more_dinner"]:
            if "Breakfast" in chosen_meal_val:
                body_msg = f"🍴 *{chosen_meal_val} - More Options (Room {room_number})*:"
                buttons = [
                    {"id": "dish_healthy_bf", "title": "Wellness Bowl"},
                    {"id": "dish_other_food", "title": "Custom Dish"},
                    {"id": "btn_main_menu", "title": "Back to Menu"},
                ]
            elif "Lunch" in chosen_meal_val:
                body_msg = f"🍴 *{chosen_meal_val} - More Options (Room {room_number})*:"
                buttons = [
                    {"id": "dish_biryani", "title": "Dum Biryani"},
                    {"id": "dish_other_food", "title": "Custom Dish"},
                    {"id": "btn_main_menu", "title": "Back to Menu"},
                ]
            else: # Dinner
                body_msg = f"🍴 *{chosen_meal_val} - More Options (Room {room_number})*:"
                buttons = [
                    {"id": "dish_light_meal", "title": "Light Meal"},
                    {"id": "dish_other_food", "title": "Custom Dish"},
                    {"id": "btn_main_menu", "title": "Back to Menu"},
                ]
            send_buttons(phone, body_msg, buttons)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, body_msg)
            return

        is_other_dish = (
            dish_id in ["dish_other_bf", "dish_other_lunch", "dish_other_dinner", "dish_other_food"] or
            "other" in text_lower or "custom" in text_lower
        )

        # If user picked "Custom Dish / Other" -> Ask description
        if is_other_dish:
            session["dish_option"] = "Custom Dish / Special Order"
            session["stage"] = "room_food_desc"
            hotel_sessions.set(phone, session, timeout=HOTEL_SESSION_TTL)

            prompt_msg = (
                f"🍲 *Custom Food Order for Room {room_number}*\n\n"
                "Please describe the food items and preparation preferences you'd like to order "
                "(e.g., *2 Masala Dosas, Jain preparation, less oil, 1 hot filter coffee*):"
            )
            send_text(phone, prompt_msg)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, prompt_msg)
            return

        # Direct dish selection from buttons -> Confirm directly without asking description!
        dish_title_map = {
            "dish_continental": "Continental Platter",
            "dish_indian_bf": "Indian Classic Thali",
            "dish_healthy_bf": "Healthy Wellness Bowl",
            "dish_deluxe_thali": "Deluxe Indian Thali",
            "dish_pasta_pizza": "Italian Delight Combo",
            "dish_biryani": "Dum Biryani Special",
            "dish_chef_special": "Chef Special Dinner",
            "dish_sizzler": "Sizzler Platter",
            "dish_light_meal": "Light Comfort Meal",
        }
        dish = dish_title_map.get(dish_id) or interactive_title or text_clean

        # Direct dish selection from buttons -> Confirm directly without asking description!
        chosen_meal_val = session.get("meal_type", "Meal")
        session["dish_option"] = dish
        session["description"] = f"Standard {chosen_meal_val} delivery: {dish}"

        stype = "Food Delivery"
        details = f"{dish} | Room: {room_number}"

        # Notify Admin & Record in CRM
        _send_hotel_admin_notification(
            customer_phone=phone,
            customer_name=session.get("client_name") or client_name,
            service_type=stype,
            details_summary=details,
            description=session["description"],
            phone_number_id=phone_number_id,
            inbound_msg_id=inbound_msg_id
        )

        thank_you_msg = (
            f"✅ *Thank You! Your In-Room Food Order is Placed.* 🍽️\n\n"
            f"🔑 *Delivery To:* Room {room_number}\n"
            f"🍲 *Meal:* {chosen_meal_val}\n"
            f"🍛 *Selected Dish:* {dish}\n\n"
            "Our master chefs are preparing your food. It will be delivered hot to your room shortly. Bon appétit! ✨\n"
            "Send *service* anytime if you need anything else."
        )
        send_text(phone, thank_you_msg)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, thank_you_msg)

        hotel_sessions.delete(phone)
        return

    # ── Stage 4C (Room Food): Receive Custom Description (Only for Other) ─────
    if stage == "room_food_desc":
        description = text_clean
        session["description"] = description
        chosen_meal_val = session.get("meal_type", "Meal")

        stype = "Food Delivery"
        details = f"Custom Food Order | Room: {room_number}"

        # Notify Admin & Record in CRM
        _send_hotel_admin_notification(
            customer_phone=phone,
            customer_name=session.get("client_name") or client_name,
            service_type=stype,
            details_summary=details,
            description=description,
            phone_number_id=phone_number_id,
            inbound_msg_id=inbound_msg_id
        )

        thank_you_msg = (
            f"✅ *Thank You! Your In-Room Food Order is Placed.* 🍽️\n\n"
            f"🔑 *Delivery To:* Room {room_number}\n"
            f"🍲 *Meal:* {chosen_meal_val}\n"
            f"📝 *Order Details:* {description}\n\n"
            "Our master chefs are preparing your order according to your instructions. It will be served hot to your room shortly. Bon appétit! ✨\n"
            "Send *service* anytime if you need anything else."
        )
        send_text(phone, thank_you_msg)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, thank_you_msg)

        hotel_sessions.delete(phone)
        return

    # ── Stage 5A (Restaurant): Restaurant Selected -> Ask Meal Type ───────────
    if stage == "restaurant_selection":
        chosen_id = interactive_id or text_clean.lower()

        # More Venues clicked -> show remaining options
        if chosen_id in ["rest_more", "more venues"] or "more" in text_lower:
            body_msg = (
                f"🍽️ *More Dining Venues (Room {room_number})*\n\n"
                "Please choose from our other restaurant & café venues:"
            )
            buttons = [
                {"id": "rest_bistro_cafe", "title": "Garden Bistro Café"},
                {"id": "rest_other", "title": "Other Venue"},
                {"id": "btn_main_menu", "title": "Back to Menu"},
            ]
            send_buttons(phone, body_msg, buttons)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, body_msg)
            return

        rest_map = {
            "rest_royal_dine": "The Royal Spice",
            "rest_sky_lounge": "Skyview Rooftop",
            "rest_bistro_cafe": "Garden Bistro Café",
            "rest_other": "Special Hotel Venue",
        }
        rest_name = rest_map.get(chosen_id) or interactive_title
        if not rest_name:
            if "royal" in text_lower or "spice" in text_lower or text_clean == "1":
                rest_name = "The Royal Spice"
            elif "sky" in text_lower or "rooftop" in text_lower or text_clean == "2":
                rest_name = "Skyview Rooftop"
            elif "bistro" in text_lower or "café" in text_lower or "cafe" in text_lower or text_clean == "3":
                rest_name = "Garden Bistro Café"
            else:
                rest_name = text_clean

        session["restaurant_name"] = rest_name
        session["stage"] = "rest_meal_type"
        hotel_sessions.set(phone, session, timeout=HOTEL_SESSION_TTL)

        body_msg = f"🍽️ *{rest_name}*\n\nPlease select the meal session for your table reservation:"
        buttons = [
            {"id": "rest_meal_breakfast", "title": "Breakfast"},
            {"id": "rest_meal_lunch", "title": "Lunch"},
            {"id": "rest_meal_dinner", "title": "Dinner"},
        ]
        send_buttons(phone, body_msg, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body_msg)
        return

    # ── Stage 5B (Restaurant): Meal Selected -> Ask Number of Persons ─────────
    if stage == "rest_meal_type":
        meal_map = {
            "rest_meal_breakfast": "Breakfast",
            "rest_meal_lunch": "Lunch",
            "rest_meal_dinner": "Dinner",
        }
        chosen_id = interactive_id or text_lower
        chosen_meal = meal_map.get(chosen_id) or interactive_title
        if not chosen_meal:
            if "break" in text_lower or text_clean == "1":
                chosen_meal = "Breakfast"
            elif "lunch" in text_lower or text_clean == "2":
                chosen_meal = "Lunch"
            elif "din" in text_lower or text_clean == "3":
                chosen_meal = "Dinner"
            else:
                chosen_meal = text_clean.capitalize()

        session["meal_type"] = chosen_meal
        session["stage"] = "rest_pax"
        hotel_sessions.set(phone, session, timeout=HOTEL_SESSION_TTL)

        body_msg = (
            f"Table reservation for *{chosen_meal}* at *{session.get('restaurant_name')}*.\n\n"
            "👥 How many guests will be joining?"
        )
        buttons = [
            {"id": "pax_2", "title": "1 - 2 Guests"},
            {"id": "pax_4", "title": "3 - 4 Guests"},
            {"id": "pax_more", "title": "5+ Guests"},
        ]
        send_buttons(phone, body_msg, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body_msg)
        return

    # ── Stage 5C (Restaurant): Pax Selected -> Ask Timing ─────────────────────
    if stage == "rest_pax":
        pax_map = {
            "pax_2": "1 - 2 Guests",
            "pax_4": "3 - 4 Guests",
            "pax_more": "5+ Guests",
        }
        chosen_id = interactive_id or text_clean.lower()
        pax = pax_map.get(chosen_id) or interactive_title or text_clean
        session["pax"] = pax
        session["stage"] = "rest_timing"
        hotel_sessions.set(phone, session, timeout=HOTEL_SESSION_TTL)

        prompt_msg = (
            f"Table for *{pax}* at *{session.get('restaurant_name')}*.\n\n"
            "⏰ Please reply with your preferred dining time (e.g., *1:30 PM, 8:00 PM, or Now*):"
        )
        send_text(phone, prompt_msg)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, prompt_msg)
        return

    # ── Stage 5D (Restaurant): Timing Entered -> Ask Description / Special Request
    if stage == "rest_timing":
        timing = text_clean
        session["timing"] = timing
        session["stage"] = "rest_desc"
        hotel_sessions.set(phone, session, timeout=HOTEL_SESSION_TTL)

        prompt_msg = (
            f"Timing noted: *{timing}*.\n\n"
            "📝 Any special requests? (e.g., *window seat, anniversary decor, baby chair, or type 'None'*):"
        )
        send_text(phone, prompt_msg)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, prompt_msg)
        return

    # ── Stage 5E (Restaurant): Description Entered -> Send Thank You & Notify Admin
    if stage == "rest_desc":
        description = text_clean
        session["description"] = description

        stype = "Restaurant Reservation"
        details = (
            f"Room: {room_number} | "
            f"Restaurant: {session.get('restaurant_name', 'N/A')} | "
            f"Meal: {session.get('meal_type', 'N/A')} | "
            f"Guests: {session.get('pax', 'N/A')} | "
            f"Time: {session.get('timing', 'N/A')}"
        )

        # Notify Admin & Record in CRM
        _send_hotel_admin_notification(
            customer_phone=phone,
            customer_name=session.get("client_name") or client_name,
            service_type=stype,
            details_summary=details,
            description=description,
            phone_number_id=phone_number_id,
            inbound_msg_id=inbound_msg_id
        )

        thank_you_msg = (
            "✅ *Thank You! Your Table Reservation is Confirmed.* 🥂\n\n"
            f"🔑 *Guest Room:* Room {room_number}\n"
            f"🏛️ *Restaurant:* {session.get('restaurant_name')}\n"
            f"🍽️ *Meal:* {session.get('meal_type')}\n"
            f"👥 *Guests:* {session.get('pax')}\n"
            f"⏰ *Time:* {session.get('timing')}\n"
            f"📝 *Special Requests:* {description}\n\n"
            "Our restaurant captain has reserved your table and looks forward to hosting you! "
            "Send *service* anytime if you need anything else."
        )
        send_text(phone, thank_you_msg)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, thank_you_msg)

        hotel_sessions.delete(phone)
        return

    # Fallback if stage is unknown or stale
    fb_msg = f"How may we assist you with our guest services for Room {room_number}?"
    buttons = [
        {"id": "svc_room_service", "title": "Room Service"},
        {"id": "svc_food", "title": "Food & Dining"},
    ]
    send_buttons(phone, fb_msg, buttons)
    if inbound_msg_id:
        _save_reply(inbound_msg_id, fb_msg)
    return


# ═════════════════════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────
#  BOT: MODY TOYOTA BOT (trigger: "mody")
#  Features:
#    - Left Branch: Buy New Car (Model -> Action -> Details -> Extra -> Conf)
#    - Right Branch: Service (Model/Reg -> Booking Type/Date/Slot -> Pickup -> Conf)
#    - Shared "Anything else?" step at end
#    - Back button & Main Menu button on every step
#    - WhatsApp reply buttons with "Other / More" pagination (no meta template/list)
#    - Mobile & Reg number validation with re-prompting
# ─────────────────────────────────────────────────────────────────────────────
# ═════════════════════════════════════════════════════════════════════════════

def _is_valid_indian_mobile(text: str) -> bool:
    """Validate 10-digit mobile number, optionally prefixed by +91 or 91 or 0."""
    cleaned = re.sub(r"[\s\-\+]", "", text.strip())
    if cleaned.startswith("91") and len(cleaned) == 12:
        cleaned = cleaned[2:]
    elif cleaned.startswith("0") and len(cleaned) == 11:
        cleaned = cleaned[1:]
    return bool(re.match(r"^[6-9]\d{9}$", cleaned))


def _clean_indian_mobile(text: str) -> str:
    cleaned = re.sub(r"[\s\-\+]", "", text.strip())
    if cleaned.startswith("91") and len(cleaned) == 12:
        return cleaned[2:]
    if cleaned.startswith("0") and len(cleaned) == 11:
        return cleaned[1:]
    return cleaned


def _is_valid_vehicle_reg(text: str) -> bool:
    """
    Validate vehicle registration number (e.g. GJ01AB1234, MH02C5678, DL3CAB1234, etc.)
    Allows spaces / hyphens. Minimum 6 alphanumeric characters.
    """
    cleaned = re.sub(r"[\s\-]", "", text.strip().upper())
    # Format: 2 state letters + 1-2 district digits + 0-3 series letters + 1-4 digits
    # Or Bharat series (e.g. 22BH1234AA)
    pattern = r"^[A-Z]{2}[0-9]{1,2}[A-Z]{0,3}[0-9]{1,4}$|^[0-9]{2}BH[0-9]{4}[A-Z]{1,2}$"
    return bool(re.match(pattern, cleaned)) and len(cleaned) >= 6


def _mody_send_welcome(phone: str, inbound_msg_id: int = None, client_name: str = ""):
    cust_name = client_name or "Valued Customer"
    session = {
        "stage": "welcome",
        "phone": phone,
        "client_name": cust_name,
        "history": [],
    }
    mody_sessions.set(phone, session, timeout=MODY_SESSION_TTL)

    welcome_text = (
        f"🚘 *Welcome to Mody Toyota!* 🌟\n\n"
        f"Hello {cust_name}, thank you for reaching out to us.\n\n"
        "Your trusted partner for Toyota sales, certified maintenance, and reliable customer care. "
        "We are committed to delivering Quality, Durability & Peace of Mind for every journey.\n\n"
        "How can we assist you today? Please choose an option below:"
    )
    buttons = [
        {"id": "mody_buy_new_car", "title": "Buy New Car"},
        {"id": "mody_service", "title": "Toyota Service"},
    ]
    send_buttons(phone, welcome_text, buttons)
    if inbound_msg_id:
        _save_reply(inbound_msg_id, welcome_text)


def _handle_mody_bot(phone: str, text: str, phone_number_id: str = None, inbound_msg_id: int = None, raw_msg: dict = None, client_name: str = ""):
    """
    State machine for Mody Toyota WhatsApp Bot.
    Triggers on 'mody' or continues through session stages.
    """
    text_clean = (text or "").strip()
    text_lower = text_clean.lower()
    session = mody_sessions.get(phone)

    # Extract interactive ID and Title if sent via button reply
    interactive_id = ""
    interactive_title = ""
    if raw_msg and raw_msg.get("type") == "interactive":
        interactive = raw_msg.get("interactive", {})
        itype = interactive.get("type", "")
        if itype == "button_reply":
            interactive_id = interactive.get("button_reply", {}).get("id", "")
            interactive_title = interactive.get("button_reply", {}).get("title", "")
        elif itype == "list_reply":
            interactive_id = interactive.get("list_reply", {}).get("id", "")
            interactive_title = interactive.get("list_reply", {}).get("title", "")

    # Quick exit / cancel check
    if text_lower in ["cancel", "exit", "quit"]:
        mody_sessions.delete(phone)
        bye_msg = "Thank you for contacting Mody Toyota! Send *mody* anytime to start again. Have a great day! 🚗✨"
        send_text(phone, bye_msg)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, bye_msg)
        return

    # Trigger or explicit Main Menu check
    is_main_menu = (
        interactive_id == "btn_mody_main_menu" or
        text_lower in ["main menu", "menu", "mody", "start", "restart"]
    )
    if is_main_menu or not session:
        _mody_send_welcome(phone, inbound_msg_id=inbound_msg_id, client_name=client_name)
        return

    # Handle universal BACK button navigation
    if interactive_id == "btn_mody_back" or text_lower == "back":
        history = session.get("history", [])
        if not history:
            _mody_send_welcome(phone, inbound_msg_id=inbound_msg_id, client_name=client_name)
            return
        previous_stage = history.pop()
        session["stage"] = previous_stage
        session["history"] = history
        mody_sessions.set(phone, session, timeout=MODY_SESSION_TTL)

        # Re-render previous stage
        _mody_render_stage(phone, session, inbound_msg_id)
        return

    stage = session.get("stage", "welcome")

    def _push_stage_and_set(new_stage: str):
        hist = session.get("history", [])
        if stage and stage not in ["welcome", new_stage]:
            hist.append(stage)
        session["history"] = hist
        session["stage"] = new_stage
        mody_sessions.set(phone, session, timeout=MODY_SESSION_TTL)

    # ═════════════════════════════════════════════════════════════════════════
    # STAGE: WELCOME -> Select "Buy New Car" or "Service"
    # ═════════════════════════════════════════════════════════════════════════
    if stage == "welcome":
        selected = interactive_id or text_lower
        if selected in ["mody_buy_new_car", "buy new car"] or "buy" in text_lower or "new car" in text_lower:
            _push_stage_and_set("buy_select_model_1")
            _mody_render_stage(phone, session, inbound_msg_id)
            return

        elif selected in ["mody_service", "toyota service", "car service"] or "toyota service" in text_lower or "car service" in text_lower:
            _push_stage_and_set("srv_select_action")
            _mody_render_stage(phone, session, inbound_msg_id)
            return

        else:
            _mody_send_welcome(phone, inbound_msg_id=inbound_msg_id, client_name=client_name)
            return

    # ═════════════════════════════════════════════════════════════════════════
    # [TEAL FLOW] BUY NEW CAR
    # ═════════════════════════════════════════════════════════════════════════

    # --- Select Car Model (Pages 1 to 5) ---
    if stage in ["buy_select_model_1", "buy_select_model_2", "buy_select_model_3", "buy_select_model_4", "buy_select_model_5"]:
        selected = interactive_id or text_clean

        # Handle pagination between model screens (Page 1 -> 2 -> 3 -> 4 -> 5)
        if interactive_id == "mody_car_other_1" or (stage == "buy_select_model_1" and ("other" in text_lower or "more" in text_lower)):
            session["stage"] = "buy_select_model_2"
            mody_sessions.set(phone, session, timeout=MODY_SESSION_TTL)
            _mody_render_stage(phone, session, inbound_msg_id)
            return

        if interactive_id == "mody_car_other_2" or (stage == "buy_select_model_2" and ("other" in text_lower or "more" in text_lower)):
            session["stage"] = "buy_select_model_3"
            mody_sessions.set(phone, session, timeout=MODY_SESSION_TTL)
            _mody_render_stage(phone, session, inbound_msg_id)
            return

        if interactive_id == "mody_car_other_3" or (stage == "buy_select_model_3" and ("other" in text_lower or "more" in text_lower)):
            session["stage"] = "buy_select_model_4"
            mody_sessions.set(phone, session, timeout=MODY_SESSION_TTL)
            _mody_render_stage(phone, session, inbound_msg_id)
            return

        if interactive_id == "mody_car_other_4" or (stage == "buy_select_model_4" and ("other" in text_lower or "more" in text_lower)):
            session["stage"] = "buy_select_model_5"
            mody_sessions.set(phone, session, timeout=MODY_SESSION_TTL)
            _mody_render_stage(phone, session, inbound_msg_id)
            return

        # Model mapping (Toyota official lineup from Mody Autonation)
        car_map = {
            "car_glanza": "Glanza",
            "car_taisor": "Urban Cruiser Taisor",
            "car_hyryder": "Urban Cruiser Hyryder",
            "car_rumion": "Rumion",
            "car_hycross": "Innova Hycross",
            "car_crysta": "Innova Crysta",
            "car_fortuner": "Fortuner",
            "car_legender": "Fortuner Legender",
            "car_hilux": "Hilux",
            "car_camry": "New Camry Hybrid",
            "car_vellfire": "Vellfire",
            "car_lc300": "Land Cruiser 300",
        }
        chosen_car = car_map.get(selected) or interactive_title or text_clean
        session["car_model"] = chosen_car
        _push_stage_and_set("buy_choose_action")
        _mody_render_stage(phone, session, inbound_msg_id)
        return

    # --- Choose Action: Test drive, quote, callback ---
    if stage == "buy_choose_action":
        action_map = {
            "act_test_drive": "Book Test Drive",
            "act_quote": "Get Price Quote",
            "act_callback": "Request Callback",
        }
        chosen_action = action_map.get(interactive_id) or interactive_title or text_clean
        session["buy_action"] = chosen_action

        if interactive_id == "act_test_drive" or "test drive" in chosen_action.lower():
            _push_stage_and_set("buy_td_date")
            _mody_render_stage(phone, session, inbound_msg_id)
            return
        else:
            _push_stage_and_set("buy_collect_name")
            _mody_render_stage(phone, session, inbound_msg_id)
            return

    # --- Test Drive: Preferred Date ---
    if stage == "buy_td_date":
        d1 = datetime.now().strftime("%d-%b (%a)")
        d2 = (datetime.now() + timedelta(days=1)).strftime("%d-%b (%a)")
        td_date_map = {
            "td_date_today": f"Today ({d1})",
            "td_date_tomorrow": f"Tomorrow ({d2})",
            "td_date_other": "Other Date / Weekend",
        }
        chosen_td_date = td_date_map.get(interactive_id) or interactive_title or text_clean
        session["td_date"] = chosen_td_date
        _push_stage_and_set("buy_td_time")
        _mody_render_stage(phone, session, inbound_msg_id)
        return

    # --- Test Drive: Preferred Time Slot ---
    if stage == "buy_td_time":
        td_time_map = {
            "td_time_morning": "Morning (10:00 AM – 01:00 PM)",
            "td_time_afternoon": "Afternoon (01:00 PM – 04:00 PM)",
            "td_time_evening": "Evening (04:00 PM – 07:00 PM)",
        }
        chosen_td_time = td_time_map.get(interactive_id) or interactive_title or text_clean
        session["td_time"] = chosen_td_time
        _push_stage_and_set("buy_collect_name")
        _mody_render_stage(phone, session, inbound_msg_id)
        return

    # --- Collect Details: Full Name (Mobile automatically taken from WhatsApp session) ---
    if stage == "buy_collect_name":
        entered_name = text_clean
        if len(entered_name) < 2 or re.match(r"^\d+$", entered_name):
            err_msg = "Please enter your *Full Name*:"
            send_text(phone, err_msg)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, err_msg)
            return
        session["cust_name"] = entered_name
        session["cust_mobile"] = phone.lstrip("+")
        _push_stage_and_set("buy_finance_exchange")
        _mody_render_stage(phone, session, inbound_msg_id)
        return

    # --- Quick Preferences: Finance / Exchange / Direct Confirm ---
    if stage == "buy_finance_exchange":
        pref_id = interactive_id or text_lower
        if pref_id in ["pref_finance", "finance"]:
            session["finance_pref"] = "Yes, Need Finance"
            session["car_exchange"] = "No Exchange"
        elif pref_id in ["pref_exchange", "exchange"]:
            session["finance_pref"] = "Self-Funded"
            session["car_exchange"] = "Yes, Car Exchange"
        else: # Direct quote / standard inquiry
            session["finance_pref"] = "Standard / Discuss on Call"
            session["car_exchange"] = "Not Required"

        # Confirmation & Lead submission (Fast 3-step completion!)
        _mody_confirm_buy_lead(phone, session, inbound_msg_id)
        _push_stage_and_set("anything_else")
        _mody_render_stage(phone, session, inbound_msg_id)
        return

    # ═════════════════════════════════════════════════════════════════════════
    # [CORAL FLOW] SERVICE FLOW
    # ═════════════════════════════════════════════════════════════════════════

    # --- Service action: Book, Status, Advisor ---
    if stage == "srv_select_action":
        srv_action_map = {
            "srv_book": "Book Service",
            "srv_status": "Service Status",
            "srv_advisor": "Connect with Advisor",
        }
        chosen_action = srv_action_map.get(interactive_id) or interactive_title or text_clean
        session["service_action"] = chosen_action

        if interactive_id == "srv_status":
            _push_stage_and_set("srv_status_check")
            _mody_render_stage(phone, session, inbound_msg_id)
            return
        elif interactive_id == "srv_advisor":
            _push_stage_and_set("srv_advisor_connect")
            _mody_render_stage(phone, session, inbound_msg_id)
            return
        else:
            # Default: Book service -> Collect vehicle model & reg number
            _push_stage_and_set("srv_vehicle_model")
            _mody_render_stage(phone, session, inbound_msg_id)
            return

    # --- Service Status inquiry: Enter Reg Number ---
    if stage == "srv_status_check":
        if not _is_valid_vehicle_reg(text_clean):
            err_msg = (
                "*Invalid Vehicle Registration Number.*\n\n"
                "Please enter a valid registration number (e.g. *GJ01AB1234* or *MH02CD5678*):"
            )
            buttons = [
                {"id": "btn_mody_main_menu", "title": "Main Menu"},
            ]
            send_buttons(phone, err_msg, buttons)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, err_msg)
            return
        reg_no = text_clean.upper()
        status_msg = (
            f"*Service Status for {reg_no}*\n\n"
            "Your vehicle is currently in *Periodic Inspection & Maintenance* stage.\n"
            "Estimated completion: *Today by 5:30 PM*.\n\n"
            "Our Service Advisor will reach out once your car is ready for delivery."
        )
        send_text(phone, status_msg)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, status_msg)
        _push_stage_and_set("anything_else")
        _mody_render_stage(phone, session, inbound_msg_id)
        return

    # --- Advisor Connect / Complaint Registration ---
    if stage == "srv_advisor_connect":
        session["advisor_note"] = text_clean
        adv_msg = (
            "*Service Request & Complaint Logged*\n\n"
            f"• *Concern / Requirement:* {text_clean}\n"
            f"• *Contact:* +{phone.lstrip('+')}\n"
            f"• *Status:* Assigned to Service Advisor\n\n"
            "Our dedicated Mody Toyota Service Advisor has been notified and will call you within 15–30 minutes to assist."
        )
        send_text(phone, adv_msg)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, adv_msg)
        _push_stage_and_set("anything_else")
        _mody_render_stage(phone, session, inbound_msg_id)
        return

    # --- Vehicle Details: Model (Pages 1 to 5) ---
    if stage in ["srv_vehicle_model", "srv_vehicle_model_2", "srv_vehicle_model_3", "srv_vehicle_model_4", "srv_vehicle_model_5"]:
        selected = interactive_id or text_clean

        # Handle pagination between service model screens
        if interactive_id == "srv_car_other_1" or (stage == "srv_vehicle_model" and ("other" in text_lower or "more" in text_lower)):
            session["stage"] = "srv_vehicle_model_2"
            mody_sessions.set(phone, session, timeout=MODY_SESSION_TTL)
            _mody_render_stage(phone, session, inbound_msg_id)
            return

        if interactive_id == "srv_car_other_2" or (stage == "srv_vehicle_model_2" and ("other" in text_lower or "more" in text_lower)):
            session["stage"] = "srv_vehicle_model_3"
            mody_sessions.set(phone, session, timeout=MODY_SESSION_TTL)
            _mody_render_stage(phone, session, inbound_msg_id)
            return

        if interactive_id == "srv_car_other_3" or (stage == "srv_vehicle_model_3" and ("other" in text_lower or "more" in text_lower)):
            session["stage"] = "srv_vehicle_model_4"
            mody_sessions.set(phone, session, timeout=MODY_SESSION_TTL)
            _mody_render_stage(phone, session, inbound_msg_id)
            return

        if interactive_id == "srv_car_other_4" or (stage == "srv_vehicle_model_4" and ("other" in text_lower or "more" in text_lower)):
            session["stage"] = "srv_vehicle_model_5"
            mody_sessions.set(phone, session, timeout=MODY_SESSION_TTL)
            _mody_render_stage(phone, session, inbound_msg_id)
            return

        car_map = {
            "car_glanza": "Glanza",
            "car_taisor": "Urban Cruiser Taisor",
            "car_hyryder": "Urban Cruiser Hyryder",
            "car_rumion": "Rumion",
            "car_hycross": "Innova Hycross",
            "car_crysta": "Innova Crysta",
            "car_fortuner": "Fortuner",
            "car_legender": "Fortuner Legender",
            "car_hilux": "Hilux",
            "car_camry": "New Camry Hybrid",
            "car_vellfire": "Vellfire",
            "car_lc300": "Land Cruiser 300",
            # Legacy/compatibility keys
            "srv_m_innova": "Innova / Hycross",
            "srv_m_fortuner": "Fortuner / Legender",
            "srv_m_hyryder": "UC Hyryder",
            "srv_m_glanza": "Glanza",
            "srv_m_yaris": "Yaris / Etios",
            "srv_m_other": "Other Toyota Model",
        }
        chosen_car = car_map.get(selected) or interactive_title or text_clean
        session["srv_car_model"] = chosen_car
        _push_stage_and_set("srv_reg_number")
        _mody_render_stage(phone, session, inbound_msg_id)
        return

    # --- Vehicle Details: Registration Number ---
    if stage == "srv_reg_number":
        if not _is_valid_vehicle_reg(text_clean):
            err_msg = (
                "*Invalid Vehicle Registration Number.*\n\n"
                "Please enter a valid registration number (e.g. *GJ01AB1234* or *MH02CD5678*):"
            )
            buttons = [
                {"id": "btn_mody_main_menu", "title": "Main Menu"},
            ]
            send_buttons(phone, err_msg, buttons)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, err_msg)
            return
        session["srv_reg_number"] = text_clean.upper()
        _push_stage_and_set("srv_booking_type")
        _mody_render_stage(phone, session, inbound_msg_id)
        return

    # --- Service Booking: Service Type ---
    if stage == "srv_booking_type":
        srv_types = {
            "srv_type_periodic": "Periodic Maintenance",
            "srv_type_repair": "General Repair / Checkup",
            "srv_type_body": "Body & Paint Work",
        }
        chosen_type = srv_types.get(interactive_id) or interactive_title or text_clean
        session["srv_type"] = chosen_type
        _push_stage_and_set("srv_booking_date")
        _mody_render_stage(phone, session, inbound_msg_id)
        return

    # --- Service Booking: Date ---
    if stage == "srv_booking_date":
        date_map = {
            "srv_date_tomorrow": (datetime.now() + timedelta(days=1)).strftime("%d-%b-%Y (%A)"),
            "srv_date_day_after": (datetime.now() + timedelta(days=2)).strftime("%d-%b-%Y (%A)"),
            "srv_date_weekend": "Upcoming Weekend",
        }
        chosen_date = date_map.get(interactive_id) or interactive_title or text_clean
        session["srv_date"] = chosen_date
        _push_stage_and_set("srv_booking_time_slot")
        _mody_render_stage(phone, session, inbound_msg_id)
        return

    # --- Service Booking: Time Slot ---
    if stage == "srv_booking_time_slot":
        slot_map = {
            "srv_slot_morning": "Morning (09:30 AM – 12:00 PM)",
            "srv_slot_afternoon": "Afternoon (12:00 PM – 03:00 PM)",
            "srv_slot_evening": "Evening (03:00 PM – 06:00 PM)",
        }
        chosen_slot = slot_map.get(interactive_id) or interactive_title or text_clean
        session["srv_time_slot"] = chosen_slot
        _push_stage_and_set("srv_pickup_choice")
        _mody_render_stage(phone, session, inbound_msg_id)
        return

    # --- Pickup & Drop: Yes or No ---
    if stage == "srv_pickup_choice":
        if interactive_id in ["srv_pk_yes", "yes"] or "yes" in text_lower:
            session["srv_pickup"] = "Yes, Pickup & Drop Required"
            _push_stage_and_set("srv_pickup_name")
            _mody_render_stage(phone, session, inbound_msg_id)
            return
        else:
            session["srv_pickup"] = "No, Self Drop at Workshop"
            session["srv_contact_name"] = session.get("client_name") or "Valued Customer"
            session["srv_contact_mobile"] = phone.lstrip("+")
            # Complete Service Booking
            _mody_confirm_service_booking(phone, session, inbound_msg_id)
            _push_stage_and_set("anything_else")
            _mody_render_stage(phone, session, inbound_msg_id)
            return

    # --- Pickup & Drop: Contact Name ---
    if stage == "srv_pickup_name":
        entered_name = text_clean
        if len(entered_name) < 2 or re.match(r"^\d+$", entered_name):
            err_msg = "Please enter a valid contact name for pickup:"
            send_text(phone, err_msg)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, err_msg)
            return
        session["srv_contact_name"] = entered_name
        _push_stage_and_set("srv_pickup_mobile")
        _mody_render_stage(phone, session, inbound_msg_id)
        return

    # --- Pickup & Drop: Mobile ---
    if stage == "srv_pickup_mobile":
        if not _is_valid_indian_mobile(text_clean):
            err_msg = (
                "*Invalid Mobile Number.*\n\n"
                "Please enter a valid 10-digit mobile number for pickup coordination (e.g. *9876543210*):"
            )
            buttons = [
                {"id": "btn_mody_main_menu", "title": "Main Menu"},
            ]
            send_buttons(phone, err_msg, buttons)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, err_msg)
            return
        session["srv_contact_mobile"] = _clean_indian_mobile(text_clean)
        _push_stage_and_set("srv_pickup_address")
        _mody_render_stage(phone, session, inbound_msg_id)
        return

    # --- Pickup & Drop: Address or Live Location Pin ---
    if stage == "srv_pickup_address":
        # Check if sent via location or text
        entered_addr = text_clean
        if raw_msg and raw_msg.get("type") == "location":
            loc = raw_msg.get("location", {})
            lat = loc.get("latitude")
            lon = loc.get("longitude")
            name = loc.get("name") or ""
            addr = loc.get("address") or ""
            entered_addr = f"Location Pin: https://maps.google.com/?q={lat},{lon}"
            if name or addr:
                entered_addr += f" ({name}, {addr})".strip()
        elif len(entered_addr) < 4:
            err_msg = (
                "*Please provide a complete pickup address.*\n\n"
                "Type your House/Flat No, Landmark & Area, or share your WhatsApp Location Pin:"
            )
            buttons = [
                {"id": "btn_mody_main_menu", "title": "Main Menu"},
            ]
            send_buttons(phone, err_msg, buttons)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, err_msg)
            return

        session["srv_pickup_address"] = entered_addr

        # Complete Service Booking
        _mody_confirm_service_booking(phone, session, inbound_msg_id)
        _push_stage_and_set("anything_else")
        _mody_render_stage(phone, session, inbound_msg_id)
        return

    # ═════════════════════════════════════════════════════════════════════════
    # SHARED: "ANYTHING ELSE?" STEP
    # ═════════════════════════════════════════════════════════════════════════
    if stage == "anything_else":
        if interactive_id in ["btn_anything_yes", "yes"] or "yes" in text_lower:
            _mody_send_welcome(phone, inbound_msg_id=inbound_msg_id, client_name=client_name)
            return
        else:
            mody_sessions.delete(phone)
            thank_msg = (
                "🙏 *Thank you for choosing Mody Toyota!*\n\n"
                "Our team is always here for you. Wishing you safe travels and joyful journeys! 🚗✨\n\n"
                "Send *mody* anytime whenever you need us."
            )
            send_text(phone, thank_msg)
            if inbound_msg_id:
                _save_reply(inbound_msg_id, thank_msg)
            return

    # Fallback
    _mody_send_welcome(phone, inbound_msg_id=inbound_msg_id, client_name=client_name)


# ── Mody Toyota Stage Render Helper ──────────────────────────────────────────

def _mody_render_stage(phone: str, session: dict, inbound_msg_id: int = None):
    stage = session.get("stage", "welcome")

    # ── BUY NEW CAR RENDERING (12 Models across 5 stages) ──
    if stage == "buy_select_model_1":
        body = (
            "*Discover the Toyota Range*\n\n"
            "Please select the Toyota vehicle you are interested in:"
        )
        buttons = [
            {"id": "car_hycross", "title": "Innova Hycross"},
            {"id": "car_crysta", "title": "Innova Crysta"},
            {"id": "mody_car_other_1", "title": "More Models >>"},
        ]
        send_buttons(phone, body, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    if stage == "buy_select_model_2":
        body = (
            "*Popular SUVs*\n\n"
            "Please choose from our popular SUVs:"
        )
        buttons = [
            {"id": "car_fortuner", "title": "Fortuner"},
            {"id": "car_legender", "title": "Legender"},
            {"id": "mody_car_other_2", "title": "More Models >>"},
        ]
        send_buttons(phone, body, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    if stage == "buy_select_model_3":
        body = (
            "*Compact SUVs & Crossovers*\n\n"
            "Please choose your preferred model:"
        )
        buttons = [
            {"id": "car_hyryder", "title": "UC Hyryder"},
            {"id": "car_taisor", "title": "UC Taisor"},
            {"id": "mody_car_other_3", "title": "More Models >>"},
        ]
        send_buttons(phone, body, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    if stage == "buy_select_model_4":
        body = (
            "*Hatchback & MPV*\n\n"
            "Please choose your preferred model:"
        )
        buttons = [
            {"id": "car_glanza", "title": "Glanza"},
            {"id": "car_rumion", "title": "Rumion"},
            {"id": "mody_car_other_4", "title": "More Models >>"},
        ]
        send_buttons(phone, body, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    if stage == "buy_select_model_5":
        body = (
            "*Premium & Luxury Range*\n\n"
            "Please choose from our flagship lineup:"
        )
        buttons = [
            {"id": "car_camry", "title": "Camry Hybrid"},
            {"id": "car_hilux", "title": "Hilux Pick-up"},
            {"id": "car_vellfire", "title": "Vellfire / LC300"},
        ]
        send_buttons(phone, body, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    if stage == "buy_choose_action":
        model = session.get("car_model", "Toyota")
        body = (
            f"✨ *Great Choice! Toyota {model}*\n\n"
            "What action would you like to take today?"
        )
        buttons = [
            {"id": "act_test_drive", "title": "Test Drive"},
            {"id": "act_quote", "title": "Get Quote"},
            {"id": "act_callback", "title": "Request Callback"},
        ]
        send_buttons(phone, body, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    if stage == "buy_td_date":
        model = session.get("car_model", "Toyota")
        d1 = datetime.now().strftime("%d %b")
        d2 = (datetime.now() + timedelta(days=1)).strftime("%d %b")
        body = (
            f"🚗 *Test Drive Date — Toyota {model}*\n\n"
            "Please select your preferred date for the test drive:"
        )
        buttons = [
            {"id": "td_date_today", "title": f"Today ({d1})"},
            {"id": "td_date_tomorrow", "title": f"Tomorrow ({d2})"},
            {"id": "td_date_other", "title": "This Weekend"},
        ]
        send_buttons(phone, body, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    if stage == "buy_td_time":
        date_sel = session.get("td_date", "Selected Date")
        body = (
            f"⏰ *Test Drive Time Slot ({date_sel})*\n\n"
            "What time would suit you best?"
        )
        buttons = [
            {"id": "td_time_morning", "title": "Morning (10-1)"},
            {"id": "td_time_afternoon", "title": "Afternoon (1-4)"},
            {"id": "td_time_evening", "title": "Evening (4-7)"},
        ]
        send_buttons(phone, body, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    if stage == "buy_collect_name":
        model = session.get("car_model", "Toyota")
        action = session.get("buy_action", "Request")
        body = (
            f"📋 *{action} — Toyota {model}*\n\n"
            "Please type your *Full Name*:"
        )
        send_text(phone, body)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    if stage == "buy_finance_exchange":
        name = session.get("cust_name", "Sir/Madam")
        body = (
            f"✨ Thank you, *{name}*!\n\n"
            "Would you like finance assistance or old car exchange with your inquiry?"
        )
        buttons = [
            {"id": "pref_finance", "title": "Need Finance"},
            {"id": "pref_exchange", "title": "Exchange Car"},
            {"id": "pref_standard", "title": "Just Best Price"},
        ]
        send_buttons(phone, body, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    # ── SERVICE FLOW RENDERING ──
    if stage == "srv_select_action":
        body = (
            "*Mody Toyota Certified Service*\n\n"
            "How can our service team assist you today?"
        )
        buttons = [
            {"id": "srv_book", "title": "Book Service"},
            {"id": "srv_status", "title": "Check Status"},
            {"id": "srv_advisor", "title": "Support / Advisor"},
        ]
        send_buttons(phone, body, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    if stage == "srv_status_check":
        body = (
            "*Check Vehicle Service Status*\n\n"
            "Please type your vehicle *Registration Number* (e.g. *GJ01AB1234*):"
        )
        buttons = [
            {"id": "btn_mody_main_menu", "title": "Main Menu"},
        ]
        send_buttons(phone, body, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    if stage == "srv_advisor_connect":
        body = (
            "*Service Support & Complaint Registration*\n\n"
            "Please type your complaint, concern, or service requirement below:\n\n"
            "(Our service advisor will review and contact you Shortly.)"
        )
        buttons = [
            {"id": "btn_mody_main_menu", "title": "Main Menu"},
        ]
        send_buttons(phone, body, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    # ── SERVICE FLOW RENDERING: 12 Models across 5 stages ──
    if stage == "srv_vehicle_model":
        body = (
            "*Select Your Toyota Model*\n\n"
            "Which vehicle are you bringing for certified service?"
        )
        buttons = [
            {"id": "car_hycross", "title": "Innova Hycross"},
            {"id": "car_crysta", "title": "Innova Crysta"},
            {"id": "srv_car_other_1", "title": "More Models >>"},
        ]
        send_buttons(phone, body, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    if stage == "srv_vehicle_model_2":
        body = (
            "*Select Your Toyota SUV*\n\n"
            "Please choose from our popular SUVs:"
        )
        buttons = [
            {"id": "car_fortuner", "title": "Fortuner"},
            {"id": "car_legender", "title": "Legender"},
            {"id": "srv_car_other_2", "title": "More Models >>"},
        ]
        send_buttons(phone, body, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    if stage == "srv_vehicle_model_3":
        body = (
            "*Select Your Toyota Vehicle*\n\n"
            "Please choose your preferred model:"
        )
        buttons = [
            {"id": "car_hyryder", "title": "UC Hyryder"},
            {"id": "car_taisor", "title": "UC Taisor"},
            {"id": "srv_car_other_3", "title": "More Models >>"},
        ]
        send_buttons(phone, body, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    if stage == "srv_vehicle_model_4":
        body = (
            "*Select Your Toyota Vehicle*\n\n"
            "Please choose your preferred model:"
        )
        buttons = [
            {"id": "car_glanza", "title": "Glanza"},
            {"id": "car_rumion", "title": "Rumion"},
            {"id": "srv_car_other_4", "title": "More Models >>"},
        ]
        send_buttons(phone, body, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    if stage == "srv_vehicle_model_5":
        body = (
            "*Premium & Flagship Models*\n\n"
            "Please choose from our premium lineup:"
        )
        buttons = [
            {"id": "car_camry", "title": "Camry Hybrid"},
            {"id": "car_hilux", "title": "Hilux Pick-up"},
            {"id": "car_vellfire", "title": "Vellfire / LC300"},
        ]
        send_buttons(phone, body, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    if stage == "srv_reg_number":
        car = session.get("srv_car_model", "Toyota")
        body = (
            f"*Vehicle Registration Number ({car})*\n\n"
            "Please type your vehicle registration number (e.g. *GJ01AB1234*):"
        )
        buttons = [
            {"id": "btn_mody_main_menu", "title": "Main Menu"},
        ]
        send_buttons(phone, body, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    if stage == "srv_booking_type":
        reg = session.get("srv_reg_number", "")
        body = (
            f"*Select Service Type for {reg}:*\n\n"
            "Please select the kind of service required:"
        )
        buttons = [
            {"id": "srv_type_periodic", "title": "Periodic Maint."},
            {"id": "srv_type_repair", "title": "General Repair"},
            {"id": "srv_type_body", "title": "Body & Paint"},
        ]
        send_buttons(phone, body, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    if stage == "srv_booking_date":
        d1 = (datetime.now() + timedelta(days=1)).strftime("%a, %d %b")
        d2 = (datetime.now() + timedelta(days=2)).strftime("%a, %d %b")
        body = (
            "*Select Preferred Service Date:*\n\n"
            "Choose a convenient date below:"
        )
        buttons = [
            {"id": "srv_date_tomorrow", "title": f"{d1}"},
            {"id": "srv_date_day_after", "title": f"{d2}"},
            {"id": "srv_date_weekend", "title": "This Weekend"},
        ]
        send_buttons(phone, body, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    if stage == "srv_booking_time_slot":
        date_val = session.get("srv_date", "Selected Date")
        body = (
            f"*Select Time Slot ({date_val}):*\n\n"
            "Pick a convenient drop-off/pickup slot:"
        )
        buttons = [
            {"id": "srv_slot_morning", "title": "Morning (9-12)"},
            {"id": "srv_slot_afternoon", "title": "Afternoon (12-3)"},
            {"id": "srv_slot_evening", "title": "Evening (3-6)"},
        ]
        send_buttons(phone, body, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    if stage == "srv_pickup_choice":
        body = (
            "*Doorstep Pickup & Drop Facility*\n\n"
            "Would you like our executive to pick up and drop your vehicle at your doorstep?"
        )
        buttons = [
            {"id": "srv_pk_yes", "title": "Yes, Need Pickup"},
            {"id": "srv_pk_no", "title": "No, Self Drop"},
            {"id": "btn_mody_main_menu", "title": "Main Menu"},
        ]
        send_buttons(phone, body, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    if stage == "srv_pickup_name":
        body = (
            "*Pickup Coordination Contact*\n\n"
            "Please type the *Contact Person Name* for vehicle handover:"
        )
        send_text(phone, body)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    if stage == "srv_pickup_mobile":
        name = session.get("srv_contact_name", "Customer")
        body = (
            f"*Pickup Mobile Number for {name}*\n\n"
            "Please enter the *10-digit Mobile Number*:"
        )
        send_text(phone, body)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    if stage == "srv_pickup_address":
        body = (
            "📍 *Pickup Address / Location*\n\n"
            "Please enter your *Complete Pickup Address* (House/Flat No, Society/Building, Landmark, Area)\n\n"
            "📌 *Tip:* You can also tap the WhatsApp attachment icon and share your *Current Location* pin directly!"
        )
        send_text(phone, body)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return

    # ── ANYTHING ELSE? ──
    if stage == "anything_else":
        body = (
            "*Is there anything else we can help you with today?*"
        )
        buttons = [
            {"id": "btn_anything_yes", "title": "Yes, More Help"},
            {"id": "btn_anything_no", "title": "No, All Done"},
        ]
        send_buttons(phone, body, buttons)
        if inbound_msg_id:
            _save_reply(inbound_msg_id, body)
        return


# ── Confirmation & CRM Lead Logging ──────────────────────────────────────────

def _mody_confirm_buy_lead(phone: str, session: dict, inbound_msg_id: int = None):
    model = session.get("car_model", "Toyota")
    action = session.get("buy_action", "Inquiry")
    name = session.get("cust_name", "Valued Customer")
    mobile = session.get("cust_mobile", phone.lstrip("+"))
    exchange = session.get("car_exchange", "N/A")
    finance = session.get("finance_pref", "N/A")

    td_date = session.get("td_date")
    td_time = session.get("td_time")
    td_info = ""
    if td_date or td_time:
        td_info = f"• *Test Drive Schedule:* {td_date or 'N/A'} at {td_time or 'Preferred Time'}\n"

    conf_msg = (
        "🎉 *Thank You! Your Request Has Been Received.* 🚗✨\n\n"
        "Here are your request details:\n"
        f"• *Vehicle:* Toyota {model}\n"
        f"• *Interest:* {action}\n"
        f"{td_info}"
        f"• *Customer Name:* {name}\n"
        f"• *Contact:* +{mobile}\n"
        f"• *Finance:* {finance}\n"
        f"• *Exchange:* {exchange}\n\n"
        "Our Senior Sales Consultant from Mody Toyota will get in touch with you shortly."
    )
    send_text(phone, conf_msg)
    if inbound_msg_id:
        _save_reply(inbound_msg_id, conf_msg)


def _mody_confirm_service_booking(phone: str, session: dict, inbound_msg_id: int = None):
    model = session.get("srv_car_model", "Toyota")
    reg = session.get("srv_reg_number", "N/A")
    stype = session.get("srv_type", "General Service")
    sdate = session.get("srv_date", "Upcoming")
    slot = session.get("srv_time_slot", "Standard")
    pickup = session.get("srv_pickup", "No")
    cname = session.get("srv_contact_name", session.get("client_name", "Customer"))
    cmobile = session.get("srv_contact_mobile", phone.lstrip("+"))
    address = session.get("srv_pickup_address", "")

    addr_info = ""
    if "yes" in pickup.lower() and address:
        addr_info = f"• *Pickup Address:* {address}\n"

    conf_msg = (
        "*Service Appointment Request Submitted*\n\n"
        f"• *Vehicle:* {model} ({reg})\n"
        f"• *Service Type:* {stype}\n"
        f"• *Date:* {sdate}\n"
        f"• *Time Slot:* {slot}\n"
        f"• *Pickup & Drop:* {pickup}\n"
        f"• *Contact:* {cname} ({cmobile})\n"
        f"{addr_info}\n"
        "Our dedicated Mody Toyota Service Advisor will call you shortly to confirm your booking and checklist."
    )
    send_text(phone, conf_msg)
    if inbound_msg_id:
        _save_reply(inbound_msg_id, conf_msg)



