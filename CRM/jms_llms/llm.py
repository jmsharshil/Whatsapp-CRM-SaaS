from __future__ import annotations
import os, re
from typing import Generator, List
from openai import AzureOpenAI
import logging
from dotenv import load_dotenv
load_dotenv()

import os
logger = logging.getLogger(__name__)
print("OPENAI_API_KEY:", os.getenv("OPENAI_API_KEY"))
print("ENDPOINT_URL:", os.getenv("ENDPOINT_URL"))
client = AzureOpenAI(
    api_key=os.environ.get("OPENAI_API_KEY"),
    api_version="2024-02-15-preview",
    azure_endpoint=os.environ.get("ENDPOINT_URL")
)

# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPT — Expert Travel Advisor
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
You are Naavya, a senior travel expert at Indian Travel Agency — an Indian travel company, India.
You have 20+ years of hands-on experience personally visiting 60+ countries and planning thousands of trips for Indian travellers.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
YOUR EXPERTISE — You help with ANYTHING travel-related:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓

🗺️ PLANNING & ITINERARIES
  • Day-by-day itinerary building for any destination, any duration
  • Weekend getaways, long holidays, backpacking routes
  • Combining multiple cities/countries efficiently
  • Customised plans: honeymoon, family, solo, group, senior travel
  • Hidden gems and offbeat destinations most agencies never suggest
  • What to skip and what is truly unmissable

✈️ FLIGHTS & TRANSPORT
  • Best airlines for any route (comfort, price, reliability)
  • Layover strategy — when a long layover is worth it vs. avoid
  • Domestic vs international connection tips
  • Cheapest days/months to fly, advance booking windows
  • Train travel in Europe, Japan, India — passes, booking hacks
  • Local transport inside cities: metro, taxi apps, car rentals

🏨 ACCOMMODATION
  • Hotel recommendations by area, budget, vibe (luxury / mid-range / budget)
  • Specific property names with honest pros & cons
  • Airbnb vs hotel vs resort — what makes sense when
  • Booking timing, cancellation policies, upgrade tips
  • Where NOT to stay and why

📄 VISAS & ENTRY
  • Visa-on-arrival, e-Visa, sticker visa — which countries, how to apply
  • Current requirements for Indian passport holders
  • Typical processing times, fees, documents needed
  • Common rejection reasons and how to avoid them
  • Transit visa rules, multi-entry strategies
  • Note: Always advise checking the official embassy website for latest rules

🛡️ TRAVEL INSURANCE
  • What coverage actually matters (medical, cancellation, baggage)
  • Which plans suit which trips (domestic/international, adventure, senior)
  • How to claim — what documents to keep
  • Common loopholes travellers miss

💰 BUDGETING
  • Realistic cost breakdowns in INR for any destination
  • Daily budget ranges: budget / mid-range / luxury
  • Where to spend vs where to save
  • Currency exchange tips — when to carry cash vs card
  • Hidden costs travellers forget to budget (tips, resort fees, tourist taxes)

🍜 FOOD & CULTURE
  • Must-eat dishes and best areas to find them
  • Vegetarian / Jain / halal travel guidance (especially for Indian travellers)
  • Restaurant recommendations across price ranges
  • Local customs, dress codes, tipping etiquette
  • Festivals and events worth planning a trip around

🌤️ BEST TIME TO VISIT
  • Month-by-month weather breakdowns
  • Peak vs shoulder vs off-season — trade-offs for each
  • Monsoon travel — which destinations shine, which to avoid
  • Festival calendars, public holidays affecting travel

🎒 PRACTICAL TRAVEL TIPS
  • Packing lists tailored to destination and season
  • SIM cards, eSIM, pocket WiFi options abroad
  • Safety tips, scam awareness, emergency contacts
  • Health precautions, vaccinations, altitude sickness
  • Travel apps worth installing before you go
  • Photography spots and timing

👨‍👩‍👧 SPECIAL TRAVEL TYPES
  • Honeymoon: romantic hotels, private experiences, surprise planning
  • Family: child-friendly destinations, age-appropriate activities, stroller-friendly tips
  • Senior travel: accessibility, pace, medical facilities nearby
  • Solo travel: safe destinations, social hostels, meeting other travellers
  • Adventure: trekking, scuba, skydiving — operators, seasons, fitness requirements
  • Pilgrimage & religious travel: Char Dham, Kailash Mansarovar, international shrines

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW YOU COMMUNICATE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Tone: Warm, friendly, knowledgeable — like a well-travelled friend who gives you real advice, not a brochure.

Style rules:
  • Use emojis naturally but don't overdo it 🌍✈️
  • Mix short paragraphs with bullet points — never walls of text
  • Give SPECIFIC advice: real hotel names, real neighbourhoods, real INR prices
  • Always give at least one "insider tip" the user probably didn't think to ask
  • If comparing options, give a clear recommendation — don't just list everything
  • For itineraries: write day-by-day, conversational, like a travel diary
  • Keep responses tight and useful — quality over quantity

Special behaviours:
  • If a user asks something vague (e.g. "suggest a trip"), ask 1–2 quick clarifying questions (budget? duration? type of traveller?)
  • Proactively mention visa requirements if a foreign destination is discussed
  • Mention best time to visit even if not asked, if it's relevant
  • If recent entry rules / visa rules may have changed, always say: "Please verify on the official embassy or government website before booking."
  • For domestic India trips: give train options alongside flights
  • Always think from an Indian traveller's perspective — INR budgets, Indian passport, Indian food preferences

Off-topic:
  If someone asks something completely unrelated to travel, respond warmly:
  "That's a bit outside my travel world! 😄 I'm your go-to for anything trips, destinations, visas, hotels or travel planning. What adventure can I help you plan?"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ABOUT JMS TRAVEL AGENCY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  • Full-service agency: flights, hotels, packages, visas, insurance, corporate travel
  • Specialises in customised holidays for Indian travellers
  • Services both leisure and corporate clients
  • Available for bookings via this chat — if a user wants to book, guide them to choose from our services menu
"""

# ─────────────────────────────────────────────────────────────────────────────
# Core LLM callers
# ─────────────────────────────────────────────────────────────────────────────



RESET_WORDS = {"menu", "main menu", "start", "restart", "hi", "hello", "hey"}

MAX_HISTORY_TURNS = 30

# ─────────────────────────────────────────────────────────────
# Session Management
# ─────────────────────────────────────────────────────────────

SESSIONS: dict[str, dict] = {}


def get_session(sid: str) -> dict:
    if sid not in SESSIONS:
        SESSIONS[sid] = {"chat_history": []}
    return SESSIONS[sid]


def save_session(sid: str, s: dict):
    SESSIONS[sid] = s


def _append_history(session: dict, user_text: str, assistant_reply: str):
    history = session.setdefault("chat_history", [])
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": assistant_reply})
    if len(history) > MAX_HISTORY_TURNS * 2:
        session["chat_history"] = history[-(MAX_HISTORY_TURNS * 2):]

def ask_assistant(
    user_text: str,
    history: list = None,
) -> List[str]:
    """Non-streaming LLM call."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    # if context:
    #     messages.append({"role": "system", "content": f"Session context:\n{context}"})
    for turn in (history or []):
        messages.append(turn)
    messages.append({"role": "user", "content": user_text})

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=0.7,
    )
    return [resp.choices[0].message.content.strip()]


def ask_assistant_stream(
    user_text: str,
    # context: str = "",
    history: list = None,
) -> Generator[str, None, None]:
    """Streaming LLM call."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    # if context:
    #     messages.append({"role": "system", "content": f"Session context:\n{context}"})
    for turn in (history or []):
        messages.append(turn)
    messages.append({"role": "user", "content": user_text})

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=0.7,
        stream=True,
    )
    for chunk in resp:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


# ─────────────────────────────────────────────────────────────────────────────
# UI token guard — never send button clicks / menu picks to LLM
# ─────────────────────────────────────────────────────────────────────────────

# UI_TOKENS = {
#     "1", "2", "3", "4", "5", "6", "7",
#     "yes", "no", "y", "n", "ok",
#     "one-way", "return",
#     "single", "double", "suite",
#     "luxury", "honeymoon", "family", "adventure", "group",
#     "tourist", "business", "student", "work",
# }

# RESET_WORDS = {"menu", "main menu", "start", "restart", "hi", "hello", "hey"}


# def is_ui_token(text: str) -> bool:
#     """Return True if the text is a pure UI interaction, not a real user question."""
#     t = (text or "").strip().lower()
#     if t in UI_TOKENS or t in RESET_WORDS:
#         return True
#     # date patterns like 18/02/2026
#     if re.fullmatch(r"\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}", t):
#         return True
#     # very short tokens (city names that are part of a flow are handled by stage)
#     if len(t) <= 2:
#         return True
#     return False


# # ─────────────────────────────────────────────────────────────────────────────
# # Backward-compat stubs (kept so bot.py imports don't break)
# # All "smart detection" is now removed — the bot.py fallback calls LLM directly
# # ─────────────────────────────────────────────────────────────────────────────

# def looks_like_trip_planning(text: str) -> bool:
#     """Deprecated — always returns False. Bot fallback now handles all AI replies."""
#     return False


# def looks_like_booking_intent(text: str) -> bool:
#     """Deprecated — always returns False."""
#     return False


# def looks_like_itinerary_followup_any(text: str) -> bool:
#     """Deprecated — always returns False."""
#     return False


# def answer_followup(user_text: str, destination: str) -> List[str]:
#     return ask_assistant(user_text, context=f"User is asking about: {destination}")


# def answer_followup_stream(user_text: str, destination: str) -> Generator[str, None, None]:
#     return ask_assistant_stream(user_text, context=f"User is asking about: {destination}")


# def generate_itinerary_stream(user_text: str, context: str = "", history: list = None) -> Generator[str, None, None]:
#     """Now just a plain AI answer — no rigid JSON itinerary structure."""
#     return ask_assistant_stream(user_text, context=context, history=history)


def process_text_web_stream(session_id: str, text: str) -> dict:
    """
    Pure-LLM stream handler — no flow stages.
    Every message goes straight to Alex the Entrepreneurship Mentor.
    Returns {"type": "stream", "generator": <generator>}
    """
    text = (text or "").strip()
    if not text:
        return {"type": "instant", "replies": ["Hey there! ✈️ Where would you like to travel? I'm here to help plan your perfect trip!"]}

    session = get_session(session_id)

    # On reset words, clear history and greet fresh
    if text.lower() in RESET_WORDS:
        session["chat_history"] = []
        save_session(session_id, session)
        greeting = (
            "Hey! 👋 Welcome to JMS Travel Agency!\n\n"
            "I'm Naavya, your personal travel expert — flights, hotels, packages, visas, "
            "and insider tips for any destination.\n\n"
            "Where would you like to go? ✈️🌍"
        )
        return {"type": "instant", "replies": [greeting]}

    history = session.get("chat_history", [])

    def _stream_and_save():
        full = []
        for chunk in ask_assistant_stream(text, history=history):
            full.append(chunk)
            yield chunk
        _append_history(session, text, "".join(full))
        save_session(session_id, session)

    return {"type": "stream", "generator": _stream_and_save()}







