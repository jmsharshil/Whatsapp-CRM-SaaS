import os
import random
import datetime
from django.core.mail import send_mail, EmailMultiAlternatives
from django.core.cache import cache
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from rest_framework import status, permissions
from rest_framework_simplejwt.tokens import RefreshToken

from django.contrib.auth import get_user_model, authenticate
from django.shortcuts import get_object_or_404

from .models import *
from .serializers import*
from django.conf import settings
from django.db.models import Count, Max
from django.db.models.functions import TruncDate
from django.core.paginator import Paginator
from django.db.models import Q, Prefetch

User = get_user_model()

import requests

import logging
import httpx

logger = logging.getLogger(__name__)



class SyncTechProviderWabaView(APIView):

    permission_classes = [IsAuthenticated]

    def post(self, request):

        user = request.user

        email = user.email.lower()

        # ONLY TECH PROVIDER
        if (
            email != "pranjalvejani2111@gmail.com"
            and not email.endswith("@jmstech.co")
        ):
            return Response({
                "success": False,
                "message": "Not allowed"
            }, status=403)

        org = Organization.objects.filter(
            owner=user
        ).first()

        # AUTO CREATE FOR TECH PROVIDER
        if not org:

            org = Organization.objects.create(
                owner=user,
                name="JMS TechNova",
                email=user.email,
                website="https://jmstechnova.com"
            )

        BUSINESS_ID = settings.META_BUSINESS_ID
        ACCESS_TOKEN = settings.META_PERMANENT_TOKEN

        # FETCH WABA
        url = f"https://graph.facebook.com/v22.0/{BUSINESS_ID}/owned_whatsapp_business_accounts"

        response = requests.get(
            url,
            params={
                "access_token": ACCESS_TOKEN
            }
        )

        data = response.json()

        if "data" not in data or not data["data"]:
            return Response({
                "success": False,
                "message": "No WABA found",
                "meta_response": data
            }, status=400)

        waba_id = data["data"][0]["id"]

        # FETCH PHONE NUMBER
        phone_url = f"https://graph.facebook.com/v22.0/{waba_id}/phone_numbers"

        phone_response = requests.get(
            phone_url,
            params={
                "access_token": ACCESS_TOKEN
            }
        )

        phone_data = phone_response.json()

        phone_number_id = None

        if phone_data.get("data"):
            phone_number_id = phone_data["data"][0]["id"]

        # SAVE / UPDATE
        WABAAccount.objects.update_or_create(
            organization=org,
            defaults={
                "waba_id": waba_id,
                "phone_number_id": phone_number_id,
                "business_id": BUSINESS_ID,
                "access_token": ACCESS_TOKEN,
                "status": "connected",       # ← was missing; is_connected() checks this
            }
        )

        return Response({
            "success": True,
            "waba_id": waba_id,
            "phone_number_id": phone_number_id,
        })

class SignUpView(APIView):
    """
    POST /api/signup/
    Body: { full_name, email, name, website? }

    Creates the User + Organization in one request.
    After this the user signs in via OTP (/api/send-code/ + /api/verify-code/).
    If waba_connected is False after login, frontend redirects to /setup (WhatsApp connect).
    """
    def post(self, request):
        full_name = request.data.get("full_name", "").strip()
        email     = request.data.get("email",     "").strip().lower()
        password  = request.data.get("password",  "")
        name      = request.data.get("name",      "").strip()
        website   = request.data.get("website",   "").strip() or None

        if not full_name:
            return Response({"message": "Your name is required"}, status=400)

        if not email:
            return Response({"message": "Email is required"}, status=400)
            
        if not password:
            return Response({"message": "Password/PIN is required"}, status=400)

        if not name:
            return Response({"message": "Organisation name is required"}, status=400)

        if User.objects.filter(email=email).exists():
            return Response({"message": "User already exists"}, status=400)

        # Create user with full_name and password
        user = User.objects.create_user(email=email, password=password)
        user.full_name = full_name
        user.save(update_fields=["full_name"])

        # Create organisation immediately (no separate /setup step needed)
        Organization.objects.create(
            owner=user,
            name=name,
            email=email,
            website=website,
        )

        return Response({"message": "Account created successfully"}, status=201)
# ─────────────────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────────────────

class LoginView(APIView):
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data['email']
        password = serializer.validated_data['password']

        user = authenticate(email=email, password=password)

        if not user:
            return Response({'error': 'Invalid email or PIN'}, status=400)

        refresh = RefreshToken.for_user(user)

        # Determine role + org (same logic as UserMeView)
        role, org_name, org_id, has_org, waba_connected = _resolve_user_profile(user)

        return Response({
            'refresh': str(refresh),
            'access': str(refresh.access_token),
            'role': role,
            'organization': org_name,
            'org_id': org_id,
            'has_organization': has_org,
            'waba_connected': waba_connected,
        })

# ─────────────────────────────────────────────────────────────────────────────
# Reset PIN
# ─────────────────────────────────────────────────────────────────────────────

class ResetPinSendCodeView(APIView):
    def post(self, request):
        email = request.data.get("email", "").strip()
        if not email:
            return Response({"error": "Email is required"}, status=400)
            
        user = User.objects.filter(email__iexact=email).first()
        if not user:
            return Response({"error": "No account found with this email"}, status=404)
            
        code = str(random.randint(100000, 999999))
        formatted_code = " ".join(list(code))
        cache.set(f"reset_{email}", code, 300) # Valid for 5 mins
        current_year = datetime.datetime.now().year
        
        html_content = f"""
        <div style="font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f4f7fb; padding: 50px 20px; text-align: center;">
            <div style="max-width: 500px; margin: 0 auto; background-color: #ffffff; padding: 40px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.05); text-align: left;">
                
                <div style="text-align: center; margin-bottom: 30px;">
                    <img src="https://hrmsknowcraftstorage.blob.core.windows.net/media/JMS.png" alt="JMS Logo" style="max-width: 150px; height: auto;" />
                </div>

                <h2 style="color: #1a202c; font-size: 22px; font-weight: 700; margin-top: 0; margin-bottom: 20px;">Verify Your Email</h2>
                <p style="color: #4a5568; font-size: 15px; margin-bottom: 15px;">Hello,</p>
                <p style="color: #4a5568; font-size: 15px; margin-bottom: 30px; line-height: 1.5;">Use the verification code below to securely reset your PIN.</p>

                <div style="background-color: #f0f7ff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 20px; text-align: center; margin-bottom: 30px;">
                    <span style="font-size: 30px; font-weight: 700; color: #2b6cb0; letter-spacing: 8px;">{formatted_code}</span>
                </div>

                <p style="color: #718096; font-size: 13px; margin-bottom: 15px;">This verification code will expire in <strong>5 minutes</strong>.</p>
                <p style="color: #718096; font-size: 13px; margin-bottom: 0;">If you did not request this email, you can safely ignore it.</p>
            </div>
            
            <div style="margin-top: 30px; text-align: center;">
                <p style="color: #a0aec0; font-size: 12px; margin: 5px 0;">&copy; {current_year} JMS TechNova</p>
                <p style="color: #a0aec0; font-size: 12px; margin: 5px 0;">Secure Authentication System</p>
            </div>
        </div>
        """
        
        try:
            send_mail(
                subject="Reset Your PIN - JMS TechNova",
                message=f"Your reset code is: {code}. It is valid for 5 minutes.",
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[email],
                fail_silently=False,
                html_message=html_content
            )
        except Exception as e:
            print(f"Failed to send email: {e}")
            
        return Response({"message": "If an account exists, a reset code has been sent."}, status=200)


class ResetPinVerifyView(APIView):
    def post(self, request):
        email = request.data.get("email", "").strip()
        code = str(request.data.get("code", "")).replace(" ", "").strip()
        new_password = request.data.get("new_password")
        
        if not all([email, code, new_password]):
            return Response({"error": "Email, code, and new PIN are required"}, status=400)
            
        cached_code = cache.get(f"reset_{email}")
        
        if not cached_code or str(cached_code) != str(code):
            return Response({"error": "Invalid or expired reset code"}, status=400)
            
        user = User.objects.filter(email__iexact=email).first()
        if not user:
            return Response({"error": "User not found"}, status=404)
            
        user.set_password(new_password)
        user.save()
        
        cache.delete(f"reset_{email}")
        
        return Response({"message": "PIN reset successfully"}, status=200)

# ─────────────────────────────────────────────────────────────────────────────
# NEW: /api/user/me/  — call this on app load to rehydrate Redux state
# ─────────────────────────────────────────────────────────────────────────────

class UserMeView(APIView):
    """
    GET /api/user/me/

    Returns the authenticated user's full profile including organization
    details (email, website, owner info) so the Team page can render
    dynamically without a separate /api/organization/ call.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        role, org_name, org_id, has_org, waba_connected = _resolve_user_profile(user)

        # ── Resolve org object to get full details ────────────────────────────
        org = None
        org_email   = None
        org_website = None
        owner_name  = None
        owner_email = None
        waba_status = "not_connected"
        waba_name = None
        waba_id = None
        phone_number = None
        phone_number_id = None

        if hasattr(user, "organization") and user.organization is not None:
            org = user.organization
        elif hasattr(user, "membership") and user.membership is not None:
            org = user.membership.organization

        if org:
            org_email   = org.email   if hasattr(org, "email")   else None
            org_website = org.website if hasattr(org, "website") else None
            # owner is the User who created the org (OneToOne → user.organization)
            try:
                waba = org.waba_account

                waba_status = waba.status
                waba_name = waba.waba_name
                waba_id = waba.waba_id
                phone_number = waba.phone_number
                phone_number_id = waba.phone_number_id

            except WABAAccount.DoesNotExist:
                pass
            try:
                owner       = org.owner
                owner_name  = getattr(owner, "full_name",  None) or owner.email
                owner_email = owner.email
            except Exception:
                pass

        return Response({
            "email":          user.email,
            "role":           role,
            "organization":   org_name,
            "org_id":         org_id,

            # Organization
            "org_email":      org_email,
            "org_website":    org_website,
            "owner_name":     owner_name,
            "owner_email":    owner_email,

            # WhatsApp Business
            "waba_status":        waba_status,
            "waba_name":          waba_name,
            "waba_id":            waba_id,
            "phone_number":       phone_number,
            "phone_number_id":    phone_number_id,

            "has_organization": has_org,
            "waba_connected": waba_connected,
        })
            


# ─────────────────────────────────────────────────────────────────────────────
# Organization
# ─────────────────────────────────────────────────────────────────────────────

class OrganizationCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        # Prevent creating duplicate orgs
        if hasattr(request.user, "organization") and request.user.organization is not None:
            return Response(
                {"error": "You already own an organization."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = OrganizationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        org = serializer.save(owner=request.user)
        return Response(
            {"id": org.id, "name": org.name, "email": org.email, "website": org.website},
            status=201,
        )


class OrganizationMemberCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = request.user
        org = None

        if hasattr(user, "organization") and user.organization is not None:
            org = user.organization
        elif hasattr(user, "membership") and user.membership.role == "manager":
            org = user.membership.organization
        else:
            return Response({"error": "Unauthorized"}, status=403)

        serializer = OrganizationMemberSerializer(
            data=request.data, context={"organization": org}
        )
        serializer.is_valid(raise_exception=True)
        member = serializer.save()

        return Response(
            {
                "id": member.id,
                "full_name": member.full_name,
                "email": member.user.email,
                "role": member.role,
            },
            status=201,
        )


class OrganizationMemberListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        org = None

        if hasattr(user, "organization") and user.organization is not None:
            org = user.organization
        elif hasattr(user, "membership") and user.membership.role == "manager":
            org = user.membership.organization
        else:
            return Response({"error": "Unauthorized"}, status=403)

        members = org.members.select_related("user").all()
        serializer = OrganizationMemberSerializer(members, many=True)
        return Response(serializer.data, status=200)


class OrganizationMemberDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _get_org(self, request):
        user = request.user
        if hasattr(user, "organization") and user.organization is not None:
            return user.organization, None
        return None, Response({"error": "You do not own an organization"}, status=403)

    def _get_member(self, request, pk):
        org, err = self._get_org(request)
        if err:
            return None, err
        try:
            return org.members.get(pk=pk), None
        except OrganizationMember.DoesNotExist:
            return None, Response({"error": "Member not found"}, status=404)

    def put(self, request, pk):
        member, err = self._get_member(request, pk)
        if err:
            return err
        serializer = OrganizationMemberSerializer(member, data=request.data, partial=False)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=200)

    def patch(self, request, pk):
        member, err = self._get_member(request, pk)
        if err:
            return err
        serializer = OrganizationMemberSerializer(member, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=200)

    def delete(self, request, pk):
        member, err = self._get_member(request, pk)
        if err:
            return err
        member.delete()
        return Response({"message": "Member deleted successfully"}, status=204)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_user_profile(user):
    role = None
    org_name = None
    org_id = None
    has_org = False
    waba_connected = False

    if hasattr(user, "organization") and user.organization is not None:
        role = "owner"
        org = user.organization
        org_name = org.name
        org_id = org.id
        has_org = True
        try:
            waba = org.waba_account
            waba_connected = waba.is_connected()
        except Exception:
            waba_connected = False

    elif hasattr(user, "membership") and user.membership is not None:
        membership = user.membership
        role = membership.role
        org = membership.organization
        org_name = org.name
        org_id = org.id
        has_org = True
        try:
            waba = org.waba_account
            waba_connected = waba.is_connected()
        except Exception:
            waba_connected = False

    elif hasattr(user, "client_membership") and user.client_membership is not None:
        client_membership = user.client_membership
        role = f"client_{client_membership.role}"
        client = client_membership.client
        org_name = client.name
        org_id = client.id
        has_org = True
        waba_connected = client.waba_connected()

    # ✅ has_org=False → frontend redirects to /setup (Phase 1)
    return role, org_name, org_id, has_org, waba_connected



class MetaDashboardAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        org = getattr(user, "organization", None)

        if not org and hasattr(user, "membership"):
            org = user.membership.organization

        if not org:
            return Response({"status": "no_org"})

        try:
            waba = org.waba_account
            phone_number_id = waba.phone_number_id
        except WABAAccount.DoesNotExist:
            return Response({"status": "not_connected"})

        # ✅ FIXED: Filter messages correctly for both Tech Providers and direct clients
        if phone_number_id:
            messages = Message.objects.filter(
                Q(conversation__client__phone_number_id=phone_number_id) | Q(conversation__phone_number_id=phone_number_id),
                direction='outbound'  # ← Only count sent messages
            )
        else:
            messages = Message.objects.filter(
                conversation__client__tech_provider=org,
                direction='outbound'  # ← Only count sent messages
            )

        templates = Template.objects.filter(organization=org)

        total_messages = messages.count()

        # ✅ NOW THESE WILL WORK (instead of hardcoded 0)
        delivered = messages.filter(status='delivered').count()
        read = messages.filter(status='read').count()
        failed = messages.filter(status='failed').count()

        total_templates = templates.count()

        # Unique conversations (distinct customers)
        conversations = (
            messages.values("conversation")
            .distinct()
            .count()
        )

        # Daily stats (last 7 days)
        daily_stats = (
            messages.annotate(day=TruncDate("timestamp"))
            .values("day")
            .annotate(count=Count("id"))
            .order_by("-day")[:7]
        )

        # ✅ NOW THIS WILL WORK (template breakdown)
        template_stats = (
            messages.filter(
                message_type='template',
                template_name__isnull=False
            )
            .values("template_name")
            .annotate(sent=Count("id"))
            .order_by("-sent")[:10]  # Top 10 templates
        )

        # 📊 NEW: Client Summary (Total Customers, Active Today, Leads, Prospects)
        from CRM.models import WhatsAppSession, ConversationState, Customer
        from datetime import date
        
        if phone_number_id:
            client_customers = Customer.objects.filter(
                Q(conversations__client__phone_number_id=phone_number_id) | Q(conversations__phone_number_id=phone_number_id)
            ).distinct()
            active_today = client_customers.filter(conversations__messages__timestamp__date=date.today()).distinct().count()
        else:
            client_customers = Customer.objects.filter(conversations__client__tech_provider=org).distinct()
            active_today = client_customers.filter(conversations__messages__timestamp__date=date.today()).distinct().count()

        raw_ticket_phones = WhatsAppSession.objects.exclude(ticket_id="").values_list("mobile_number", flat=True)
        ticket_phones_10d = set(p[-10:] for p in raw_ticket_phones if p)
        
        ticket_customer_ids = []
        for c in client_customers:
            c_phone = c.phone[-10:] if c.phone else ""
            if c_phone in ticket_phones_10d:
                ticket_customer_ids.append(c.id)

        completed_conv_ids = ConversationState.objects.filter(
            organization=org,
            is_complete=True
        ).values_list("conversation_id", flat=True)

        leads_count = client_customers.filter(
            Q(conversations__id__in=completed_conv_ids) | Q(id__in=ticket_customer_ids) | Q(conversations__status="confirmed")
        ).distinct().count()

        prospects_count = client_customers.exclude(
            Q(conversations__id__in=completed_conv_ids) | Q(id__in=ticket_customer_ids) | Q(conversations__status="confirmed")
        ).distinct().count()

        client_summary = {
            "total_customers": client_customers.count(),
            "active_today": active_today,
            "leads": leads_count,
            "prospects": prospects_count
        }

        return Response({
            "status": "connected",

            "waba": {
                "waba_id": waba.waba_id,
                "waba_name": waba.waba_name,
                "phone_number": waba.phone_number,
                "status": waba.status,
            },

            "client_summary": client_summary,

            "totals": {
                "messages": total_messages,
                "delivered": delivered,
                "read": read,
                "failed": failed,
                "templates": total_templates,
                "conversations": conversations,
            },

            "daily_stats": list(daily_stats),
            "template_stats": list(template_stats),
        })



"""
Template Management
==============================
Endpoints:
  GET    /api/templates/              → list all templates for the org
  POST   /api/templates/              → create template in CRM + submit to Meta
  DELETE /api/templates/<id>/         → delete from CRM + Meta
  POST   /api/templates/<id>/sync/    → pull latest status from Meta for one template
  POST   /api/templates/sync-all/     → bulk sync all PENDING templates from Meta

Meta Graph API reference:
  POST   /{waba_id}/message_templates  → submit template
  GET    /{waba_id}/message_templates  → list templates (for sync)
  DELETE /{template_id}                → delete template from Meta
"""

META_GRAPH_URL = "https://graph.facebook.com/v19.0"


# ── helpers ─────────────────────────────────────────────────────────────────

def _get_org_and_waba(user):
    """
    Resolve the org and WABAAccount for any user role.
    Returns (org, waba) or raises an appropriate error dict.
    """
    org = getattr(user, "organization", None)
    if not org and hasattr(user, "membership"):
        org = user.membership.organization

    if not org:
        return None, None, {"error": "No organisation found for this user"}

    try:
        waba = org.waba_account
    except WABAAccount.DoesNotExist:
        return org, None, {"error": "WABA not connected. Complete Phase 2 first."}

    if not waba.is_connected():
        return org, None, {"error": "WABA is not in 'connected' status."}

    return org, waba, None


def _build_meta_components(data):
    """
    Convert flat CRM fields into the nested `components` list Meta expects.
    """
    components = []

    # HEADER
    header_type = data.get("header_type", "").upper()
    header_text = data.get("header_text", "")
    if header_type == "TEXT" and header_text:
        components.append({
            "type": "HEADER",
            "format": "TEXT",
            "text": header_text,
        })
    elif header_type in ("IMAGE", "VIDEO", "DOCUMENT"):
        components.append({
            "type": "HEADER",
            "format": header_type,
        })

    # BODY — required
    body_text = data.get("body_text", "")
    if body_text:
        body_component = {"type": "BODY", "text": body_text}
        # extract {{n}} variables
        import re
        vars_found = re.findall(r"\{\{\d+\}\}", body_text)
        if vars_found:
            body_component["example"] = {
                "body_text": [["sample"] * len(vars_found)]
            }
        components.append(body_component)

    # FOOTER
    footer_text = data.get("footer_text", "")
    if footer_text:
        components.append({"type": "FOOTER", "text": footer_text})

    # BUTTONS
    buttons = data.get("buttons", [])
    if buttons:
        components.append({"type": "BUTTONS", "buttons": buttons})

    return components


# ── Views ────────────────────────────────────────────────────────────────────

class TemplateListCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    # ── GET /api/templates/ ──────────────────────────────────────────────────
    def get(self, request):
        org, waba, err = _get_org_and_waba(request.user)
        if err:
            return Response(err, status=400)

        templates = Template.objects.filter(organization=org)

        # optional filter by status
        status_filter = request.query_params.get("status")
        if status_filter:
            templates = templates.filter(status=status_filter.upper())

        results = []
        for t in templates:
            results.append({
                "id":           t.id,
                "name":         t.name,
                "template_id":  t.template_id,
                "category":     t.category,
                "language":     t.language,
                "status":       t.status,
                "header_type":  t.header_type,
                "header_text":  t.header_text,
                "body_text":    t.body_text,
                "footer_text":  t.footer_text,
                "buttons":      t.buttons,
                "variables_count": t.variables_count,
                "is_active":    t.is_active,
                "created_at":   t.created_at.isoformat(),
                "updated_at":   t.updated_at.isoformat(),
            })

        return Response({"count": len(results), "results": results})

    # ── POST /api/templates/ ─────────────────────────────────────────────────
    def post(self, request):
        org, waba, err = _get_org_and_waba(request.user)
        if err:
            return Response(err, status=400)

        # Convert request.data to a regular dict to avoid pickling errors with files
        data = {}
        for key, value in request.data.items():
            data[key] = value
        
        # If data came via FormData, buttons might be a string
        import json
        if isinstance(data.get("buttons"), str):
            try:
                data["buttons"] = json.loads(data["buttons"])
            except json.JSONDecodeError:
                data["buttons"] = []

        # Basic validation
        required = ["name", "body_text", "category", "language"]
        for field in required:
            if not data.get(field):
                return Response({"error": f"'{field}' is required."}, status=400)

        name     = data["name"].lower().replace(" ", "_")   # Meta requires snake_case
        category = data["category"].upper()
        language = data["language"]

        # ── 1. Save to CRM DB (status=PENDING) ──────────────────────────────
        import re
        body_text = data.get("body_text", "")
        variables_count = len(re.findall(r"\{\{\d+\}\}", body_text))

        template = Template.objects.create(
            organization=org,
            name=name,
            category=category,
            language=language,
            status="PENDING",
            header_type=data.get("header_type", ""),
            header_text=data.get("header_text", ""),
            body_text=body_text,
            footer_text=data.get("footer_text", ""),
            buttons=data.get("buttons", []),
            variables_count=variables_count,
        )

        # ── 2. Submit to Meta Graph API ──────────────────────────────────────
        components = _build_meta_components(data)
        
        # Handle media upload for header
        example_media = request.FILES.get("example_media")
        header_type = data.get("header_type", "")
        
        import os
        token = os.environ.get("WHATSAPP_TOKEN", waba.access_token)
        
        if example_media and header_type in ("IMAGE", "VIDEO", "DOCUMENT"):
            app_id = os.environ.get("META_APP_ID")
            if not app_id:
                template.delete()
                return Response({"error": "META_APP_ID is missing in server config."}, status=500)
            
            # Step 1: Initialize session
            init_url = f"{META_GRAPH_URL}/{app_id}/uploads"
            init_params = {
                "file_length": example_media.size,
                "file_type": example_media.content_type,
            }
            init_headers = {"Authorization": f"Bearer {token}"}
            
            try:
                init_resp = requests.post(init_url, params=init_params, headers=init_headers, timeout=15)
                if not init_resp.ok:
                    template.delete()
                    err_msg = init_resp.json().get("error", {}).get("message", "Unknown Meta error on upload init")
                    return Response({"error": f"Failed to initialize upload: {err_msg}"}, status=400)
                
                session_id = init_resp.json().get("id")
                
                # Step 2: Upload file data
                upload_url = f"{META_GRAPH_URL}/{session_id}"
                upload_headers = {
                    "Authorization": f"OAuth {token}",
                    "file_offset": "0",
                }
                upload_resp = requests.post(upload_url, headers=upload_headers, data=example_media.read(), timeout=30)
                
                if not upload_resp.ok:
                    template.delete()
                    err_msg = upload_resp.json().get("error", {}).get("message", "Unknown Meta error on upload")
                    return Response({"error": f"Failed to upload media: {err_msg}"}, status=400)
                    
                header_handle = upload_resp.json().get("h")
                
                # Inject example into components
                for comp in components:
                    if comp.get("type") == "HEADER" and comp.get("format") == header_type:
                        comp["example"] = {"header_handle": [header_handle]}
                        break
                        
            except requests.RequestException as exc:
                template.delete()
                return Response({"error": f"Meta Upload API call failed: {exc}"}, status=502)

        meta_payload = {
            "name":       name,
            "language":   language,
            "category":   category,
            "components": components,
        }

        meta_url = f"{META_GRAPH_URL}/{waba.waba_id}/message_templates"
        token = os.environ.get("WHATSAPP_TOKEN", waba.access_token)
        headers  = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        }

        try:
            meta_resp = requests.post(meta_url, json=meta_payload, headers=headers, timeout=15)
            meta_data = meta_resp.json()
        except requests.RequestException as exc:
            # Save locally but flag Meta call failed
            template.status = "PENDING"
            template.save()
            return Response({
                "id":      template.id,
                "warning": f"Template saved locally but Meta submission failed: {exc}",
                "status":  "PENDING",
            }, status=202)

        if meta_resp.ok:
            template.template_id = meta_data.get("id", "")
            # Meta sometimes returns status directly
            meta_status = meta_data.get("status", "PENDING").upper()
            template.status = meta_status if meta_status in ("APPROVED", "PENDING", "REJECTED") else "PENDING"
            template.save()

            return Response({
                "id":          template.id,
                "template_id": template.template_id,
                "name":        template.name,
                "status":      template.status,
                "message":     "Template submitted to Meta for review.",
            }, status=201)
        else:
            # Meta rejected the submission
            error_msg = meta_data.get("error", {}).get("message", "Meta API error")
            template.status = "REJECTED"
            template.save()
            return Response({
                "id":          template.id,
                "error":       error_msg,
                "meta_error":  meta_data.get("error", {}),
                "status":      "REJECTED",
            }, status=400)


class TemplateDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _get_template(self, request, pk):
        org, waba, err = _get_org_and_waba(request.user)
        if err:
            return None, None, None, err
        template = get_object_or_404(Template, pk=pk, organization=org)
        return org, waba, template, None


    # ── PATCH /api/templates/<id>/ ───────────────────────────────────────────
    def patch(self, request, pk):
        org, waba, template, err = self._get_template(request, pk)
        if err:
            return Response(err, status=400)

        editable = [
            "header_type", "header_text", "body_text",
            "footer_text", "buttons", "variables_count", "is_active",
        ]

        for field in editable:
            if field in request.data:
                setattr(template, field, request.data[field])

        # If it's already synced to Meta, push the edit
        if template.template_id and waba:
            # Build components from updated template fields
            # We construct a dict simulating request.data for the builder
            comp_data = {
                "header_type": template.header_type,
                "header_text": template.header_text,
                "body_text":   template.body_text,
                "footer_text": template.footer_text,
                "buttons":     template.buttons,
            }
            components = _build_meta_components(comp_data)

            import os
            token = os.environ.get("WHATSAPP_TOKEN", waba.access_token)
            meta_url = f"{META_GRAPH_URL}/{template.template_id}"
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            }
            meta_payload = {
                "components": components,
            }

            try:
                resp = requests.post(meta_url, json=meta_payload, headers=headers, timeout=15)
                if not resp.ok:
                    meta_error = resp.json().get("error", {}).get("message", "Unknown Meta error")
                    return Response({"error": f"Failed to update on Meta: {meta_error}"}, status=400)
                
                # Edit successful, Meta puts it back in PENDING
                template.status = "PENDING"
            except requests.RequestException as exc:
                return Response({"error": f"Meta API call failed: {exc}"}, status=502)

        template.save()

        return Response({
            "id":              template.id,
            "name":            template.name,
            "template_id":     template.template_id,
            "category":        template.category,
            "language":        template.language,
            "status":          template.status,
            "header_type":     template.header_type,
            "header_text":     template.header_text,
            "body_text":       template.body_text,
            "footer_text":     template.footer_text,
            "buttons":         template.buttons,
            "variables_count": template.variables_count,
            "is_active":       template.is_active,
        })

    # ── DELETE /api/templates/<id>/ ──────────────────────────────────────────
    def delete(self, request, pk):
        org, waba, template, err = self._get_template(request, pk)
        if err:
            return Response(err, status=400)

        meta_deleted = False
        meta_error = None

        # Delete from Meta if we have a template_id and waba
        if template.template_id and waba:
            try:
                import os
                token = os.environ.get("WHATSAPP_TOKEN", waba.access_token)
                meta_url = f"{META_GRAPH_URL}/{waba.waba_id}/message_templates"
                headers  = {"Authorization": f"Bearer {token}"}
                params   = {"name": template.name}
                resp = requests.delete(meta_url, headers=headers, params=params, timeout=15)
                meta_deleted = resp.ok
                if not resp.ok:
                    meta_error = resp.json().get("error", {}).get("message", "Unknown Meta error")
            except requests.RequestException as exc:
                meta_error = str(exc)

        template.delete()

        return Response({
            "message":      "Template deleted from CRM.",
            "meta_deleted": meta_deleted,
            "meta_error":   meta_error,
        }, status=200)


class TemplateSyncView(APIView):
    """
    POST /api/templates/<id>/sync/
    Pull the latest status for a single template from Meta.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        org = getattr(request.user, "organization", None)
        if not org and hasattr(request.user, "membership"):
            org = request.user.membership.organization
        if not org:
            return Response({"error": "No organisation found."}, status=400)

        template = get_object_or_404(Template, pk=pk, organization=org)

        try:
            waba = org.waba_account
        except WABAAccount.DoesNotExist:
            return Response({"error": "WABA not connected."}, status=400)

        if not template.template_id:
            return Response({"error": "Template has no Meta ID yet (submission may have failed)."}, status=400)

        # Fetch from Meta
        meta_url = f"{META_GRAPH_URL}/{template.template_id}"
        headers  = {"Authorization": f"Bearer {waba.access_token}"}
        try:
            resp = requests.get(meta_url, headers=headers, timeout=15)
            data = resp.json()
        except requests.RequestException as exc:
            return Response({"error": f"Meta API call failed: {exc}"}, status=502)

        if resp.ok:
            new_status = data.get("status", template.status).upper()
            template.status = new_status
            template.save()
            return Response({
                "id":     template.id,
                "status": template.status,
                "synced": True,
            })
        else:
            err_msg = data.get("error", {}).get("message", "Unknown Meta error")
            return Response({"error": err_msg, "synced": False}, status=400)


class TemplateSyncAllView(APIView):
    """
    POST /api/templates/sync-all/
    List all templates for this WABA from Meta and update local statuses.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        org = getattr(request.user, "organization", None)
        if not org and hasattr(request.user, "membership"):
            org = request.user.membership.organization
        if not org:
            return Response({"error": "No organisation found."}, status=400)

        try:
            waba = org.waba_account
        except WABAAccount.DoesNotExist:
            return Response({"error": "WABA not connected."}, status=400)

        meta_url = f"{META_GRAPH_URL}/{waba.waba_id}/message_templates"
        
        # Use permanent TechProvider token if available, else fallback
        token = os.environ.get("WHATSAPP_TOKEN", waba.access_token)
        headers  = {"Authorization": f"Bearer {token}"}
        params   = {"fields": "id,name,status,category,language,components", "limit": 250}

        try:
            resp = requests.get(meta_url, headers=headers, params=params, timeout=20)
            data = resp.json()
        except requests.RequestException as exc:
            return Response({"error": f"Meta API call failed: {exc}"}, status=502)

        if not resp.ok:
            return Response(
                {"error": data.get("error", {}).get("message", "Meta API error")},
                status=400,
            )

        meta_templates = data.get("data", [])
        updated = 0
        created = 0

        for mt in meta_templates:
            meta_id     = mt.get("id")
            meta_status = mt.get("status", "").upper()
            meta_name   = mt.get("name", "")
            meta_cat    = mt.get("category", "UTILITY").upper()
            meta_lang   = mt.get("language", "en")
            
            # Extract body text from components
            meta_components = mt.get("components", [])
            body_text = ""
            for comp in meta_components:
                if comp.get("type") == "BODY":
                    body_text = comp.get("text", "")
                    break

            import re
            variables_count = len(re.findall(r"\{\{\d+\}\}", body_text))

            if not meta_id or not meta_status:
                continue

            rows = Template.objects.filter(organization=org, template_id=meta_id)
            if rows.exists():
                rows.update(status=meta_status, body_text=body_text, variables_count=variables_count)
                updated += rows.count()
            else:
                # Template exists on Meta but not locally — create it
                Template.objects.create(
                    organization=org,
                    template_id=meta_id,
                    name=meta_name,
                    status=meta_status,
                    category=meta_cat if meta_cat in ("MARKETING", "UTILITY", "AUTHENTICATION") else "UTILITY",
                    language=meta_lang,
                    body_text=body_text,
                    variables_count=variables_count,
                )
                created += 1

        return Response({
            "synced_from_meta": len(meta_templates),
            "updated_locally":  updated,
            "created_locally":  created,
        })


class MetaCustomerListView(APIView):
    permission_classes = [permissions.IsAuthenticated]
 
    def get(self, request):
        user = request.user
 
        # ── Resolve org ───────────────────────────────────────────────────────
        org = getattr(user, "organization", None)
        if not org and hasattr(user, "membership"):
            org = user.membership.organization
        if not org and hasattr(user, "client_membership"):
            org = user.client_membership.client
 
        if not org:
            return Response({"detail": "No organization found."}, status=400)
 
        # ── Get WABA phone_number_id for this org ─────────────────────────────
        phone_number_id = None
        waba_phone = None
        try:
            waba = org.waba_account
            phone_number_id = waba.phone_number_id
            waba_phone = waba.phone_number
        except Exception:
            pass
 
        from django.db.models import Q
        
        # ── Base queryset ─────────────────────────────────────────────────────
        if phone_number_id:
            qs = Conversation.objects.select_related("customer").filter(
                Q(client__phone_number_id=phone_number_id) | Q(phone_number_id=phone_number_id)
            ).annotate(
                last_msg_time=Max('messages__timestamp')
            ).order_by("-last_msg_time", "-created_at")
        else:
            # Fallback: if no WABA connected yet, filter by org's clients
            qs = Conversation.objects.select_related("customer").filter(
                client__tech_provider=org
            ).annotate(
                last_msg_time=Max('messages__timestamp')
            ).order_by("-last_msg_time", "-created_at")
 
        # ── Optional query filters ────────────────────────────────────────────
        number    = request.query_params.get("number",    "").strip()
        from_date = request.query_params.get("from_date", "").strip()
        to_date   = request.query_params.get("to_date",   "").strip()
        status    = request.query_params.get("status",    "").strip()
 
        if number:
            qs = qs.filter(customer__phone__icontains=number)
        if from_date:
            qs = qs.filter(created_at__date__gte=from_date)
        if to_date:
            qs = qs.filter(created_at__date__lte=to_date)
        if status:
            qs = qs.filter(status=status)
 
        # ── Pagination ────────────────────────────────────────────────────────
        page_size = int(request.query_params.get("page_size", 20))
        page_num  = int(request.query_params.get("page", 1))
        paginator = Paginator(qs, page_size)
        page      = paginator.get_page(page_num)
 
        base_url = request.build_absolute_uri(request.path)
 
        def make_url(p):
            if p is None:
                return None
            params = request.query_params.copy()
            params["page"] = p
            return f"{base_url}?{params.urlencode()}"
 
        results = []
        for conv in page.object_list:
            cust = conv.customer
            phone = cust.phone
            if phone and len(phone) == 10 and phone.isdigit():
                phone = f"91{phone}"
                
            results.append({
                "conversation_id": conv.id,
                "customer_id":     cust.id,
                "name":            cust.name,
                "phone":           phone,
                "status":          conv.status,   # or cust.status — check your model
                "created_at":      conv.created_at.isoformat(),
            })
 
        return Response({
            "count":     paginator.count,
            "next":      make_url(page.next_page_number()     if page.has_next()     else None),
            "previous":  make_url(page.previous_page_number() if page.has_previous() else None),
            "results":   results,
            "waba_phone": waba_phone,  # shown as badge in sidebar
        })

class MetaConversationMessageListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, conversation_id):
        user = request.user
        org = getattr(user, "organization", None)
        if not org and hasattr(user, "membership"):
            org = user.membership.organization
        if not org and hasattr(user, "client_membership"):
            org = user.client_membership.client

        if not org:
            return Response({"detail": "No organization found."}, status=400)

        try:
            conv = Conversation.objects.select_related("client").get(id=conversation_id)
        except Conversation.DoesNotExist:
            return Response({"error": "Conversation not found"}, status=404)

        # Basic security check
        phone_number_id = None
        try:
            phone_number_id = org.waba_account.phone_number_id
        except Exception:
            pass
            
        if phone_number_id:
            # Check client's phone_number_id first
            if conv.client and conv.client.phone_number_id == phone_number_id:
                pass # Authorized
            # Check conversation's own phone_number_id fallback
            elif conv.phone_number_id == phone_number_id:
                pass # Authorized
            # Check if tech provider
            elif conv.client and conv.client.tech_provider == org:
                pass # Authorized
            else:
                return Response({"error": "Unauthorized"}, status=403)

        messages = [
            {
                "id": m.id, 
                "user_msg": m.content if m.direction == "inbound" else None, 
                "bot_msg": m.content if m.direction == "outbound" else None,
                "user_timestamp": m.timestamp.isoformat() if m.direction == "inbound" else None, 
                "bot_timestamp": m.timestamp.isoformat() if m.direction == "outbound" else None,
            }
            for m in conv.messages.order_by("timestamp")
        ]
        return Response({
            "conversationId": conv.id,
            "messages": messages,
        })


class LeadsProspectsView(APIView):
    """
    GET /api/leads-prospects/
    
    Query params:
      - tab: 'leads' | 'prospects' | 'all' (default: 'all')
      - page: page number (default: 1)
      - page_size: items per page (default: 20)
      - search: filter by name or phone
    
    Returns customers with their chatbot state and collected fields.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        
        # ── Resolve org ───────────────────────────────────────────────────
        org = getattr(user, "organization", None)
        if not org and hasattr(user, "membership"):
            org = user.membership.organization
        
        if not org:
            return Response({"error": "No organization found."}, status=400)

        # ── Get WABA phone_number_id for filtering ────────────────────────
        phone_number_id = None
        try:
            waba = org.waba_account
            phone_number_id = waba.phone_number_id
        except Exception:
            pass

        # ── Query parameters ──────────────────────────────────────────────
        tab       = request.query_params.get("tab", "all")        # leads/prospects/all
        search    = request.query_params.get("search", "").strip()
        page_num  = int(request.query_params.get("page", 1))
        page_size = int(request.query_params.get("page_size", 20))

        # ── Base queryset: Customers with conversations ───────────────────
        if phone_number_id:
            # Filter by org's WABA or directly by conversation's phone_number_id
            customers = Customer.objects.filter(
                Q(conversations__client__phone_number_id=phone_number_id) |
                Q(conversations__phone_number_id=phone_number_id)
            ).distinct()
        else:
            # Fallback: filter by org's clients
            customers = Customer.objects.filter(
                conversations__client__tech_provider=org
            ).distinct()

        # ── Search filter ─────────────────────────────────────────────────
        if search:
            customers = customers.filter(
                Q(name__icontains=search) | Q(phone__icontains=search)
            )

        # ── Tab filtering & Global Counts ─────────────────────────────────
        from CRM.models import WhatsAppSession
        # A phone is considered a "ticket lead" if they have a non-empty ticket_id
        raw_ticket_phones = WhatsAppSession.objects.exclude(ticket_id="").values_list("mobile_number", flat=True)
        ticket_phones_10d = set(p[-10:] for p in raw_ticket_phones if p)
        
        ticket_customer_ids = []
        for c_id, c_phone in customers.values_list('id', 'phone'):
            c_phone_10 = c_phone[-10:] if c_phone else ""
            if c_phone_10 in ticket_phones_10d:
                ticket_customer_ids.append(c_id)

        completed_conv_ids = ConversationState.objects.filter(
            organization=org,
            is_complete=True
        ).values_list("conversation_id", flat=True)

        leads_qs = customers.filter(
            Q(conversations__id__in=completed_conv_ids) | Q(id__in=ticket_customer_ids) | Q(conversations__status="confirmed")
        ).distinct()

        prospects_qs = customers.exclude(
            Q(conversations__id__in=completed_conv_ids) | Q(id__in=ticket_customer_ids) | Q(conversations__status="confirmed")
        ).distinct()

        total_count = customers.count()
        lead_count = leads_qs.count()
        prospect_count = total_count - lead_count

        if tab in ["leads", "lead"]:
            customers = leads_qs
        elif tab in ["prospects", "prospect"]:
            customers = prospects_qs

        # ── Order by latest conversation ──────────────────────────────────
        customers = customers.order_by("-conversations__created_at")

        # ── Pagination ────────────────────────────────────────────────────
        paginator = Paginator(customers, page_size)
        page = paginator.get_page(page_num)

        # ── Build response ────────────────────────────────────────────────
        results = []
        for customer in page.object_list:
            # Get latest conversation
            conv = customer.conversations.order_by("-created_at").first()
            
            # Get chatbot state if exists
            state = None
            collected_fields = {}
            chatbot_stage = None
            is_complete = False
            
            if conv:
                try:
                    state = conv.chatbot_state
                    collected_fields = state.collected_fields
                    chatbot_stage = state.stage
                    is_complete = state.is_complete
                except ConversationState.DoesNotExist:
                    pass
            
            # Check if this customer has a ticket
            has_ticket = customer.id in ticket_customer_ids

            results.append({
                "id": customer.id,
                "name": customer.name,
                "phone": customer.phone,
                "status": "lead" if (is_complete or has_ticket or (conv and conv.status == "confirmed")) else "prospect",
                "chatbot_stage": chatbot_stage,
                "collected_fields": collected_fields,
                "message_count": state.message_count if state else 0,
                "last_chat": conv.created_at.isoformat() if conv else None,
                "conversation_id": conv.id if conv else None,
            })

        # ── Build pagination URLs ─────────────────────────────────────────
        base_url = request.build_absolute_uri(request.path)
        
        def make_url(p):
            if p is None:
                return None
            params = request.query_params.copy()
            params["page"] = p
            return f"{base_url}?{params.urlencode()}"

        return Response({
            "count": paginator.count,
            "total_count": total_count,
            "lead_count": lead_count,
            "prospect_count": prospect_count,
            "next": make_url(page.next_page_number() if page.has_next() else None),
            "previous": make_url(page.previous_page_number() if page.has_previous() else None),
            "results": results,
        })
    
META_GRAPH_BASE = "https://graph.facebook.com/v19.0"

# ─── Tech-provider guard ──────────────────────────────────────────────────────

TECH_PROVIDER_EMAILS = {"pranjalvejani2111@gmail.com"}
TECH_PROVIDER_DOMAIN = "@jmsadvisory.in"


def _is_tech_provider(user) -> bool:
    email = (user.email or "").lower().strip()
    email_ok = (
        email in TECH_PROVIDER_EMAILS
        or email.endswith(TECH_PROVIDER_DOMAIN)
    )
    has_org = hasattr(user, "organization") and user.organization is not None
    return email_ok and has_org


def _get_tech_org(user):
    return user.organization if _is_tech_provider(user) else None


# ─── Live Meta fetch ──────────────────────────────────────────────────────────

def _meta_get(path: str, token: str, params: dict = None):
    """GET /{META_GRAPH_BASE}/{path}. Returns (data_dict, is_success)."""
    try:
        resp = httpx.get(
            f"{META_GRAPH_BASE}/{path}",
            params={"access_token": token, **(params or {})},
            timeout=10,
        )
        return resp.json(), resp.is_success
    except Exception as exc:
        logger.warning("Meta GET /%s failed: %s", path, exc)
        return {}, False


def _fetch_waba_meta_status(client, tech_provider_token: str) -> dict:
    """
    Fetch live WABA + phone status from Meta for a ClientAccount.

    Priority:
      1. tech_provider_token  (permanent token fallback)
      2. client.access_token  (Embedded Signup token — fallback)

    Returns dict with meta_* keys that get merged into the client payload.
    These are exactly what TechProviderClients.jsx reads:
      meta_status, meta_waba_name, meta_account_review_status,
      meta_ban_state, meta_phone_number, meta_verified_name,
      meta_quality_rating, meta_phone_status, meta_error
    """
    waba_id         = client.waba_id
    phone_number_id = client.phone_number_id

    if not waba_id:
        return {"meta_status": "no_waba"}

    # 1. tech_provider_token  (permanent system token — always preferred to avoid expiry)
    # 2. client.access_token  (Embedded Signup token fallback)
    token = tech_provider_token or client.access_token
    if not token:
        return {"meta_status": "no_token"}

    # 1. Fetch WABA object
    waba_data, ok = _meta_get(
        waba_id,
        token,
        params={"fields": "id,name,account_review_status"},

    )

    if not ok:
        err = waba_data.get("error", {})
        return {
            "meta_status":      "error",
            "meta_error":       err.get("message", "Meta API error"),
            "meta_error_code":  err.get("code"),
        }

    result = {
        "meta_status":                "connected",
        "meta_waba_name":             waba_data.get("name"),
        "meta_account_review_status": waba_data.get("account_review_status"),  # APPROVED/PENDING/REJECTED
        "meta_ban_state":             None,               # NONE/SCHEDULE_FOR_DISABLE/DISABLE
    }

    # 2. Fetch phone number details
    if phone_number_id:
        phone_data, phone_ok = _meta_get(
            phone_number_id,
            token,
            params={"fields": "display_phone_number,verified_name,quality_rating,status"},
        )
        if phone_ok:
            result["meta_phone_number"]  = phone_data.get("display_phone_number")
            result["meta_verified_name"] = phone_data.get("verified_name")
            result["meta_quality_rating"]= phone_data.get("quality_rating")  # GREEN/YELLOW/RED
            result["meta_phone_status"]  = phone_data.get("status")          # CONNECTED/FLAGGED/RESTRICTED

    return result


# ─── Serialise helpers ────────────────────────────────────────────────────────

def _serialise_member(m):
    return {
        "id":        m.id,
        "full_name": m.full_name,
        "email":     m.user.email,
        "role":      m.role,
        "joined_at": m.created_at.isoformat(),
    }


def _serialise_client(client, *, tech_provider_token: str, fetch_meta: bool = True):
    """
    Build full client payload for TechProviderClients.jsx.

    DB fields + live Meta fields merged together.
    TechProviderClients.jsx reads ALL of these directly off the client object.
    """
    members = [_serialise_member(m) for m in client.members.all()]

    payload = {
        # DB fields
        "id":             client.id,
        "name":           client.name,
        "email":          client.email,
        "website":        client.website,
        "industry":       client.industry,
        "status":         client.status,           # CRM: pending/active/suspended
        "waba_connected": client.waba_connected(),  # True if waba_id+phone_number_id+access_token
        "waba_id":        client.waba_id,
        "waba_name":      client.waba_name,
        "phone_number":   client.phone_number,
        "member_count":   len(members),
        "members":        members,
        "created_at":     client.created_at.isoformat(),
        "updated_at":     client.updated_at.isoformat(),
    }

    # Live Meta fields — merged in
    if fetch_meta and client.waba_id:
        meta = _fetch_waba_meta_status(client, tech_provider_token)
        payload.update(meta)
    else:
        payload["meta_status"] = "skipped"

    return payload


class TechProviderClientListView(APIView):
    """
    GET /api/techprovider/clients/

    Returns all ClientAccounts for this tech provider.
    Each client includes live WABA status from Meta Graph API.

    Query params (all optional):
      ?status=pending|active|suspended   — CRM filter
      ?search=<name or email>
      ?meta=0                            — skip live Meta fetch (faster, debug)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        org = _get_tech_org(request.user)
        if org is None:
            return Response(
                {"detail": "Access denied. Tech-provider credentials required."},
                status=status.HTTP_403_FORBIDDEN,
            )

        tech_token = getattr(settings, "META_PERMANENT_TOKEN", "") or ""

        qs = (
            org.clients
            .prefetch_related("members", "members__user")
            .order_by("-created_at")
        )

        status_filter = request.query_params.get("status", "").strip()
        search        = request.query_params.get("search", "").strip().lower()
        fetch_meta    = request.query_params.get("meta", "1") != "0"

        if status_filter:
            qs = qs.filter(status=status_filter)
        if search:
            qs = qs.filter(name__icontains=search) | qs.filter(email__icontains=search)

        clients_data = [
            _serialise_client(c, tech_provider_token=tech_token, fetch_meta=fetch_meta)
            for c in qs
        ]

        all_clients = org.clients.all()
        summary = {
            "total":     all_clients.count(),
            "active":    all_clients.filter(status="active").count(),
            "pending":   all_clients.filter(status="pending").count(),
            "suspended": all_clients.filter(status="suspended").count(),
        }

        return Response({
            "tech_provider": org.name,
            "summary":       summary,
            "count":         len(clients_data),
            "clients":       clients_data,
        })


class TechProviderClientDetailView(APIView):
    """
    GET   /api/techprovider/clients/<pk>/   — full detail + live Meta status
    PATCH /api/techprovider/clients/<pk>/   — update CRM status
    """
    permission_classes = [IsAuthenticated]

    def _resolve(self, request, pk):
        org = _get_tech_org(request.user)
        if org is None:
            return None, None, None, Response(
                {"detail": "Access denied. Tech-provider credentials required."},
                status=status.HTTP_403_FORBIDDEN,
            )
        tech_token = getattr(settings, "META_PERMANENT_TOKEN", "") or ""
        try:
            client = (
                org.clients
                .prefetch_related("members", "members__user")
                .get(pk=pk)
            )
            return org, tech_token, client, None
        except ClientAccount.DoesNotExist:
            return None, None, None, Response(
                {"detail": "Client not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

    def get(self, request, pk):
        org, tech_token, client, err = self._resolve(request, pk)
        if err:
            return err

        data = _serialise_client(client, tech_provider_token=tech_token, fetch_meta=True)
        # Extra fields only in detail view
        data["phone_number_id"] = client.phone_number_id
        data["business_id"]     = client.business_id
        return Response(data)

    def patch(self, request, pk):
        """Update CRM status. Body: { "status": "active" }"""
        org, tech_token, client, err = self._resolve(request, pk)
        if err:
            return err

        new_status = request.data.get("status", "").strip()
        valid = {s[0] for s in ClientAccount.STATUS_CHOICES}
        if not new_status or new_status not in valid:
            return Response(
                {"detail": f"Invalid status. Choose from: {', '.join(sorted(valid))}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        client.status = new_status
        client.save(update_fields=["status", "updated_at"])
        return Response({
            "id":      client.id,
            "status":  client.status,
            "message": "Client status updated successfully.",
        })

class ClientMetaRegistrationView(APIView):
    permission_classes = [IsAuthenticated]

    def get_client(self, request):
        if hasattr(request.user, "client_membership"):
            return request.user.client_membership.client
            
        # If the user is a tech provider, they might be testing or setting up their own WABA.
        # Let's resolve their organization and treat them as their own client.
        org = None
        if hasattr(request.user, "organization") and request.user.organization:
            org = request.user.organization
        elif hasattr(request.user, "membership") and request.user.membership:
            org = request.user.membership.organization
            
        if org:
            # Get or create a "Self" client account for the tech provider
            email = org.email or request.user.email
            try:
                client = ClientAccount.objects.get(email=email)
                # Ensure it belongs to this tech provider (optional, but good for data integrity)
                if client.tech_provider_id != org.id:
                    client.tech_provider = org
                    client.save(update_fields=['tech_provider'])
            except ClientAccount.DoesNotExist:
                client = ClientAccount.objects.create(
                    tech_provider=org,
                    email=email,
                    name=f"{org.name} (Self)",
                    status="active",
                    website=org.website
                )
            return client
            
        return None

    def get(self, request):
        client = self.get_client(request)
        if not client:
            return Response({"error": "User is not associated with a client account."}, status=403)
        
        obj, created = MetaRegistrationDetails.objects.get_or_create(client=client)
        serializer = MetaRegistrationSerializer(obj)
        return Response(serializer.data)

    def put(self, request):
        client = self.get_client(request)
        if not client:
            return Response({"error": "User is not associated with a client account."}, status=403)
            
        obj, created = MetaRegistrationDetails.objects.get_or_create(client=client)
        
        # Update using partial=True and support for form-data
        serializer = MetaRegistrationSerializer(obj, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=400)


class TechProviderMetaRegistrationView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        org = _get_tech_org(request.user)
        if not org:
            return Response({"error": "Access denied. Tech-provider credentials required."}, status=403)
            
        try:
            client = org.clients.get(pk=pk)
        except ClientAccount.DoesNotExist:
            return Response({"error": "Client not found."}, status=404)
            
        obj, created = MetaRegistrationDetails.objects.get_or_create(client=client)
        serializer = MetaRegistrationSerializer(obj)
        return Response(serializer.data)