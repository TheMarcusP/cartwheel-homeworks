"""Tests for get_refund_status, the HW1 extra tool (offline, no API keys)."""

from __future__ import annotations

from pathlib import Path

from agent import tools
from agent.agent import TOOLS_BY_ROLE, issue_refund_logic
from agent.auth import AuthContext

SHOPPER_1 = AuthContext(user_id=1, role="shopper")
MERCHANT_STORE_1 = AuthContext(user_id=9001, role="merchant", store_id=1)
MERCHANT_STORE_2 = AuthContext(user_id=9002, role="merchant", store_id=2)
SUPPORT = AuthContext(user_id=9501, role="support")


def test_no_refund_yet(world_copy: Path) -> None:
    result = tools.get_refund_status(SHOPPER_1, 4127)
    assert result["ok"] is True
    assert result["already_requested"] is False
    assert result["refunds"] == []
    assert result["total_requested_usd"] == 0


def test_queued_refund_is_visible(world_copy: Path) -> None:
    issued = issue_refund_logic(SHOPPER_1, 4455, 240.0, "test")
    assert issued["status"] == "queued_for_approval"

    result = tools.get_refund_status(SHOPPER_1, 4455)
    assert result["already_requested"] is True
    assert [r["refund_id"] for r in result["refunds"]] == [issued["refund_id"]]
    assert result["refunds"][0]["status"] == "queued_for_approval"
    assert result["total_requested_usd"] == 240.0
    assert result["order_total_usd"] == 240.0
    assert "do not call issue_refund again" in result["note"]


def test_duplicate_refunds_show_full_total(world_copy: Path) -> None:
    # issue_refund itself does not block duplicates (record 11); the status
    # tool must at least make the over-refund visible.
    issue_refund_logic(SHOPPER_1, 4455, 240.0, "first")
    issue_refund_logic(SHOPPER_1, 4455, 240.0, "second")
    result = tools.get_refund_status(SHOPPER_1, 4455)
    assert len(result["refunds"]) == 2
    assert result["total_requested_usd"] == 480.0


def test_scope_matches_get_order(world_copy: Path) -> None:
    issue_refund_logic(SHOPPER_1, 4455, 240.0, "test")

    denied = tools.get_refund_status(MERCHANT_STORE_2, 4455)
    assert denied["ok"] is False
    assert denied["error"] == "permission_denied"
    assert "refunds" not in denied and "240" not in denied["reason"]

    assert tools.get_refund_status(MERCHANT_STORE_1, 4455)["already_requested"] is True
    assert tools.get_refund_status(SUPPORT, 4455)["already_requested"] is True


def test_unknown_order(world_copy: Path) -> None:
    assert tools.get_refund_status(SHOPPER_1, 999_999)["error"] == "not_found"


def test_registered_for_every_role() -> None:
    for role, role_tools in TOOLS_BY_ROLE.items():
        assert "get_refund_status" in [t.name for t in role_tools], role
