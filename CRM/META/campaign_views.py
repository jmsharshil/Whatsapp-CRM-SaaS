"""
Campaign API — CSV phone numbers + approved Meta template → broadcast.

Endpoints:
  GET  /api/campaigns/       → list campaigns for this org
  POST /api/campaigns/       → create + send campaign immediately
  GET  /api/campaigns/<id>/  → get single campaign detail with recipients

How it works (POST):
  1. Validate: template must exist, status=APPROVED, belongs to same org
  2. Normalise phone numbers (strip spaces, add country code if missing)
  3. Create Campaign row (status=running)
  4. Create CampaignRecipient rows (status=pending)
  5. For each recipient → POST to Meta Cloud API send-template-message
  6. Update recipient row (sent / failed + meta_message_id)
  7. Update Campaign totals → status=completed / failed

Meta Cloud API reference:
  POST https://graph.facebook.com/v19.0/{phone_number_id}/messages
  {
    "messaging_product": "whatsapp",
    "to": "919876543210",
    "type": "template",
    "template": {
      "name": "template_name",
      "language": { "code": "en" },
      "components": [
        {
          "type": "body",
          "parameters": [
            { "type": "text", "text": "value1" },
            { "type": "text", "text": "value2" }
          ]
        }
      ]
    }
  }
"""

import re
import requests
import logging
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from CRM.models import Campaign, CampaignRecipient, Template, WABAAccount
from CRM.views import _get_org_and_waba   

logger = logging.getLogger(__name__)

META_GRAPH_VERSION = "v19.0"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _normalise_phone(raw: str, default_country_code: str = "91") -> str | None:
    """
    Return E.164 digits-only string (no +).
    e.g. "+91 98765 43210" → "919876543210"
         "98765 43210"     → "919876543210"   (India default)
         "1 800 555 0100"  → "18005550100"
    Returns None if the result has fewer than 7 digits (invalid).
    """
    # Remove all non-digit chars except leading +
    stripped = re.sub(r"[^\d+]", "", raw.strip())

    if stripped.startswith("+"):
        digits = stripped[1:]
    else:
        digits = stripped

    if len(digits) < 7:
        return None

    # If it looks like a local number (10 digits for India), prepend country code
    if len(digits) == 10:
        digits = default_country_code + digits

    return digits


def _build_template_components(template: Template, variables: dict) -> list:
    """
    Build the Meta API `components` list from our Template model + user variables.
    variables: { "1": "Hello", "2": "Promo Code" }
    """
    components = []

    # HEADER
    if template.header_type:
        header_type = template.header_type.upper()
        if header_type == "TEXT" and template.header_text:
            header_params = []
            header_vars = re.findall(r"\{\{(\d+)\}\}", template.header_text)
            unique_header_vars = sorted(list(set(header_vars)), key=int)
            for v in unique_header_vars:
                val = variables.get(v, "")
                if not val:
                    val = " "
                header_params.append({"type": "text", "text": str(val)})
            if header_params:
                components.append({
                    "type": "header",
                    "parameters": header_params
                })
            
        elif header_type in ["IMAGE", "VIDEO", "DOCUMENT"]:
            media_url = variables.get("media_url")
            media_id = variables.get("media_id")
            
            media_obj = {}
            if media_id:
                media_obj["id"] = media_id
            elif media_url:
                media_obj["link"] = media_url
                
            if media_obj:
                components.append({
                    "type": "header",
                    "parameters": [
                        {
                            "type": header_type.lower(),
                            header_type.lower(): media_obj
                        }
                    ]
                })

    # BODY
    body_params = []
    body_vars = re.findall(r"\{\{(\d+)\}\}", template.body_text or "")
    unique_body_vars = sorted(list(set(body_vars)), key=int)
    for v in unique_body_vars:
        val = variables.get(v, "")
        if not val:
            val = " "
        body_params.append({"type": "text", "text": str(val)})
    
    if body_params:
        components.append({
            "type": "body",
            "parameters": body_params
        })

    return components


def _upload_media_to_meta(file, phone_number_id: str, access_token: str) -> str:
    """
    Uploads a media file directly to Meta's /media endpoint and returns the media_id.
    """
    url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{phone_number_id}/media"
    headers = {
        "Authorization": f"Bearer {access_token}"
    }
    
    import mimetypes
    content_type = getattr(file, 'content_type', None)
    if not content_type:
        content_type = mimetypes.guess_type(file.name)[0] or 'application/octet-stream'

    files = {
        "file": (file.name, file.read(), content_type)
    }
    data = {
        "messaging_product": "whatsapp"
    }

    try:
        resp = requests.post(url, headers=headers, data=data, files=files, timeout=60)
        resp_data = resp.json()
        if resp.status_code == 200 and "id" in resp_data:
            return resp_data["id"]
        logger.error(f"Media upload failed: {resp_data}")
        return None
    except Exception as e:
        logger.error(f"Media upload exception: {e}")
        return None


def _send_meta_template_message(
    phone_number_id: str,
    access_token: str,
    to_number: str,
    template_name: str,
    language_code: str,
    components: list,
) -> tuple[bool, str, str]:
    """
    Call Meta Cloud API to send a template message.
    Returns (success: bool, meta_message_id: str, error_detail: str)
    """
    url = f"https://graph.facebook.com/{META_GRAPH_VERSION}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
        },
    }
    if components:
        payload["template"]["components"] = components

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        data = resp.json()

        if resp.status_code == 200 and "messages" in data:
            msg_id = data["messages"][0].get("id", "")
            return True, msg_id, ""
        else:
            error = data.get("error", {})
            error_msg = error.get("message", str(data))
            return False, "", error_msg

    except requests.exceptions.Timeout:
        return False, "", "Meta API timeout"
    except requests.exceptions.RequestException as e:
        return False, "", str(e)


# ─── Views ────────────────────────────────────────────────────────────────────

class CampaignListCreateView(APIView):
    """
    GET  /api/campaigns/   → list campaigns (most recent first)
    POST /api/campaigns/   → create + send a campaign
    """
    permission_classes = [permissions.IsAuthenticated]

    # ── GET ──────────────────────────────────────────────────────────────────
    def get(self, request):
        org, waba, err = _get_org_and_waba(request.user)
        if err:
            return Response(err, status=400)

        campaigns = Campaign.objects.filter(organization=org).order_by("-created_at")[:50]

        results = []
        for c in campaigns:
            results.append({
                "id":            c.id,
                "name":          c.name,
                "template_name": c.template_name,
                "status":        c.status,
                "total_count":   c.total_count,
                "sent_count":    c.sent_count,
                "failed_count":  c.failed_count,
                "created_at":    c.created_at.isoformat(),
            })

        return Response({"count": len(results), "results": results})

    # ── POST ─────────────────────────────────────────────────────────────────
    def post(self, request):
        org, waba, err = _get_org_and_waba(request.user)
        if err:
            return Response(err, status=400)

        # Convert request.data to a regular dict
        data = {}
        for key, value in request.data.items():
            data[key] = value

        # ── 1. Validate inputs ───────────────────────────────────────────────
        name          = (data.get("name") or "").strip()
        template_id   = data.get("template_id")
        
        # Phone numbers might be stringified if sent via FormData
        phone_numbers_raw = data.get("phone_numbers", [])
        if isinstance(phone_numbers_raw, str):
            import json
            try:
                phone_numbers = json.loads(phone_numbers_raw)
            except:
                phone_numbers = []
        else:
            phone_numbers = phone_numbers_raw
            
        variables_raw = data.get("variables") or {}
        if isinstance(variables_raw, str):
            import json
            try:
                variables = json.loads(variables_raw)
            except:
                variables = {}
        else:
            variables = variables_raw
            
        media_file = request.FILES.get("media_file")

        if not name:
            return Response({"error": "'name' is required."}, status=400)

        if not template_id:
            return Response({"error": "'template_id' is required."}, status=400)

        if not phone_numbers or not isinstance(phone_numbers, list):
            return Response({"error": "'phone_numbers' must be a non-empty list."}, status=400)

        if len(phone_numbers) > 10_000:
            return Response(
                {"error": "Maximum 10,000 recipients per campaign."},
                status=400,
            )

        # ── 2. Load & validate template ──────────────────────────────────────
        try:
            template = Template.objects.get(id=template_id, organization=org)
        except Template.DoesNotExist:
            return Response(
                {"error": "Template not found or does not belong to your organisation."},
                status=404,
            )

        if template.status != "APPROVED":
            return Response(
                {"error": f"Template '{template.name}' is not APPROVED (current: {template.status}). Only APPROVED templates can be sent."},
                status=400,
            )

        # ── 3. Normalise phone numbers ───────────────────────────────────────
        normalised = []
        invalid    = []
        seen       = set()

        for raw in phone_numbers:
            n = _normalise_phone(str(raw))
            if n is None:
                invalid.append(str(raw))
            elif n in seen:
                pass  # silent dedup
            else:
                seen.add(n)
                normalised.append(n)

        if not normalised:
            return Response(
                {"error": "No valid phone numbers found after normalisation.",
                 "invalid_samples": invalid[:10]},
                status=400,
            )

        import os
        token = os.environ.get("WHATSAPP_TOKEN", waba.access_token)

        # ── 3.5 Upload Media File (if provided) ──────────────────────────────
        if media_file:
            media_id = _upload_media_to_meta(media_file, waba.phone_number_id, token)
            if media_id:
                variables["media_id"] = media_id
            else:
                return Response({"error": "Failed to upload media file to Meta. Please try again."}, status=500)

        # ── 4. Create Campaign record ────────────────────────────────────────
        campaign = Campaign.objects.create(
            organization=org,
            template=template,
            template_name=template.name,
            name=name,
            variables=variables,
            total_count=len(normalised),
            status="running",
        )

        # ── 5. Bulk-create recipient rows ────────────────────────────────────
        recipient_objs = [
            CampaignRecipient(campaign=campaign, phone_number=n)
            for n in normalised
        ]
        CampaignRecipient.objects.bulk_create(recipient_objs)

        # ── 6. Build Meta components (same for all recipients) ───────────────
        components = _build_template_components(template, variables)

        # ── 7. Send to Meta one-by-one ───────────────────────────────────────
        sent_count   = 0
        failed_count = 0
        error_samples = []

        recipients = CampaignRecipient.objects.filter(campaign=campaign).order_by("id")

        for recipient in recipients:
            success, msg_id, error_detail = _send_meta_template_message(
                phone_number_id=waba.phone_number_id,
                access_token=token,
                to_number=recipient.phone_number,
                template_name=template.name,
                language_code=template.language,
                components=components,
            )

            if success:
                recipient.status         = "sent"
                recipient.meta_message_id = msg_id
                recipient.sent_at        = timezone.now()
                sent_count += 1
            else:
                recipient.status       = "failed"
                recipient.error_detail = error_detail
                failed_count += 1
                if len(error_samples) < 5:
                    error_samples.append(f"{recipient.phone_number}: {error_detail}")
                logger.warning(
                    "Campaign %s — failed to send to %s: %s",
                    campaign.id, recipient.phone_number, error_detail,
                )

            recipient.save()

        # ── 8. Finalise Campaign ─────────────────────────────────────────────
        campaign.sent_count   = sent_count
        campaign.failed_count = failed_count
        campaign.error_log    = "\n".join(error_samples)
        campaign.status       = "completed" if failed_count == 0 else (
            "failed" if sent_count == 0 else "completed"
        )
        campaign.save()

        return Response({
            "id":            campaign.id,
            "name":          campaign.name,
            "template_name": campaign.template_name,
            "status":        campaign.status,
            "total_count":   campaign.total_count,
            "sent_count":    sent_count,
            "failed_count":  failed_count,
            "invalid_numbers_skipped": len(invalid),
            "created_at":    campaign.created_at.isoformat(),
        }, status=201)


class CampaignDetailView(APIView):
    """
    GET /api/campaigns/<id>/  → full campaign detail + recipient list
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        org, waba, err = _get_org_and_waba(request.user)
        if err:
            return Response(err, status=400)

        try:
            campaign = Campaign.objects.get(id=pk, organization=org)
        except Campaign.DoesNotExist:
            return Response({"error": "Campaign not found."}, status=404)

        recipients = campaign.recipients.all().order_by("id")
        recipient_data = [
            {
                "phone_number":    r.phone_number,
                "status":          r.status,
                "meta_message_id": r.meta_message_id,
                "error_detail":    r.error_detail,
                "sent_at":         r.sent_at.isoformat() if r.sent_at else None,
            }
            for r in recipients
        ]

        return Response({
            "id":            campaign.id,
            "name":          campaign.name,
            "template_name": campaign.template_name,
            "status":        campaign.status,
            "total_count":   campaign.total_count,
            "sent_count":    campaign.sent_count,
            "failed_count":  campaign.failed_count,
            "variables":     campaign.variables,
            "error_log":     campaign.error_log,
            "created_at":    campaign.created_at.isoformat(),
            "recipients":    recipient_data,
        })
