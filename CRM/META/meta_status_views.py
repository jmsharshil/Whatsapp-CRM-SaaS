import requests

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from CRM.models import ClientAccount


class WhatsAppAccountStatusView(APIView):

    permission_classes = [IsAuthenticated]

    def get(self, request):

        user = request.user

        # ─────────────────────────────────────
        # GET CLIENT ACCOUNT
        # ─────────────────────────────────────

        try:

            client = ClientAccount.objects.get(
                user=user
            )

        except ClientAccount.DoesNotExist:

            return Response(
                {
                    "success": False,
                    "message": "Client account not found"
                },
                status=404
            )

        # ─────────────────────────────────────
        # CHECK CONNECTION
        # ─────────────────────────────────────

        if not client.access_token:

            return Response(
                {
                    "success": False,
                    "message": "WhatsApp not connected"
                },
                status=400
            )

        if not client.phone_number_id:

            return Response(
                {
                    "success": False,
                    "message": "Phone number ID missing"
                },
                status=400
            )

        if not client.waba_id:

            return Response(
                {
                    "success": False,
                    "message": "WABA ID missing"
                },
                status=400
            )

        # ─────────────────────────────────────
        # HEADERS
        # ─────────────────────────────────────

        headers = {
            "Authorization":
                f"Bearer {client.access_token}"
        }

        try:

            # ─────────────────────────────────
            # PHONE NUMBER DETAILS API
            # ─────────────────────────────────

            phone_url = (
                f"https://graph.facebook.com/v22.0/"
                f"{client.phone_number_id}"
            )

            phone_params = {
                "fields": ",".join([

                    "verified_name",

                    "display_phone_number",

                    "quality_rating",

                    "name_status",

                    "code_verification_status",

                    "platform_type",

                    "throughput",

                ])
            }

            phone_response = requests.get(
                phone_url,
                headers=headers,
                params=phone_params,
                timeout=20
            )

            phone_data = phone_response.json()

            # ─────────────────────────────────
            # PHONE API ERROR
            # ─────────────────────────────────

            if "error" in phone_data:
                phone_data = {}

            # ─────────────────────────────────
            # WABA DETAILS API
            # ─────────────────────────────────

            waba_url = (
                f"https://graph.facebook.com/v22.0/"
                f"{client.waba_id}"
            )

            waba_params = {
                "fields": ",".join([

                    "id",

                    "name",

                    "currency",

                    "account_review_status",

                ])
            }

            waba_response = requests.get(
                waba_url,
                headers=headers,
                params=waba_params,
                timeout=20
            )

            waba_data = waba_response.json()

            # ─────────────────────────────────
            # WABA API ERROR
            # ─────────────────────────────────

            if "error" in waba_data:
                waba_data = {}

            # ─────────────────────────────────
            # THROUGHPUT
            # ─────────────────────────────────

            throughput_data = (
                phone_data.get(
                    "throughput",
                    {}
                )
            )

            messaging_limit = (
                throughput_data.get(
                    "level"
                )
                or "STANDARD"
            )

            # ─────────────────────────────────
            # CONNECTION STATUS
            # ─────────────────────────────────

            connection_status = (
                "connected"
                if client.waba_id
                else "not_connected"
            )

            webhook_status = (
                "connected"
                if client.phone_number_id
                else "not_connected"
            )

            # ─────────────────────────────────
            # FINAL RESPONSE
            # ─────────────────────────────────

            return Response({

                "success": True,

                "whatsapp": {

                    # BASIC

                    "waba_name":
                        waba_data.get("name") or client.waba_name,

                    "waba_id":
                        client.waba_id,

                    "currency":
                        waba_data.get("currency"),

                    # PHONE

                    "phone_number":
                        phone_data.get(
                            "display_phone_number"
                        ) or client.phone_number,

                    "phone_number_id":
                        client.phone_number_id,

                    "verified_name":
                        phone_data.get(
                            "verified_name"
                        ),

                    # STATUS

                    "quality_rating":
                        phone_data.get(
                            "quality_rating"
                        ),

                    "name_status":
                        phone_data.get(
                            "name_status"
                        ),

                    "verification_status":
                        phone_data.get(
                            "code_verification_status"
                        ),

                    "review_status":
                        waba_data.get(
                            "account_review_status"
                        ),

                    # PLATFORM

                    "platform_type":
                        phone_data.get(
                            "platform_type"
                        ),

                    # MESSAGING LIMIT

                    "throughput":
                        messaging_limit,

                    # WEBHOOK

                    "webhook_status":
                        webhook_status,

                    # CONNECTION

                    "connection_status":
                        connection_status,

                    # META STATUS

                    "is_connected":
                        (
                            connection_status
                            == "connected"
                        ),

                    # RAW META DATA

                    "raw_phone_data":
                        phone_data,

                    "raw_waba_data":
                        waba_data,

                }

            })

        except requests.Timeout:

            return Response(
                {
                    "success": False,
                    "message":
                        "Meta API timeout"
                },
                status=408
            )

        except requests.ConnectionError:

            return Response(
                {
                    "success": False,
                    "message":
                        "Unable to connect Meta API"
                },
                status=503
            )

        except Exception as e:

            return Response(
                {
                    "success": False,
                    "message": str(e)
                },
                status=500
            )