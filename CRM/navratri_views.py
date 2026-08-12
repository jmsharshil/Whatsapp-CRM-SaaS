import os
import requests
import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.conf import settings
from CRM.models import NavratriRegistration, Customer, Conversation, Message

logger = logging.getLogger(__name__)

NAVRATRI_PHONE_NUMBER_ID = os.getenv("NAVRATRI_PHONE_NUMBER_ID", "1160424227149252")

class NavratriRegistrationAPIView(APIView):
    def get(self, request):
        registrations = NavratriRegistration.objects.all().values()
        return Response({"status": "success", "data": list(registrations)}, status=status.HTTP_200_OK)

    def post(self, request):
        data = request.data
        try:
            pass_type = data.get('pass_type', '')
            select_date = data.get('select_date', '')
            
            if pass_type == '9_day_pass':
                select_date = '11 Oct 2026 - 19 Oct 2026'
                
            reg = NavratriRegistration.objects.create(
                name=data.get('name', ''),
                phone_number=data.get('phone_number', ''),
                email=data.get('email', ''),
                address=data.get('address', ''),
                aadhar_card_number=data.get('aadhar_card_number', ''),
                guardian_relation=data.get('guardian_relation', ''),
                guardian_name=data.get('guardian_name', ''),
                guardian_phone_number=data.get('guardian_phone_number', ''),
                guardian_email=data.get('guardian_email', ''),
                pass_type=pass_type,
                select_date=select_date,
                pass_quantity=int(data.get('pass_quantity', 1))
            )
            
            # Send WhatsApp Template Message
            self.send_whatsapp_template(reg.phone_number, reg.name)
            
            # Sync to Google Sheets
            self.sync_to_google_sheet(reg)
            
            # Schedule the pass generation and sending 5 minutes (300 seconds) later
            import threading
            threading.Timer(300, self.send_navratri_pass_task, args=[reg.id]).start()
            
            return Response({"status": "success", "message": "Registration successful", "id": reg.id}, status=status.HTTP_201_CREATED)
        except Exception as e:
            logger.error(f"Navratri Registration Error: {str(e)}")
            return Response({"status": "error", "message": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    def send_whatsapp_template(self, phone_number, name):
        # We try to get token from settings, otherwise from env
        token = getattr(settings, "META_PERMANENT_TOKEN", os.getenv("META_ACCESS_TOKEN", ""))
        url = f"https://graph.facebook.com/v22.0/{NAVRATRI_PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        # Make sure phone number has country code, assume 91 if not present for India
        if not phone_number.startswith("91") and len(phone_number) == 10:
            phone_number = "91" + phone_number

        payload = {
            "messaging_product": "whatsapp",
            "to": phone_number,
            "type": "template",
            "template": {
                "name": "registration_success",
                "language": {
                    "code": "en"
                }
            }
        }
        
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=10)
            if r.status_code not in (200, 201):
                logger.error(f"Meta API error for Navratri Registration: {r.status_code} - {r.text}")
            else:
                logger.info(f"WhatsApp template sent to {phone_number} successfully.")
                
                # Setup 2-way conversation tracking
                meta_id = ""
                try:
                    resp_data = r.json()
                    if "messages" in resp_data and len(resp_data["messages"]) > 0:
                        meta_id = resp_data["messages"][0].get("id", "")
                except Exception:
                    pass
                
                customer, _ = Customer.objects.get_or_create(
                    phone=phone_number,
                    defaults={"name": name}
                )
                
                conversation, _ = Conversation.objects.get_or_create(
                    customer=customer,
                    phone_number_id=NAVRATRI_PHONE_NUMBER_ID,
                    defaults={"bot_state": "NAVRATRI"}
                )
                # Ensure the conversation has the correct state if returning
                if conversation.bot_state != "NAVRATRI":
                    conversation.bot_state = "NAVRATRI"
                    conversation.save(update_fields=["bot_state"])
                
                Message.objects.create(
                    conversation=conversation,
                    customer=customer,
                    direction="outbound",
                    message_type="template",
                    content=f"[Template: registration_success] Sent to {name}",
                    status="sent",
                    meta_message_id=meta_id,
                    template_name="registration_success"
                )
                
        except Exception as e:
            logger.error(f"Failed to send WhatsApp message: {str(e)}")

    def sync_to_google_sheet(self, reg):
        # The Web App URL you will get from Google Apps Script
        # Please replace this with your actual Web App URL
        WEB_APP_URL = os.getenv("NAVRATRI_SHEETS_WEB_APP_URL", "https://script.google.com/macros/s/AKfycbxMptmG34HCaDqM7oeMQA8emyyqnbhPNvIc_Tn3h_KRNeJC1OywuJmCqtJUF9XiDa-5/exec")
        
        if WEB_APP_URL == "REPLACE_WITH_YOUR_WEB_APP_URL":
            logger.error("Web App URL for Google Sheets is not configured.")
            return

        payload = {
            "created_at": reg.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "name": reg.name,
            "phone_number": reg.phone_number,
            "email": reg.email or "",
            "address": reg.address or "",
            "aadhar_card_number": reg.aadhar_card_number or "",
            "guardian_relation": reg.guardian_relation or "",
            "guardian_name": reg.guardian_name or "",
            "guardian_phone_number": reg.guardian_phone_number or "",
            "guardian_email": reg.guardian_email or "",
            "pass_type": reg.pass_type or "",
            "select_date": reg.select_date or "",
            "pass_quantity": reg.pass_quantity
        }
        
        try:
            r = requests.post(WEB_APP_URL, json=payload, timeout=15)
            if r.status_code == 200:
                logger.info(f"Successfully synced Navratri registration for {reg.phone_number} to Google Sheets via Web App.")
            else:
                logger.error(f"Failed to sync to Google Sheets via Web App. Status: {r.status_code} - {r.text}")
        except Exception as e:
            logger.error(f"Error calling Google Sheets Web App: {str(e)}")

    def send_navratri_pass_task(self, registration_id):
        try:
            reg = NavratriRegistration.objects.get(id=registration_id)
            pass_content = self.generate_navratri_pass(reg)
            if not pass_content:
                logger.error(f"Failed to generate pass for registration {registration_id}")
                return
            
            # Upload media to Meta
            token = getattr(settings, "META_PERMANENT_TOKEN", os.getenv("META_ACCESS_TOKEN", ""))
            upload_url = f"https://graph.facebook.com/v22.0/{NAVRATRI_PHONE_NUMBER_ID}/media"
            headers = {"Authorization": f"Bearer {token}"}
            
            files = {
                'file': ('pass.jpg', pass_content, 'image/jpeg')
            }
            data = {
                'messaging_product': 'whatsapp',
                'type': 'image/jpeg'
            }
            upload_resp = requests.post(upload_url, headers=headers, data=data, files=files)
                
            if upload_resp.status_code == 200:
                media_id = upload_resp.json().get('id')
                
                # Send Media Message
                send_url = f"https://graph.facebook.com/v22.0/{NAVRATRI_PHONE_NUMBER_ID}/messages"
                send_headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"
                }
                
                phone_number = reg.phone_number
                if not phone_number.startswith("91") and len(phone_number) == 10:
                    phone_number = "91" + phone_number
                    
                send_payload = {
                    "messaging_product": "whatsapp",
                    "to": phone_number,
                    "type": "template",
                    "template": {
                        "name": "navratri_entry_pass",
                        "language": {
                            "code": "en"
                        },
                        "components": [
                            {
                                "type": "header",
                                "parameters": [
                                    {
                                        "type": "image",
                                        "image": {
                                            "id": media_id
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                }
                
                send_resp = requests.post(send_url, json=send_payload, headers=send_headers)
                if send_resp.status_code in [200, 201]:
                    logger.info(f"Navratri pass sent successfully to {phone_number}")
                else:
                    logger.error(f"Failed to send pass message to {phone_number}: {send_resp.text}")
                    
            else:
                logger.error(f"Failed to upload media to Meta: {upload_resp.text}")
                
        except Exception as e:
            logger.error(f"Error in send_navratri_pass_task: {str(e)}")

    def generate_navratri_pass(self, reg):
        try:
            import qrcode
            from PIL import Image
            import os
            
            # Generate QR Code with user details
            qr_data = f"Name: {reg.name}\nPass Type: {reg.pass_type.replace('_', ' ').title()}\nDate: {reg.select_date}\nPhone: {reg.phone_number}\nReg ID: {reg.id}"
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=10,
                border=2,
            )
            qr.add_data(qr_data)
            qr.make(fit=True)
            qr_img = qr.make_image(fill_color="black", back_color="white")
            
            # Load Background Template
            bg_path = os.path.join(settings.BASE_DIR, 'CRM', 'static', 'navratri_pass_bg.jpg')
            if not os.path.exists(bg_path):
                logger.error(f"Background template not found at {bg_path}")
                return None
                
            bg_img = Image.open(bg_path)
            
            # Ensure bg_img is in RGB mode so we can save it as JPEG
            if bg_img.mode != 'RGB':
                bg_img = bg_img.convert('RGB')
            
            # Calculate coordinates to place QR code in the center (assuming standard size like the template)
            qr_width, qr_height = qr_img.size
            
            # Resize QR to fit well on the template. Given standard portrait dimensions, 450x450 usually fits well.
            qr_size = 730
            qr_img = qr_img.resize((qr_size, qr_size))
            
            bg_w, bg_h = bg_img.size
            # Assuming center placement for the QR code
            offset_w = (bg_w - qr_size) // 2
            offset_h = (bg_h - qr_size) // 2 + 50 # Slightly lower than exact center to align with the design's placeholder box
            
            bg_img.paste(qr_img, (offset_w, offset_h))
            
            # Save to memory buffer
            from io import BytesIO
            from django.core.files.base import ContentFile
            from django.core.files.storage import default_storage
            
            buffer = BytesIO()
            bg_img.save(buffer, format="JPEG", quality=95)
            image_content = buffer.getvalue()
            
            # Save to Azure Blob Storage via default_storage
            file_name = f'passes/pass_{reg.id}.jpg'
            if default_storage.exists(file_name):
                default_storage.delete(file_name)
            default_storage.save(file_name, ContentFile(image_content))
            
            return image_content
        except Exception as e:
            logger.error(f"Error generating pass image: {str(e)}")
            return None
