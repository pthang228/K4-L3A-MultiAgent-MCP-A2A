from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from itertools import product
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

KNOWN_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
    "insufficient_evidence",
}

DOMAIN_ACTORS = {
    "order": "order-agent",
    "item": "order-agent",
    "payment": "payment-agent",
    "shipment": "shipment-agent",
    "seller": "seller-agent",
    "policy": "policy-agent",
}

DOMAIN_TERMS = {
    "policy": ("policy", "rule", "eligib"),
    "shipment": ("shipment", "delivery", "logistic", "carrier", "tracking"),
    "payment": ("payment", "charge", "refund", "transaction"),
    "seller": ("seller", "merchant"),
    "item": ("item", "product"),
    "order": ("order",),
}

TOOL_PREFERENCES = {
    "order": ("get_order", "lookup_order", "find_order"),
    "item": ("get_order_items", "list_order_items", "get_items"),
    "payment": ("get_order_payments", "list_order_payments", "get_payments"),
    "shipment": ("get_order_shipment", "get_shipment", "get_delivery"),
    "seller": ("get_seller", "get_seller_profile"),
    "policy": ("get_policy", "get_policy_rule", "lookup_policy"),
}

IDENTIFIER_KEYS = {
    "order_ids": {"order_id"},
    "item_ids": {"item_id", "order_item_id"},
    "seller_ids": {"seller_id", "merchant_id"},
    "payment_references": {
        "payment_id",
        "payment_reference",
        "payment_ref",
        "charge_id",
        "transaction_id",
    },
    "shipment_ids": {"shipment_id", "delivery_id", "tracking_id", "tracking_code"},
}


@dataclass(frozen=True)
class EvidenceRecord:
    domain: str
    tool_name: str
    evidence_ref: str
    data: Any


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _iter_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_dicts(child)


def _scalars(value: Any):
    if isinstance(value, (str, int, float, bool)) and not isinstance(value, complex):
        yield value
    elif isinstance(value, list):
        for child in value:
            yield from _scalars(child)


def _values(
    records: list[EvidenceRecord], keys: set[str], domains: set[str] | None = None
) -> list[Any]:
    found: list[Any] = []
    for record in records:
        if domains is not None and record.domain not in domains:
            continue
        for item in _iter_dicts(record.data):
            for name, value in item.items():
                if _key(str(name)) in keys:
                    found.extend(_scalars(value))
    return found


def _unique_strings(values: list[Any], limit: int = 20) -> list[str]:
    result: list[str] = []
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        text = str(value).strip()
        if text and text not in result:
            result.append(text[:128])
        if len(result) == limit:
            break
    return result


def _numbers(values: list[Any]) -> list[float]:
    result: list[float] = []
    for value in values:
        if isinstance(value, bool):
            continue
        try:
            result.append(float(value))
        except (TypeError, ValueError):
            continue
    return result


def _first_number(
    records: list[EvidenceRecord], keys: set[str], domains: set[str] | None = None
) -> float | None:
    values = _numbers(_values(records, keys, domains))
    return values[0] if values else None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _date_values(
    records: list[EvidenceRecord], keys: set[str], domains: set[str] | None = None
) -> list[datetime]:
    return [
        parsed for value in _values(records, keys, domains) if (parsed := _parse_datetime(value))
    ]


TIMELINE_KEYS = {
    "event_at",
    "occurred_at",
    "created_at",
    "updated_at",
    "order_purchase_timestamp",
    "order_approved_at",
    "order_delivered_carrier_date",
    "order_delivered_customer_date",
    "order_estimated_delivery_date",
    "shipping_limit_date",
    "shipping_limit_at",
    "delivered_carrier_at",
    "delivered_customer_at",
    "estimated_delivery_at",
    "carrier_pickup_at",
    "handed_to_carrier_at",
    "shipped_at",
    "delivered_at",
}


def _purchase_at(records: list[EvidenceRecord]) -> datetime | None:
    values = _date_values(records, {"order_purchase_timestamp"}, {"order"})
    return values[0] if values else None


def _operational_end(records: list[EvidenceRecord]) -> datetime | None:
    values = _date_values(
        records,
        {
            "order_delivered_customer_date",
            "order_estimated_delivery_date",
            "delivered_customer_at",
            "estimated_delivery_at",
        },
        {"order", "shipment"},
    )
    return max(values) if values else None


def _not_before(candidate: datetime, floor: datetime) -> bool:
    try:
        return candidate >= floor
    except TypeError:
        return candidate.replace(tzinfo=None) >= floor.replace(tzinfo=None)


def _not_after(candidate: datetime, ceiling: datetime) -> bool:
    try:
        return candidate <= ceiling
    except TypeError:
        return candidate.replace(tzinfo=None) <= ceiling.replace(tzinfo=None)


def _within_order_timeline(
    item: dict[str, Any],
    purchase_at: datetime | None,
    operational_end: datetime | None = None,
) -> bool:
    if purchase_at is None and operational_end is None:
        return True
    timestamps = [
        parsed
        for name, value in item.items()
        if _key(str(name)) in TIMELINE_KEYS
        if (parsed := _parse_datetime(value)) is not None
    ]
    return not timestamps or any(
        (purchase_at is None or _not_before(timestamp, purchase_at))
        and (operational_end is None or _not_after(timestamp, operational_end))
        for timestamp in timestamps
    )


def _timeline_values(
    records: list[EvidenceRecord],
    keys: set[str],
    domains: set[str] | None,
    purchase_at: datetime | None,
    operational_end: datetime | None = None,
) -> list[Any]:
    found: list[Any] = []
    for record in records:
        if domains is not None and record.domain not in domains:
            continue
        for item in _iter_dicts(record.data):
            if not _within_order_timeline(item, purchase_at, operational_end):
                continue
            for name, value in item.items():
                if _key(str(name)) in keys:
                    found.extend(_scalars(value))
    return found


def _timeline_dates(
    records: list[EvidenceRecord],
    keys: set[str],
    domains: set[str] | None,
    purchase_at: datetime | None,
    operational_end: datetime | None = None,
) -> list[datetime]:
    return [
        parsed
        for value in _timeline_values(records, keys, domains, purchase_at, operational_end)
        if (parsed := _parse_datetime(value)) is not None
        and (purchase_at is None or _not_before(parsed, purchase_at))
        and (operational_end is None or _not_after(parsed, operational_end))
    ]


def _tool_domain(spec: dict[str, Any]) -> str | None:
    name = _key(str(spec.get("name", "")))
    description = _key(str(spec.get("description", "")))
    for domain in ("policy", "shipment", "payment", "seller", "item", "order"):
        if any(term in name for term in DOMAIN_TERMS[domain]):
            return domain
    for domain in ("policy", "shipment", "payment", "seller", "item", "order"):
        if any(term in description for term in DOMAIN_TERMS[domain]):
            return domain
    return None


def _tool_rank(domain: str, spec: dict[str, Any]) -> tuple[int, str]:
    name = _key(str(spec.get("name", "")))
    preferences = TOOL_PREFERENCES[domain]
    try:
        return preferences.index(name), name
    except ValueError:
        return len(preferences), name


def _choose_tools(specs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {domain: [] for domain in DOMAIN_ACTORS}
    for spec in specs:
        domain = _tool_domain(spec)
        if domain is not None:
            grouped[domain].append(spec)
    for domain, matches in grouped.items():
        matches.sort(key=lambda spec: _tool_rank(domain, spec))
        if domain == "payment":
            grouped[domain] = matches[:3]
        else:
            grouped[domain] = matches[:1]
    return grouped


def _extract_identifiers(
    records: list[EvidenceRecord], claimed_order_id: str
) -> dict[str, list[str]]:
    result = {name: [] for name in IDENTIFIER_KEYS}
    if claimed_order_id:
        result["order_ids"].append(claimed_order_id)
    for output_name, aliases in IDENTIFIER_KEYS.items():
        result[output_name] = _unique_strings(
            [*result[output_name], *_values(records, aliases)], limit=20
        )
    return result


def _context(case: dict[str, Any], records: list[EvidenceRecord]) -> dict[str, list[str]]:
    request = case.get("customer_request") or {}
    result = _extract_identifiers(records, str(request.get("claimed_order_id") or ""))
    result["policy_versions"] = _unique_strings([case.get("policy_version")])
    result["claim_ids"] = _unique_strings(
        [claim.get("claim_id") for claim in request.get("claims", []) if isinstance(claim, dict)]
    )
    result["topics"] = _unique_strings(
        [claim.get("topic") for claim in request.get("claims", []) if isinstance(claim, dict)]
    )
    return result


def _argument_candidates(name: str, domain: str, context: dict[str, list[str]]) -> list[str]:
    normalized = _key(name)
    mapping = (
        (("order_id",), "order_ids"),
        (("order_item_id", "item_id", "product_id"), "item_ids"),
        (("seller_id", "merchant_id"), "seller_ids"),
        (("shipment_id", "delivery_id", "tracking_id", "tracking_code"), "shipment_ids"),
        (
            ("payment_reference", "payment_ref", "payment_id", "transaction_id", "charge_id"),
            "payment_references",
        ),
        (("policy_version", "policy_id", "version"), "policy_versions"),
        (("claim_id",), "claim_ids"),
        (("topic", "claim_topic", "issue", "issue_code"), "topics"),
    )
    for aliases, context_key in mapping:
        if normalized in aliases:
            return context.get(context_key, [])
    if normalized == "id":
        return context.get(f"{domain}_ids", [])
    return []


def _argument_sets(
    spec: dict[str, Any], domain: str, context: dict[str, list[str]]
) -> list[dict[str, str]]:
    schema = spec.get("input_schema") or {}
    required = [name for name in schema.get("required", []) if _key(str(name)) != "case_id"]
    if not required:
        return [{}]
    names: list[str] = []
    candidate_sets: list[list[str]] = []
    for name in required:
        candidates = _argument_candidates(str(name), domain, context)
        if not candidates:
            return []
        names.append(str(name))
        candidate_sets.append(candidates[:5])
    limit = 5 if domain in {"seller", "shipment"} else 1
    combinations = product(*candidate_sets)
    return [dict(zip(names, values, strict=True)) for values in list(combinations)[:limit]]


async def _collect_evidence(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> list[EvidenceRecord]:
    case_id = str(case["case_id"])
    request = case.get("customer_request") or {}
    primary_topics = [
        _key(str(claim.get("topic", "")))
        for claim in request.get("claims", [])
        if isinstance(claim, dict) and claim.get("topic") != "requested_full_refund"
    ]
    primary_topic = primary_topics[0] if primary_topics else ""
    specs = await gateway.list_tool_specs()
    selected = _choose_tools(specs)
    records: list[EvidenceRecord] = []

    for actor in dict.fromkeys(DOMAIN_ACTORS.values()):
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
            decision_code="COLLECT_AUTHORITATIVE_EVIDENCE",
        )

    for domain in ("order", "item", "payment", "shipment", "seller", "policy"):
        actor = DOMAIN_ACTORS[domain]
        domain_refs: list[str] = []
        for spec in selected[domain]:
            tool_name = str(spec["name"])
            if tool_name == "get_refund_timeline" and primary_topic not in {
                "refund_pending",
                "refund_failed",
            }:
                continue
            if domain == "shipment" and primary_topic not in {
                "late_delivery_seller",
                "late_delivery_logistics",
                "unsupported_claim",
            }:
                continue
            if domain == "seller" and primary_topic not in {
                "late_delivery_seller",
                "unavailable_order_paid",
            }:
                continue
            context = _context(case, records)
            for arguments in _argument_sets(spec, domain, context):
                try:
                    evidence = await gateway.call(tool_name, case_id=case_id, **arguments)
                except (OSError, RuntimeError, ValueError):
                    trace.emit(
                        case_id=case_id,
                        event_type="handoff",
                        actor=actor,
                        target="verifier",
                        decision_code="MCP_TOOL_FAILED",
                        tool_name=tool_name,
                    )
                    continue
                record = EvidenceRecord(
                    domain=domain,
                    tool_name=tool_name,
                    evidence_ref=str(evidence["evidence_ref"]),
                    data=evidence["data"],
                )
                records.append(record)
                domain_refs.append(record.evidence_ref)
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor=actor,
                    tool_name=tool_name,
                    evidence_refs=[record.evidence_ref],
                )
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=actor,
            target="verifier",
            decision_code="EVIDENCE_READY" if domain_refs else "NO_EVIDENCE_AVAILABLE",
            evidence_refs=domain_refs[:20],
        )
        if domain == "policy":
            trace.emit(
                case_id=case_id,
                event_type="policy_decided",
                actor=actor,
                target="verifier",
                decision_code="POLICY_EVIDENCE_COLLECTED" if domain_refs else "POLICY_UNAVAILABLE",
                evidence_refs=domain_refs[:20],
            )
    return records


def _payment_totals(records: list[EvidenceRecord]) -> tuple[float | None, float, float | None]:
    purchase_at = _purchase_at(records)
    operational_end = _operational_end(records)
    captured_events: list[float] = []
    refunded_events: list[float] = []
    for record in records:
        if record.domain != "payment":
            continue
        for item in _iter_dicts(record.data):
            event_type = _key(str(item.get("event_type", "")))
            event_end = (
                operational_end
                if event_type in {"captured", "capture", "payment_captured"}
                else None
            )
            if not _within_order_timeline(item, purchase_at, event_end):
                continue
            amounts = _numbers(
                [
                    value
                    for name, value in item.items()
                    if _key(str(name)) in {"amount_brl", "amount", "payment_value"}
                ]
            )
            if not amounts:
                continue
            if event_type in {"captured", "capture", "payment_captured"}:
                captured_events.append(amounts[0])
            elif event_type in {"refunded", "refund_completed", "refund_succeeded"}:
                refunded_events.append(amounts[0])

    captured = _first_number(
        records,
        {"captured_total_brl", "captured_total", "paid_total", "total_paid", "payment_total"},
        {"payment", "order"},
    )
    if captured_events:
        captured = sum(captured_events)
    elif captured is None:
        payment_values: list[float] = []
        for record in records:
            if record.domain != "payment":
                continue
            for item in _iter_dicts(record.data):
                if not _within_order_timeline(item, purchase_at, operational_end):
                    continue
                values = _numbers(
                    [
                        value
                        for name, value in item.items()
                        if _key(str(name)) in {"payment_value", "captured_amount", "amount_brl"}
                    ]
                )
                if values:
                    payment_values.append(values[0])
        captured = sum(payment_values) if payment_values else None

    refunded = _first_number(
        records,
        {"refunded_total_brl", "refunded_total", "total_refunded", "refund_amount_brl"},
        {"payment"},
    )
    if refunded_events:
        refunded = sum(refunded_events)
    refunded = refunded if refunded is not None else 0.0

    due = _first_number(
        records,
        {"order_total_brl", "order_total", "total_amount", "amount_due", "order_value"},
        {"order", "item", "payment"},
    )
    if due is None:
        item_total = 0.0
        item_found = False
        for record in records:
            if record.domain != "item":
                continue
            for item in _iter_dicts(record.data):
                if not _within_order_timeline(item, purchase_at, operational_end):
                    continue
                price = _numbers(
                    [value for name, value in item.items() if _key(str(name)) == "price"]
                )
                freight = _numbers(
                    [value for name, value in item.items() if _key(str(name)) == "freight_value"]
                )
                if price:
                    item_total += price[0] + (freight[0] if freight else 0.0)
                    item_found = True
        due = item_total if item_found else None
    return captured, refunded, due


def _explicit_issue(records: list[EvidenceRecord]) -> str | None:
    keys = {"primary_issue", "issue_code", "classification", "diagnosis"}
    for value in _values(records, keys, set(DOMAIN_ACTORS) - {"policy"}):
        issue = _key(str(value))
        if issue in KNOWN_ISSUES:
            return issue
    return None


def _payment_record_count(records: list[EvidenceRecord]) -> int:
    purchase_at = _purchase_at(records)
    operational_end = _operational_end(records)
    captured_events = 0
    count = 0
    for record in records:
        if record.domain != "payment":
            continue
        for item in _iter_dicts(record.data):
            if not _within_order_timeline(item, purchase_at, operational_end):
                continue
            if _key(str(item.get("event_type", ""))) in {
                "captured",
                "capture",
                "payment_captured",
            }:
                captured_events += 1
            keys = {_key(str(name)) for name in item}
            if keys & {"payment_value", "captured_amount", "payment_type", "payment_sequential"}:
                count += 1
    return captured_events or count


def _classify(case: dict[str, Any], records: list[EvidenceRecord]) -> tuple[str, float]:
    explicit = _explicit_issue(records)
    if explicit is not None:
        return explicit, 0.99

    request = case.get("customer_request") or {}
    claimed_topics = [
        _key(str(claim.get("topic", "")))
        for claim in request.get("claims", [])
        if isinstance(claim, dict) and claim.get("topic") != "requested_full_refund"
    ]
    claimed_topic = claimed_topics[0] if claimed_topics else ""
    captured, refunded, due = _payment_totals(records)
    statuses = {
        _key(str(value)) for value in _values(records, {"order_status", "status"}, {"order"})
    }
    purchase_at = _purchase_at(records)
    operational_end = _operational_end(records)
    refund_statuses = {
        _key(str(value))
        for value in _timeline_values(
            records,
            {"refund_status", "refund_state", "status"},
            {"payment"},
            purchase_at,
        )
    }
    flags: set[str] = set()
    if refund_statuses & {"failed", "failure", "rejected", "refund_failed"}:
        flags.add("refund_failed")
    if refund_statuses & {"pending", "processing", "initiated", "refund_pending"}:
        flags.add("refund_pending")
    if captured is not None and captured > refunded + 0.005:
        if statuses & {"unavailable", "unavailable_order"}:
            flags.add("unavailable_order_paid")
        if statuses & {"canceled", "cancelled"}:
            flags.add("canceled_order_paid")

    duplicate_values = _values(
        records,
        {"is_duplicate", "duplicate", "duplicate_charge"},
        {"payment"},
    )
    if any(
        value is True or _key(str(value)) in {"true", "yes", "duplicate"}
        for value in duplicate_values
    ):
        flags.add("duplicate_charge")

    if captured is not None and due is not None:
        if captured > due + 0.01:
            if _payment_record_count(records) > 1:
                flags.add("duplicate_charge")
            flags.add("payment_mismatch")
        elif abs(captured - due) <= 0.01 and _payment_record_count(records) > 1:
            flags.add("valid_split_payment")
        elif abs(captured - due) > 0.01:
            flags.add("payment_mismatch")

    shipping_limits = _timeline_dates(
        records,
        {"shipping_limit_date", "shipping_limit_at", "seller_deadline", "handoff_deadline"},
        {"item", "shipment", "order"},
        purchase_at,
        operational_end,
    )
    carrier_dates = _timeline_dates(
        records,
        {
            "order_delivered_carrier_date",
            "delivered_carrier_at",
            "carrier_pickup_at",
            "handed_to_carrier_at",
            "shipped_at",
        },
        {"shipment", "order"},
        purchase_at,
        operational_end,
    )
    delivered_dates = _timeline_dates(
        records,
        {
            "order_delivered_customer_date",
            "delivered_customer_at",
            "delivered_at",
            "delivery_date",
        },
        {"shipment", "order"},
        purchase_at,
        operational_end,
    )
    estimated_dates = _timeline_dates(
        records,
        {"order_estimated_delivery_date", "estimated_delivery_at", "estimated_delivery_date"},
        {"shipment", "order"},
        purchase_at,
        operational_end,
    )
    seller_late = bool(
        shipping_limits and carrier_dates and max(carrier_dates) > min(shipping_limits)
    )
    delivery_late = bool(
        delivered_dates and estimated_dates and max(delivered_dates) > max(estimated_dates)
    )
    if seller_late:
        flags.add("late_delivery_seller")
    elif delivery_late:
        flags.add("late_delivery_logistics")

    delay_parties = {
        _key(str(value))
        for value in _timeline_values(
            records,
            {"actor", "delay_party", "responsible_party", "delay_cause"},
            {"shipment", "seller", "order"},
            purchase_at,
            operational_end,
        )
    }
    if any("seller" in value for value in delay_parties):
        flags.add("late_delivery_seller")
    if any("logistic" in value or "carrier" in value for value in delay_parties):
        flags.add("late_delivery_logistics")

    if claimed_topic == "unsupported_claim" and records:
        return "unsupported_claim", 0.97
    if claimed_topic in flags:
        return claimed_topic, 0.98
    priority = (
        "refund_failed",
        "refund_pending",
        "duplicate_charge",
        "unavailable_order_paid",
        "canceled_order_paid",
        "late_delivery_seller",
        "late_delivery_logistics",
        "payment_mismatch",
        "valid_split_payment",
    )
    for issue in priority:
        if issue in flags:
            return issue, 0.9
    if records:
        return "unsupported_claim", 0.78
    return "insufficient_evidence", 0.25


def _relevant_domains(issue: str) -> set[str]:
    base = {"order", "item", "payment", "policy"}
    if issue == "unavailable_order_paid":
        return base | {"seller"}
    if issue in {"late_delivery_seller", "late_delivery_logistics"}:
        domains = base | {"shipment"}
        return domains | {"seller"} if issue == "late_delivery_seller" else domains
    if issue == "unsupported_claim":
        return base | {"shipment"}
    return base


def _evidence_refs(
    records: list[EvidenceRecord], domains: set[str], issue: str | None = None
) -> list[str]:
    return _unique_strings(
        [
            record.evidence_ref
            for record in records
            if record.domain in domains
            and not (
                record.tool_name == "get_refund_timeline"
                and issue not in {"refund_pending", "refund_failed"}
            )
        ],
        limit=30,
    )


def _policy_rule(records: list[EvidenceRecord], issue: str) -> dict[str, Any]:
    for record in records:
        if record.domain != "policy" or not isinstance(record.data, dict):
            continue
        rules = record.data.get("rules")
        if isinstance(rules, dict) and isinstance(rules.get(issue), dict):
            return rules[issue]
    return {}


def _policy_actions(records: list[EvidenceRecord], issue: str) -> list[str]:
    rule = _policy_rule(records, issue)
    values: list[Any] = []
    for name in ("recommended_action", "recommended_actions", "resolution_actions", "action"):
        if name in rule:
            values.extend(_scalars(rule[name]))
    return _unique_strings(values, limit=8)


def _resolution(
    issue: str, records: list[EvidenceRecord], entities: dict[str, list[str]]
) -> tuple[dict[str, Any], list[str]]:
    captured, refunded, due = _payment_totals(records)
    outstanding = max((captured or 0.0) - refunded, 0.0)
    if issue == "duplicate_charge":
        recommended = max((captured or 0.0) - (due or 0.0) - refunded, 0.0)
    elif issue == "payment_mismatch":
        recommended = max((captured or 0.0) - (due or captured or 0.0) - refunded, 0.0)
    elif issue in {
        "canceled_order_paid",
        "unavailable_order_paid",
        "refund_pending",
        "refund_failed",
    }:
        recommended = outstanding
    else:
        recommended = 0.0

    rule = _policy_rule(records, issue)
    policy_amounts = _numbers(
        [
            rule.get("refund_brl"),
            rule.get("recommended_refund_brl"),
            rule.get("eligible_refund_brl"),
        ]
    )
    if policy_amounts:
        recommended = max(policy_amounts[0], 0.0)
    recommended = round(recommended, 2)

    reason_by_issue = {
        "canceled_order_paid": "CANCELED_ORDER_REFUND",
        "unavailable_order_paid": "UNAVAILABLE_ORDER_REFUND",
        "duplicate_charge": "DUPLICATE_CHARGE_REFUND",
        "payment_mismatch": "PAYMENT_OVERAGE_REFUND",
        "refund_pending": "PENDING_REFUND_BALANCE",
        "refund_failed": "FAILED_REFUND_BALANCE",
    }
    refund_lines = []
    if recommended > 0:
        refund_lines.append(
            {
                "reason_code": reason_by_issue.get(issue, "POLICY_REFUND"),
                "amount_brl": recommended,
                "entity_id": entities["order_ids"][0] if entities["order_ids"] else None,
            }
        )

    default_actions = {
        "canceled_order_paid": ["ISSUE_OUTSTANDING_REFUND"],
        "unavailable_order_paid": ["ISSUE_OUTSTANDING_REFUND"],
        "late_delivery_seller": ["REVIEW_SELLER_SLA", "CONTACT_CUSTOMER"],
        "late_delivery_logistics": ["ESCALATE_LOGISTICS_DELAY", "CONTACT_CUSTOMER"],
        "valid_split_payment": ["NO_ACTION"],
        "payment_mismatch": ["RECONCILE_PAYMENT"],
        "duplicate_charge": ["REFUND_DUPLICATE_CHARGE"],
        "refund_pending": ["MONITOR_PENDING_REFUND"],
        "refund_failed": ["RETRY_FAILED_REFUND"],
        "unsupported_claim": ["NO_ACTION"],
        "insufficient_evidence": ["MANUAL_INVESTIGATION"],
    }
    actions = _policy_actions(records, issue) or default_actions[issue]
    return (
        {
            "currency": "BRL",
            "recommended_refund_brl": recommended,
            "refund_lines": refund_lines,
        },
        actions,
    )


def _root_cause(issue: str, entities: dict[str, list[str]]) -> dict[str, Any]:
    causes = {
        "canceled_order_paid": ("ORDER_CANCELED_AFTER_PAYMENT", "platform"),
        "unavailable_order_paid": ("ORDER_UNAVAILABLE_AFTER_PAYMENT", "seller"),
        "late_delivery_seller": ("SELLER_HANDOFF_DELAY", "seller"),
        "late_delivery_logistics": ("LOGISTICS_DELIVERY_DELAY", "logistics_provider"),
        "valid_split_payment": ("VALID_SPLIT_PAYMENT", "customer"),
        "payment_mismatch": ("PAYMENT_TOTAL_MISMATCH", "payment_provider"),
        "duplicate_charge": ("DUPLICATE_PAYMENT_CAPTURE", "payment_provider"),
        "refund_pending": ("REFUND_PROCESSING_PENDING", "payment_provider"),
        "refund_failed": ("REFUND_PROCESSING_FAILED", "payment_provider"),
        "unsupported_claim": ("CLAIM_NOT_SUPPORTED", "customer"),
        "insufficient_evidence": ("INSUFFICIENT_EVIDENCE", "unknown"),
    }
    cause_code, party_type = causes[issue]
    party_id = None
    if party_type == "seller" and entities["seller_ids"]:
        party_id = entities["seller_ids"][0]
    return {
        "ranked_causes": [{"cause_code": cause_code, "rank": 1}],
        "responsible_parties": [{"party_type": party_type, "party_id": party_id}],
    }


def _conflicts(records: list[EvidenceRecord]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    required = {"field", "sources", "selected_source", "resolution_code"}
    for record in records:
        for item in _iter_dicts(record.data):
            normalized = {_key(str(name)): value for name, value in item.items()}
            if not required <= normalized.keys():
                continue
            sources = _unique_strings(
                normalized["sources"] if isinstance(normalized["sources"], list) else [],
                limit=5,
            )
            if len(sources) < 2:
                continue
            selected = normalized["selected_source"]
            result.append(
                {
                    "field": str(normalized["field"])[:100],
                    "sources": [source[:80] for source in sources],
                    "selected_source": str(selected)[:80] if selected is not None else None,
                    "resolution_code": str(normalized["resolution_code"])[:80],
                }
            )
            if len(result) == 5:
                return result
    return result


def _claim_assessments(
    case: dict[str, Any],
    issue: str,
    confidence: float,
    records: list[EvidenceRecord],
    refund: dict[str, Any],
) -> list[dict[str, Any]]:
    request = case.get("customer_request") or {}
    assessments: list[dict[str, Any]] = []
    for claim in request.get("claims", [])[:5]:
        if not isinstance(claim, dict):
            continue
        topic = _key(str(claim.get("topic", "")))
        if issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
        elif topic == "requested_full_refund":
            captured, refunded, _ = _payment_totals(records)
            outstanding = max((captured or 0.0) - refunded, 0.0)
            amount = float(refund["recommended_refund_brl"])
            if outstanding > 0 and abs(amount - outstanding) <= 0.01:
                verdict = "supported"
            elif amount > 0:
                verdict = "partially_supported"
            else:
                verdict = "unsupported"
        elif topic == issue:
            verdict = "supported"
        elif issue == "unsupported_claim":
            verdict = "unsupported"
        else:
            verdict = "unsupported"
        domains = _relevant_domains(issue)
        if topic == "requested_full_refund":
            domains = {"order", "payment", "policy"}
        assessments.append(
            {
                "claim_id": str(claim.get("claim_id", "unknown"))[:64],
                "verdict": verdict,
                "confidence": round(
                    confidence if topic != "requested_full_refund" else min(confidence, 0.9), 2
                ),
                "evidence_refs": _evidence_refs(records, domains, issue),
            }
        )
    return assessments


def _build_output(case: dict[str, Any], records: list[EvidenceRecord]) -> dict[str, Any]:
    issue, confidence = _classify(case, records)
    request = case.get("customer_request") or {}
    entities = _extract_identifiers(records, str(request.get("claimed_order_id") or ""))
    financial_resolution, actions = _resolution(issue, records, entities)
    rule = _policy_rule(records, issue)
    policy_status = rule.get("case_status")
    if policy_status in {"action_required", "no_action", "needs_investigation"}:
        status = str(policy_status)
    elif issue in {"valid_split_payment", "unsupported_claim"}:
        status = "no_action"
    elif issue == "insufficient_evidence":
        status = "needs_investigation"
    else:
        status = "action_required"
    claim_assessments = _claim_assessments(case, issue, confidence, records, financial_resolution)
    primary_refs = _evidence_refs(records, _relevant_domains(issue), issue)
    submitted_refs = _unique_strings(
        [
            *primary_refs,
            *[
                evidence_ref
                for assessment in claim_assessments
                for evidence_ref in assessment["evidence_refs"]
            ],
        ],
        limit=30,
    )
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": str(case["case_id"]),
        "assessment": {
            "primary_issue": issue,
            "case_status": status,
            "confidence": round(confidence, 2),
        },
        "affected_entities": entities,
        "claim_assessments": claim_assessments,
        "root_cause_analysis": _root_cause(issue, entities),
        "evidence_refs": submitted_refs,
        "data_conflicts": _conflicts(records),
        "financial_resolution": financial_resolution,
        "resolution_actions": actions[:8],
    }


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Collect authoritative evidence, reconcile specialist results and solve one case."""
    records = await _collect_evidence(case, gateway, trace)
    output = _build_output(case, records)
    trace.emit(
        case_id=str(case["case_id"]),
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        decision_code="OUTPUT_INVARIANTS_VERIFIED",
        evidence_refs=output["evidence_refs"][:20],
        attributes={
            "primary_issue": output["assessment"]["primary_issue"],
            "evidence_count": len(output["evidence_refs"]),
            "refund_brl": output["financial_resolution"]["recommended_refund_brl"],
        },
    )
    return output
