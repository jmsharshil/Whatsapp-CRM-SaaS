import os
import logging
from openai import AzureOpenAI

logger = logging.getLogger(__name__)

# Initialize Azure OpenAI Client
try:
    openai_client = AzureOpenAI(
        api_key=os.environ.get("OPENAI_API_KEY", ""),
        api_version="2024-05-01-preview",
        azure_endpoint=os.environ.get("ENDPOINT_URL", "")
    )
except Exception as e:
    logger.error(f"Failed to initialize AzureOpenAI for MF bot: {e}")
    openai_client = None

SYSTEM_PROMPT = """You are a highly professional, knowledgeable, and India-based Mutual Fund Consultant.
Your primary role is to assist users with accurate information, insights, and details exclusively regarding Indian mutual funds and the Indian financial market.
Always maintain a courteous, professional, and objective tone.
If a user asks about mutual funds outside of India, politely redirect them by stating that your expertise is focused solely on the Indian mutual fund market.
When given scheme NAV data or context, use it accurately to inform your answers. Do not provide unverified financial advice; instead, offer data-driven insights and general educational information.

CRITICAL FORMATTING RULES FOR WHATSAPP:
1. Start with a direct, conversational opening line (e.g., "Here is what you need to know about this fund:"). Do NOT use overly warm greetings like "Welcome" or "Hello" since the user is already mid-conversation.
2. Provide exactly 3 short bullet points. Use highly relatable emojis (that match the context of the point) instead of boring hyphens. DO NOT overuse emojis—use exactly ONE emoji at the start of each bullet point (maximum 3 emojis in the entire response).
3. You MUST leave exactly one empty line (a blank line gap) between each bullet point to make it highly readable.
4. End with a polite and helpful closing line.
5. DO NOT use standard markdown formatting (like **this** or #). If you want to emphasize text, use WhatsApp bolding with a single asterisk (*like this*).
6. Keep the overall message concise, attractive, and highly readable on mobile screens."""

def ask_mf_assistant(user_text: str, context_str: str) -> str:
    """
    Calls the AzureOpenAI LLM to answer mutual fund questions based on scheme context.
    """
    if not openai_client:
        return "Sorry, the AI service is currently unavailable."
        
    prompt = f"User asked: {user_text}\n\nContext about the Mutual Fund Scheme:\n{context_str}\n\nProvide a concise and helpful answer based on this mutual fund context."
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ],
            max_tokens=200,
            temperature=0.3
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error("MF LLM Error: %s", str(e))
        return "Sorry, I couldn't process your request right now."
