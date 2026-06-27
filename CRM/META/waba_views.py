"""
Meta WhatsApp Business Embedded Signup — complete server-side flow.

Endpoints (all under /api/):
  POST   meta/embedded-signup/start/   ← Called by frontend after FB.login() popup
  GET    meta/waba/status/             ← Frontend polls for connection state
  DELETE meta/waba/disconnect/         ← Owner revokes WABA

Flow:
  1. Frontend launches FB.login() via Meta JS SDK
  2. sessionInfoListener captures { waba_id, phone_number_id } from postMessage
  3. FB.login callback gives response.authResponse.code
  4. Frontend POSTs { code, waba_id, phone_number_id } to /api/meta/embedded-signup/start/
  5. Backend exchanges code → access_token via Graph API
  6. Backend fetches WABA name + phone display number
  7. WABAAccount saved as status=connected
  8. Frontend receives { status, waba_id, waba_name, phone_number }
"""

import logging
import httpx
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import permissions, status

from CRM.models import ClientAccount, WABAAccount, Organization

logger = logging.getLogger(__name__)

META_GRAPH_VERSION = "v19.0"
META_GRAPH_BASE = f"https://graph.facebook.com/{META_GRAPH_VERSION}"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_org(user):
    """Return Organization if user is an owner, else None."""
    try:
        return user.organization
    except Organization.DoesNotExist:
        return None


def _exchange_code_for_token(auth_code: str) -> str | None:
    app_id = getattr(settings, "META_APP_ID", "")
    app_secret = getattr(settings, "META_APP_SECRET", "")

    try:
        resp = httpx.post(
            f"{META_GRAPH_BASE}/oauth/access_token",
            params={
                "client_id": app_id,
                "client_secret": app_secret,
                "code": auth_code,
            },
            timeout=20,
        )

        resp.raise_for_status()

        data = resp.json()

        return data.get("access_token")

    except Exception as exc:
        logger.error("Token exchange error: %s", exc)
        return None

def _fetch_waba_name(waba_id: str, access_token: str) -> str | None:
    """Fetch WABA display name from Graph API."""
    try:
        resp = httpx.get(
            f"{META_GRAPH_BASE}/{waba_id}",
            params={"fields": "name", "access_token": access_token},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("name")
    except Exception as exc:
        logger.error("WABA name fetch error: %s", exc)
        return None


def _fetch_phone_display(phone_number_id: str, access_token: str) -> str | None:
    """Fetch display phone number for a phone_number_id."""
    try:
        resp = httpx.get(
            f"{META_GRAPH_BASE}/{phone_number_id}",
            params={"fields": "display_phone_number,verified_name", "access_token": access_token},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("display_phone_number")
    except Exception as exc:
        logger.error("Phone number fetch error: %s", exc)
        return None

def _fetch_business_accounts(access_token: str):
    """
    Fetch businesses + WABAs available for logged-in user.
    """

    try:
        resp = httpx.get(
            f"{META_GRAPH_BASE}/me/accounts",
            params={
                "access_token": access_token,
            },
            timeout=20,
        )

        resp.raise_for_status()

        return resp.json()

    except Exception as exc:
        logger.error("Business account fetch error: %s", exc)
        return None


class EmbeddedSignupView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = request.user
        org = _get_org(user)

        # ── First-time user: no org yet -> create one here from Meta data ──
        if not org:
            org = Organization.objects.create(
                owner=user,
                name=user.full_name or user.email,  
                email=user.email,
            )

        auth_code = request.data.get("code", "").strip()
        waba_id = (request.data.get("waba_id") or "").strip()
        phone_number_id = (request.data.get("phone_number_id") or "").strip()
        business_id = (request.data.get("business_id") or "").strip()

        if not auth_code:
            return Response(
                {"error": "code is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not waba_id:
            logger.warning("No WABA ID received for user=%s", user.email)

        if not phone_number_id:
            logger.warning("No phone_number_id received for user=%s", user.email)

        # Create or refresh WABAAccount (one per org)
        waba_account, _ = WABAAccount.objects.get_or_create(organization=org)
        waba_account.auth_code = auth_code
        if waba_id:
            waba_account.waba_id = waba_id
        if phone_number_id:
            waba_account.phone_number_id = phone_number_id
        waba_account.business_id = business_id
        waba_account.status = "pending"
        waba_account.error_message = None
        waba_account.save()

        # Exchange code → access token
        access_token = _exchange_code_for_token(auth_code)
        if not access_token:
            waba_account.status = "error"
            waba_account.error_message = (
                "Token exchange with Meta failed. "
                "Verify META_APP_ID and META_APP_SECRET in server environment."
            )
            waba_account.save()
            return Response(
                {
                    "error": "Failed to exchange auth code with Meta. Please try again.",
                    "status": "error",
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # ---------------------------------------------------
        # FIRST-TIME USER FLOW (no WABA yet)
        # ---------------------------------------------------
        if not waba_id:
            logger.info("User does not yet have WhatsApp Business setup.")

            waba_account.access_token = access_token
            waba_account.status = "pending_setup"
            waba_account.save()

            return Response(
                {
                    "status": "pending_setup",
                    "message": (
                        "Please complete Meta Business "
                        "and WhatsApp setup in popup."
                    ),
                },
                status=status.HTTP_200_OK,
            )

        # ---------------------------------------------------
        # EXISTING BUSINESS FLOW
        # ---------------------------------------------------
        waba_name = _fetch_waba_name(waba_id, access_token) or org.name

        phone_display = None
        if phone_number_id:
            phone_display = _fetch_phone_display(phone_number_id, access_token)

        waba_account.access_token = access_token
        waba_account.waba_name = waba_name
        waba_account.phone_number = phone_display
        waba_account.status = "connected"
        waba_account.save()

        # ── Auto-create ClientAccount for this client ─────────────────
        try:
            tech_org = Organization.objects.get(
                owner__email="pranjalvejani2111@gmail.com"
            )
            # Org j client che — tech_org nahi
            if org != tech_org:
                ClientAccount.objects.update_or_create(
                    email=org.email,
                    defaults={
                        "tech_provider":   tech_org,
                        "name":            waba_name or org.name,
                        "waba_id":         waba_id,
                        "phone_number_id": phone_number_id,
                        "waba_name":       waba_name,
                        "phone_number":    phone_display,
                        "access_token":    access_token,
                        "status":          "pending",
                    }
                )
                logger.info("ClientAccount created for org=%s", org.name)
        except Exception as e:
            logger.error("ClientAccount auto-create failed: %s", e)


        # ── Update org name/email from Meta's business data (first-time user) ──
        if org.name in (user.full_name, user.email):
            org.name = waba_name
            org.save(update_fields=["name"])

        logger.info(
            "WABA connected for org=%s waba_id=%s phone=%s",
            org.name, waba_id, phone_display,
        )

        return Response(
            {
                "status": "connected",
                "waba_id": waba_id,
                "waba_name": waba_name,
                "phone_number": phone_display,
                "phone_number_id": phone_number_id,
                "org_id": org.id,
                "organization": org.name,
            },
            status=status.HTTP_200_OK,
        )


class WABAStatusView(APIView):
    """
    GET /api/meta/waba/status/

    Returns the current WABA connection state for the requesting user's org.
    Frontend calls this on every page load to show connected/not-connected UI.

    Response examples:

    Not connected:
        { "status": "not_connected" }

    Connected:
        {
            "status": "connected",
            "waba_id": "...",
            "waba_name": "JMS Eye Hospital",
            "phone_number": "+91 98765 43210",
            "phone_number_id": "...",
            "connected_at": "2026-05-18T10:00:00Z"
        }
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user

        # Owner
        org = _get_org(user)
        # Member — use their org
        if not org and hasattr(user, "membership"):
            org = user.membership.organization

        if not org:
            return Response({"status": "no_org"}, status=status.HTTP_200_OK)

        try:
            waba = org.waba_account
        except WABAAccount.DoesNotExist:
            return Response({"status": "not_connected"}, status=status.HTTP_200_OK)

        return Response(
            {
                "status": waba.status,
                "waba_id": waba.waba_id,
                "waba_name": waba.waba_name,
                "phone_number": waba.phone_number,
                "phone_number_id": waba.phone_number_id,
                "connected_at": waba.updated_at.isoformat() if waba.updated_at else None,

            },
            status=status.HTTP_200_OK,
        )


class WABADisconnectView(APIView):
    """
    DELETE /api/meta/waba/disconnect/

    Clears the access token and marks the WABA as disconnected.
    Only org owners can do this.
    """

    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request):
        user = request.user
        org = _get_org(user)
        if not org:
            return Response(
                {"error": "Only organization owners can disconnect WhatsApp."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            waba = org.waba_account
        except WABAAccount.DoesNotExist:
            return Response(
                {"error": "No WhatsApp account is connected."},
                status=status.HTTP_404_NOT_FOUND,
            )

        waba.access_token = None
        waba.status = "disconnected"
        waba.save()

        logger.info("WABA disconnected for org=%s", org.name)

        return Response(
            {"message": "WhatsApp disconnected successfully."},
            status=status.HTTP_200_OK,
        )