"""Policy layer: pure decisions about freeze orders and balance checks.

Everything here takes already-loaded rows/dicts and returns a verdict
(message string) or ``None``. It owns no SQLite state and performs no I/O.
"""
from __future__ import annotations

from typing import Any

EPSILON = 1e-9


def order_payload_ok(payload: dict[str, Any]) -> tuple[str, str, str, str, str]:
    reach = str(payload.get("reach", "")).strip()
    reason = str(payload.get("reason", "")).strip()
    started_on = str(payload.get("started_on", "")).strip()
    planned_end_on = str(payload.get("planned_end_on", "")).strip()
    condition_note = str(payload.get("condition_note", "")).strip()
    if not reach:
        raise ValueError("河段不能为空")
    if not started_on or not planned_end_on:
        raise ValueError("禁调开始日期和计划恢复日期不能为空")
    # ISO date check without importing date internals into the policy contract.
    from datetime import date as _date

    def _parse(value: str, label: str) -> str:
        try:
            return _date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise ValueError(f"{label}必须是 YYYY-MM-DD") from exc

    start = _parse(started_on, "禁调开始日期")
    end = _parse(planned_end_on, "计划恢复日期")
    if end < start:
        raise ValueError("计划恢复日期不能早于禁调开始日期")
    return reach, reason, start, end, condition_note


def conditions_ok(planned_end_on: str, condition_note: str) -> tuple[str, str]:
    from datetime import date as _date

    try:
        end = _date.fromisoformat(planned_end_on).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError("计划恢复日期必须是 YYYY-MM-DD") from exc
    return end, str(condition_note or "").strip()


def is_active(order: Any) -> bool:
    return order is not None and order["status"] == "active"


def verdict_for_account(order: Any, account: Any) -> str | None:
    """A freeze order covers an account when the account's reach matches."""
    if not is_active(order):
        return None
    if account is not None and order["reach"] == account["reach"]:
        return f"河段 {order['reach']} 处于禁调期（禁调令 #{order['id']}），水权账户暂时冻结"
    return None


def transfer_blocked(order: Any, source: Any, target: Any) -> str | None:
    """Only the affected reach is blocked; other reaches keep operating."""
    if not is_active(order):
        return None
    if source is not None and order["reach"] == source["reach"]:
        return f"河段 {order['reach']} 禁调期内不能发起或审批转出（禁调令 #{order['id']}）"
    return None


def usage_blocked(order: Any, account: Any) -> str | None:
    verdict = verdict_for_account(order, account)
    if verdict is None:
        return None
    return f"河段 {order['reach']} 禁调期内不接受新的取水确认（禁调令 #{order['id']}）"


def should_suspend_approval(source: Any, order: Any) -> bool:
    """An approval made while the source's reach is frozen cannot execute yet."""
    return is_active(order) and source is not None and order["reach"] == source["reach"]


def valid_on(account: Any, day: str) -> bool:
    return account["valid_from"] <= day <= account["valid_to"]


def priority_ok(source: Any, target: Any) -> bool:
    return int(source["priority"]) <= int(target["priority"])


def impact_remaining_ok(available_after: float, quota: float, min_fraction: float) -> bool:
    return available_after + EPSILON >= quota * float(min_fraction)


def enough(amount: float, available: float) -> bool:
    return amount <= available + EPSILON


def monthly_cap_ok(month_total: float, amount: float, quota: float, max_fraction: float) -> bool:
    return month_total + amount <= quota * float(max_fraction) + EPSILON


def order_version_current(order: Any, expected_version: Any) -> bool:
    try:
        return int(expected_version) == int(order["version"])
    except (TypeError, ValueError):
        return False
