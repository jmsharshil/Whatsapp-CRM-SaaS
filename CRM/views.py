import random
from django.core.mail import send_mail, EmailMultiAlternatives
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from rest_framework import status, permissions
from rest_framework_simplejwt.tokens import RefreshToken

from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404

from .models import *
from .serializers import*
from django.conf import settings
from django.db.models import Count
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
            and not email.endswith("@jmsadvisory.in")
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
        name      = request.data.get("name",      "").strip()
        website   = request.data.get("website",   "").strip() or None

        if not full_name:
            return Response({"message": "Your name is required"}, status=400)

        if not email:
            return Response({"message": "Email is required"}, status=400)

        if not name:
            return Response({"message": "Organisation name is required"}, status=400)

        if User.objects.filter(email=email).exists():
            return Response({"message": "User already exists"}, status=400)

        # Create user with full_name
        user = User.objects.create_user(email=email)
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

class SendCodeView(APIView):
    def post(self, request):
        serializer = EmailRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"]

        code = str(random.randint(100000, 999999))
        EmailVerificationCode.objects.create(email=email, code=code)

        subject = "Your Verification Code"

        text_content = (
            f"Your verification code is {code}. "
            f"It is valid for 5 minutes."
        )

        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>Verification Code</title>
        </head>

        <body style="
            margin:0;
            padding:0;
            background-color:#f4f7fb;
            font-family:Arial,sans-serif;
        ">

            <table width="100%" cellpadding="0" cellspacing="0" border="0"
                   style="background-color:#f4f7fb; padding:40px 0;">

                <tr>
                    <td align="center">

                        <table width="600" cellpadding="0" cellspacing="0" border="0"
                               style="
                                    background:#ffffff;
                                    border-radius:18px;
                                    overflow:hidden;
                                    box-shadow:0 4px 20px rgba(0,0,0,0.08);
                               ">

                            <!-- Header -->
                            <tr>
                                <td align="center"
                                    style="
                                        padding:35px 20px 20px;
                                        background:#ffffff;
                                        border-bottom:1px solid #eef2f7;
                                    ">

                                    <img
                                        src="https://hrmsknowcraftstorage.blob.core.windows.net/media/JMS.png"
                                        alt="JMS"
                                        style="
                                            width:180px;
                                            max-width:100%;
                                            display:block;
                                        "
                                    />

                                </td>
                            </tr>

                            <!-- Content -->
                            <tr>
                                <td style="padding:40px 35px;">

                                    <h2 style="
                                        margin:0 0 18px;
                                        color:#1f2937;
                                        font-size:26px;
                                        font-weight:700;
                                    ">
                                        Verify Your Email
                                    </h2>

                                    <p style="
                                        margin:0 0 15px;
                                        color:#4b5563;
                                        font-size:16px;
                                        line-height:26px;
                                    ">
                                        Hello,
                                    </p>

                                    <p style="
                                        margin:0 0 30px;
                                        color:#4b5563;
                                        font-size:16px;
                                        line-height:26px;
                                    ">
                                        Use the verification code below to securely log in to your account.
                                    </p>

                                    <!-- OTP Box -->
                                    <div style="text-align:center; margin:35px 0;">

                                        <span style="
                                            display:inline-block;
                                            background:#f3f7ff;
                                            color:#2563eb;
                                            font-size:34px;
                                            font-weight:700;
                                            letter-spacing:10px;
                                            padding:18px 35px;
                                            border-radius:14px;
                                            border:1px solid #dbeafe;
                                        ">
                                            {code}
                                        </span>

                                    </div>

                                    <p style="
                                        margin:0;
                                        color:#6b7280;
                                        font-size:15px;
                                        line-height:24px;
                                    ">
                                        This verification code will expire in
                                        <strong>5 minutes</strong>.
                                    </p>

                                    <p style="
                                        margin:18px 0 0;
                                        color:#6b7280;
                                        font-size:15px;
                                        line-height:24px;
                                    ">
                                        If you did not request this email, you can safely ignore it.
                                    </p>

                                </td>
                            </tr>

                            <!-- Footer -->
                            <tr>
                                <td align="center"
                                    style="
                                        padding:24px;
                                        background:#f9fafb;
                                        border-top:1px solid #eef2f7;
                                    ">

                                    <p style="
                                        margin:0;
                                        color:#9ca3af;
                                        font-size:13px;
                                        line-height:22px;
                                    ">
                                        © 2026 JMS TechNova <br>
                                        Secure Authentication System
                                    </p>

                                </td>
                            </tr>

                        </table>

                    </td>
                </tr>

            </table>

        </body>
        </html>
        """

        msg = EmailMultiAlternatives(
            subject,
            text_content,
            "noreply@example.com",
            [email]
        )

        msg.attach_alternative(html_content, "text/html")
        msg.send()

        return Response(
            {"message": "Verification code sent"},
            status=200
        )

class VerifyCodeView(APIView):
    def post(self, request):
        serializer = CodeVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data["email"]
        code = serializer.validated_data["code"]

        try:
            record = EmailVerificationCode.objects.filter(
                email=email, code=code, is_used=False
            ).latest("created_at")
        except EmailVerificationCode.DoesNotExist:
            return Response({"error": "Invalid code"}, status=400)

        if record.is_expired():
            return Response({"error": "Code expired"}, status=400)

        record.is_used = True
        record.save()

        user, _ = User.objects.get_or_create(email=email)

        refresh = RefreshToken.for_user(user)

        # Determine role + org (same logic as UserMeView)
        role, org_name, org_id, has_org, waba_connected = _resolve_user_profile(user)

        return Response({
            "refresh": str(refresh),
            "access": str(refresh.access_token),
            "role": role,
            "organization": org_name,
            "org_id": org_id,
            "has_organization": has_org,
            "waba_connected": waba_connected,
        })


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
        except WABAAccount.DoesNotExist:
            return Response({"status": "not_connected"})

        # ✅ FIXED: Filter messages by organization's clients
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

        return Response({
            "status": "connected",

            "waba": {
                "waba_id": waba.waba_id,
                "waba_name": waba.waba_name,
                "phone_number": waba.phone_number,
                "status": waba.status,
            },

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

        data = request.data

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

        meta_payload = {
            "name":       name,
            "language":   language,
            "category":   category,
            "components": components,
        }

        meta_url = f"{META_GRAPH_URL}/{waba.waba_id}/message_templates"
        headers  = {
            "Authorization": f"Bearer {waba.access_token}",
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
                meta_url = f"{META_GRAPH_URL}/{template.template_id}"
                headers  = {"Authorization": f"Bearer {waba.access_token}"}
                params   = {"name": template.name, "hsm_id": template.template_id}
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
        headers  = {"Authorization": f"Bearer {waba.access_token}"}
        params   = {"fields": "id,name,status,category,language", "limit": 250}

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

            if not meta_id or not meta_status:
                continue

            rows = Template.objects.filter(organization=org, template_id=meta_id)
            if rows.exists():
                rows.update(status=meta_status)
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
                    body_text="",   # full body can be filled via template detail/edit
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
 
        # ── Base queryset ─────────────────────────────────────────────────────
        if phone_number_id:
            qs = Conversation.objects.select_related("customer").filter(
                client__phone_number_id=phone_number_id   # ← FIXED (was: phone_number_id=...)
            ).order_by("-created_at")
        else:
            # Fallback: if no WABA connected yet, filter by org's clients
            qs = Conversation.objects.select_related("customer").filter(
                client__tech_provider=org
            ).order_by("-created_at")
 
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
            results.append({
                "conversation_id": conv.id,
                "customer_id":     cust.id,
                "name":            cust.name,
                "phone":           cust.phone,
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
            # Filter by org's WABA
            customers = Customer.objects.filter(
                conversations__client__phone_number_id=phone_number_id
            ).distinct()
        else:
            # Fallback: filter by org's clients
            customers = Customer.objects.filter(
                conversations__client__tech_provider=org
            ).distinct()

        # ── Apply tab filter (Lead vs Prospect) ──────────────────────────
        if tab == "leads":
            # Customers with completed chatbot qualification
            completed_conv_ids = ConversationState.objects.filter(
                organization=org,
                is_complete=True
            ).values_list("conversation_id", flat=True)
            
            customers = customers.filter(
                conversations__id__in=completed_conv_ids
            )
        
        elif tab == "prospects":
            # Customers with incomplete chatbot qualification
            incomplete_conv_ids = ConversationState.objects.filter(
                organization=org,
                is_complete=False
            ).values_list("conversation_id", flat=True)
            
            customers = customers.filter(
                conversations__id__in=incomplete_conv_ids
            )

        # ── Search filter ─────────────────────────────────────────────────
        if search:
            customers = customers.filter(
                Q(name__icontains=search) | Q(phone__icontains=search)
            )

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

            results.append({
                "id": customer.id,
                "name": customer.name,
                "phone": customer.phone,
                "status": "Lead" if is_complete else "Prospect",
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
      1. client.access_token  (Embedded Signup token — always preferred)
      2. tech_provider_token  (permanent token fallback)

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

    token = client.access_token or tech_provider_token
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