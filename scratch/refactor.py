import re
import os

FILE_PATH = r"C:\Users\pranj\vscode\JMS_\CRM\gigatel_views.py"

with open(FILE_PATH, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Imports
if "from CRM.models import" in content and "Customer" not in content:
    content = content.replace("from CRM.models import ", "from CRM.models import Customer, Conversation, Message, ClientAccount, ")
elif "from CRM.models import" not in content:
    content = content.replace("from django.conf import settings", "from django.conf import settings\nfrom CRM.models import Customer, Conversation, Message, ClientAccount")

# 2. Add GigatelSession Wrapper
wrapper = """
class GigatelSession:
    def __init__(self, conversation):
        super().__setattr__('_conv', conversation)

    def save(self):
        self._conv.save()

    @property
    def mobile_number(self):
        return self._conv.customer.phone

    @property
    def state(self):
        return self._conv.bot_state

    @state.setter
    def state(self, value):
        self._conv.bot_state = value

    @property
    def updated_at(self):
        return self._conv.created_at

    def __getattr__(self, item):
        return self._conv.bot_metadata.get(item)

    def __setattr__(self, key, value):
        if key in ['_conv', 'state']:
            super().__setattr__(key, value)
        else:
            if not isinstance(self._conv.bot_metadata, dict):
                self._conv.bot_metadata = {}
            self._conv.bot_metadata[key] = value

class GigatelBot:
"""
content = content.replace("class GigatelBot:", wrapper)

# 3. Replace Session Get/Create
old_session_create = 'session, created = WhatsAppSession.objects.get_or_create(mobile_number=number)'
new_session_create = '''customer, _ = Customer.objects.get_or_create(phone=number, defaults={'name': number})
        client_account = ClientAccount.objects.filter(phone_number_id=os.environ.get("META_PHONE_NUMBER_ID")).first()
        conv, created = Conversation.objects.get_or_create(
            customer=customer, 
            client=client_account,
            defaults={'client_name': client_account.name if client_account else "Gigatel"}
        )
        session = GigatelSession(conv)
'''
content = content.replace(old_session_create, new_session_create)

# 4. Replace IN Message Create
old_in_msg = '''WhatsAppMessage.objects.create(
            mobile_number=number,
            whatsapp_message_id=msg_id,
            direction="IN",
            message_type=msg_type,
            body=body,
        )'''
new_in_msg = '''customer_obj, _ = Customer.objects.get_or_create(phone=number, defaults={'name': number})
        client_account_obj = ClientAccount.objects.filter(phone_number_id=os.environ.get("META_PHONE_NUMBER_ID")).first()
        conv_obj, _ = Conversation.objects.get_or_create(customer=customer_obj, client=client_account_obj)
        Message.objects.create(
            conversation=conv_obj,
            client=client_account_obj,
            customer=customer_obj,
            meta_message_id=msg_id,
            direction="inbound",
            message_type=msg_type if msg_type in ['text', 'template', 'image', 'document', 'video'] else 'text',
            content=body,
            status='delivered'
        )'''
content = content.replace(old_in_msg, new_in_msg)

# 5. Replace OUT Message Create
old_out_msg = '''WhatsAppMessage.objects.create(
            mobile_number=number,
            direction="OUT",
            message_type="template",
            body=text,
        )'''
new_out_msg = '''customer_obj, _ = Customer.objects.get_or_create(phone=number, defaults={'name': number})
        client_account_obj = ClientAccount.objects.filter(phone_number_id=os.environ.get("META_PHONE_NUMBER_ID")).first()
        conv_obj, _ = Conversation.objects.get_or_create(customer=customer_obj, client=client_account_obj)
        Message.objects.create(
            conversation=conv_obj,
            client=client_account_obj,
            customer=customer_obj,
            direction="outbound",
            message_type="template",
            content=text,
            status='sent'
        )'''
content = content.replace(old_out_msg, new_out_msg)

with open(FILE_PATH, "w", encoding="utf-8") as f:
    f.write(content)
print("Refactoring applied.")
