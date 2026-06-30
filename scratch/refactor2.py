import re

JMS_VIEWS = r"c:\Users\pranj\vscode\JMS_\CRM\jmschatagents_views.py"
WEBHOOK_VIEWS = r"c:\Users\pranj\vscode\JMS_\CRM\META\webhook_views.py"

with open(JMS_VIEWS, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Update _jms_handle_message signature
content = content.replace(
    "def _jms_handle_message(phone: str, text: str):",
    "def _jms_handle_message(phone: str, text: str, phone_number_id: str = None):"
)
# Update save_message calls in _jms_handle_message
content = content.replace(
    "save_message(phone=phone, content=text.strip(), reply_of=None)",
    "save_message(phone=phone, content=text.strip(), reply_of=None, phone_number_id=phone_number_id)"
)
content = content.replace(
    "save_message(phone=phone, content=text, reply_of=None)",
    "save_message(phone=phone, content=text, reply_of=None, phone_number_id=phone_number_id)"
)
content = content.replace(
    "save_message(phone=phone, content=raw_text, reply_of=None)",
    "save_message(phone=phone, content=raw_text, reply_of=None, phone_number_id=phone_number_id)"
)

# 2. Update _handle_wa_bot signature
content = content.replace(
    "def _handle_wa_bot(phone: str, text: str) -> bool:",
    "def _handle_wa_bot(phone: str, text: str, phone_number_id: str = None) -> bool:"
)
# Update save_message calls in _handle_wa_bot if any (but I think there aren't directly, let's check)

with open(JMS_VIEWS, "w", encoding="utf-8") as f:
    f.write(content)

with open(WEBHOOK_VIEWS, "r", encoding="utf-8") as f:
    content = f.read()

# 3. Update webhook dispatcher calls
content = content.replace(
    "_handle_wa_bot(raw_phone, text)",
    "_handle_wa_bot(raw_phone, text, phone_number_id)"
)
content = content.replace(
    "_jms_handle_message(raw_phone, text)",
    "_jms_handle_message(raw_phone, text, phone_number_id)"
)

with open(WEBHOOK_VIEWS, "w", encoding="utf-8") as f:
    f.write(content)

print("Refactor complete.")
