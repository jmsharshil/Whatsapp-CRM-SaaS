import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

GIGATEL_BASE    = os.environ.get("GIGATEL_API_BASE", "http://mob.gigatel.me:60114/api")
GIGATEL_HEADERS = {
    "X-Authorization": os.environ.get(
        "GIGATEL_AUTH",
        "basic Q29tcGFsaW50QUk6Q29tcGFsaW50QUlAMTI1Iw=="
    )
}
META_SEND_URL = "https://graph.facebook.com/v22.0/{phone_id}/messages"

FAULT_MAP = {
    "packet_loss":  5,
    "fibre_cut":    6,
    "link_down":    7,
    "other":  9,
    "high_latency": 10,
    "intermittent": 11,
}

FAULT_LABELS = {
    "packet_loss":  "Packet Loss",
    "fibre_cut":    "Fibre Cut",
    "link_down":    "Link Down",
    "other":  "Other",
    "high_latency": "High Latency",
    "intermittent": "Intermittent",
}


# ---------------------------------------------------------------------------
# CRM helpers
# ---------------------------------------------------------------------------

def verify_customer(mobile_no: str) -> dict | None:
    logger.debug("[CRM] verify_customer: calling API for mobile=%s", mobile_no)
    try:
        r = requests.get(
            f"{GIGATEL_BASE}/Customer/GetCustomerVerifyByMobileNo",
            params={"mobileNo": mobile_no},
            headers=GIGATEL_HEADERS,
            timeout=10,
        )
        logger.debug("[CRM] verify_customer: status=%s body=%s", r.status_code, r.text[:500])
        r.raise_for_status()
        logger.debug("[CRM] verify_customer: raw_response=%s", r.text[:1000])

        data = r.json()

        if isinstance(data, dict) and not data.get("success", True):
            logger.warning("[CRM] verify_customer: success=false for mobile=%s", mobile_no)
            return None
        if isinstance(data, list) and data:
            logger.info("[CRM] verify_customer: found (list) mobile=%s", mobile_no)
            return data[0]
        if isinstance(data, dict) and data.get("data"):
            logger.info("[CRM] verify_customer: found (wrapped) mobile=%s", mobile_no)
            return data["data"]
        if isinstance(data, dict) and data.get("customerId"):
            logger.info("[CRM] verify_customer: found (direct) mobile=%s", mobile_no)
            return data

        logger.warning(
            "[CRM] verify_customer: unrecognised response shape mobile=%s data=%s",
            mobile_no, data
        )
        return None

    except Exception as exc:
        logger.error("[CRM] verify_customer: EXCEPTION mobile=%s error=%s", mobile_no, exc)
        return None


def get_circuits_by_customer(customer_id: int) -> list:
    logger.debug(
        "[CRM] get_circuits_by_customer: calling API for customer_id=%s", customer_id
    )
    try:
        r = requests.get(
            f"{GIGATEL_BASE}/Circuit/GetCircuitByCustomerId",
            params={"customerId": customer_id},
            headers=GIGATEL_HEADERS,
            timeout=10,
        )
        logger.debug(
            "[CRM] get_circuits_by_customer: status=%s body=%s", r.status_code, r.text[:500]
        )
        r.raise_for_status()
        logger.debug(
            "[CRM] get_circuits_by_customer: raw_response=%s", r.text[:1000]
        )
        data = r.json()

        if isinstance(data, dict):
            logger.info(
                "[CRM] get_circuits_by_customer: found circuits for customer_id=%s",
                customer_id
            )
            return data.get("data", [])

        if isinstance(data, list):
            logger.info(
                "[CRM] get_circuits_by_customer: found %d circuits for customer_id=%s",
                len(data), customer_id
            )
            return data

        logger.warning(
            "[CRM] get_circuits_by_customer: unexpected shape customer_id=%s data=%s",
            customer_id, data
        )
        return []

    except Exception as exc:
        logger.error(
            "[CRM] get_circuits_by_customer: EXCEPTION customer_id=%s error=%s",
            customer_id, exc
        )
        return []


def get_circuit_detail(circuit_id: str) -> dict | None:
    logger.debug("[CRM] get_circuit_detail: calling API for circuit_id=%s", circuit_id)
    try:
        r = requests.get(
            f"{GIGATEL_BASE}/Circuit/GetCircuitDetailByCircuitId",
            params={"circuitId": circuit_id},
            headers=GIGATEL_HEADERS,
            timeout=10,
        )
        logger.debug(
            "[CRM] get_circuit_detail: status=%s body=%s", r.status_code, r.text[:500]
        )
        r.raise_for_status()
        logger.warning("[CRM] get_circuit_detail: FULL raw_response=%s", r.text[:2000])

        data = r.json()

        raw = None
        if isinstance(data, dict) and data.get("success") and data.get("data"):
            raw = data["data"]
        elif isinstance(data, list) and data:
            raw = data[0]
        elif isinstance(data, dict) and data.get("circuitId"):
            raw = data

        if raw:
            logger.info("[CRM] get_circuit_detail: found circuit_id=%s", circuit_id)
            return _normalise_circuit_detail(raw)

        logger.warning(
            "[CRM] get_circuit_detail: unrecognised shape circuit_id=%s data=%s",
            circuit_id, data
        )
        return None

    except Exception as exc:
        logger.error(
            "[CRM] get_circuit_detail: EXCEPTION circuit_id=%s error=%s", circuit_id, exc
        )
        return None


# def check_otdr_fault_side(circuit_id: int, otdr_from_to: str, otdr_value: float) -> dict | None:
#     """
#     Calls OTDRCheckCustomerOrGigatel to determine fault side.
#     Returns dict like {"faultSide": "Gigatel"} or {"faultSide": "Customer"}, or None on failure.
#     """
#     logger.debug(
#         "[CRM] check_otdr_fault_side: circuit_id=%s otdr_from_to=%s otdr=%s",
#         circuit_id, otdr_from_to, otdr_value
#     )
#     try:
#         r = requests.post(
#             f"{GIGATEL_BASE}/Circuit/OTDRCheckCustomerOrGigatel",
#             json={
#                 "circuitId": circuit_id,
#                 "otdrFromTo": otdr_from_to,
#                 "otdr": otdr_value,
#             },
#             headers={**GIGATEL_HEADERS, "Content-Type": "application/json"},
#             timeout=10,
#         )
#         r.raise_for_status()
#         data = r.json()
#         logger.info("[CRM] check_otdr_fault_side: result=%s", data)
#         return data
#     except Exception as exc:
#         logger.error("[CRM] check_otdr_fault_side: EXCEPTION circuit_id=%s error=%s", circuit_id, exc)
#         return None

def get_last_mile_segment(circuit_detail: dict, otdr_from_to: str) -> dict | None:
    """
    Returns the last-mile segment (routeType is null/empty) for the given side
    ('from' or 'to') from the circuit's OTDR bifurcation data.
    """
    segments = (circuit_detail or {}).get("circuitOTDRBiferctionVM") or []
    side = "from" if otdr_from_to.lower() == "from" else "to"
    for seg in segments:
        part = (seg.get("circuitPart") or "").strip().lower()
        if part == side and not seg.get("routeType"):
            return seg
    return None


def check_otdr_fault_side(
    circuit_id: int,
    otdr_from_to: str,
    otdr_value: float,
    circuit_detail: dict | None = None,
) -> dict | None:
    """
    Calls OTDRCheckCustomerOrGigatel to determine fault side, then cross-checks
    the result against the circuit's last-mile ownership data
    (fromLastMileManagedBy / toLastMileManagedBy + bifurcation last-mile segment)
    and overrides the API result when it contradicts that data.
    """
    logger.debug(
        "[CRM] check_otdr_fault_side: circuit_id=%s otdr_from_to=%s otdr=%s",
        circuit_id, otdr_from_to, otdr_value
    )
    try:
        r = requests.post(
            f"{GIGATEL_BASE}/Circuit/OTDRCheckCustomerOrGigatel",
            json={
                "circuitId": circuit_id,
                "otdrFromTo": otdr_from_to,
                "otdr": otdr_value,
            },
            headers={**GIGATEL_HEADERS, "Content-Type": "application/json"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        logger.info("[CRM] check_otdr_fault_side: raw API result=%s", data)
    except Exception as exc:
        logger.error("[CRM] check_otdr_fault_side: EXCEPTION circuit_id=%s error=%s", circuit_id, exc)
        return None

    raw_fault_side = str((data or {}).get("data", "")).strip().lower()

    # ── Last-mile sanity check / override ───────────────────────────────────
    if circuit_detail:
        last_mile_owner = (
            circuit_detail.get("toLastMileManagedBy")
            if otdr_from_to.lower() == "to"
            else circuit_detail.get("fromLastMileManagedBy")
        )
        last_mile_owner_norm = (last_mile_owner or "").strip().lower()

        last_mile_seg = get_last_mile_segment(circuit_detail, otdr_from_to)
        last_mile_length = (
            float(last_mile_seg.get("fiberLength") or 0) if last_mile_seg else None
        )

        logger.info(
            "[CRM] check_otdr_fault_side: last_mile_check side=%s owner=%s length=%s "
            "otdr=%s api_result=%s",
            otdr_from_to, last_mile_owner, last_mile_length, otdr_value, raw_fault_side
        )

        if last_mile_owner_norm == "gigatel":
            if raw_fault_side != "gigatel":
                logger.warning(
                    "[CRM] check_otdr_fault_side: ⚠️ API said %r but %s-side last-mile is "
                    "Gigatel-owned (circuit_id=%s) — overriding to 'Gigatel'",
                    raw_fault_side, otdr_from_to, circuit_id
                )
            data["data"] = "Gigatel"
            return data

        if last_mile_owner_norm == "customer" and last_mile_length is not None:
            if otdr_value <= last_mile_length:
                if raw_fault_side != "customer":
                    logger.warning(
                        "[CRM] check_otdr_fault_side: ⚠️ API said %r but OTDR=%.3f is within "
                        "%s-side customer last-mile (%.3fm) — overriding to 'Customer'",
                        raw_fault_side, otdr_value, otdr_from_to, last_mile_length
                    )
                data["data"] = "Customer"
            else:
                if raw_fault_side != "gigatel":
                    logger.warning(
                        "[CRM] check_otdr_fault_side: ⚠️ API said %r but OTDR=%.3f exceeds "
                        "%s-side customer last-mile (%.3fm) — overriding to 'Gigatel'",
                        raw_fault_side, otdr_value, otdr_from_to, last_mile_length
                    )
                data["data"] = "Gigatel"
            return data

    logger.info("[CRM] check_otdr_fault_side: final result=%s", data)
    return data


def _normalise_circuit_detail(raw: dict) -> dict:
    if not raw:
        return raw
    out = dict(raw)

    if not out.get("ticketId"):
        out["ticketId"] = (
            out.get("ticketNo") or out.get("TicketNo") or out.get("TicketId")
        )

    if not out.get("ticketStatus"):
        raw_status = (
            out.get("Status") or out.get("status")
            or out.get("TicketStatus") or out.get("ticket_status")
            or ""
        ).strip()
        out["ticketStatus"] = raw_status if raw_status else (
            "Open" if out.get("ticketId") else ""
        )

    if not out.get("ticketCreatedOn"):
        out["ticketCreatedOn"] = (
            out.get("ticketAllottedOn") or out.get("TicketAllottedOn")
            or out.get("ticketStartTime") or out.get("CreatedOn")
            or out.get("createdOn") or ""
        ) or None

    if not out.get("ticketType"):
        out["ticketType"] = (
            out.get("complaintType") or out.get("ComplaintType")
            or out.get("natureOfFault") or out.get("NatureOfFault")
        )

    return out


def raise_complaint(payload: dict) -> dict | None:
    logger.warning(
        "[CRM] raise_complaint: EXACT PAYLOAD → %s",
        json.dumps(payload, default=str)
    )
    try:
        r = requests.post(
            f"{GIGATEL_BASE}/Customer/InsertUpdateCustomerComplaintFormData",
            data=payload,
            headers=GIGATEL_HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        logger.warning("[CRM] raise_complaint: FULL response status=%s body=%s",
                      r.status_code, r.text[:2000])

        result = r.json()
        data = result.get("data") if isinstance(result, dict) else None
        if isinstance(data, dict) and data.get("result") is True:
            data["ticketId"]    = data.get("transactionNo") or data.get("id")
            data["isDuplicate"] = not result.get("success", True)
            data["crmMessage"]  = result.get("message", "")
            logger.info("[CRM] raise_complaint: ✅ SUCCESS ticketId=%s", data["ticketId"])
            return data

        logger.error("[CRM] raise_complaint: ❌ data.result not true — result=%s", result)
        return None

    except Exception as exc:
        logger.error("[CRM] raise_complaint: ❌ EXCEPTION error=%s", exc)
        return None


# ---------------------------------------------------------------------------
# Meta helpers
# ---------------------------------------------------------------------------

def _meta_post(body: dict) -> bool:
    phone_id = os.environ.get("META_PHONE_NUMBER_ID", "")
    token    = os.environ.get("WHATSAPP_TOKEN", "")

    template_name = (
        body.get("template", {}).get("name", "unknown")
        if body.get("type") == "template"
        else body.get("type", "unknown")
    )
    to = body.get("to", "unknown")

    if not phone_id:
        logger.error(
            "[META] ❌ META_PHONE_NUMBER_ID is not set — cannot send template=%s",
            template_name
        )
        return False
    if not token:
        logger.error(
            "[META] ❌ WHATSAPP_TOKEN is not set — cannot send template=%s",
            template_name
        )
        return False

    components  = (
        body.get("template", {}).get("components", [])
        if body.get("type") == "template"
        else []
    )
    body_params = next(
        (c.get("parameters", []) for c in components if c.get("type") == "body"), []
    )
    param_values = [p.get("text") or p.get("payload") for p in body_params]

    logger.debug(
        "[META] ▶ SEND template=%s to=%s | body_params=%s | full_payload=%s",
        template_name, to, param_values, body
    )

    try:
        r = requests.post(
            META_SEND_URL.format(phone_id=phone_id),
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            },
            timeout=10,
        )

        logger.debug(
            "[META] Response ← template=%s to=%s | HTTP %s | body=%s",
            template_name, to, r.status_code, r.text
        )

        if r.status_code == 200:
            try:
                resp_json = r.json()
                msg_id = (
                    resp_json.get("messages", [{}])[0].get("id", "N/A")
                    if resp_json.get("messages")
                    else "N/A"
                )
                logger.info(
                    "[META] ✅ SENT template=%s to=%s | wamid=%s",
                    template_name, to, msg_id
                )
            except Exception:
                logger.info(
                    "[META] ✅ SENT template=%s to=%s | (could not parse msg_id)",
                    template_name, to
                )
            return True

        else:
            logger.error(
                "[META] ❌ REJECTED template=%s to=%s | HTTP %s | error=%s",
                template_name, to, r.status_code, r.text
            )
            r.raise_for_status()
            return False

    except requests.exceptions.HTTPError as exc:
        logger.error(
            "[META] ❌ HTTP ERROR template=%s to=%s | %s | response=%s",
            template_name, to, exc,
            exc.response.text if exc.response else "no response"
        )
        return False
    except requests.exceptions.Timeout:
        logger.error("[META] ❌ TIMEOUT template=%s to=%s", template_name, to)
        return False
    except Exception as exc:
        logger.error(
            "[META] ❌ EXCEPTION template=%s to=%s | %s", template_name, to, exc
        )
        return False


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def tpl_auth_failed(to: str) -> bool:
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gigatel_auth_failed",
            "language": {"code": "en"},
        },
    })


def tpl_main_menu(to: str, customer_name: str) -> bool:
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gigatel_menu",
            "language": {"code": "en"},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": customer_name}],
                },
                {
                    "type": "button", "sub_type": "quick_reply", "index": "0",
                    "parameters": [{"type": "payload", "payload": "complaints"}],
                },
                {
                    "type": "button", "sub_type": "quick_reply", "index": "1",
                    "parameters": [{"type": "payload", "payload": "current_ticket"}],
                },
                {
                    "type": "button", "sub_type": "quick_reply", "index": "2",
                    "parameters": [{"type": "payload", "payload": "sales"}],
                },
            ],
        },
    })


def tpl_sales_coming_soon(to: str) -> bool:
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gigatel_sales_coming_soon",
            "language": {"code": "en"},
        },
    })


def _tpl_circuit_text_message(to: str, circuits: list) -> bool:
    """Plain text circuit list for when count > 10."""
    logger.debug(
        "[TPL] Sending plain-text circuit list (count=%d) to=%s", len(circuits), to
    )

    seen_ids = set()
    lines = ["Your circuits — please reply with the Circuit ID:\n"]

    for i, circuit in enumerate(circuits, start=1):
        cid_raw = circuit.get("circuitIdStr") or circuit.get("circuitId")
        if not cid_raw:
            continue
        cid = str(cid_raw).strip()
        if cid.lower() in ("none", "null", ""):
            continue
        if cid in seen_ids:
            continue
        seen_ids.add(cid)

        from_st = (
            circuit.get("fromStation")
            or circuit.get("startAddress")
            or circuit.get("fromAddress")
            or ""
        ).strip()
        to_st = (
            circuit.get("toStation")
            or circuit.get("endAddress")
            or circuit.get("toAddress")
            or ""
        ).strip()

        route = f"  ({from_st} → {to_st})" if (from_st or to_st) else ""
        lines.append(f"{i}. {cid}{route}")

    if len(lines) <= 1:
        logger.error("[TPL] _tpl_circuit_text_message: no valid circuits to list")
        return False

    # Fit within WhatsApp 4096 char limit
    MAX_CHARS = 4096
    fitted_lines = [lines[0]]  # always keep the header
    for line in lines[1:]:
        candidate = "\n".join(fitted_lines + [line])
        if len(candidate) > MAX_CHARS:
            fitted_lines.append("...and more. Please reply with your Circuit ID.")
            break
        fitted_lines.append(line)

    message_body = "\n".join(fitted_lines)
    logger.debug("[TPL] plain-text circuit list: %d chars, %d circuits shown",
                 len(message_body), len(fitted_lines) - 1)

    phone_id = os.environ.get("META_PHONE_NUMBER_ID", "")
    token    = os.environ.get("WHATSAPP_TOKEN", "")

    if not phone_id or not token:
        logger.error("[TPL] _tpl_circuit_text_message: missing META_PHONE_NUMBER_ID or TOKEN")
        return False

    try:
        r = requests.post(
            META_SEND_URL.format(phone_id=phone_id),
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": message_body},
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            },
            timeout=10,
        )
        if r.status_code == 200:
            logger.info("[TPL] ✅ plain-text circuit list sent to=%s", to)
            return True
        else:
            logger.error(
                "[TPL] ❌ plain-text circuit list failed to=%s | HTTP %s | %s",
                to, r.status_code, r.text
            )
            return False
    except Exception as exc:
        logger.error("[TPL] ❌ plain-text circuit list exception to=%s | %s", to, exc)
        return False


def _tpl_circuit_list_message(to: str, circuits: list, action_type: str) -> bool:
    logger.debug(
        "[TPL] Sending circuit selection list (count=%d) to=%s", len(circuits), to
    )

    seen_ids = set()
    rows = []
    for circuit in circuits[:10]:
        cid_raw = circuit.get("circuitIdStr") or circuit.get("circuitId")
        if not cid_raw:
            logger.warning("[TPL] Skipping circuit with no ID: %s", circuit)
            continue
        cid = str(cid_raw).strip()
        if cid.lower() in ("none", "null", ""):
            logger.warning("[TPL] Skipping circuit with invalid cid=%r", cid)
            continue
        if cid in seen_ids:
            logger.warning("[TPL] Skipping duplicate cid=%r", cid)
            continue
        seen_ids.add(cid)

        logger.warning("[TPL] circuit raw data: %s", circuit)

        from_st = (
            circuit.get("fromStation")
            or circuit.get("startAddress")
            or circuit.get("fromAddress")
            or ""
        ).strip()
        to_st = (
            circuit.get("toStation")
            or circuit.get("endAddress")
            or circuit.get("toAddress")
            or ""
        ).strip()

        description = (
            f"{from_st} → {to_st}"[:72]
            if (from_st or to_st)
            else "Unknown route"
        )

        rows.append({
            "id":          cid,
            "title":       cid[:30],
            "description": description,
        })

    if not rows:
        logger.error("[TPL] tpl_circuit_list_message: no valid circuits")
        return False

    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": "Select Circuit"},
            "body":   {"text": "Choose a circuit to report fault or check status:"},
            "footer": {"text": "Select from the options below"},
            "action": {
                "button": "Select Circuit",
                "sections": [{"title": "Your Circuits", "rows": rows}],
            },
        },
    })


def tpl_circuit_list_interactive(
    to: str, circuits: list, is_for_ticket: bool = False
) -> bool:
    if not circuits:
        logger.warning("[TPL] tpl_circuit_list_interactive: no circuits provided")
        return False

    action_type = "current_ticket" if is_for_ticket else "complaints"

    if len(circuits) <= 10:
        return _tpl_circuit_list_message(to, circuits, action_type)
    else:
        return _tpl_circuit_text_message(to, circuits)


def tpl_open_ticket_exists(
    to: str, circuit_id: str, ticket_id: str, status: str, raised_on: str
) -> bool:
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gigatel_open_ticket_exists",
            "language": {"code": "en"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": circuit_id},
                        {"type": "text", "text": ticket_id},
                        {"type": "text", "text": status},
                        {"type": "text", "text": raised_on},
                    ],
                }
            ],
        },
    })


def tpl_complaint_type(to: str, circuit_id: str) -> bool:
    """Interactive list with all 6 fault types."""
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": "Select Fault Type"},
            "body":   {"text": f"Circuit: *{circuit_id}*\n\nPlease select the nature of fault:"},
            "footer": {"text": "Choose the fault that best describes the issue"},
            "action": {
                "button": "Select Fault",
                "sections": [
                    {
                        "title": "Fault Types",
                        "rows": [
                            {"id": "fault__link_down",   "title": "Link Down"},
                            {"id": "fault__fibre_cut",   "title": "Fibre Cut"},
                            {"id": "fault__packet_loss", "title": "Packet Loss"},
                            {"id": "fault__high_latency","title": "High Latency"},
                            {"id": "fault__intermittent","title": "Intermittent"},
                            {"id": "fault__other",       "title": "Other"},
                        ],
                    }
                ],
            },
        },
    })


def tpl_otdr_question(to: str, fault_label: str, circuit_id: str) -> bool:
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gigatel_otdr_question",
            "language": {"code": "en"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": fault_label},
                        {"type": "text", "text": circuit_id},
                    ],
                },
                {
                    "type": "button", "sub_type": "quick_reply", "index": "0",
                    "parameters": [{"type": "payload", "payload": "otdr__yes"}],
                },
                {
                    "type": "button", "sub_type": "quick_reply", "index": "1",
                    "parameters": [{"type": "payload", "payload": "otdr__no"}],
                },
            ],
        },
    })


def tpl_remark_prompt(to: str) -> bool:
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gigatel_remark_prompt",
            "language": {"code": "en"},
        },
    })


def tpl_otdr_field_select(to: str, from_name: str = "", to_name: str = "") -> bool:
    """
    Interactive list showing the ACTUAL From/To station names
    (pulled from circuit detail) — user taps the side matching the fault.
    """
    from_name = (from_name or "").strip() or "From station"
    to_name   = (to_name or "").strip() or "To station"

    rows = [
        {
            "id": "otdr_field__from",
            "title": from_name[:24],
        },
        {
            "id": "otdr_field__to",
            "title": to_name[:24],
        },
    ]

    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": "Select Fault Side"},
            "body": {"text": "Tap the station that matches where the OTDR reading was taken:"},
            "footer": {"text": "Select one option"},
            "action": {
                "button": "Select Station",
                "sections": [{"title": "Circuit Stations", "rows": rows}],
            },
        },
    })


def tpl_ticket_confirmation(
    to: str, ticket_id: str, circuit_id: str, fault_label: str, raised_on: str
) -> bool:
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gigatel_ticket_confirmation",
            "language": {"code": "en"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": ticket_id},
                        {"type": "text", "text": circuit_id},
                        {"type": "text", "text": fault_label},
                        {"type": "text", "text": raised_on},
                    ],
                }
            ],
        },
    })


def tpl_complaint_failed(to: str) -> bool:
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gigatel_complaint_failed",
            "language": {"code": "en"},
        },
    })


def tpl_current_ticket(
    to: str, circuit_id: str, ticket_id: str, status: str, created_on: str
) -> bool:
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gigatel_current_ticket",
            "language": {"code": "en"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": circuit_id},
                        {"type": "text", "text": ticket_id},
                        {"type": "text", "text": status},
                        {"type": "text", "text": created_on},
                    ],
                }
            ],
        },
    })


def tpl_no_ticket_found(to: str, circuit_id: str) -> bool:
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gigatel_no_ticket_found",
            "language": {"code": "en"},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": circuit_id}],
                },
                {
                    "type": "button", "sub_type": "quick_reply", "index": "0",
                    "parameters": [{"type": "payload", "payload": "complaints"}],
                },
                {
                    "type": "button", "sub_type": "quick_reply", "index": "1",
                    "parameters": [{"type": "payload", "payload": "main_menu"}],
                },
            ],
        },
    })


def tpl_otdr_from_prompt(to: str) -> bool:
    """Step 1 of OTDR: ask for source station name."""
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gigatel_otdr_from_prompt",
            "language": {"code": "en"},
        },
    })


def tpl_otdr_to_prompt(to: str) -> bool:
    """Ask for destination station name."""
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gigatel_otdr_to_prompt",
            "language": {"code": "en"},
        },
    })


def tpl_otdr_value_prompt(to: str) -> bool:
    """Ask for OTDR reading value."""
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gigatel_otdr_value_prompt",
            "language": {"code": "en"},
        },
    })


def tpl_otdr_image_prompt(to: str) -> bool:
    """After remark collected — ask user to upload first OTDR image."""
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gigatel_otdr_image_prompt",
            "language": {"code": "en"},
        },
    })


def tpl_otdr_second_image_prompt(to: str) -> bool:
    """After first image received — ask if user wants to send a second image."""
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gigatel_otdr_second_image_prompt_",
            "language": {"code": "en"},
        },
    })


def tpl_otdr_customer_fault_notice(to: str) -> bool:
    """OTDR result indicates fault is on the customer's side."""
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gigatel_otdr_customer_fault_notice",
            "language": {"code": "en"},
        },
    })


def tpl_raise_anyway_question(to: str) -> bool:
    """Ask whether to raise a ticket anyway despite customer-side OTDR result."""
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gigatel_raise_anyway_question",
            "language": {"code": "en"},
            "components": [
                {
                    "type": "button", "sub_type": "quick_reply", "index": "0",
                    "parameters": [{"type": "payload", "payload": "raise_anyway__yes"}],
                },
                {
                    "type": "button", "sub_type": "quick_reply", "index": "1",
                    "parameters": [{"type": "payload", "payload": "raise_anyway__no"}],
                },
            ],
        },
    })


def tpl_request_closed_customer_fault(to: str) -> bool:
    """Terminal message when customer declines to raise a ticket after customer-fault OTDR result."""
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gigatel_request_closed_customer_fault",
            "language": {"code": "en"},
        },
    })


def tpl_ticket_confirmation_disputed(
    to: str, ticket_id: str, circuit_id: str, fault_label: str, raised_on: str
) -> bool:
    """Ticket confirmation when raised despite OTDR indicating customer-side fault."""
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gigatel_ticket_confirmation_disputed",
            "language": {"code": "en"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": ticket_id},
                        {"type": "text", "text": circuit_id},
                        {"type": "text", "text": fault_label},
                        {"type": "text", "text": raised_on},
                    ],
                }
            ],
        },
    })


def tpl_circuit_last4_prompt(to: str, circuit_count: int = 0) -> bool:
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gigatel_circuit_last4",
            "language": {"code": "en"},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": str(circuit_count)}],
                },
            ],
        },
    })


def tpl_circuit_confirm(to: str, circuit_id: str, from_st: str = "", to_st: str = "") -> bool:
    """Matched circuit batavi ne Yes/No confirm mangvanu."""
    route = f"{from_st} → {to_st}" if (from_st or to_st) else "N/A"
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gigatel_circuit_confirm",
            "language": {"code": "en"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": circuit_id},
                        {"type": "text", "text": route},
                    ],
                },
                {
                    "type": "button", "sub_type": "quick_reply", "index": "0",
                    "parameters": [{"type": "payload", "payload": "circuit_confirm__yes"}],
                },
                {
                    "type": "button", "sub_type": "quick_reply", "index": "1",
                    "parameters": [{"type": "payload", "payload": "circuit_confirm__no"}],
                },
            ],
        },
    })


def tpl_circuit_not_found(to: str) -> bool:
    """Entered digits sathe koi circuit match na thayu — retry."""
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gigatel_circuit_not_found",
            "language": {"code": "en"},
        },
    })


def tpl_circuit_multiple_match(to: str) -> bool:
    """Same last-4-digits sathe ek thi vadhare circuit match thaya — ambiguous."""
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gigatel_circuit_multiple_match",
            "language": {"code": "en"},
        },
    })


def tpl_contact_team(to: str) -> bool:
    """Terminal message — support team ne contact karva kahevu."""
    return _meta_post({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": "gigatel_contact_team",
            "language": {"code": "en"},
        },
    })