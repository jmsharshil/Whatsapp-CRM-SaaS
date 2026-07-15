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

JMS_SESSION_TTL = 60 * 60
WA_SESSION_TTL  = 60 * 15
IND_SESSION_TTL = 60 * 60


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
    if not _auth(request):
        return _forbidden()

    now   = timezone.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    total_customers     = Customer.objects.count()
    total_conversations = Conversation.objects.count()
    total_messages      = Message.objects.count()
    active_today        = Conversation.objects.filter(messages__timestamp__gte=today).distinct().count()
    new_today           = Customer.objects.filter(conversations__created_at__gte=today).distinct().count()

    status_qs               = Conversation.objects.values("status").annotate(count=Count("id"))
    conversations_by_status = {row["status"]: row["count"] for row in status_qs}

    seven_days_ago = today - timedelta(days=6)
    daily_qs = (
        Message.objects
        .filter(timestamp__gte=seven_days_ago)
        .extra(select={"day": "DATE(timestamp)"})
        .values("day")
        .annotate(count=Count("id"))
        .order_by("day")
    )
    messages_last_7_days = [{"date": str(row["day"]), "count": row["count"]} for row in daily_qs]

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
    
    from django.db.models import Q
    base_qs = Customer.objects.all()
    if phone_number_id:
        base_qs = base_qs.filter(
            Q(conversations__client__phone_number_id=phone_number_id) |
            Q(conversations__phone_number_id=phone_number_id)
        ).distinct()
    else:
        # If no phone_number_id is found for TechProvider, do not leak clients
        base_qs = Customer.objects.none()

    qs = base_qs.annotate(
        total_conversations=Count("conversations", distinct=True),
        total_messages=Count("messages", distinct=True),
        last_seen=Max("messages__timestamp"),
        reply_count=Count(
            "messages",
            filter=Q(messages__direction="outbound"),
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
    ).order_by("-id")

    if search:
        qs = qs.filter(Q(name__icontains=search) | Q(phone__icontains=search))

    all_customers  = list(qs)
    total          = len(all_customers)
    lead_count     = sum(1 for c in all_customers if c.reply_count > 0)
    prospect_count = total - lead_count

    results = []
    for c in all_customers:
        customer_status = "lead" if c.reply_count > 0 else "prospect"
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
    if not _auth(request):
        return _forbidden()

    status_filter  = request.GET.get("status", "").strip()
    phone_filter   = request.GET.get("phone",  "").strip()
    page           = max(int(request.GET.get("page", 1)), 1)
    page_size      = min(int(request.GET.get("page_size", 100)), 100)
    from_dt, to_dt = _parse_date_range(request)

    qs = Conversation.objects.select_related("customer").annotate(
        message_count=Count("messages"),
        last_msg_time=Max("messages__timestamp")
    ).order_by("-last_msg_time", "-created_at")

    if status_filter: qs = qs.filter(status=status_filter)
    if phone_filter:  qs = qs.filter(customer__phone__icontains=phone_filter)
    if from_dt:       qs = qs.filter(created_at__gte=from_dt)
    if to_dt:         qs = qs.filter(created_at__lt=to_dt)

    total = qs.count()
    convs = qs[(page - 1) * page_size : page * page_size]

    results = []
    for conv in convs:
        last_msg   = conv.messages.order_by("-timestamp").first()
        has_reply  = conv.messages.filter(reply_of__isnull=False).exclude(reply_of="").exists()
        auto_status = "lead" if has_reply else "prospect"
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

MF_BOT_TRIGGER = "mutual funds"
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
            text_lower = MF_BOT_TRIGGER

    # Welcome Menu
    if text_lower == MF_BOT_TRIGGER:
        session["stage"] = "active"
        session["company"] = ""
        session["last_scheme_context"] = None
        session["history"] = []
        mf_save_session(session)
        
        welcome_text = (
            "📈 *Welcome to the Mutual Funds Bot!*\n\n"
            "How can I help you today? If you know the mutual fund name then write it, or else select an option from below."
        )
        
        amcs = ["SBI", "ICICI", "HDFC", "Nippon", "Kotak", "UTI", "Aditya", "DSP", "Franklin", "Axis"]
        items = []
        for amc in amcs:
            items.append({
                "id": f"company_{amc}",
                "title": amc[:24],
                "description": "Select company"
            })
        sections = [{"title": "Select Company", "rows": items}]
        send_interactive_list(
            to=phone, 
            body_text=welcome_text, 
            button_text="Select Company", 
            sections=sections
        )
        _save_reply(inbound_msg_id, welcome_text)
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
        query = f"{company} {selected_category}".strip()
        
        session["stage"] = "active" # Reset stage so direct typing falls back to search
        session["last_query"] = query
        mf_save_session(session)
        
        _mf_search_and_send(phone, query, inbound_msg_id)
        return

    # Handle Scheme Selection (Scheme Code is always numeric)
    if interactive_id and interactive_id.isdigit():
        scheme_code = interactive_id
        try:
            resp = requests.get(f"https://api.mfapi.in/mf/{scheme_code}/latest", timeout=30)
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
                            {"id": "mf_restart", "title": "Find Another"},
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

def _mf_search_and_send(phone: str, query: str, inbound_msg_id: int = None):
    try:
        resp = requests.get(f"https://api.mfapi.in/mf/search?q={query}", timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            if not data:
                send_text(phone, "No schemes found for that query. Try another one.")
            else:
                items = []
                seen_titles = set()
                for item in data:
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
                    if len(items) == 10:
                        break
                        
                sections = [{"title": "Search Results", "rows": items}]
                send_interactive_list(
                    to=phone, 
                    body_text=f"Search Results for '{query}'", 
                    button_text="Select Scheme", 
                    sections=sections
                )
                if inbound_msg_id:
                    _save_reply(inbound_msg_id, f"Sent search results for {query}")
        else:
            send_text(phone, "Search failed.")
    except Exception as e:
        send_text(phone, f"Error: {str(e)}")