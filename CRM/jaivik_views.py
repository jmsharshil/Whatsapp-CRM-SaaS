import os
import requests
import logging
import threading
from django.conf import settings
from .models import AvantikaContact, AvantikaTemplate
from django.db import models
from .jmschatagents_views import SafeCache

logger = logging.getLogger(__name__)

avantika_sessions = SafeCache(prefix="avantika_sess:", default_ttl=60 * 60)
AVANTIKA_SESSION_TTL = 60 * 60


AVANTIKA_BOT_TRIGGER = "avantika"
JAIVIK_PHONE_NUMBER_ID = "1232951769906831"

def _jaivik_api_url() -> str:
    version = os.getenv("WHATSAPP_API_VERSION", "v22.0")
    return f"https://graph.facebook.com/{version}/{JAIVIK_PHONE_NUMBER_ID}/messages"

def _jaivik_headers() -> dict:
    token = os.getenv("WHATSAPP_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

def _jaivik_send_payload(payload: dict):
    def _send():
        try:
            res = requests.post(_jaivik_api_url(), json=payload, headers=_jaivik_headers(), timeout=15)
            if res.status_code >= 300:
                logger.error(f"[Jaivik] Error sending message: {res.text}")
        except Exception as e:
            logger.error(f"[Jaivik] Exception sending message: {e}")
    
    threading.Thread(target=_send, daemon=True).start()


def jaivik_send_text(to: str, text: str):
    if not to: return
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": f"91{to.lstrip('+')}" if len(to.lstrip('+')) == 10 else to.lstrip('+'),
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    _jaivik_send_payload(payload)


def jaivik_send_image(to: str, image_url: str, caption: str = ""):
    if not to: return
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": f"91{to.lstrip('+')}" if len(to.lstrip('+')) == 10 else to.lstrip('+'),
        "type": "image",
        "image": {"link": image_url, "caption": caption},
    }
    _jaivik_send_payload(payload)


def jaivik_send_template(to: str, template_name: str, language_code: str = "en", components: list = None):
    if not to: return
    template_data = {
        "name": template_name,
        "language": {"code": language_code},
    }
    if components:
        template_data["components"] = components

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": f"91{to.lstrip('+')}" if len(to.lstrip('+')) == 10 else to.lstrip('+'),
        "type": "template",
        "template": template_data,
    }
    _jaivik_send_payload(payload)


def _handle_avantika_bot(phone: str, text: str, phone_number_id: str = None, inbound_msg_id: int = None, client_name: str = "") -> bool:
    """Avantika bot logic using Jaivik views.
    As per user request, this bot should NOT auto-reply to incoming messages.
    The inbound message is already saved by the unified webhook.
    """
    return True


def generate_avantika_image(contact, active_template) -> str:
    from PIL import Image, ImageDraw, ImageFont
    from io import BytesIO
    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage
    
    clean_phone = contact.phone.lstrip('+')
    
    with active_template.base_image.open('rb') as f:
        img = Image.open(f).convert("RGB")
        draw = ImageDraw.Draw(img)
        
        def get_specific_font(font_filename, size):
            from django.conf import settings
            import os
            primary_path = os.path.join(settings.BASE_DIR, 'CRM', 'fonts', font_filename)
            font_paths = [
                primary_path,
                os.path.join(settings.BASE_DIR, 'CRM', 'fonts', 'Roboto-Regular.ttf'),
                r"C:\Windows\Fonts\segoeuib.ttf",
                r"C:\Windows\Fonts\segoeui.ttf",
                r"C:\Windows\Fonts\calibrib.ttf",
                r"C:\Windows\Fonts\calibri.ttf",
                r"C:\Windows\Fonts\arialbd.ttf",
                r"C:\Windows\Fonts\arial.ttf",
                "arial.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
                "/usr/share/fonts/truetype/freefont/FreeSans.ttf"
            ]
            for path in font_paths:
                try:
                    return ImageFont.truetype(path, size)
                except IOError:
                    continue
            return ImageFont.load_default()
            
        name_font = get_specific_font('Montserrat-ExtraBold.ttf', active_template.font_size)
        
        # Fallback to calculated defaults if DB doesn't have the new fields yet
        p_font_size = getattr(active_template, 'phone_font_size', max(12, int(active_template.font_size * 0.75)))
        a_font_size = getattr(active_template, 'address_font_size', max(12, int(active_template.font_size * 0.75)))
        
        phone_font = get_specific_font('Montserrat-SemiBold.ttf', p_font_size)
        address_font = get_specific_font('Montserrat-Medium.ttf', a_font_size)
        
        display_name = contact.name.strip()
        
        draw.text((active_template.name_x, active_template.name_y), display_name, fill=active_template.text_color, font=name_font, anchor="mt")
        
        name_bbox = draw.textbbox((active_template.name_x, active_template.name_y), display_name, font=name_font, anchor="mt")
        phone_y = name_bbox[3] + 12
        
        if len(clean_phone) == 10:
            formatted_phone = f"+91 {clean_phone}"
        elif len(clean_phone) == 12 and clean_phone.startswith("91"):
            formatted_phone = f"+91 {clean_phone[2:]}"
        else:
            formatted_phone = clean_phone
            
        draw.text((active_template.name_x, phone_y), formatted_phone, fill=active_template.text_color, font=phone_font, anchor="mt")
        
        address = getattr(contact, "address", "")
        if address:
            phone_bbox = draw.textbbox((active_template.name_x, phone_y), formatted_phone, font=phone_font, anchor="mt")
            address_y = phone_bbox[3] + 12
            draw.text((active_template.name_x, address_y), address.strip(), fill=active_template.text_color, font=address_font, anchor="mt")
        
        buffer = BytesIO()
        img.save(buffer, format="JPEG")
        
        import time
        timestamp = int(time.time())
        generated_filename = f"avantika/generated/gen_{clean_phone}_{timestamp}.jpg"
        
        # We don't necessarily need to delete the exact old one since the timestamp is unique now,
        # but if we wanted to clean up we could list and delete old files. 
        # For now, just save the new one to avoid Meta cache.
        default_storage.save(generated_filename, ContentFile(buffer.getvalue()))
        
        file_url = default_storage.url(generated_filename)
        
        if file_url.startswith('/'):
            domain = getattr(settings, "DOMAIN_URL", "https://whatsappcrmsaas-emdke9dnb4f8bne6.centralindia-01.azurewebsites.net").rstrip('/')
            image_url = f"{domain}{file_url}"
        else:
            image_url = file_url
            
    return image_url
