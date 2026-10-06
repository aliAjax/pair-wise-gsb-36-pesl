"""Service layer: water-rights business logic on top of :mod:`storage`.

The :class:`Database` facade keeps the public API that the HTTP layer and
tests use; decisions delegate to :mod:`freeze_policy`, persistence to
:mod:`storage`.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import date
from typing import Any

from freeze_policy import (
    conditions_ok,
    enough,
    impact_remaining_ok,
    monthly_cap_ok,
    order_payload_ok,
    order_version_current,
    priority_ok,
    should_suspend_approval,
    transfer_blocked,
    usage_blocked,
    valid_on,
)
from storage import DEFAULT_DB, RESERVED_STATUSES, Storage, utcnow


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def parse_date(value: str, field: str = "日期") -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DomainError(f"{field}必须是 YYYY-MM-DD") from exc


DISPATCH_ROLES = {"editor", "dispatcher"}
REVIEW_ROLES = {"reviewer", "dispatcher"}


class Database:
    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB):
        self.store = Storage(path)

    def connect(self) -> sqlite3.Connection:
        return self.store.connect()

    # ---------------------------------------------------------------- helpers
    def _audit_row(self, conn: sqlite3.Connection, actor: str, action: str, entity_type: str,
                   entity_id: int | None, details: dict[str, Any]) -> None:
        self.store.audit(conn, actor, action, entity_type, entity_id, details)

    def _account(self, conn: sqlite3.Connection, account_id: int) -> sqlite3.Row:
        row = self.store.get_account(conn, account_id)
        if not row:
            raise DomainError("水权账户不存在", 404)
        return row

    def _require_role(self, role: str, allowed: set[str], message: str) -> None:
        if role not in allowed:
            raise DomainError(message, 403)

    def _reserved_outgoing(self, conn: sqlite3.Connection, account_id: int,
                           exclude_id: int | None = None) -> float:
        return self.store.sum_outgoing(conn, account_id, RESERVED_STATUSES, exclude_id)

    def _available(self, conn: sqlite3.Connection, account: sqlite3.Row) -> float:
        reserved = self._reserved_outgoing(conn, int(account["id"]))
        suspended_out = self.store.sum_suspended(conn, int(account["id"]), "out")
        # Suspended transfers keep living in the quota column, but their amount
        # is committed to the restore queue and must not look spendable.
        return max(0.0, float(account["quota"]) - float(account["used"])
                   - reserved - suspended_out)

    # ---------------------------------------------------------------- accounts
    def create_account(self, actor: str, payload: dict[str, Any], role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有配额管理员可以创建账户", 403)
        name = str(payload.get("name", "")).strip()
        region = str(payload.get("region", "")).strip()
        reach = str(payload.get("reach", "")).strip() or region
        holder = str(payload.get("holder", "")).strip()
        if not name or not region or not holder:
            raise DomainError("账户名称、地区和持有人不能为空")
        try:
            priority = int(payload.get("priority"))
            quota = float(payload.get("quota"))
        except (TypeError, ValueError) as exc:
            raise DomainError("优先级和额度必须是数值") from exc
        if not 1 <= priority <= 5 or quota < 0:
            raise DomainError("优先级应在 1 到 5 之间，额度不能为负")
        valid_from = parse_date(str(payload.get("valid_from", "")), "生效日期")
        valid_to = parse_date(str(payload.get("valid_to", "")), "失效日期")
        if valid_from > valid_to:
            raise DomainError("生效日期不能晚于失效日期")
        with self.connect() as conn:
            try:
                cur = self.store.insert_account(conn, {
                    "name": name, "region": region, "reach": reach, "holder": holder,
                    "priority": priority, "valid_from": valid_from.isoformat(),
                    "valid_to": valid_to.isoformat(), "quota": quota,
                })
            except sqlite3.IntegrityError as exc:
                raise DomainError("账户名称已存在", 409) from exc
            self._audit_row(conn, actor, "account.created", "account", cur.lastrowid,
                            {"name": name, "quota": quota, "reach": reach})
            return dict(self.store.get_account(conn, cur.lastrowid))

    def available(self, account_id: int, as_of: str | None = None) -> dict[str, Any]:
        if as_of:
            parse_date(as_of, "查询日期")
        with self.connect() as conn:
            account = self._account(conn, account_id)
            reserved = self._reserved_outgoing(conn, account_id)
            frozen_out = self.store.sum_suspended(conn, account_id, "out")
            frozen_in = self.store.sum_suspended(conn, account_id, "in")
            order = self.store.get_active_order(conn, account["reach"])
            value = max(0.0, float(account["quota"]) - float(account["used"])
                        - reserved - frozen_out)
            result = {
                "account_id": account_id,
                "available": value,
                "reserved_outgoing": reserved,
                "frozen_outgoing": frozen_out,
                "frozen_incoming": frozen_in,
                "frozen": bool(order),
                "freeze_order_id": int(order["id"]) if order else None,
                "restore_queue": self._restore_queue(conn, account_id),
                "quota": account["quota"],
                "used": account["used"],
            }
        return result

    def _restore_queue(self, conn: sqlite3.Connection, account_id: int) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT t.id,t.amount,t.restore_seq,t.restore_state,t.restore_error,"
            "t.to_account_id,t.from_account_id,a.name AS counterpart "
            "FROM transfers t JOIN accounts a ON a.id=CASE WHEN t.from_account_id=? THEN t.to_account_id ELSE t.from_account_id END "
            "WHERE (t.from_account_id=? OR t.to_account_id=?) AND t.status='suspended' ORDER BY t.restore_seq",
            (account_id, account_id, account_id),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- rules
    def set_season_rule(self, actor: str, region: str, month: int, max_fraction: float,
                        note: str = "", role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有配额管理员可以设置季节规则", 403)
        if not 1 <= int(month) <= 12 or not 0 < float(max_fraction) <= 1:
            raise DomainError("月份或季节比例不合法")
        with self.connect() as conn:
            self.store.upsert_season_rule(conn, region.strip(), int(month), float(max_fraction), note)
            self._audit_row(conn, actor, "season_rule.saved", "region", None,
                            {"region": region, "month": month, "max_fraction": max_fraction})
        return {"region": region, "month": month, "max_fraction": max_fraction, "note": note}

    def set_impact_rule(self, actor: str, source_region: str, target_region: str, min_source_fraction: float,
                        note: str = "", role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有配额管理员可以设置第三方影响规则", 403)
        if not 0 <= float(min_source_fraction) <= 1:
            raise DomainError("最小留存比例必须在 0 到 1 之间")
        with self.connect() as conn:
            self.store.upsert_impact_rule(conn, source_region, target_region, float(min_source_fraction), note)
            self._audit_row(conn, actor, "impact_rule.saved", "region", None,
                            {"source": source_region, "target": target_region, "min_fraction": min_source_fraction})
        return {"source_region": source_region, "target_region": target_region,
                "min_source_fraction": min_source_fraction, "note": note}

    # ---------------------------------------------------------------- transfers
    def create_transfer(self, actor: str, payload: dict[str, Any], role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有水权编辑人员可以发起转让", 403)
        try:
            source_id = int(payload.get("from_account_id"))
            target_id = int(payload.get("to_account_id"))
            amount = float(payload.get("amount"))
        except (TypeError, ValueError) as exc:
            raise DomainError("账户和转让量必须是数值") from exc
        if source_id == target_id or amount <= 0:
            raise DomainError("转让账户不能相同，转让量必须为正")
        effective = parse_date(str(payload.get("effective_date", "")), "生效日期")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = self._account(conn, source_id)
            target = self._account(conn, target_id)
            reason = transfer_blocked(self.store.get_active_order(conn, source["reach"]), source, target)
            if reason:
                raise DomainError(reason, 409)
            if not valid_on(source, effective.isoformat()):
                raise DomainError("转出账户在生效日无效", 409)
            if not valid_on(target, effective.isoformat()):
                raise DomainError("转入账户在生效日无效", 409)
            reserved = self._reserved_outgoing(conn, source_id)
            avail = float(source["quota"]) - float(source["used"]) - reserved
            if not enough(amount, avail):
                raise DomainError("可用额度不足，待审批转让会预占额度", 409)
            if not priority_ok(source, target):
                raise DomainError("不能把较低优先级水量转给更高优先级账户", 409)
            impact = self.store.get_impact_rule(conn, source["region"], target["region"])
            if impact and not impact_remaining_ok(avail - amount, float(source["quota"]),
                                                  float(impact["min_source_fraction"])):
                raise DomainError("转让会违反下游第三方最小留存约束", 409)
            cur = self.store.insert_transfer(conn, {
                "from": source_id, "to": target_id, "amount": amount,
                "effective_date": effective.isoformat(), "created_by": actor,
            })
            self._audit_row(conn, actor, "transfer.created", "transfer", cur.lastrowid,
                            {"source": source_id, "target": target_id, "amount": amount,
                             "effective_date": effective.isoformat()})
            return dict(self.store.get_transfer(conn, cur.lastrowid))

    def _impact_ok(self, conn: sqlite3.Connection, source: sqlite3.Row, target: sqlite3.Row,
                   avail_after: float) -> bool:
        impact = self.store.get_impact_rule(conn, source["region"], target["region"])
        if impact is None:
            return True
        return impact_remaining_ok(avail_after, float(source["quota"]), float(impact["min_source_fraction"]))

    def approve_transfer(self, transfer_id: int, actor: str, role: str = "reviewer") -> dict[str, Any]:
        self._require_role(role, REVIEW_ROLES, "只有审核人可以批准转让")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            transfer = self.store.get_transfer(conn, transfer_id)
            if not transfer:
                raise DomainError("转让记录不存在", 404)
            if transfer["status"] != "pending":
                raise DomainError("该转让已处理，不能重复批准", 409)
            if actor == transfer["created_by"]:
                raise DomainError("发起人不能批准自己的转让", 403)
            source = self._account(conn, transfer["from_account_id"])
            target = self._account(conn, transfer["to_account_id"])
            amount = float(transfer["amount"])
            order = self.store.get_active_order(conn, source["reach"])
            if transfer_blocked(order, source, target):
                raise DomainError(
                    f"河段 {order['reach']} 已发布禁调令 #{order['id']}，该转让暂缓批准；"
                    "恢复后可按原顺序补办", 409)
            other_reserved = self._reserved_outgoing(conn, int(source["id"]), exclude_id=transfer_id)
            avail = float(source["quota"]) - float(source["used"]) - other_reserved
            if not enough(amount, avail):
                raise DomainError("审批时额度已被其他记录占用，不能批准", 409)
            if not self._impact_ok(conn, source, target, avail - amount):
                raise DomainError("审批时下游最小留存约束不再满足", 409)
            # Approval reserves the amount; quota physically moves when the
            # transfer is executed (settlement) so a later freeze can suspend it.
            conn.execute(
                "UPDATE transfers SET status='approved',approved_by=?,approved_at=? WHERE id=?",
                (actor, utcnow(), transfer_id),
            )
            self._audit_row(conn, actor, "transfer.approved", "transfer", transfer_id, {"amount": amount})
            row = self.store.get_transfer(conn, transfer_id)
        return dict(row)

    def reject_transfer(self, transfer_id: int, actor: str, role: str = "reviewer") -> dict[str, Any]:
        self._require_role(role, REVIEW_ROLES, "只有审核人可以退回转让")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self.store.get_transfer(conn, transfer_id)
            if not row or row["status"] != "pending":
                raise DomainError("转让不存在或已经处理", 409)
            if actor == row["created_by"]:
                raise DomainError("发起人不能自行退回", 403)
            conn.execute(
                "UPDATE transfers SET status='rejected',approved_by=?,approved_at=? WHERE id=?",
                (actor, utcnow(), transfer_id),
            )
            self._audit_row(conn, actor, "transfer.rejected", "transfer", transfer_id, {})
        return {"id": transfer_id, "status": "rejected"}

    # ---------------------------------------------------------------- settlement
    def execute_transfers(self, actor: str, as_of: str | None = None,
                          role: str = "reviewer") -> dict[str, Any]:
        """Settlement batch: execute approved transfers due on/before ``as_of``.

        Transfers whose source reach is frozen are captured into the order's
        restoration queue (suspended) instead of failing the batch.
        """
        self._require_role(role, REVIEW_ROLES, "只有审核人或调度员可以执行转让结算")
        day = as_of or date.today().isoformat()
        parse_date(day, "结算日期")
        executed: list[int] = []
        suspended: list[int] = []
        failures: list[dict[str, Any]] = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for transfer in self.store.due_approved_transfers(conn, day):
                source = self.store.get_account(conn, transfer["from_account_id"])
                target = self.store.get_account(conn, transfer["to_account_id"])
                order = self.store.get_active_order(conn, source["reach"])
                if order and source["reach"] == order["reach"]:
                    seq = self.store.next_restore_seq(conn, int(order["id"]))
                    self.store.suspend_during_run(conn, int(transfer["id"]), int(order["id"]), seq)
                    suspended.append(int(transfer["id"]))
                    self._audit_row(conn, actor, "transfer.suspended", "transfer", int(transfer["id"]),
                                    {"order_id": int(order["id"]), "restore_seq": seq})
                    continue
                reason = self._execution_blocker(conn, transfer, source, target)
                if reason:
                    self.store.mark_execute_failed(conn, int(transfer["id"]), reason)
                    failures.append({"transfer_id": int(transfer["id"]), "reason": reason})
                    continue
                self.store.move_quota(conn, int(source["id"]), int(target["id"]), float(transfer["amount"]))
                self.store.mark_executed(conn, int(transfer["id"]), actor, restored=False)
                executed.append(int(transfer["id"]))
                self._audit_row(conn, actor, "transfer.executed", "transfer", int(transfer["id"]),
                                {"amount": transfer["amount"]})
        return {"as_of": day, "executed": executed, "suspended": suspended, "failures": failures}

    def _execution_blocker(self, conn: sqlite3.Connection, transfer: sqlite3.Row,
                           source: sqlite3.Row, target: sqlite3.Row) -> str | None:
        amount = float(transfer["amount"])
        if not valid_on(source, transfer["effective_date"]) or not valid_on(target, transfer["effective_date"]):
            return "执行时账户许可有效期已不满足"
        other_reserved = self._reserved_outgoing(conn, int(source["id"]), exclude_id=int(transfer["id"]))
        avail = float(source["quota"]) - float(source["used"]) - other_reserved
        if not enough(amount, avail):
            return "执行时转出账户可用额度不足"
        if not priority_ok(source, target):
            return "执行时优先级约束不再满足"
        if not self._impact_ok(conn, source, target, avail - amount):
            return "执行时下游最小留存约束不再满足"
        return None

    # ---------------------------------------------------------------- freeze
    def create_freeze_order(self, actor: str, payload: dict[str, Any],
                            role: str = "dispatcher") -> dict[str, Any]:
        self._require_role(role, DISPATCH_ROLES, "只有调度员可以发布禁调令")
        try:
            reach, reason, started_on, planned_end_on, condition_note = order_payload_ok(payload)
        except ValueError as exc:
            raise DomainError(str(exc)) from exc
        suspended: list[dict[str, Any]] = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if self.store.get_active_order(conn, reach):
                raise DomainError(f"河段 {reach} 已有生效中的禁调令", 409)
            try:
                cur = self.store.insert_order(conn, {
                    "reach": reach, "reason": reason, "started_on": started_on,
                    "planned_end_on": planned_end_on, "condition_note": condition_note,
                    "created_by": actor,
                })
            except sqlite3.IntegrityError as exc:
                # Two dispatchers racing on the same reach: only one writes.
                raise DomainError(f"河段 {reach} 的禁调令刚被其他调度员创建，请刷新查看当前状态", 409) from exc
            order_id = int(cur.lastrowid)
            accounts = self.store.accounts_by_reach(conn, reach)
            for account in accounts:
                self.store.upsert_freeze_account(conn, order_id, int(account["id"]))
            # Already-approved, not-yet-executed transfers out of the reach are
            # paused immediately and queued in their original order.
            for transfer in self.store.approved_unexecuted_by_reach(conn, reach):
                seq = self.store.next_restore_seq(conn, order_id)
                self.store.suspend_transfer(conn, int(transfer["id"]), order_id, seq)
                suspended.append({"transfer_id": int(transfer["id"]), "restore_seq": seq,
                                  "amount": float(transfer["amount"])})
                self._audit_row(conn, actor, "transfer.suspended", "transfer", int(transfer["id"]),
                                {"order_id": order_id, "restore_seq": seq})
            self._audit_row(conn, actor, "freeze.created", "freeze_order", order_id,
                            {"reach": reach, "started_on": started_on, "planned_end_on": planned_end_on,
                             "accounts": [int(a["id"]) for a in accounts],
                             "suspended_transfer_ids": [s["transfer_id"] for s in suspended]})
            order = dict(self.store.get_order(conn, order_id))
        order["suspended"] = suspended
        return order

    def update_freeze_conditions(self, order_id: int, actor: str, payload: dict[str, Any],
                                 role: str = "dispatcher") -> dict[str, Any]:
        """Dispatcher changes the recovery conditions the same day.

        Optimistic concurrency: the client must echo the version it saw. If a
        competing write already moved the version, the loser keeps its record
        and gets back the current state (HTTP 409) instead of overwriting.
        """
        self._require_role(role, DISPATCH_ROLES, "只有调度员可以修改恢复条件")
        try:
            planned_end_on, condition_note = conditions_ok(
                str(payload.get("planned_end_on", "")), str(payload.get("condition_note", "")))
        except ValueError as exc:
            raise DomainError(str(exc)) from exc
        expected = payload.get("version")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            order = self.store.get_order(conn, order_id)
            if not order:
                raise DomainError("禁调令不存在", 404)
            if order["status"] != "active":
                raise DomainError("禁调令已解除，不能再修改恢复条件", 409)
            if expected is not None and not order_version_current(order, expected):
                raise _Conflict(order, "恢复条件已被其他调度员更新，你看到的是旧版本；以下为当前状态")
            self.store.update_conditions(conn, order_id, planned_end_on, condition_note, actor)
            self._audit_row(conn, actor, "freeze.conditions_updated", "freeze_order", order_id,
                            {"planned_end_on": planned_end_on, "condition_note": condition_note,
                             "from_version": order["version"]})
            return dict(self.store.get_order(conn, order_id))

    def lift_freeze(self, order_id: int, actor: str, payload: dict[str, Any] | None = None,
                    role: str = "dispatcher") -> dict[str, Any]:
        self._require_role(role, DISPATCH_ROLES, "只有调度员可以解除禁调令")
        payload = payload or {}
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            order = self.store.get_order(conn, order_id)
            if not order:
                raise DomainError("禁调令不存在", 404)
            expected = payload.get("version")
            if expected is not None and not order_version_current(order, expected):
                raise _Conflict(order, "禁调令版本已变化，解除前请查看当前状态")
            if order["status"] != "active":
                # Idempotent: a duplicate lift reports the current record.
                return dict(order)
            self.store.lift_order(conn, order_id, actor)
            self._audit_row(conn, actor, "freeze.lifted", "freeze_order", order_id, {})
        # Restoration runs as its own batch after the order is lifted.
        report = self.restore_freeze(order_id, actor, role=role)
        return {"order": self.get_freeze_order(order_id), "restoration": report}

    def restore_freeze(self, order_id: int, actor: str, role: str = "dispatcher") -> dict[str, Any]:
        """Recovery batch: retry only unfinished water-right accounts.

        Accounts already restored and transfers already executed are skipped,
        so reruns never move quota twice.
        """
        self._require_role(role, DISPATCH_ROLES, "只有调度员可以执行恢复批处理")
        executed: list[int] = []
        failed: list[dict[str, Any]] = []
        restored_accounts: list[int] = []
        skipped_accounts: list[int] = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            order = self.store.get_order(conn, order_id)
            if not order:
                raise DomainError("禁调令不存在", 404)
            frozen_accounts = [int(r["account_id"]) for r in self.store.list_freeze_accounts(conn, order_id)]
            # Retry only accounts that still have suspended transfers pending.
            for row in self.store.unfinished_source_accounts(conn, order_id):
                account_id = int(row["account_id"])
                items = self.store.unfinished_for_account(conn, order_id, account_id)
                if not items:
                    continue
                account_done = True
                for transfer in items:
                    source = self.store.get_account(conn, account_id)
                    target = self.store.get_account(conn, transfer["to_account_id"])
                    reason = self._execution_blocker(conn, transfer, source, target)
                    if reason:
                        self.store.mark_restore_failed(conn, int(transfer["id"]), reason)
                        self.store.touch_freeze_account(conn, order_id, account_id, reason)
                        failed.append({"transfer_id": int(transfer["id"]),
                                       "account_id": account_id, "restore_seq": transfer["restore_seq"],
                                       "reason": reason})
                        # Original ordering preserved: stop this account at its
                        # first blocked transfer; later items keep waiting.
                        account_done = False
                        break
                    self.store.move_quota(conn, account_id, int(target["id"]), float(transfer["amount"]))
                    self.store.mark_executed(conn, int(transfer["id"]), actor, restored=True)
                    self.store.touch_freeze_account(conn, order_id, account_id, None)
                    executed.append(int(transfer["id"]))
                    self._audit_row(conn, actor, "transfer.restored", "transfer", int(transfer["id"]),
                                    {"order_id": order_id, "restore_seq": transfer["restore_seq"]})
                if account_done:
                    self.store.restore_freeze_account_if_done(conn, order_id, account_id)
                    restored_accounts.append(account_id)
                else:
                    skipped_accounts.append(account_id)
            # Accounts that never held a suspended outgoing transfer (target-only
            # accounts or quiet accounts) are freed immediately on recovery.
            for account_id in frozen_accounts:
                self.store.restore_freeze_account_if_done(conn, order_id, account_id)
        return {
            "order_id": order_id,
            "executed": executed,
            "failed": failed,
            "restored_accounts": restored_accounts,
            "pending_accounts": skipped_accounts,
        }

    def get_freeze_order(self, order_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            order = self.store.get_order(conn, order_id)
            if not order:
                raise DomainError("禁调令不存在", 404)
            data = dict(order)
            data["queue"] = [dict(t) for t in self.store.suspended_for_order(conn, order_id)]
            data["accounts"] = [dict(a) for a in self.store.list_freeze_accounts(conn, order_id)]
        return data

    def freeze_board(self) -> dict[str, Any]:
        """Page data grouped by reach: frozen accounts, restore queue, failures."""
        with self.connect() as conn:
            orders = self.store.list_orders(conn)
            reaches: dict[str, Any] = {}
            for order_row in orders:
                order = dict(order_row)
                order_id = int(order["id"])
                fa_rows = self.store.list_freeze_accounts(conn, order_id)
                accounts = []
                for fa in fa_rows:
                    account = self.store.get_account(conn, int(fa["account_id"]))
                    frozen_out = self.store.sum_suspended(conn, int(fa["account_id"]), "out")
                    frozen_in = self.store.sum_suspended(conn, int(fa["account_id"]), "in")
                    item = dict(fa)
                    item["name"] = account["name"] if account else None
                    item["region"] = account["region"] if account else None
                    item["frozen_outgoing"] = frozen_out
                    item["frozen_incoming"] = frozen_in
                    accounts.append(item)
                queue = []
                failures = []
                for t in self.store.suspended_for_order(conn, order_id):
                    entry = dict(t)
                    source = self.store.get_account(conn, t["from_account_id"])
                    target = self.store.get_account(conn, t["to_account_id"])
                    entry["from_name"] = source["name"] if source else None
                    entry["to_name"] = target["name"] if target else None
                    queue.append(entry)
                    if t["restore_state"] == "failed":
                        failures.append({"transfer_id": t["id"], "restore_seq": t["restore_seq"],
                                         "from_name": entry["from_name"], "reason": t["restore_error"]})
                order["accounts"] = accounts
                order["restore_queue"] = queue
                order["failures"] = failures
                reaches.setdefault(order["reach"], []).append(order)
        return {"reaches": [{"reach": reach, "orders": ord} for reach, ord in sorted(reaches.items())]}

    # ---------------------------------------------------------------- usage
    def record_usage(self, actor: str, payload: dict[str, Any], role: str = "meter") -> dict[str, Any]:
        if role not in {"meter", "editor"}:
            raise DomainError("只有计量员可以登记取水", 403)
        try:
            account_id = int(payload.get("account_id"))
            amount = float(payload.get("amount"))
        except (TypeError, ValueError) as exc:
            raise DomainError("账户和取水量必须是数值") from exc
        meter_event_id = str(payload.get("meter_event_id", "")).strip()
        occurred = parse_date(str(payload.get("occurred_at", "")), "计量日期")
        if amount <= 0 or not meter_event_id:
            raise DomainError("取水量必须大于 0，计量事件编号不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            account = self._account(conn, account_id)
            reason = usage_blocked(self.store.get_active_order(conn, account["reach"]), account)
            if reason:
                raise DomainError(reason, 409)
            if not valid_on(account, occurred.isoformat()):
                raise DomainError("取水日期不在许可有效期内", 409)
            reserved = self._reserved_outgoing(conn, account_id)
            avail = float(account["quota"]) - float(account["used"]) - reserved
            if not enough(amount, avail):
                raise DomainError("取水超过可用额度", 409)
            season = self.store.get_season_rule(conn, account["region"], occurred.month)
            month_total = self.store.month_usage(conn, account_id, occurred.strftime("%Y-%m"))
            if season and not monthly_cap_ok(month_total, amount, float(account["quota"]),
                                             float(season["max_fraction"])):
                raise DomainError("本次取水超过该月份的季节配额", 409)
            try:
                cur = self.store.insert_usage(conn, {
                    "account_id": account_id, "meter_event_id": meter_event_id,
                    "amount": amount, "occurred_at": occurred.isoformat(), "actor": actor,
                })
            except sqlite3.IntegrityError as exc:
                raise DomainError("计量事件已登记，不能重复计水", 409) from exc
            self.store.add_used(conn, account_id, amount)
            self._audit_row(conn, actor, "usage.recorded", "account", account_id,
                            {"amount": amount, "occurred_at": occurred.isoformat(),
                             "meter_event_id": meter_event_id})
            row = conn.execute("SELECT * FROM usage_records WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)

    # ---------------------------------------------------------------- drought
    def simulate_drought(self, total_supply: float, reduction: float = 0.0,
                         role: str = "viewer") -> dict[str, Any]:
        try:
            total_supply, reduction = float(total_supply), float(reduction)
        except (TypeError, ValueError) as exc:
            raise DomainError("供水量和削减比例必须是数值") from exc
        if total_supply < 0 or not 0 <= reduction < 1:
            raise DomainError("供水量不能为负，削减比例应在 0 到 1 之间")
        with self.connect() as conn:
            rows = self.store.list_accounts(conn)
        supply = total_supply * (1 - reduction)
        allocation: dict[int, float] = {}
        deficit: dict[int, float] = {}
        remaining = supply
        for priority in range(1, 6):
            group = [r for r in rows if int(r["priority"]) == priority]
            if not group:
                continue
            requested = sum(max(0.0, float(r["quota"]) - float(r["used"])) for r in group)
            take = min(remaining, requested)
            if requested <= 0:
                continue
            for row in group:
                quota_left = max(0.0, float(row["quota"]) - float(row["used"]))
                share = take * quota_left / requested
                allocation[int(row["id"])] = share
                deficit[int(row["id"])] = quota_left - share
            remaining -= take
            if remaining <= 1e-9:
                for lower in rows:
                    if int(lower["priority"]) > priority:
                        allocation[int(lower["id"])] = 0.0
                        deficit[int(lower["id"])] = max(0.0, float(lower["quota"]) - float(lower["used"]))
                break
        return {"total_supply": total_supply, "reduction": reduction, "effective_supply": supply,
                "unallocated": remaining, "allocations": [
                    {"account_id": int(r["id"]), "name": r["name"], "priority": r["priority"],
                     "allocation": allocation.get(int(r["id"]), 0.0),
                     "deficit": deficit.get(int(r["id"]), 0.0)}
                    for r in rows
                ]}

    # ---------------------------------------------------------------- lists
    def list_accounts(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            result = []
            for row in self.store.list_accounts(conn):
                item = dict(row)
                item["available"] = self._available(conn, row)
                order = self.store.get_active_order(conn, row["reach"])
                item["frozen"] = bool(order)
                item["freeze_order_id"] = int(order["id"]) if order else None
                result.append(item)
        return result

    def list_transfers(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in self.store.list_transfers(conn)]

    def audit(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.store.list_audit()]


class _Conflict(DomainError):
    """409 carrying the current server state for the losing writer."""

    def __init__(self, order: sqlite3.Row, message: str):
        super().__init__(message, 409)
        self.current = dict(order)
