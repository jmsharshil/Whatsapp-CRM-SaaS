import json
import logging
import os
import tempfile
import uuid
import re
import time
from datetime import datetime

import requests
from django.conf import settings
from CRM.models import Customer, Conversation, Message, ClientAccount
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from rest_framework import status
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import *
from .gigatel_utils import *
from .serializers import *

logger = logging.getLogger(__name__)


def handle_gigatel_message(msg: dict):
    view = WebhookView()
    view._handle_message(msg)



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

@method_decorator(csrf_exempt, name="dispatch")
class WebhookView(View):

    def get(self, request):
        mode      = request.GET.get("hub.mode")
        token     = request.GET.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge")
#        logger.debug(
#            "[WEBHOOK] GET verify: mode=%s token_match=%s",
#            mode, token == os.environ.get("VERIFY_TOKEN", "")
#        )
        if mode == "subscribe" and token == os.environ.get("VERIFY_TOKEN", ""):
#            logger.info("[WEBHOOK] ✅ Hub verification passed")
            return HttpResponse(challenge, status=200)
#        logger.warning("[WEBHOOK] ❌ Hub verification failed: mode=%s", mode)
        return HttpResponse("Forbidden", status=403)

    def post(self, request):
#        logger.debug("[WEBHOOK] POST raw body=%s", request.body[:1000])
        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            logger.error("[WEBHOOK] ❌ JSON decode failed")
            return JsonResponse({"status": "bad request"}, status=400)

        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})

                for status_update in value.get("statuses", []):
                    pass
#                    logger.debug(
#                        "[WEBHOOK] Status update: msg_id=%s status=%s",
#                        status_update.get("id"), status_update.get("status")
#                    )

                for msg in value.get("messages", []):
#                    logger.info(
#                        "[WEBHOOK] Inbound message: from=%s type=%s id=%s",
#                        msg.get("from"), msg.get("type"), msg.get("id")
#                    )
                    self._handle_message(msg)

        return JsonResponse({"status": "ok"})

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN DISPATCHER
    # ─────────────────────────────────────────────────────────────────────────

    def _handle_message(self, msg: dict):
        number   = msg.get("from", "")
        msg_id   = msg.get("id", "")
        msg_type = msg.get("type", "text")

        body = ""
        if msg_type == "text":
            body = msg.get("text", {}).get("body", "").strip()
        elif msg_type == "interactive":
            idata = msg.get("interactive", {})
            itype = idata.get("type")
            if itype == "button_reply":
                body = idata["button_reply"]["id"]
            elif itype == "list_reply":
                body = idata["list_reply"]["id"]
        elif msg_type == "button":
            body = msg.get("button", {}).get("payload", "").strip()
        elif msg_type == "image":
            body = "[image]"

#        logger.info(
#            "[DISPATCH] from=%s type=%s body=%r id=%s",
#            number, msg_type, body, msg_id
#        )

        customer_obj, _ = Customer.objects.get_or_create(phone=number, defaults={'name': number})
        gigatel_phone_id = os.environ.get("META_PHONE_NUMBER_ID")
        client_account_obj = ClientAccount.objects.filter(phone_number_id=gigatel_phone_id).first()
        conv_obj, _ = Conversation.objects.get_or_create(customer=customer_obj, phone_number_id=gigatel_phone_id, defaults={'client': client_account_obj})
        Message.objects.create(
            conversation=conv_obj,
            client=client_account_obj,
            customer=customer_obj,
            meta_message_id=msg_id,
            direction="inbound",
            message_type=msg_type if msg_type in ['text', 'template', 'image', 'document', 'video'] else 'text',
            content=body,
            status='delivered'
        )

        session, created = WhatsAppSession.objects.get_or_create(mobile_number=number)
        if created:
            pass
#            logger.info("[DISPATCH] New session created for number=%s", number)

        state = session.state
#        logger.info("[DISPATCH] from=%s current_state=%s", number, state)

        # ── Ignore unexpected images (likely for Voicebot) ───────────────────
        if msg_type == "image" and state not in ["AWAIT_OTDR_IMAGE", "AWAIT_OTDR_IMAGE2"]:
#            logger.info("[DISPATCH] Ignoring unexpected image for state=%s (likely for Voicebot).", state)
            return

        # ── Trigger / Reset ──────────────────────────────────────────────────
        if (state in ("INIT", "DONE") and body) or re.search(r'\bhi\b', body.lower()):
#            logger.info(
#                "[DISPATCH] Trigger word found — resetting session for number=%s (was state=%s)",
#                number, state
#            )
            session.state               = "INIT"
            session.selected_circuit_id = ""
            session.nature_of_fault_id  = None
            session.otdr_applicable     = None
            session.otdr_from           = ""
            session.otdr_to             = ""
            session.otdr_value          = ""
            session.otdr_remark         = ""
            session.otdr_image1_path    = ""
            session.otdr_image1_url     = ""
            session.otdr_image2_url     = ""
            session.ticket_id           = ""
            session.ticket_raised_on    = ""
            session.save()
            self._step_init(number, session)
            return

        # ── State dispatch ───────────────────────────────────────────────────

        if state == "MENU":
            self._step_menu(number, session, body)

        elif state == "AWAIT_CIRCUIT_DIGITS":
            self._step_await_circuit_digits(number, session, body, for_ticket=False)

        elif state == "AWAIT_CIRCUIT_DIGITS_FOR_TICKET":
            self._step_await_circuit_digits(number, session, body, for_ticket=True)

        elif state == "AWAIT_CIRCUIT_CONFIRM":
            self._step_await_circuit_confirm(number, session, body, for_ticket=False)

        elif state == "AWAIT_CIRCUIT_CONFIRM_FOR_TICKET":
            self._step_await_circuit_confirm(number, session, body, for_ticket=True)

        elif state == "CIRCUIT_LIST":
            self._step_circuit_list(number, session, body)

        elif state == "CIRCUIT_SELECT_OR_DIGITS":
            self._step_circuit_select_or_digits(number, session, body, for_ticket=False)

        elif state == "CIRCUIT_SELECT_OR_DIGITS_FOR_TICKET":
            self._step_circuit_select_or_digits(number, session, body, for_ticket=True)

        elif state == "CIRCUIT_LIST_FOR_TICKET":
            self._step_circuit_list_for_ticket(number, session, body)

        elif state == "COMPLAINT_TYPE":
            self._step_complaint_type(number, session, body)

        elif state == "AWAIT_OTDR":
            self._step_await_otdr(number, session, body)

        elif state == "AWAIT_OTDR_FROM":
            self._step_await_otdr_from(number, session, body)

        elif state == "AWAIT_OTDR_TO":
            self._step_await_otdr_to(number, session, body)

        elif state == "AWAIT_OTDR_VALUE":
            self._step_await_otdr_value(number, session, body)

        elif state == "AWAIT_REMARK":
            self._step_await_remark(number, session, body)

        elif state == "AWAIT_OTDR_IMAGE":
            self._step_await_otdr_image(number, session, msg)

        elif state == "AWAIT_OTDR_IMAGE2":
            self._step_await_otdr_image2(number, session, msg)

        elif state == "AWAIT_OTDR_FIELD_SELECT":
            self._step_await_otdr_field_select(number, session, body)

        elif state == "AWAIT_FAULT_SIDE_CHECK":
            self._run_otdr_validation(number, session)

        elif state == "AWAIT_RAISE_ANYWAY":
            self._step_await_raise_anyway(number, session, body)

        else:
#            logger.warning(
#                "[DISPATCH] Unknown state=%s for number=%s — resetting", state, number
#            )
            session.state = "INIT"
            session.save()

    # ─────────────────────────────────────────────────────────────────────────
    # STEP: INIT
    # ─────────────────────────────────────────────────────────────────────────

    def _step_init(self, number: str, session: "WhatsAppSession"):
#        logger.info("[STEP] INIT → verifying mobile=%s", number)

        mobile   = self._clean_number(number)
        customer = verify_customer(mobile)

        if not customer:
#            logger.warning("[STEP] INIT: mobile=%s not found in CRM", mobile)
            ok = tpl_auth_failed(number)
            self._log_out(number, "[tpl] gigatel_auth_failed (mobile not in CRM)", ok)
            session.state = "DONE"
            session.save()
            return

        customer_id = customer.get("customerCompanyId") or customer.get("id") or customer.get("customerId")
        company_id = customer.get("customerCompanyId")

        session.customer_id         = customer_id
        session.customer_company_id = company_id

        contact_person = None
        
        def clean_api_num(num):
            return self._clean_number(str(num)) if num else ""

        if mobile == clean_api_num(customer.get("registeredOfficeMobileNo")):
            contact_person = customer.get("registeredOfficeContactPerson")
        elif mobile == clean_api_num(customer.get("billingOfficeMobileNo")):
            contact_person = customer.get("billingOfficeContactPerson")
        elif mobile == clean_api_num(customer.get("corporateOfficeMobileNo")):
            contact_person = customer.get("corporateOfficeContactPerson")
            
        if not contact_person:
            contact_person = (
                customer.get("registeredOfficeContactPerson")
                or customer.get("corporateOfficeContactPerson")
                or customer.get("billingOfficeContactPerson")
                or mobile
            )

        session.contact_person_name = contact_person

        session.customer_email = (
            customer.get("registeredOfficeEmail")
            or customer.get("billingOfficeEmail", "").split(";")[0].strip()
            or ""
        )

        session.state = "MENU"
        session.save()
#        logger.info("[STEP] INIT: ✅ verified → state=MENU for number=%s", number)

        ok = tpl_main_menu(number, session.contact_person_name)
        self._log_out(number, "[tpl] gigatel_main_menu", ok)

    def _step_menu(self, number: str, session: "WhatsAppSession", text: str):
#        logger.info("[STEP] MENU: number=%s choice=%r", number, text)

        if text == "complaints":
            circuits = get_circuits_by_customer(session.customer_id)
            valid_circuits = [
                c for c in circuits
                if str(c.get("circuitIdStr") or c.get("circuitId", "")).strip()
                not in ("", "None", "null")
            ]
            if not valid_circuits:
                ok = tpl_main_menu(number, session.contact_person_name)
                self._log_out(number, "[tpl] gigatel_main_menu (no valid circuits)", ok)
                return

            session_circuits = [
                str(c.get("circuitIdStr") or c.get("circuitId", "")).strip()
                for c in valid_circuits
            ]
            session.selected_circuit_id = json.dumps(session_circuits)

            if len(session_circuits) > 10:
                session.state = "CIRCUIT_SELECT_OR_DIGITS"
                session.save()
                ok1 = tpl_circuit_list_interactive(number, valid_circuits[:10])
                self._log_out(number, "[tpl] gigatel_circuit_list_interactive (combined, first 10)", ok1)
                
                ok0 = tpl_circuit_list_interactive(number, valid_circuits)
                self._log_out(number, "[tpl] gigatel_circuit_list_pdf_and_text", ok0)
                
                ok2 = tpl_circuit_last4_prompt(number, len(session_circuits))
                self._log_out(number, "[tpl] gigatel_circuit_last4_prompt (combined)", ok2)
            else:
                session.state = "CIRCUIT_LIST"
                session.save()
                ok = tpl_circuit_list_interactive(number, valid_circuits)
                self._log_out(number, "[tpl] gigatel_circuit_list_interactive", ok)

        elif text == "current_ticket":
            # API integration: fetch running tickets directly
            tickets = get_running_tickets(session.customer_company_id)

            if not tickets:
                ok = tpl_no_ticket_found(number, "N/A")
                self._log_out(number, "[tpl] gigatel_no_ticket_found", ok)
            else:
                for t in tickets[:5]:  # limit to max 5 to avoid spam
                    t_id = str(t.get("transactionNo") or t.get("id", "N/A"))
                    status = str(t.get("ticketStatus", "Open"))
                    
                    circuit_from = str(t.get("circuitFrom") or "").strip()
                    circuit_to = str(t.get("circuitTo") or "").strip()
                    circuit_id_str = str(t.get("circuitIdStr") or "").strip()
                    
                    if circuit_from and circuit_to:
                        circuit_id_display = f"{circuit_from} to {circuit_to}"
                    elif circuit_from:
                        circuit_id_display = circuit_from
                    elif circuit_to:
                        circuit_id_display = circuit_to
                    else:
                        circuit_id_display = "N/A"
                        
                    if circuit_id_str:
                        circuit_id = circuit_id_str
                        from_and_to = circuit_id_display
                    else:
                        circuit_id = circuit_id_display
                        from_and_to = ""
                        
                    ticket_date_raw = t.get("ticketDate", "")
                    created_on = "N/A"
                    if ticket_date_raw:
                        try:
                            # 2026-06-30T15:39:57.81 or similar ISO format
                            dt = datetime.fromisoformat(ticket_date_raw)
                            created_on = dt.strftime("%d %b %Y, %H:%M")
                        except Exception:
                            # Fallback if unparseable
                            created_on = str(ticket_date_raw)
                            
                    ok = tpl_current_ticket(number, circuit_id, t_id, status, created_on, from_and_to)
                    self._log_out(number, "[tpl] gigatel_current_ticket", ok)
                    time.sleep(1)

            session.state = "MENU"
            session.save()

        elif text == "sales":
#            logger.info("[STEP] MENU: sales selected (coming soon) — number=%s", number)
            ok = tpl_sales_coming_soon(number)
            self._log_out(number, "[tpl] gigatel_sales_coming_soon", ok)

        elif text == "feasibility":
#            logger.info("[STEP] MENU: feasibility selected (coming soon) — number=%s", number)
            ok = tpl_feasibility_coming_soon(number)
            self._log_out(number, "[tpl] check_feasibility_coming_soon", ok)

        elif text == "main_menu":
            ok = tpl_main_menu(number, session.contact_person_name)
            self._log_out(number, "[tpl] gigatel_main_menu (re-sent)", ok)

        else:
#            logger.warning("[STEP] MENU: unrecognised choice=%r — re-sending menu", text)
            ok = tpl_main_menu(number, session.contact_person_name)
            self._log_out(number, "[tpl] gigatel_main_menu (unrecognised choice)", ok)

    # ─────────────────────────────────────────────────────────────────────────
    # STEP: CIRCUIT LIST → COMPLAINT
    #
    # Flow:
    #   1. Fetch circuit detail
    #   2. Check open ticket → if open: show existing ticket info, go back to MENU
    # ─────────────────────────────────────────────────────────────────────────

    def _step_circuit_list(self, number: str, session: "WhatsAppSession", text: str):
#        logger.info("[STEP] CIRCUIT_LIST: number=%s input=%r", number, text)

        try:
            circuit_list = json.loads(session.selected_circuit_id)
        except Exception:
            circuit_list = []

        circuit_id = text.strip() if text else None

        if not circuit_id or circuit_id not in circuit_list:
#            logger.warning("[STEP] CIRCUIT_LIST: invalid circuit_id=%r", circuit_id)
            circuits = get_circuits_by_customer(session.customer_id)
            if circuits:
                ok = tpl_circuit_list_interactive(number, circuits[:10])
                self._log_out(number, "[tpl] gigatel_circuit_list_interactive (retry)", ok)
            return

        # Fetch circuit detail
        detail = get_circuit_detail(circuit_id)

        if not detail:
            logger.error(
                "[STEP] CIRCUIT_LIST: failed to fetch detail for circuit_id=%s", circuit_id
            )
            ok = tpl_main_menu(number, session.contact_person_name)
            self._log_out(number, "[tpl] gigatel_main_menu (circuit detail fetch failed)", ok)
            session.state = "MENU"
            session.save()
            return

        # Check if open ticket exists
        open_ticket_id = detail.get("ticketNo")
        ticket_status  = (detail.get("ticketStatus") or "").strip().lower()

        CLOSED_STATUSES = {"task complete", "closed"}
        is_open = bool(open_ticket_id) and (ticket_status not in CLOSED_STATUSES)

#        logger.info(
#            "[STEP] CIRCUIT_LIST: circuit_id=%s open_ticket_id=%s is_open=%s",
#            circuit_id, open_ticket_id, is_open
#        )

        if is_open:
#            logger.info(
#                "[STEP] CIRCUIT_LIST: ❌ Open ticket=%s exists — blocking new complaint",
#                open_ticket_id
#            )

            raised_on = detail.get("ticketCreatedOn") or session.ticket_raised_on or "N/A"
            ticket_status_display = detail.get("ticketStatus") or "Open"

            if session.customer_email:
                email_existing_ticket(session.customer_email, circuit_id, str(open_ticket_id), ticket_status_display, raised_on)

            ok = tpl_open_ticket_exists(
                number,
                circuit_id,
                str(open_ticket_id),
                ticket_status_display,
                raised_on,
            )
            self._log_out(number, "[tpl] gigatel_open_ticket_exists", ok)
            session.state = "MENU"
            session.selected_circuit_id = circuit_id
            session.save()
            return

        # No open ticket → proceed to complaint type
        session.selected_circuit_id = circuit_id
        session.state = "AWAIT_OTDR"

        # Store numeric circuit ID for OTDR validation
        numeric_id = detail.get("id")

        if numeric_id is not None:
            try:
                session.circuit_numeric_id = int(numeric_id)
            except (TypeError, ValueError):
                session.circuit_numeric_id = None

        session.nature_of_fault_id = 7
        session.save()

#        logger.info("[STEP] CIRCUIT_LIST: ✅ Proceeding to OTDR for circuit=%s", circuit_id)
        from_and_to = detail.get("fromAndTo") or ""
        ok = tpl_otdr_question(number, circuit_id, from_and_to)
        self._log_out(number, "[tpl] gigatel_otdr_question", ok)

    def _step_circuit_select_or_digits(self, number: str, session: "WhatsAppSession", text: str, for_ticket: bool):
        """
        Combined step for the >10-circuits case: user can EITHER
          - tap a row from the interactive list (circuit_id arrives as body), OR
          - type the last 4 digits of their circuit id directly.
        """
#        logger.info(
#            "[STEP] CIRCUIT_SELECT_OR_DIGITS%s: number=%s input=%r",
#            " (ticket)" if for_ticket else "", number, text
#        )

        try:
            circuit_list = json.loads(session.selected_circuit_id)
        except Exception:
            circuit_list = []

        candidate = (text or "").strip()

        # Case 1: tapped from interactive list — body is the full circuit_id
        if candidate and candidate in circuit_list:
            if for_ticket:
                session.selected_circuit_id = candidate
                session.state = "MENU"
                session.save()
                self._show_current_ticket(number, session)
            else:
                session.selected_circuit_id = candidate
                session.save()
                self._proceed_with_valid_circuit(number, session, candidate)
            return

        # Case 2: typed last characters directly
        is_valid_format = len(candidate) in (4, 5, 6) and candidate.isalnum()
        if is_valid_format:
            matches = [cid for cid in circuit_list if str(cid).strip().upper().endswith(candidate.upper())]

            if not matches:
                ok = tpl_circuit_not_found(number)
                self._log_out(number, "[tpl] gigatel_circuit_not_found", ok)
                return

            if len(matches) > 1:
                ok = tpl_circuit_multiple_match(number)
                self._log_out(number, "[tpl] gigatel_circuit_multiple_match", ok)
                ok2 = tpl_contact_team(number)
                self._log_out(number, "[tpl] gigatel_contact_team", ok2)
                session.state = "DONE"
                session.save()
                return

            matched_cid = matches[0]
            detail = get_circuit_detail(matched_cid) or {}
            from_st = (detail.get("startAddress") or "").strip()
            to_st   = (detail.get("endAddress") or "").strip()

            session.selected_circuit_id = matched_cid
            session.state = "AWAIT_CIRCUIT_CONFIRM_FOR_TICKET" if for_ticket else "AWAIT_CIRCUIT_CONFIRM"
            session.save()

            ok = tpl_circuit_confirm(number, matched_cid, from_st, to_st)
            self._log_out(number, "[tpl] gigatel_circuit_confirm", ok)
            return

        # Case 3: garbage input — re-send both prompts
#        logger.warning(
#            "[STEP] CIRCUIT_SELECT_OR_DIGITS%s: invalid input=%r — retry",
#            " (ticket)" if for_ticket else "", text
#        )
        circuits = get_circuits_by_customer(session.customer_id)
        valid_circuits = [
            c for c in circuits
            if str(c.get("circuitIdStr") or c.get("circuitId", "")).strip()
            not in ("", "None", "null")
        ]
        if valid_circuits:
            ok1 = tpl_circuit_list_interactive(number, valid_circuits[:10], is_for_ticket=for_ticket)
            self._log_out(number, "[tpl] gigatel_circuit_list_interactive (retry combined)", ok1)
        ok2 = tpl_circuit_last4_prompt(number, len(circuit_list))
        self._log_out(number, "[tpl] gigatel_circuit_last4_prompt (retry combined)", ok2)

    def _proceed_with_valid_circuit(self, number: str, session: "WhatsAppSession", circuit_id: str):
        detail = get_circuit_detail(circuit_id)
        if not detail:
            ok = tpl_main_menu(number, session.contact_person_name)
            self._log_out(number, "[tpl] gigatel_main_menu (circuit detail fetch failed)", ok)
            session.state = "MENU"
            session.save()
            return

        open_ticket_id = detail.get("ticketNo")
        ticket_status  = (detail.get("status") or "").strip().lower()
        CLOSED_STATUSES = {"task complete", "closed"}
        is_open = bool(open_ticket_id) and (ticket_status not in CLOSED_STATUSES)

        if is_open:
            raised_on = detail.get("ticketCreatedOn") or session.ticket_raised_on or "N/A"
            ticket_status_display = detail.get("ticketStatus") or "Open"
            
            if session.customer_email:
                email_existing_ticket(session.customer_email, circuit_id, str(open_ticket_id), ticket_status_display, raised_on)
                
            ok = tpl_open_ticket_exists(number, circuit_id, str(open_ticket_id), ticket_status_display, raised_on)
            self._log_out(number, "[tpl] gigatel_open_ticket_exists", ok)
            session.state = "MENU"
            session.selected_circuit_id = circuit_id
            session.save()
            return

        session.selected_circuit_id = circuit_id
        session.state = "AWAIT_OTDR"
        numeric_id = detail.get("id")
        if numeric_id is not None:
            try:
                session.circuit_numeric_id = int(numeric_id)
            except (TypeError, ValueError):
                session.circuit_numeric_id = None
        
        session.nature_of_fault_id = 7
        session.save()

        from_and_to = detail.get("fromAndTo") or ""
        ok = tpl_otdr_question(number, circuit_id, from_and_to)
        self._log_out(number, "[tpl] gigatel_otdr_question", ok)

    def _step_await_circuit_digits(self, number: str, session: "WhatsAppSession", text: str, for_ticket: bool):
        digits = (text or "").strip()

        is_valid_format = len(digits) in (4, 5, 6) and digits.isalnum()

        if not is_valid_format:
            try:
                circuit_count = len(json.loads(session.selected_circuit_id))
            except Exception:
                circuit_count = 0
            ok = tpl_circuit_last4_prompt(number, circuit_count)
            self._log_out(number, "[tpl] gigatel_circuit_last4_prompt (retry — invalid input)", ok)
            return

        try:
            circuit_list = json.loads(session.selected_circuit_id)
        except Exception:
            circuit_list = []

        matches = [cid for cid in circuit_list if str(cid).strip().upper().endswith(digits.upper())]

        if not matches:
            ok = tpl_circuit_not_found(number)
            self._log_out(number, "[tpl] gigatel_circuit_not_found", ok)
            return

        if len(matches) > 1:
            ok = tpl_circuit_multiple_match(number)
            self._log_out(number, "[tpl] gigatel_circuit_multiple_match", ok)
            ok2 = tpl_contact_team(number)
            self._log_out(number, "[tpl] gigatel_contact_team", ok2)
            session.state = "DONE"
            session.save()
            return

        matched_cid = matches[0]
        detail = get_circuit_detail(matched_cid) or {}
        from_st = (detail.get("startAddress") or "").strip()
        to_st   = (detail.get("endAddress") or "").strip()

        session.selected_circuit_id = matched_cid
        session.state = "AWAIT_CIRCUIT_CONFIRM_FOR_TICKET" if for_ticket else "AWAIT_CIRCUIT_CONFIRM"
        session.save()

        ok = tpl_circuit_confirm(number, matched_cid, from_st, to_st)
        self._log_out(number, "[tpl] gigatel_circuit_confirm", ok)

    def _step_await_circuit_confirm(self, number: str, session: "WhatsAppSession", text: str, for_ticket: bool):
        if text == "circuit_confirm__yes":
            circuit_id = session.selected_circuit_id
            if for_ticket:
                session.state = "MENU"
                session.save()
                self._show_current_ticket(number, session)
            else:
                self._proceed_with_valid_circuit(number, session, circuit_id)

        elif text == "circuit_confirm__no":
            ok = tpl_contact_team(number)
            self._log_out(number, "[tpl] gigatel_contact_team", ok)
            session.state = "DONE"
            session.save()

        else:
            detail = get_circuit_detail(session.selected_circuit_id) or {}
            from_st = (detail.get("startAddress") or "").strip()
            to_st   = (detail.get("endAddress") or "").strip()
            ok = tpl_circuit_confirm(number, session.selected_circuit_id, from_st, to_st)
            self._log_out(number, "[tpl] gigatel_circuit_confirm (retry)", ok)

    # ─────────────────────────────────────────────────────────────────────────
    # STEP: CIRCUIT LIST → CURRENT TICKET
    # ─────────────────────────────────────────────────────────────────────────

    def _step_circuit_list_for_ticket(self, number: str, session: "WhatsAppSession", text: str):
#        logger.info("[STEP] CIRCUIT_LIST_FOR_TICKET: number=%s input=%r", number, text)

        try:
            circuit_list = json.loads(session.selected_circuit_id)
        except Exception:
            circuit_list = []

        circuit_id = text.strip() if text else None

        if not circuit_id or circuit_id not in circuit_list:
            circuits = get_circuits_by_customer(session.customer_id)
            if circuits:
                ok = tpl_circuit_list_interactive(number, circuits[:10], is_for_ticket=True)
                self._log_out(
                    number, "[tpl] gigatel_circuit_list_interactive (retry ticket)", ok
                )
            return

        session.selected_circuit_id = circuit_id
        session.state               = "MENU"
        session.save()
        self._show_current_ticket(number, session)

    # ─────────────────────────────────────────────────────────────────────────
    # STEP: COMPLAINT TYPE (fault selection)
    # ─────────────────────────────────────────────────────────────────────────

    def _step_complaint_type(self, number: str, session: "WhatsAppSession", text: str):
#        logger.info("[STEP] COMPLAINT_TYPE: number=%s input=%r", number, text)

        fault_key = None

        if text.startswith("fault__"):
            fault_key = text[len("fault__"):]

        elif text.isdigit():
            num_map = {
                "1": "packet_loss",
                "2": "fibre_cut",
                "3": "link_down",
                "4": "degradation",
                "5": "other",
            }
            fault_key = num_map.get(text)

        if not fault_key or fault_key not in FAULT_MAP:
#            logger.warning("[STEP] COMPLAINT_TYPE: unrecognised input=%r — retry", text)
            ok = tpl_complaint_type(number, session.selected_circuit_id)
            self._log_out(number, "[tpl] gigatel_complaint_type (retry)", ok)
            return

        session.nature_of_fault_id = FAULT_MAP[fault_key]
        session.fault_label = FAULT_LABELS[fault_key]
        session.save()

#        logger.info("[STEP] COMPLAINT_TYPE: fault=%s → asking OTDR yes/no", fault_key)

        # Always ask user about OTDR
        session.state = "AWAIT_OTDR"
        session.save()

        detail = get_circuit_detail(session.selected_circuit_id) or {}
        from_and_to = detail.get("fromAndTo") or ""
        ok = tpl_otdr_question(number, session.selected_circuit_id, from_and_to)
        self._log_out(number, "[tpl] gigatel_otdr_question", ok)

    # ─────────────────────────────────────────────────────────────────────────
    # STEP: AWAIT_OTDR
    # ─────────────────────────────────────────────────────────────────────────

    def _step_await_otdr(self, number: str, session: "WhatsAppSession", text: str):
#        logger.info("[STEP] AWAIT_OTDR (manual fallback): number=%s input=%r", number, text)

        if text == "otdr__yes":
            session.otdr_applicable  = True
            session.otdr_from        = ""
            session.otdr_to          = ""
            session.otdr_value       = ""
            session.otdr_remark      = ""
            session.otdr_image1_path = ""

            detail = get_circuit_detail(session.selected_circuit_id) or {}
            from_station = (detail.get("startAddress") or "").strip()
            to_station   = (detail.get("endAddress") or "").strip()

            if not from_station and not to_station:
#                logger.warning(
#                    "[STEP] AWAIT_OTDR: no startAddress/endAddress for circuit=%s — manual fallback",
#                    session.selected_circuit_id
#                )
                session.state = "AWAIT_OTDR_FROM"
                session.save()
                ok = tpl_otdr_from_prompt(number)
                self._log_out(number, "[tpl] gigatel_otdr_from_prompt (manual fallback)", ok)
                return

            session.otdr_from = from_station
            session.otdr_to   = to_station
            session.state     = "AWAIT_OTDR_FIELD_SELECT"
            session.save()

            ok = tpl_otdr_field_select(number, from_station, to_station)
            self._log_out(number, "[tpl] gigatel_otdr_field_select", ok)

        elif text == "otdr__no":
            session.otdr_applicable = False
            session.save()
            self._submit_complaint(
                number=number,
                session=session,
                remark="",
            )

        else:
            detail = get_circuit_detail(session.selected_circuit_id) or {}
            from_and_to = detail.get("fromAndTo") or ""
            ok = tpl_otdr_question(number, session.selected_circuit_id, from_and_to)
            self._log_out(number, "[tpl] gigatel_otdr_question (retry)", ok)

    # ─────────────────────────────────────────────────────────────────────────
    # STEP: OTDR STEP-BY-STEP
    # ─────────────────────────────────────────────────────────────────────────

    def _step_await_otdr_field_select(self, number: str, session: "WhatsAppSession", text: str):
#        logger.info("[STEP] AWAIT_OTDR_FIELD_SELECT: number=%s input=%r", number, text)

        if text == "otdr_field__from":
            session.otdr_to = ""              # to clear — from is the real station name
            session.state   = "AWAIT_OTDR_VALUE"
            session.save()
            ok = tpl_otdr_value_prompt(number)
            self._log_out(number, "[tpl] gigatel_otdr_value_prompt", ok)

        elif text == "otdr_field__to":
            session.otdr_from = ""            # from clear — to is the real station name
            session.state     = "AWAIT_OTDR_VALUE"
            session.save()
            ok = tpl_otdr_value_prompt(number)
            self._log_out(number, "[tpl] gigatel_otdr_value_prompt", ok)

        else:
            ok = tpl_otdr_field_select(number, session.otdr_from, session.otdr_to)
            self._log_out(number, "[tpl] gigatel_otdr_field_select (retry)", ok)

    def _step_await_otdr_from(self, number: str, session: "WhatsAppSession", text: str):
#        logger.info("[STEP] AWAIT_OTDR_FROM: number=%s input=%r", number, text)

        if not text or text == "[image]":
            ok = tpl_otdr_from_prompt(number)
            self._log_out(number, "[tpl] gigatel_otdr_from_prompt (retry)", ok)
            return

        session.otdr_from = text.strip()
        session.otdr_to   = ""              # To clear — not used
        session.state     = "AWAIT_OTDR_VALUE"
        session.save()
        ok = tpl_otdr_value_prompt(number)
        self._log_out(number, "[tpl] gigatel_otdr_value_prompt", ok)

    def _step_await_otdr_to(self, number: str, session: "WhatsAppSession", text: str):
#        logger.info("[STEP] AWAIT_OTDR_TO: number=%s input=%r", number, text)

        if not text or text == "[image]":
            ok = tpl_otdr_to_prompt(number)
            self._log_out(number, "[tpl] gigatel_otdr_to_prompt (retry)", ok)
            return

        session.otdr_to   = text.strip()
        session.otdr_from = ""              # From clear — not used
        session.state     = "AWAIT_OTDR_VALUE"
        session.save()
        ok = tpl_otdr_value_prompt(number)
        self._log_out(number, "[tpl] gigatel_otdr_value_prompt", ok)

    def _step_await_otdr_value(self, number: str, session: "WhatsAppSession", text: str):
#        logger.info("[STEP] AWAIT_OTDR_VALUE: number=%s input=%r", number, text)

        if not text or text == "[image]":
            ok = tpl_otdr_value_prompt(number)
            self._log_out(number, "[tpl] gigatel_otdr_value_prompt (retry)", ok)
            return

        value = text.strip()
        
        # Validation: meters only (numeric), no decimals, max 6 digits
        if not value.isdigit() or len(value) > 6:
            ok = tpl_invalid_meter_value(number)
            self._log_out(number, "[tpl] invalid_meter_value", ok)
            return

        session.otdr_value = value
        session.state      = "AWAIT_OTDR_IMAGE"
        session.save()
        ok = tpl_otdr_image_prompt(number)
        self._log_out(number, "[tpl] gigatel_otdr_image_prompt", ok)

    # ─────────────────────────────────────────────────────────────────────────
    # STEP: REMARK
    # ─────────────────────────────────────────────────────────────────────────

    def _step_await_remark(self, number: str, session: "WhatsAppSession", text: str):
#        logger.info("[STEP] AWAIT_REMARK: number=%s remark=%r", number, text)

        if not text or text == "[image]":
            ok = tpl_remark_prompt(number)
            self._log_out(number, "[tpl] gigatel_remark_prompt (retry)", ok)
            return

        session.otdr_remark = text.strip()
        session.save()

        if session.otdr_applicable:
            # OTDR path → image upload
            session.state = "AWAIT_OTDR_IMAGE"
            session.save()
            ok = tpl_otdr_image_prompt(number)
            self._log_out(number, "[tpl] gigatel_otdr_image_prompt", ok)
        else:
            # No OTDR → submit directly
            self._submit_complaint(
                number=number,
                session=session,
                remark=text.strip(),
            )

    def _step_await_otdr_image(self, number: str, session: "WhatsAppSession", msg: dict):
#        logger.info("[STEP] AWAIT_OTDR_IMAGE: number=%s msg_type=%s", number, msg.get("type"))

        if msg.get("type") != "image":
            ok = tpl_otdr_image_prompt(number)
            self._log_out(number, "[tpl] gigatel_otdr_image_prompt (retry — not an image)", ok)
            return

        media_id  = msg.get("image", {}).get("id", "")
        media_url = self._get_media_url(media_id)

        if media_url:
            saved_url = self._download_and_save_image(media_url, prefix="otdr1")
            if saved_url:
                session.otdr_image1_url = saved_url
#                logger.info(
#                    "[STEP] AWAIT_OTDR_IMAGE: ✅ image1 saved locally for number=%s url=%s",
#                    number, saved_url
#                )
                
                # Send confirmation message is omitted because tpl_otdr_second_image_prompt already confirms it
            else:
                session.otdr_image1_url = media_url
#                logger.warning(
#                    "[STEP] AWAIT_OTDR_IMAGE: ⚠️ local save failed — storing Meta URL for number=%s",
#                    number
#                )
        else:
            session.otdr_image1_url = ""
#            logger.warning(
#                "[STEP] AWAIT_OTDR_IMAGE: ❌ could not get URL — continuing without image1"
#            )

        session.otdr_image1_path = ""
        session.otdr_image2_url  = ""
        session.state            = "AWAIT_OTDR_IMAGE2"
        session.save()
#        logger.info(
#            "[STEP] AWAIT_OTDR_IMAGE: image1_url=%r saved, state=AWAIT_OTDR_IMAGE2 for number=%s",
#            session.otdr_image1_url, number
#        )
        ok = tpl_otdr_second_image_prompt(number)
        self._log_out(number, "[tpl] gigatel_otdr_second_image_prompt", ok)

    def _step_await_otdr_image2(self, number: str, session: "WhatsAppSession", msg: dict):
#        logger.info("[STEP] AWAIT_OTDR_IMAGE2: number=%s type=%s", number, msg.get("type"))

        if msg.get("type") == "image":
            media_id  = msg.get("image", {}).get("id", "")
            media_url = self._get_media_url(media_id)

            if media_url:
                saved_url = self._download_and_save_image(media_url, prefix="otdr2")
                if saved_url:
                    session.otdr_image2_url = saved_url
#                    logger.info(
#                        "[STEP] AWAIT_OTDR_IMAGE2: ✅ image2 saved locally for number=%s url=%s",
#                        number, saved_url
#                    )
                    
                    # Send confirmation message
                    ok_conf = tpl_image_uploaded_successfully(number)
                    self._log_out(number, "[tpl] image_uploaded_successfully", ok_conf)
                else:
                    session.otdr_image2_url = media_url
#                    logger.warning(
#                        "[STEP] AWAIT_OTDR_IMAGE2: ⚠️ local save failed — storing Meta URL for number=%s",
#                        number
#                    )
            else:
                session.otdr_image2_url = ""
#                logger.warning(
#                    "[STEP] AWAIT_OTDR_IMAGE2: ❌ could not get image2 URL for number=%s",
#                    number
#                )
            session.save()

        else:
            pass
#            logger.info(
#                "[STEP] AWAIT_OTDR_IMAGE2: no image received — submitting with image1 only for number=%s",
#                number
#            )

        self._run_otdr_validation(number, session)

    # def _run_otdr_validation(self, number, session):
    #     if not session.circuit_numeric_id:
    #         logger.error(
    #             "[VALIDATE] circuit_numeric_id missing — submitting directly"
    #         )
    #         self._submit_complaint(number, session, remark=session.otdr_remark)
    #         return

    #     otdr_from_to = "from" if session.otdr_from else ("to" if session.otdr_to else "")

    #     result = check_otdr_fault_side(
    #         session.circuit_numeric_id,
    #         otdr_from_to,
    #         float(session.otdr_value) if session.otdr_value else 0.0,
    #     )

    #     # ── Parse fault side — API returns {"data": "Customer"} or {"data": "Gigatel"}
    #     raw_data = (result or {}).get("data", "")
    #     if isinstance(raw_data, dict):
    #         fault_side = raw_data.get("faultSide", "").strip().lower()
    #     else:
    #         fault_side = str(raw_data).strip().lower()   # "Customer" or "Gigatel"

    #     logger.info("[VALIDATE] fault_side=%r for number=%s", fault_side, number)
    #     session.fault_side = fault_side
    #     session.save()

    #     if fault_side == "gigatel":
    #         self._submit_complaint(number, session, remark=session.otdr_remark, ticket_status="COMPLAINT_REGISTERED")
    #     elif fault_side == "customer":
    #         ok1 = tpl_otdr_customer_fault_notice(number)
    #         self._log_out(number, "[tpl] gigatel_otdr_customer_fault_notice", ok1)
    #         ok2 = tpl_raise_anyway_question(number)
    #         self._log_out(number, "[tpl] gigatel_raise_anyway_question", ok2)
    #         session.state = "AWAIT_RAISE_ANYWAY"
    #         session.save()
    #     else:
    #         logger.warning("[VALIDATE] unknown fault_side=%r — submitting directly", fault_side)
    #         self._submit_complaint(
    #             number, session,
    #             remark=session.otdr_remark,
    #             ticket_status="COMPLAINT_REGISTERED"
    #         )

    def _run_otdr_validation(self, number, session):
        if not session.circuit_numeric_id:
            logger.error(
                "[VALIDATE] circuit_numeric_id missing — submitting directly"
            )
            self._submit_complaint(number, session, remark=session.otdr_remark)
            return

        otdr_from_to = "from" if session.otdr_from else ("to" if session.otdr_to else "")

        # Fetch fresh circuit detail so we have fromLastMileManagedBy /
        # toLastMileManagedBy + bifurcation data for the last-mile check.
        circuit_detail = get_circuit_detail(session.selected_circuit_id)

        result = check_otdr_fault_side(
            session.circuit_numeric_id,
            otdr_from_to,
            float(session.otdr_value) if session.otdr_value else 0.0,
            circuit_detail=circuit_detail,
        )

        # ── Parse fault side — API returns {"data": "Customer"} or {"data": "Gigatel"}
        raw_data = (result or {}).get("data", "")
        if isinstance(raw_data, dict):
            fault_side = raw_data.get("faultSide", "").strip().lower()
        else:
            fault_side = str(raw_data).strip().lower()   # "Customer" or "Gigatel"

#        logger.info("[VALIDATE] fault_side=%r for number=%s", fault_side, number)
        session.fault_side = fault_side
        session.save()

        if fault_side == "gigatel":
            self._submit_complaint(number, session, remark=session.otdr_remark, ticket_status="COMPLAINT_REGISTERED")
        elif fault_side == "customer":
            ok1 = tpl_otdr_customer_fault_notice(number)
            self._log_out(number, "[tpl] gigatel_otdr_customer_fault_notice", ok1)
            ok2 = tpl_raise_anyway_question(number)
            self._log_out(number, "[tpl] gigatel_raise_anyway_question", ok2)
            session.state = "AWAIT_RAISE_ANYWAY"
            session.save()
        else:
#            logger.warning("[VALIDATE] unknown fault_side=%r — submitting directly", fault_side)
            self._submit_complaint(
                number, session,
                remark=session.otdr_remark,
                ticket_status="COMPLAINT_REGISTERED"
            )

    def _step_await_raise_anyway(self, number, session, text):
        if text == "raise_anyway__yes":
            self._submit_complaint(number, session, remark=session.otdr_remark, ticket_status="CUSTOMER_DISPUTED_OTDR_RESULT")
        elif text == "raise_anyway__no":
            if session.customer_email:
                email_request_closed(session.customer_email, session.selected_circuit_id)
                
            ok = tpl_request_closed_customer_fault(number)
            self._log_out(number, "[tpl] gigatel_request_closed_customer_fault", ok)
            session.ticket_status_local = "CUSTOMER_FAULT"
            session.state = "DONE"
            session.save()
        else:
            ok = tpl_raise_anyway_question(number)
            self._log_out(number, "[tpl] gigatel_raise_anyway_question (retry)", ok)

    # ─────────────────────────────────────────────────────────────────────────
    # SUBMIT HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _submit_with_stored_images(self, number: str, session: "WhatsAppSession"):
        # cleanup old temp file if exists
        if session.otdr_image1_path:
            try:
                os.unlink(session.otdr_image1_path)
            except OSError:
                pass
            session.otdr_image1_path = ""
            session.save()

        self._submit_complaint(
            number=number,
            session=session,
            remark=session.otdr_remark or "OTDR submitted",
        )

    def _submit_complaint(
        self,
        number: str,
        session: "WhatsAppSession",
        remark: str,
        ticket_status: str = "COMPLAINT_REGISTERED",
    ):
        mobile = self._clean_number(number)
#        logger.info(
#            "[SUBMIT] number=%s circuit=%s fault_id=%s otdr=%s",
#            number, session.selected_circuit_id,
#            session.nature_of_fault_id, session.otdr_applicable
#        )

        # Ensure all values are strings for form-data encoding
        payload = {
            "Remark": str(remark or "Link Down"),
            "ContactPersonName": str(session.contact_person_name or mobile),
            "ContactPersonMobile": mobile,
            "ContactPersonEmail": str(session.customer_email or ""),
            "CircuitIdStr": str(session.selected_circuit_id or ""),
            "NatureOfFaultId": str(session.nature_of_fault_id) if session.nature_of_fault_id else "",
            "CustomerCompanyId": str(session.customer_company_id) if session.customer_company_id else "",
            "OTDRAvailable": "yes" if session.otdr_applicable else "no",
            "OTDRProvidedByCustomer": "yes" if session.otdr_applicable else "no",
            "UserId": "1",
            "CompanyCode": "GTPL",
        }

        if session.otdr_applicable:
            otdr_from_to = session.otdr_from or session.otdr_to or ""
            if otdr_from_to:
                payload["OTDRFrom"] = str(otdr_from_to)
            if session.otdr_value:
                payload["OTDRValue"] = str(session.otdr_value)
            if session.otdr_image1_url:
                payload["OTDRImage1Url"] = str(session.otdr_image1_url)
            if session.otdr_image2_url:
                payload["OTDRImage2Url"] = str(session.otdr_image2_url)

#        logger.warning("[SUBMIT] FINAL PAYLOAD → %s", payload)

        result = raise_complaint(payload)
        now = datetime.now().strftime("%d/%m/%Y %H:%M")

        if result:
            ticket_id = result.get("ticketId") or "N/A"
            is_duplicate = result.get("isDuplicate", False)
#            logger.info(
#                "[SUBMIT] ✅ ticket_id=%s duplicate=%s circuit=%s",
#                ticket_id, is_duplicate, session.selected_circuit_id
#            )

            if ticket_status == "CUSTOMER_DISPUTED_OTDR_RESULT":
                if session.customer_email:
                    email_ticket_raised(session.customer_email, session.selected_circuit_id, str(ticket_id), now, ticket_status)
                    
                ok = tpl_ticket_confirmation_disputed(
                    number,
                    str(ticket_id),
                    session.selected_circuit_id,
                    now,
                )
                self._log_out(number, f"[tpl] gigatel_ticket_confirmation_disputed ticket_id={ticket_id}", ok)
            else:
                if session.customer_email:
                    email_ticket_raised(session.customer_email, session.selected_circuit_id, str(ticket_id), now, ticket_status)
                    
                ok = tpl_ticket_confirmation(
                    number,
                    str(ticket_id),
                    session.selected_circuit_id,
                    now,
                )
                suffix = " (duplicate)" if is_duplicate else ""
                self._log_out(number, f"[tpl] gigatel_ticket_confirmation{suffix} ticket_id={ticket_id}", ok)

            session.ticket_id = str(ticket_id)
            session.ticket_raised_on = now
        else:
            logger.error("[SUBMIT] ❌ raise_complaint returned None")
            ok = tpl_complaint_failed(number)
            self._log_out(number, "[tpl] gigatel_complaint_failed", ok)

        session.state = "DONE"
        session.save()
#        logger.info("[SUBMIT] Session state=DONE for number=%s", number)

    # ─────────────────────────────────────────────────────────────────────────
    # CURRENT TICKET
    # ─────────────────────────────────────────────────────────────────────────

    def _show_current_ticket(self, number: str, session: "WhatsAppSession"):
        circuit_id = session.selected_circuit_id
#        logger.info("[CURRENT_TICKET] number=%s circuit_id=%r", number, circuit_id)

        if not circuit_id or circuit_id.startswith("["):
            ok = tpl_main_menu(number, session.contact_person_name)
            self._log_out(number, "[tpl] gigatel_main_menu (no specific circuit)", ok)
            session.state = "MENU"
            session.save()
            return

        detail = get_circuit_detail(circuit_id)

#        logger.warning(
#            "[CURRENT_TICKET_DEBUG] circuit_id=%s ticketId=%r ticketType=%r "
#            "status=%r created=%r",
#            circuit_id,
#            detail.get("ticketId")        if detail else None,
#            detail.get("ticketType")      if detail else None,
#            detail.get("ticketStatus")    if detail else None,
#            detail.get("ticketCreatedOn") if detail else None,
#        )

        ticket_id = detail.get("ticketNo")
        ticket_status = detail.get("ticketStatus") or "Open"
        ticket_created_on = detail.get("ticketCreatedOn") or detail.get("ticketAllottedOn") or detail.get("ticketStartTime") or "N/A"

        if ticket_id:
            from_and_to = detail.get("fromAndTo") or ""
            ok = tpl_current_ticket(
                number,
                str(circuit_id or "N/A"),
                str(ticket_id),
                ticket_status,
                ticket_created_on,
                from_and_to
            )
            self._log_out(number, "[tpl] gigatel_current_ticket", ok)
        else:
            ok = tpl_no_ticket_found(number, circuit_id)
            self._log_out(number, "[tpl] gigatel_no_ticket_found", ok)

        session.state = "MENU"
        session.save()

    # ─────────────────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────────────────

    def _log_out(self, number: str, text: str, success: bool = None):
        symbol = "✅" if success is True else ("❌" if success is False else "?")
#        logger.info(
#            "[OUT] %s number=%s template_call=%s result=%s",
#            symbol, number, text,
#            "accepted" if success else ("rejected" if success is False else "unknown")
#        )
        WhatsAppMessage.objects.create(
            mobile_number=number,
            direction="OUT",
            message_type="template",
            body=text,
        )

        try:
            from CRM.models import Customer, Conversation, Message, ClientAccount
            import os
            customer_obj, _ = Customer.objects.get_or_create(phone=number, defaults={'name': number})
            gigatel_phone_id = os.environ.get("META_PHONE_NUMBER_ID")
            client_account_obj = ClientAccount.objects.filter(phone_number_id=gigatel_phone_id).first()
            if client_account_obj:
                conv_obj, _ = Conversation.objects.get_or_create(
                    customer=customer_obj, 
                    phone_number_id=gigatel_phone_id, 
                    defaults={'client': client_account_obj}
                )
                Message.objects.create(
                    conversation=conv_obj,
                    client=client_account_obj,
                    customer=customer_obj,
                    direction="outbound",
                    message_type="template",
                    content=text,
                    status='sent'
                )
        except Exception as e:
            logger.error("Failed to sync outbound message to Conversation model: %s", e)

    @staticmethod
    def _clean_number(number: str) -> str:
        number = str(number).strip()
        if number.startswith("+"):
            number = number[1:]
        if number.startswith("91") and len(number) == 12:
            number = number[2:]
        return number[-10:]

    @staticmethod
    def _get_media_url(media_id: str) -> str:
        """Return the WhatsApp CDN URL for a media_id."""
        token = os.environ.get("WHATSAPP_TOKEN", "")
        try:
            r = requests.get(
                f"https://graph.facebook.com/v22.0/{media_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            r.raise_for_status()
            url = r.json().get("url", "")
#            logger.info("[MEDIA] Got URL for media_id=%s", media_id)
            return url
        except Exception as exc:
            logger.error("[MEDIA] ❌ Could not get URL for media_id=%s error=%s", media_id, exc)
            return ""

    @staticmethod
    def _download_and_save_image(media_url: str, prefix: str = "otdr") -> str:
        """
        Meta CDN URL -> image download -> local disk -> save.
        Returns: public URL (e.g. /media/otdr_images/otdr_abc123.jpg)
                 or "" on failure.
        """
        token = os.environ.get("WHATSAPP_TOKEN", "")
        try:
            r = requests.get(
                media_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            r.raise_for_status()

            content_type = r.headers.get("Content-Type", "image/jpeg")
            ext_map = {
                "image/jpeg": ".jpg",
                "image/png":  ".png",
                "image/webp": ".webp",
            }
            ext = ext_map.get(content_type.split(";")[0].strip(), ".jpg")

            from django.core.files.base import ContentFile
            from django.core.files.storage import default_storage
            
            filename  = f"{prefix}_{uuid.uuid4().hex}{ext}"
            storage_path = f"otdr_images/{filename}"
            
            # Save using Django's storage backend (handles Azure Blob Storage automatically)
            saved_path = default_storage.save(storage_path, ContentFile(r.content))
            
            # Get the public URL for the saved file
            public_url = default_storage.url(saved_path)
            
#            logger.info("[MEDIA] ✅ Image saved: %s", public_url)
            return public_url

        except Exception as exc:
            logger.error("[MEDIA] ❌ Download failed url=%s error=%s", media_url, exc)
            return ""

from rest_framework.permissions import AllowAny

class GigatelDataExportView(APIView):
    """
    API endpoint for Gigatel (or other clients) to export all their conversations
    and session metadata using their phone_number_id and token.
    """
    permission_classes = [AllowAny]
    
    def get(self, request):
        phone_number_id = request.GET.get('phone_number_id')
        token = request.GET.get('token')
        
        if not phone_number_id or not token:
            return Response({"error": "phone_number_id and token are required in query params"}, status=400)
            
        client = ClientAccount.objects.filter(phone_number_id=phone_number_id).first()
        if not client:
            return Response({"error": "Invalid phone_number_id"}, status=401)
            
        if token != client.access_token and token != settings.META_PERMANENT_TOKEN:
            return Response({"error": "Invalid token"}, status=401)
            
        from django.db.models import Q
        conversations = Conversation.objects.filter(
            Q(client=client) | Q(phone_number_id=client.phone_number_id)
        ).distinct().select_related('customer').prefetch_related('messages')
        
        export_data = {
            "client": {
                "name": client.name,
                "phone_number_id": client.phone_number_id,
                "waba_id": client.waba_id,
            },
            "conversations": []
        }
        
        for conv in conversations:
            wa_session = WhatsAppSession.objects.filter(mobile_number=conv.customer.phone).first()
            session_data = None
            if wa_session:
                session_data = {
                    "state": wa_session.state,
                    "ticket_id": wa_session.ticket_id,
                    "ticket_raised_on": wa_session.ticket_raised_on,
                    "selected_circuit_id": wa_session.selected_circuit_id,
                    "nature_of_fault_id": wa_session.nature_of_fault_id,
                    "otdr_applicable": wa_session.otdr_applicable,
                    "otdr_image1_url": wa_session.otdr_image1_url,
                    "otdr_image2_url": wa_session.otdr_image2_url,
                    "otdr_from": wa_session.otdr_from,
                    "otdr_to": wa_session.otdr_to,
                    "otdr_value": wa_session.otdr_value,
                    "otdr_remark": wa_session.otdr_remark,
                    "updated_at": wa_session.updated_at.isoformat() if wa_session.updated_at else None
                }
            
            conv_data = {
                "id": conv.id,
                "customer_name": conv.customer.name,
                "customer_phone": conv.customer.phone,
                "status": conv.status,
                "created_at": conv.created_at.isoformat() if conv.created_at else None,
                "whatsapp_session": session_data,
                "messages": []
            }
            
            for msg in conv.messages.all():
                conv_data["messages"].append({
                    "id": msg.id,
                    "direction": msg.direction,
                    "status": msg.status,
                    "type": msg.message_type,
                    "content": msg.content,
                    "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
                    "meta_message_id": msg.meta_message_id
                })
            
            export_data["conversations"].append(conv_data)
            
        return Response(export_data, status=200)

