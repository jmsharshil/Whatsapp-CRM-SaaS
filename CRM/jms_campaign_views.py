import os
import json
import csv
import time
import uuid
import logging
import threading
import requests
from io import BytesIO, StringIO
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import render
from rest_framework import status
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

from rest_framework.permissions import BasePermission

# Constants for JMS
JMS_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "1160424227149252")
CAMPAIGN_HISTORY_DIR = "jms/campaigns/history/"
CSV_STORAGE_DIR = "jms/campaigns/csvs/"

class HasJMSPhoneToken(BasePermission):
    """
    Allows access only if the Authorization header contains the JMS Phone Number ID as a Bearer token.
    """
    def has_permission(self, request, view):
        auth = request.headers.get('Authorization', '')
        return auth == f"Bearer {JMS_PHONE_NUMBER_ID}"


def _jms_api_url() -> str:
    version = os.getenv("WHATSAPP_API_VERSION", "v20.0")
    return f"https://graph.facebook.com/{version}/{JMS_PHONE_NUMBER_ID}/messages"

def _jms_headers() -> dict:
    token = os.getenv("WHATSAPP_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

def _get_font(size):
    font_paths = [
        os.path.join(settings.BASE_DIR, 'CRM', 'fonts', 'Roboto-Regular.ttf'),
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\calibrib.ttf",
        r"C:\Windows\Fonts\calibri.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
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

def _generate_overlay_image(base_image_file, name: str, phone: str, overlay_enabled: bool, custom_x: int = None, custom_y: int = None, font_size: int = 40, text_color: str = "white") -> str:
    """
    Overlays text on image if enabled, uploads to blob, and returns public URL.
    """
    try:
        with base_image_file.open('rb') as f:
            img = Image.open(f).convert("RGB")
            
            if overlay_enabled:
                draw = ImageDraw.Draw(img)
                font = _get_font(font_size)
                phone_font = _get_font(int(font_size * 0.75))
                
                name_x = custom_x if custom_x is not None else img.width // 2
                name_y = custom_y if custom_y is not None else img.height - 150
                
                display_name = name.strip()
                draw.text((name_x, name_y), display_name, fill=text_color, font=font, anchor="mt")
                
                name_bbox = draw.textbbox((name_x, name_y), display_name, font=font, anchor="mt")
                phone_y = name_bbox[3] + 5 
                
                clean_phone = phone.lstrip('+')
                if len(clean_phone) == 10:
                    formatted_phone = f"+91 {clean_phone}"
                elif len(clean_phone) == 12 and clean_phone.startswith("91"):
                    formatted_phone = f"+91 {clean_phone[2:]}"
                else:
                    formatted_phone = clean_phone
                    
                draw.text((name_x, phone_y), formatted_phone, fill=text_color, font=phone_font, anchor="mt")
            
            buffer = BytesIO()
            img.save(buffer, format="JPEG")
            
            timestamp = int(time.time())
            clean_phone_for_filename = phone.lstrip('+') if phone else str(uuid.uuid4())[:8]
            generated_filename = f"jms/campaigns/generated/img_{clean_phone_for_filename}_{timestamp}.jpg"
            
            default_storage.save(generated_filename, ContentFile(buffer.getvalue()))
            file_url = default_storage.url(generated_filename)
            
            if file_url.startswith('/'):
                domain = getattr(settings, "DOMAIN_URL", "").rstrip('/')
                image_url = f"{domain}{file_url}"
            else:
                image_url = file_url
                
            return image_url
            
    except Exception as e:
        logger.error(f"[JMS Campaign] Error generating image: {e}")
        return ""

def _send_image_template(to: str, template_name: str, image_url: str, name: str):
    if not to: return False
    
    clean_to = f"91{to.lstrip('+')}" if len(to.lstrip('+')) == 10 else to.lstrip('+')
    
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": clean_to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": "en"},
            "components": [
                {
                    "type": "header",
                    "parameters": [
                        {
                            "type": "image",
                            "image": {"link": image_url}
                        }
                    ]
                },
                {
                    "type": "body",
                    "parameters": [
                        {
                            "type": "text",
                            "text": name if name else "Customer"
                        }
                    ]
                }
            ]
        }
    }
    
    try:
        res = requests.post(_jms_api_url(), json=payload, headers=_jms_headers(), timeout=15)
        if res.status_code >= 300:
            logger.error(f"[JMS Campaign] Error sending template to {clean_to}: {res.text}")
            return {"status": "failed", "error": res.text}
        return {"status": "success", "message_id": res.json().get("messages", [{}])[0].get("id")}
    except Exception as e:
        logger.error(f"[JMS Campaign] Exception sending template to {clean_to}: {e}")
        return {"status": "error", "error": str(e)}

def _append_to_campaign_history(campaign_id: str, log_entry: dict):
    history_filename = f"{CAMPAIGN_HISTORY_DIR}{campaign_id}.json"
    
    try:
        history_data = []
        if default_storage.exists(history_filename):
            with default_storage.open(history_filename, 'r') as f:
                content = f.read()
                if isinstance(content, bytes):
                    content = content.decode('utf-8')
                if content:
                    history_data = json.loads(content)
        
        history_data.append(log_entry)
        
        if default_storage.exists(history_filename):
            default_storage.delete(history_filename)
            
        default_storage.save(history_filename, ContentFile(json.dumps(history_data, indent=2).encode('utf-8')))
    except Exception as e:
        logger.error(f"[JMS Campaign] Failed to append history for {campaign_id}: {e}")

def _process_campaign(campaign_id: str, csv_path: str, base_image_path: str, template_name: str, overlay_enabled: bool, text_x: int = None, text_y: int = None, font_size: int = 40, text_color: str = "white"):
    logger.info(f"[JMS Campaign] Started processing campaign: {campaign_id}")
    
    try:
        with default_storage.open(csv_path, 'r') as csv_file:
            content = csv_file.read()
            if isinstance(content, bytes):
                content = content.decode('utf-8-sig')
            
            reader = csv.DictReader(StringIO(content))
            
            for row in reader:
                # Find phone key dynamically
                phone_keys = [k for k in row.keys() if k and ("phone" in k.lower() or "number" in k.lower() or "contact" in k.lower())]
                phone = row[phone_keys[0]].strip() if phone_keys else ""
                
                # Find name key dynamically
                name_keys = [k for k in row.keys() if k and "name" in k.lower()]
                name = row[name_keys[0]].strip() if name_keys else ""
                
                if not phone:
                    _append_to_campaign_history(campaign_id, {"phone": phone, "name": name, "status": "skipped", "reason": "No phone number"})
                    continue
                
                with default_storage.open(base_image_path, 'rb') as img_file:
                    image_url = _generate_overlay_image(img_file, name, phone, overlay_enabled, text_x, text_y, font_size, text_color)
                
                if not image_url:
                    _append_to_campaign_history(campaign_id, {"phone": phone, "name": name, "status": "failed", "reason": "Failed to generate/upload image"})
                    continue
                
                result = _send_image_template(phone, template_name, image_url, name)
                
                log_entry = {
                    "phone": phone,
                    "name": name,
                    "timestamp": time.time(),
                    "image_url": image_url,
                    "result": result
                }
                _append_to_campaign_history(campaign_id, log_entry)
                
                time.sleep(0.5)
                
    except Exception as e:
        logger.error(f"[JMS Campaign] Fatal error in campaign {campaign_id}: {e}")
        _append_to_campaign_history(campaign_id, {"status": "fatal_error", "error": str(e), "timestamp": time.time()})
    
    logger.info(f"[JMS Campaign] Completed processing campaign: {campaign_id}")
    _append_to_campaign_history(campaign_id, {"system_event": "campaign_completed", "timestamp": time.time()})

class JMSCampaignUploadView(APIView):
    authentication_classes = []
    permission_classes = [HasJMSPhoneToken]
    
    def post(self, request, *args, **kwargs):
        csv_file = request.FILES.get('csv_file')
        existing_csv = request.data.get('existing_csv')
        base_image = request.FILES.get('base_image')
        template_name = request.data.get('template_name', 'jms_image_campaign')
        overlay_enabled = str(request.data.get('overlay_enabled', 'false')).lower() == 'true'
        
        text_x = request.data.get('text_x')
        text_y = request.data.get('text_y')
        font_size = request.data.get('font_size')
        text_color = request.data.get('text_color', 'black')

        try: text_x = int(text_x) if text_x is not None else None
        except ValueError: text_x = None
        try: text_y = int(text_y) if text_y is not None else None
        except ValueError: text_y = None
        try: font_size = int(font_size) if font_size is not None else 40
        except ValueError: font_size = 40
        
        if not base_image or not template_name:
            return Response({"error": "base_image and template_name are required."}, status=status.HTTP_400_BAD_REQUEST)
        
        if not csv_file and not existing_csv:
            return Response({"error": "Either csv_file or existing_csv is required."}, status=status.HTTP_400_BAD_REQUEST)
        
        campaign_id = f"cmp_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        
        if csv_file:
            csv_path = f"jms/campaigns/uploads/{campaign_id}_{csv_file.name}"
            default_storage.save(csv_path, csv_file)
        else:
            csv_path = f"{CSV_STORAGE_DIR}{existing_csv}"
            if not default_storage.exists(csv_path):
                return Response({"error": f"Existing CSV {existing_csv} not found."}, status=status.HTTP_404_NOT_FOUND)
            
        image_path = f"jms/campaigns/uploads/{campaign_id}_{base_image.name}"
        default_storage.save(image_path, base_image)
        
        threading.Thread(
            target=_process_campaign, 
            args=(campaign_id, csv_path, image_path, template_name, overlay_enabled, text_x, text_y, font_size, text_color),
            daemon=True
        ).start()
        
        return Response({
            "status": "processing",
            "message": "Campaign started processing. Please wait...",
            "campaign_id": campaign_id
        }, status=status.HTTP_202_ACCEPTED)

def jms_campaign_ui_view(request):
    """UI view for JMS Campaign Uploader."""
    return render(request, "campaign_uploader.html", {"jms_token": JMS_PHONE_NUMBER_ID})

class JMSCampaignHistoryView(APIView):
    authentication_classes = []
    permission_classes = [HasJMSPhoneToken]

    def get(self, request, campaign_id, *args, **kwargs):
        history_filename = f"{CAMPAIGN_HISTORY_DIR}{campaign_id}.json"
        
        if not default_storage.exists(history_filename):
            return Response({"error": "Campaign history not found."}, status=status.HTTP_404_NOT_FOUND)
            
        try:
            with default_storage.open(history_filename, 'r') as f:
                content = f.read()
                data = json.loads(content) if content else []
            return Response({"campaign_id": campaign_id, "history": data})
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class JMSCampaignListView(APIView):
    authentication_classes = []
    permission_classes = [HasJMSPhoneToken]

    def get(self, request, *args, **kwargs):
        try:
            try:
                dirs, files = default_storage.listdir(CAMPAIGN_HISTORY_DIR)
            except FileNotFoundError:
                files = []
            except Exception:
                files = []
                
            campaigns = []
            for f in files:
                if f.endswith('.json'):
                    base_name = os.path.basename(f)
                    campaign_id = base_name.replace('.json', '')
                    campaigns.append({"campaign_id": campaign_id})
            
            campaigns.sort(key=lambda x: x['campaign_id'], reverse=True)
            return Response({"campaigns": campaigns})
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

class JMSCSVUploadView(APIView):
    authentication_classes = []
    permission_classes = [HasJMSPhoneToken]
    
    def post(self, request, *args, **kwargs):
        csv_file = request.FILES.get('csv_file')
        if not csv_file:
            return Response({"error": "csv_file is required."}, status=status.HTTP_400_BAD_REQUEST)
            
        file_name = csv_file.name
        # Keep original name but add timestamp if it exists to avoid overwriting issues,
        # actually for simplicity let's just use original name.
        csv_path = f"{CSV_STORAGE_DIR}{file_name}"
        
        if default_storage.exists(csv_path):
            default_storage.delete(csv_path)
            
        default_storage.save(csv_path, csv_file)
        
        return Response({
            "status": "success",
            "message": f"CSV {file_name} uploaded successfully.",
            "file_name": file_name
        }, status=status.HTTP_201_CREATED)

class JMSCSVListView(APIView):
    authentication_classes = []
    permission_classes = [HasJMSPhoneToken]
    
    def get(self, request, *args, **kwargs):
        try:
            try:
                dirs, files = default_storage.listdir(CSV_STORAGE_DIR)
            except FileNotFoundError:
                files = []
            except Exception:
                files = []
                
            csvs = []
            for f in files:
                if f.endswith('.csv'):
                    base_name = os.path.basename(f)
                    csvs.append({"file_name": base_name})
            
            csvs.sort(key=lambda x: x['file_name'])
            return Response({"csvs": csvs})
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
