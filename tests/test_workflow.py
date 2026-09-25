from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.workflow import EvidenceRecord, _build_output, solve_case


class FakeGateway:
    async def list_tool_specs(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "get_order",
                "description": "Get an order with its payment and delivery status",
                "input_schema": {
                    "type": "object",
                    "required": ["case_id", "order_id"],
                    "properties": {"case_id": {"type": "string"}, "order_id": {"type": "string"}},
                },
            },
            {
                "name": "get_order_payments",
                "description": "List payment records",
                "input_schema": {
                    "type": "object",
                    "required": ["case_id", "order_id"],
                    "properties": {"case_id": {"type": "string"}, "order_id": {"type": "string"}},
                },
            },
            {
                "name": "get_policy",
                "description": "Get a policy rule",
                "input_schema": {
                    "type": "object",
                    "required": ["case_id", "policy_version"],
                    "properties": {
                        "case_id": {"type": "string"},
                        "policy_version": {"type": "string"},
                    },
                },
            },
        ]

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        assert case_id == "L3A_CASE_001"
        if tool_name == "get_order":
            data = {"order_id": arguments["order_id"], "order_status": "canceled"}
            ref = "ev_order_abcdefghijklmnopqrstuvwxyz"
        elif tool_name == "get_order_payments":
            data = {"captured_total_brl": 100, "refunded_total_brl": 0}
            ref = "ev_payment_abcdefghijklmnopqrstuvwxyz"
        else:
            data = {"action_code": "ISSUE_OUTSTANDING_REFUND"}
            ref = "ev_policy_abcdefghijklmnopqrstuvwxyz"
        return {"evidence_ref": ref, "data": data}


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> dict[str, Any]:
        self.events.append(event)
        return event


def test_solve_case_builds_a_contract_valid_evidence_linked_output() -> None:
    case = {
        "case_id": "L3A_CASE_001",
        "policy_version": "EC_POLICY_V1",
        "customer_request": {
            "claimed_order_id": "order-1",
            "claims": [
                {"claim_id": "claim-1", "topic": "canceled_order_paid"},
                {"claim_id": "claim-2", "topic": "requested_full_refund"},
            ],
        },
    }
    trace = FakeTrace()

    output = asyncio.run(solve_case(case, FakeGateway(), trace))  # type: ignore[arg-type]

    Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas").validate_output(
        output, "test output"
    )
    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert output["financial_resolution"]["recommended_refund_brl"] == 100
    assert len(output["evidence_refs"]) == 3
    assert any(event["event_type"] == "tool_result_consumed" for event in trace.events)
    assert trace.events[-1]["event_type"] == "verification_completed"


def test_verifier_ignores_evidence_that_predates_the_order() -> None:
    case = {
        "case_id": "L3A_CASE_005",
        "policy_version": "EC_POLICY_V1",
        "customer_request": {
            "claimed_order_id": "order-5",
            "claims": [
                {"claim_id": "claim-1", "topic": "valid_split_payment"},
                {"claim_id": "claim-2", "topic": "requested_full_refund"},
            ],
        },
    }
    records = [
        EvidenceRecord(
            "order",
            "get_order",
            "ev_order_abcdefghijklmnopqrstuvwxyz",
            {
                "order_id": "order-5",
                "order_status": "delivered",
                "order_purchase_timestamp": "2018-04-23T09:00:00-03:00",
                "order_delivered_customer_date": "2018-05-02T09:00:00-03:00",
                "order_estimated_delivery_date": "2018-05-03T09:00:00-03:00",
            },
        ),
        EvidenceRecord(
            "item",
            "get_order_items",
            "ev_items_abcdefghijklmnopqrstuvwxyz",
            [
                {
                    "order_item_id": "item-5",
                    "shipping_limit_date": "2018-04-26T09:00:00-03:00",
                    "price": "79.00",
                    "freight_value": "10.00",
                },
                {
                    "order_item_id": "item-5",
                    "shipping_limit_date": "2018-08-31T09:00:00-03:00",
                    "price": "79.00",
                    "freight_value": "10.00",
                },
            ],
        ),
        EvidenceRecord(
            "payment",
            "get_payment_timeline",
            "ev_paytime_abcdefghijklmnopqrstuvwxyz",
            {
                "events": [
                    {
                        "event_at": "2018-04-23T10:00:00-03:00",
                        "event_type": "captured",
                        "amount_brl": "44.50",
                        "status": "confirmed",
                    },
                    {
                        "event_at": "2018-04-23T11:00:00-03:00",
                        "event_type": "captured",
                        "amount_brl": "44.50",
                        "status": "confirmed",
                    },
                    {
                        "event_at": "2018-08-28T10:00:00-03:00",
                        "event_type": "captured",
                        "amount_brl": "52.00",
                        "status": "confirmed",
                    },
                ]
            },
        ),
        EvidenceRecord(
            "payment",
            "get_refund_timeline",
            "ev_refund_abcdefghijklmnopqrstuvwxyz",
            {
                "events": [
                    {
                        "event_at": "2018-09-08T09:00:00-03:00",
                        "event_type": "refund_requested",
                        "amount_brl": "52.00",
                        "status": "failed",
                    }
                ]
            },
        ),
        EvidenceRecord(
            "policy",
            "get_policy",
            "ev_policy_abcdefghijklmnopqrstuvwxyz",
            {
                "rules": {
                    "valid_split_payment": {
                        "case_status": "no_action",
                        "recommended_action": "document_no_action",
                        "refund_brl": 0,
                    }
                }
            },
        ),
    ]

    output = _build_output(case, records)

    assert output["assessment"] == {
        "primary_issue": "valid_split_payment",
        "case_status": "no_action",
        "confidence": 0.98,
    }
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert output["resolution_actions"] == ["document_no_action"]
