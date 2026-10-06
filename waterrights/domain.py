"""判定层：水权账户、转让审批与河道污染预警禁调令的全部业务规则。

本层不含 HTTP，也不直接拼 SQL；所有读写都通过 :class:`waterrights.storage.Storage`
在调用方给定的事务里完成，保证"判定 + 写入"原子提交。
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from .storage import ACTIVE_ORDER_STATES, Storage, utcnow

RESERVED_STATUSES = ("pending", "approved", "suspended")
EPS = 1e-9


def parse_date(value: str, field: str = "日期") -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DomainError(f"{field}必须是 YYYY-MM-DD") from exc


class DomainError(Exception):
    """业务错误；``status`` 是 HTTP 状态，``current`` 可携带冲突时的当前状态。"""

    def __init__(self, message: str, status: int = 400, current: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.current = current


class WaterRightsService:
    def __init__(self, path: str = ":memory:"):
        self.storage = Storage(path)

    @property
    def db(self) -> Storage:
        # 兼容旧代码里的 self.db 叫法
        return self.storage

    # ================================================================ 工具
    def account_row(self, tx, account_id: int):
        row = self.storage.get_account(tx, account_id)
        if not row:
            raise DomainError("水权账户不存在", 404)
        return row

    def _reserved_outgoing(self, tx, account_id: int, exclude_id: int | None = None) -> float:
        sql = ("SELECT COALESCE(SUM(amount),0) total FROM transfers"
               " WHERE from_account_id=? AND status IN ('pending','approved','suspended')")
        params: list[Any] = [account_id]
        if exclude_id is not None:
            sql += " AND id<>?"
            params.append(exclude_id)
        return float(tx.execute(sql, params).fetchone()["total"])

    def _active_freeze(self, tx, account_id: int):
        return self.storage.active_freeze_for_account(tx, account_id)

    def _under_warning(self, tx, account_id: int):
        """仍在污染预警（active）中：硬阻断转出与取水确认。"""
        freeze = self.storage.active_freeze_for_account(tx, account_id)
        return freeze if freeze and freeze["order_status"] == "active" else None

    def _suspended_touching(self, tx, account_ids: set[int]) -> list:
        if not account_ids:
            return []
        marks = ",".join("?" for _ in account_ids)
        return tx.execute(
            f"SELECT * FROM transfers WHERE status='suspended' AND"
            f" (from_account_id IN ({marks}) OR to_account_id IN ({marks}))"
            " ORDER BY COALESCE(approved_at,created_at), id",
            [*account_ids, *account_ids],
        ).fetchall()

    def _freeze_view(self, tx, account_id: int) -> dict[str, Any]:
        """某账户在所有生效中禁调令下的冻结视图。"""
        freeze = self._active_freeze(tx, account_id)
        rows = tx.execute(
            "SELECT t.*, o.status AS order_status FROM transfers t"
            " JOIN freeze_orders o ON o.id=t.suspended_order_id"
            " WHERE t.status='suspended' AND o.status IN ('active','recovering')"
            " AND (t.from_account_id=? OR t.to_account_id=?)",
            (account_id, account_id),
        ).fetchall()
        frozen_out = sum(float(r["amount"]) for r in rows if r["from_account_id"] == account_id)
        frozen_in = sum(float(r["amount"]) for r in rows if r["to_account_id"] == account_id)
        first = rows[0] if rows else None
        return {
            "blocked": bool(freeze) and freeze["order_status"] == "active",
            "in_recovery": bool(freeze) and freeze["order_status"] == "recovering",
            "reach": freeze["reach"] if freeze else None,
            "order_id": freeze["order_id"] if freeze else None,
            "frozen_outgoing": frozen_out,
            "frozen_incoming": frozen_in,
            "restore_position": int(freeze["queue_position"]) if freeze and freeze["queue_position"] is not None else None,
            "pending_restores": len(rows),
            "next_transfer_id": int(first["id"]) if first else None,
        }

    # ================================================================ 账户
    def create_account(self, actor: str, payload: dict[str, Any], role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有配额管理员可以创建账户", 403)
        name = str(payload.get("name", "")).strip()
        region = str(payload.get("region", "")).strip()
        holder = str(payload.get("holder", "")).strip()
        reach = str(payload.get("reach", "")).strip() or region
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
        with self.storage.transaction() as tx:
            try:
                row = self.storage.insert_account(tx, {
                    "name": name, "region": region, "reach": reach, "holder": holder,
                    "priority": priority, "valid_from": valid_from.isoformat(),
                    "valid_to": valid_to.isoformat(), "quota": quota,
                })
            except Exception as exc:  # sqlite3.IntegrityError
                raise DomainError("账户名称已存在", 409) from exc
            self.storage.audit(tx, actor, "account.created", "account", row["id"],
                               {"name": name, "quota": quota, "reach": reach})
        return row

    def set_season_rule(self, actor: str, region: str, month: int, max_fraction: float,
                        note: str = "", role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有配额管理员可以设置季节规则", 403)
        if not 1 <= int(month) <= 12 or not 0 < float(max_fraction) <= 1:
            raise DomainError("月份或季节比例不合法")
        with self.storage.transaction() as tx:
            self.storage.set_season_rule(tx, region.strip(), int(month), float(max_fraction), note)
            self.storage.audit(tx, actor, "season_rule.saved", "region", None,
                               {"region": region, "month": month, "max_fraction": max_fraction})
        return {"region": region, "month": month, "max_fraction": float(max_fraction), "note": note}

    def set_impact_rule(self, actor: str, source_region: str, target_region: str, min_source_fraction: float,
                        note: str = "", role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有配额管理员可以设置第三方影响规则", 403)
        if not 0 <= float(min_source_fraction) <= 1:
            raise DomainError("最小留存比例必须在 0 到 1 之间")
        with self.storage.transaction() as tx:
            self.storage.set_impact_rule(tx, source_region, target_region, float(min_source_fraction), note)
            self.storage.audit(tx, actor, "impact_rule.saved", "region", None,
                               {"source": source_region, "target": target_region, "min_fraction": min_source_fraction})
        return {"source_region": source_region, "target_region": target_region,
                "min_source_fraction": float(min_source_fraction), "note": note}

    def available(self, account_id: int, as_of: str | None = None) -> dict[str, Any]:
        if as_of:
            parse_date(as_of, "查询日期")
        with self.storage.transaction() as tx:
            account = self.account_row(tx, account_id)
            reserved = self._reserved_outgoing(tx, account_id)
            value = max(0.0, float(account["quota"]) - float(account["used"]) - reserved)
            freeze = self._freeze_view(tx, account_id)
            result = {"account_id": account_id, "available": value,
                      "reserved_outgoing": reserved, "quota": account["quota"],
                      "used": account["used"], "freeze": freeze}
        return result

    # ================================================================ 转让
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
            raise DomainError("转让账户不能相同，转让量必须大于 0")
        effective = parse_date(str(payload.get("effective_date", "")), "生效日期")
        with self.storage.transaction() as tx:
            source = self.account_row(tx, source_id)
            target = self.account_row(tx, target_id)
            # 禁调期内水权账户不能转出（进入恢复流程后允许补办，不允许新转出）。
            if self._under_warning(tx, source_id):
                view = self._freeze_view(tx, source_id)
                raise DomainError("该河段处于污染预警禁调期，不能发起转出", 409, current=view)
            if not (source["valid_from"] <= effective.isoformat() <= source["valid_to"]):
                raise DomainError("转出账户在生效日无效", 409)
            if not (target["valid_from"] <= effective.isoformat() <= target["valid_to"]):
                raise DomainError("转入账户在生效日无效", 409)
            reserved = self._reserved_outgoing(tx, source_id)
            avail = float(source["quota"]) - float(source["used"]) - reserved
            if amount > avail + EPS:
                raise DomainError("可用额度不足，待审批/已批准转让会预占额度", 409)
            if int(source["priority"]) > int(target["priority"]):
                raise DomainError("不能把较低优先级水量转给更高优先级账户", 409)
            impact = self.storage.get_impact_rule(tx, source["region"], target["region"])
            if impact and avail - amount + EPS < float(source["quota"]) * float(impact["min_source_fraction"]):
                raise DomainError("转让会违反下游第三方最小留存约束", 409)
            row = self.storage.insert_transfer(tx, {
                "from_account_id": source_id, "to_account_id": target_id, "amount": amount,
                "effective_date": effective.isoformat(), "created_by": actor,
            })
            self.storage.audit(tx, actor, "transfer.created", "transfer", row["id"],
                               {"source": source_id, "target": target_id, "amount": amount,
                                "effective_date": effective.isoformat()})
        return row

    def _check_transfer_funds(self, tx, transfer) -> None:
        """执行前复核：额度、有效期、优先级、第三方留存（恢复时条件可能已变化）。"""
        source = self.account_row(tx, transfer["from_account_id"])
        target = self.account_row(tx, transfer["to_account_id"])
        amount = float(transfer["amount"])
        other = self._reserved_outgoing(tx, source["id"], exclude_id=transfer["id"])
        avail = float(source["quota"]) - float(source["used"]) - other
        if amount > avail + EPS:
            raise DomainError("补办时额度已被其他记录占用，不能执行")
        impact = self.storage.get_impact_rule(tx, source["region"], target["region"])
        if impact and avail - amount + EPS < float(source["quota"]) * float(impact["min_source_fraction"]):
            raise DomainError("补办时下游最小留存约束不再满足")

    def _recompute_positions(self, tx, order_id: int) -> None:
        """按批准时间（原顺序）重排该禁调令下待恢复转让的顺位，并回写冻结账户顺位。"""
        suspended = self.storage.suspended_transfers(tx, order_id)
        first_pos: dict[int, int] = {}
        for pos, tr in enumerate(suspended, start=1):
            for acc in (tr["from_account_id"], tr["to_account_id"]):
                row = self.storage.get_freeze(tx, order_id, acc)
                if row and acc not in first_pos:
                    first_pos[acc] = pos
        for acc, pos in first_pos.items():
            self.storage.touch_freeze(tx, order_id, acc, queue_position=pos)
        for row in self.storage.list_freezes(tx, order_id):
            if row["account_id"] not in first_pos and row["status"] == "frozen":
                # 无待恢复转让的账户不占用顺位。
                self.storage.touch_freeze(tx, order_id, row["account_id"], queue_position=None)

    def approve_transfer(self, transfer_id: int, actor: str, role: str = "reviewer") -> dict[str, Any]:
        if role != "reviewer":
            raise DomainError("只有审核人可以批准转让", 403)
        with self.storage.transaction() as tx:
            transfer = self.storage.get_transfer(tx, transfer_id)
            if not transfer:
                raise DomainError("转让记录不存在", 404)
            if transfer["status"] != "pending":
                raise DomainError("该转让已处理，不能重复批准", 409)
            if actor == transfer["created_by"]:
                raise DomainError("发起人不能批准自己的转让", 403)
            self._check_transfer_funds(tx, transfer)
            now = utcnow()
            source_freeze = self._under_warning(tx, transfer["from_account_id"])
            target_freeze = self._under_warning(tx, transfer["to_account_id"])
            freeze = source_freeze or target_freeze
            if freeze:
                # 禁调期内批准的转让立即暂停执行，恢复后按原顺序补办。
                self.storage.mark_transfer(tx, transfer_id, "suspended",
                                           approved_by=actor, approved_at=now,
                                           order_id=freeze["order_id"])
                self._recompute_positions(tx, freeze["order_id"])
                self.storage.audit(tx, actor, "transfer.suspended", "transfer", transfer_id,
                                   {"order_id": freeze["order_id"]})
            else:
                self.storage.mark_transfer(tx, transfer_id, "approved",
                                           approved_by=actor, approved_at=now)
                self.storage.audit(tx, actor, "transfer.approved", "transfer", transfer_id,
                                   {"amount": transfer["amount"]})
            row = dict(self.storage.get_transfer(tx, transfer_id))
        return row

    def reject_transfer(self, transfer_id: int, actor: str, role: str = "reviewer") -> dict[str, Any]:
        if role != "reviewer":
            raise DomainError("只有审核人可以退回转让", 403)
        with self.storage.transaction() as tx:
            row = self.storage.get_transfer(tx, transfer_id)
            if not row or row["status"] != "pending":
                raise DomainError("转让不存在或已经处理", 409)
            if actor == row["created_by"]:
                raise DomainError("发起人不能自行退回", 403)
            self.storage.mark_transfer(tx, transfer_id, "rejected", approved_by=actor, approved_at=utcnow())
            self.storage.audit(tx, actor, "transfer.rejected", "transfer", transfer_id, {})
        return {"id": transfer_id, "status": "rejected"}

    def execute_transfer(self, transfer_id: int, actor: str, role: str = "reviewer") -> dict[str, Any]:
        """批准后的转让正式划转额度；暂停转让在双方均解冻后也可由此补办。"""
        if role != "reviewer":
            raise DomainError("只有审核人可以执行转让", 403)
        with self.storage.transaction() as tx:
            transfer = self.storage.get_transfer(tx, transfer_id)
            if not transfer:
                raise DomainError("转让记录不存在", 404)
            if transfer["status"] == "executed":
                raise DomainError("该转让已经划转，不能重复执行", 409)
            if transfer["status"] not in ("approved", "suspended"):
                raise DomainError("只有已批准/暂停的转让可以执行", 409)
            for acc in (transfer["from_account_id"], transfer["to_account_id"]):
                if self._under_warning(tx, acc):
                    view = self._freeze_view(tx, acc)
                    raise DomainError("相关账户仍在污染预警禁调期，转让暂缓执行", 409, current=view)
            self._check_transfer_funds(tx, transfer)
            self.storage.move_quota(tx, transfer["from_account_id"],
                                    transfer["to_account_id"], float(transfer["amount"]))
            self.storage.mark_transfer(tx, transfer_id, "executed", executed_at=utcnow())
            self.storage.audit(tx, actor, "transfer.executed", "transfer", transfer_id,
                               {"amount": transfer["amount"]})
            row = dict(self.storage.get_transfer(tx, transfer_id))
        return row

    # ================================================================ 取水
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
        with self.storage.transaction() as tx:
            account = self.account_row(tx, account_id)
            # 禁调期内不接受新的取水确认。
            if self._under_warning(tx, account_id):
                view = self._freeze_view(tx, account_id)
                raise DomainError("该河段处于污染预警禁调期，不接受新的取水确认", 409, current=view)
            if not (account["valid_from"] <= occurred.isoformat() <= account["valid_to"]):
                raise DomainError("取水日期不在许可有效期内", 409)
            reserved = self._reserved_outgoing(tx, account_id)
            avail = float(account["quota"]) - float(account["used"]) - reserved
            if amount > avail + EPS:
                raise DomainError("取水超过可用额度", 409)
            season = self.storage.get_season_rule(tx, account["region"], occurred.month)
            month_total = self.storage.month_usage(tx, account_id, occurred.strftime("%Y-%m"))
            if season:
                cap = float(account["quota"]) * float(season["max_fraction"])
                if month_total + amount > cap + EPS:
                    raise DomainError("本次取水超过该月份的季节配额", 409)
            try:
                row = self.storage.insert_usage(tx, {
                    "account_id": account_id, "meter_event_id": meter_event_id,
                    "amount": amount, "occurred_at": occurred.isoformat(), "actor": actor,
                })
            except Exception as exc:  # sqlite3.IntegrityError
                raise DomainError("计量事件已登记，不能重复计水", 409) from exc
            self.storage.add_used(tx, account_id, amount)
            self.storage.audit(tx, actor, "usage.recorded", "account", account_id,
                               {"amount": amount, "occurred_at": occurred.isoformat(),
                                "meter_event_id": meter_event_id})
        return row

    # ================================================================ 干旱
    def simulate_drought(self, total_supply: float, reduction: float = 0.0, role: str = "viewer") -> dict[str, Any]:
        try:
            total_supply, reduction = float(total_supply), float(reduction)
        except (TypeError, ValueError) as exc:
            raise DomainError("供水量和削减比例必须是数值") from exc
        if total_supply < 0 or not 0 <= reduction < 1:
            raise DomainError("供水量不能为负，削减比例应在 0 到 1 之间")
        with self.storage.transaction() as tx:
            rows = self.storage.list_accounts(tx)
        supply = total_supply * (1 - reduction)
        allocation: dict[int, float] = {}
        deficit: dict[int, float] = {}
        remaining = supply
        for priority in range(1, 6):
            group = [r for r in rows if int(r["priority"]) == priority]
            if not group:
                continue
            requested = sum(max(0.0, float(r["quota"]) - float(r["used"])) for r in group)
            if requested <= 0:
                continue
            take = min(remaining, requested)
            for row in group:
                quota_left = max(0.0, float(row["quota"]) - float(row["used"]))
                share = take * quota_left / requested
                allocation[int(row["id"])] = share
                deficit[int(row["id"])] = quota_left - share
            remaining -= take
            if remaining <= EPS:
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

    # ================================================================ 禁调令
    def issue_freeze_order(self, actor: str, payload: dict[str, Any], role: str = "editor") -> dict[str, Any]:
        """发布河道污染预警禁调令：冻结河段账户、暂停已批准转让。"""
        if role != "editor":
            raise DomainError("只有调度员可以发布禁调令", 403)
        reach = str(payload.get("reach", "")).strip()
        reason = str(payload.get("reason", "")).strip()
        if not reach:
            raise DomainError("河段不能为空")
        started = parse_date(str(payload.get("started_on", "")), "预警开始日期")
        planned = parse_date(str(payload.get("planned_recovery_on", "")), "计划恢复日期")
        if started > planned:
            raise DomainError("计划恢复日期不能早于预警开始日期")
        with self.storage.transaction() as tx:
            existing = self.storage.get_order_by_reach(tx, reach)
            if existing:
                # 两人同时提交只有一方写入：唯一索引兜底，另一方拿到当前状态。
                raise DomainError("该河段已有生效中的禁调令", 409,
                                  current=self._order_summary(tx, existing))
            try:
                order = self.storage.insert_order(tx, {
                    "reach": reach, "reason": reason, "started_on": started.isoformat(),
                    "planned_recovery_on": planned.isoformat(), "created_by": actor,
                })
            except Exception as exc:  # 并发唯一索引冲突
                current = self.storage.get_order_by_reach(tx, reach)
                raise DomainError("该河段已有生效中的禁调令", 409,
                                  current=self._order_summary(tx, current) if current else None) from exc
            # 冻结该河段全部账户（无论是否有在途转让）。
            for acc in self.storage.list_accounts_by_reach(tx, reach):
                self.storage.upsert_freeze(tx, order["id"], int(acc["id"]), None)
            # 已经批准（尚未划转）且任一方在冻结河段的转让，全部暂停执行。
            freeze_ids = {int(r["account_id"]) for r in self.storage.list_freezes(tx, order["id"])}
            approved = tx.execute(
                "SELECT * FROM transfers WHERE status='approved' AND"
                " (from_account_id IN (%s) OR to_account_id IN (%s))"
                " ORDER BY COALESCE(approved_at,created_at), id"
                % (",".join("?" * len(freeze_ids)) or "NULL", ",".join("?" * len(freeze_ids)) or "NULL"),
                [*freeze_ids, *freeze_ids],
            ).fetchall() if freeze_ids else []
            for tr in approved:
                self.storage.mark_transfer(tx, int(tr["id"]), "suspended", order_id=order["id"])
            self._recompute_positions(tx, order["id"])
            self.storage.audit(tx, actor, "freeze.issued", "freeze_order", order["id"],
                               {"reach": reach, "frozen_accounts": sorted(freeze_ids),
                                "suspended_transfers": [int(t["id"]) for t in approved]})
            summary = self._order_summary(tx, self.storage.get_order(tx, order["id"]))
        return summary

    def update_freeze_order(self, order_id: int, actor: str, payload: dict[str, Any],
                            role: str = "editor") -> dict[str, Any]:
        """调度员修改恢复条件；必须携带 version，旧版本写入失败并返回当前状态。"""
        if role != "editor":
            raise DomainError("只有调度员可以修改禁调令恢复条件", 403)
        try:
            version = int(payload.get("version"))
        except (TypeError, ValueError) as exc:
            raise DomainError("必须携带禁调令版本号 version") from exc
        fields: dict[str, Any] = {}
        if "planned_recovery_on" in payload:
            planned = parse_date(str(payload.get("planned_recovery_on", "")), "计划恢复日期")
            fields["planned_recovery_on"] = planned.isoformat()
        if "reason" in payload:
            fields["reason"] = str(payload.get("reason", "")).strip()
        if not fields:
            raise DomainError("没有可修改的恢复条件")
        with self.storage.transaction() as tx:
            order = self.storage.get_order(tx, order_id)
            if not order:
                raise DomainError("禁调令不存在", 404)
            if order["status"] not in ACTIVE_ORDER_STATES:
                raise DomainError("禁调令已恢复，不能再修改", 409,
                                  current=self._order_summary(tx, order))
            if started := order["started_on"]:
                if fields.get("planned_recovery_on", started) < started:
                    raise DomainError("计划恢复日期不能早于预警开始日期")
            if int(order["version"]) != version:
                # 两个人同时改：后写一方保留原记录，只能查看当前状态。
                raise DomainError("禁调令已被他人更新，请刷新后重试", 409,
                                  current=self._order_summary(tx, order))
            fields["version"] = version + 1
            self.storage.touch_order(tx, order_id, **fields)
            self.storage.audit(tx, actor, "freeze.updated", "freeze_order", order_id,
                               {"fields": fields, "old_version": version})
            summary = self._order_summary(tx, self.storage.get_order(tx, order_id))
        return summary

    def _execute_suspended_in_tx(self, tx, transfer, order_id: int,
                                 unfinished: set[int]) -> dict[str, Any] | None:
        """在给定事务内尝试补办一条暂停转让；失败返回原因，成功返回 None。"""
        if transfer["status"] != "suspended":
            return None  # 已经处理（解冻）过，不能重复处理
        involved = {int(transfer["from_account_id"]), int(transfer["to_account_id"])}
        mine = involved & unfinished
        if not mine:
            return None
        # 任一方仍处于污染预警（active）的另一禁调令时继续等待；
        # 同样处于 recovering（恢复批处理中）则不互相死锁。
        for acc in involved:
            blocker = self._under_warning(tx, acc)
            if blocker and int(blocker["order_id"]) != order_id:
                return f"账户 {acc} 所在河段仍在污染预警，等待其恢复"
            freeze = self._active_freeze(tx, acc)
            if freeze and int(freeze["order_id"]) == order_id and acc not in unfinished:
                return None
        try:
            self._check_transfer_funds(tx, transfer)
        except DomainError as exc:
            return str(exc)
        self.storage.move_quota(tx, int(transfer["from_account_id"]),
                                int(transfer["to_account_id"]), float(transfer["amount"]))
        self.storage.mark_transfer(tx, int(transfer["id"]), "executed", executed_at=utcnow())
        return None

    def recover_freeze_order(self, order_id: int, actor: str, role: str = "editor") -> dict[str, Any]:
        """恢复批处理：逐账户补办，失败只重试未完成账户；已解冻结果不重复处理。"""
        if role != "editor":
            raise DomainError("只有调度员可以执行恢复批处理", 403)
        with self.storage.transaction() as tx:
            order = self.storage.get_order(tx, order_id)
            if not order:
                raise DomainError("禁调令不存在", 404)
            if order["status"] == "recovered":
                return {"order_id": order_id, "status": "recovered", "idempotent": True,
                        "accounts": [self._freeze_account_item(tx, r) for r in self.storage.list_freezes(tx, order_id)]}
            self.storage.touch_order(tx, order_id, status="recovering")
            freeze_rows = self.storage.list_freezes(tx, order_id)
            already_recovered = {int(r["account_id"]) for r in freeze_rows if r["status"] == "recovered"}

        executed_ids: list[int] = []
        # 只挑未完成账户；已 recovered 的账户整段跳过，转让不再为其重复划转。
        with self.storage.transaction() as tx:
            unfinished = {int(r["account_id"]) for r in freeze_rows if r["status"] != "recovered"}
            queue = self._suspended_touching(tx, unfinished)
        failures: dict[int, str] = {}
        for tr in queue:
            involved = {int(tr["from_account_id"]), int(tr["to_account_id"])}
            targets = involved & unfinished
            if not targets:
                continue
            with self.storage.transaction() as tx:
                fresh = self.storage.get_transfer(tx, int(tr["id"]))
                err = self._execute_suspended_in_tx(tx, fresh, order_id, unfinished) if fresh else "转让不存在"
                if err:
                    for acc in targets:
                        self.storage.touch_freeze(tx, order_id, acc, status="failed",
                                                  failure_reason=err, bump_attempt=True)
                        failures[acc] = err
                    self.storage.touch_order(tx, order_id, last_error=err)
                    self.storage.audit(tx, actor, "freeze.restore_failed", "freeze_order", order_id,
                                       {"transfer_id": int(tr["id"]), "accounts": sorted(targets), "reason": err})
                    # 按原顺序补办：某一顺位失败后本次批处理停止，后续顺位等重试。
                    break
                else:
                    self.storage.audit(tx, actor, "transfer.executed", "transfer", int(tr["id"]),
                                       {"amount": tr["amount"], "order_id": order_id, "batch": True})
                    executed_ids.append(int(tr["id"]))

        # 账户级收尾：没有剩余暂停转让且无本次失败的账户解冻。
        with self.storage.transaction() as tx:
            for row in self.storage.list_freezes(tx, order_id):
                acc = int(row["account_id"])
                if row["status"] == "recovered":
                    continue  # 已解冻结果不重复处理
                remaining = self._suspended_touching(tx, {acc})
                if not remaining and acc not in failures:
                    self.storage.touch_freeze(tx, order_id, acc, status="recovered",
                                              failure_reason="", queue_position=None)
                    self.storage.audit(tx, actor, "freeze.account_recovered", "account", acc,
                                       {"order_id": order_id})
                elif acc in failures:
                    self.storage.touch_freeze(tx, order_id, acc, status="failed",
                                              failure_reason=failures[acc])
            rows = self.storage.list_freezes(tx, order_id)
            unfinished_ids = {int(r["account_id"]) for r in rows if r["status"] != "recovered"}
            all_done = not unfinished_ids and not self._suspended_touching(tx, unfinished_ids)
            if all_done:
                self.storage.touch_order(tx, order_id, status="recovered",
                                         recovered_at=utcnow(), last_error="")
                self.storage.audit(tx, actor, "freeze.recovered", "freeze_order", order_id,
                                   {"executed_transfers": executed_ids})
            else:
                reason = "; ".join(sorted(set(failures.values()))) or "仍有暂停转让未补办"
                self.storage.touch_order(tx, order_id, status="recovering", last_error=reason)
            summary = self._order_summary(tx, self.storage.get_order(tx, order_id))
            account_results = [
                self._freeze_account_item(tx, r, skipped=int(r["account_id"]) in already_recovered)
                for r in self.storage.list_freezes(tx, order_id)
            ]
        summary["executed_transfers"] = executed_ids
        summary["accounts"] = account_results
        return summary

    # ================================================================ 查询
    def _freeze_account_item(self, tx, row, skipped: bool = False) -> dict[str, Any]:
        acc = self.storage.get_account(tx, int(row["account_id"]))
        suspended = tx.execute(
            "SELECT * FROM transfers WHERE status='suspended' AND suspended_order_id=? AND"
            " (from_account_id=? OR to_account_id=?) ORDER BY COALESCE(approved_at,created_at),id",
            (int(row["order_id"]), row["account_id"], row["account_id"]),
        ).fetchall()
        frozen_out = sum(float(t["amount"]) for t in suspended if t["from_account_id"] == row["account_id"])
        frozen_in = sum(float(t["amount"]) for t in suspended if t["to_account_id"] == row["account_id"])
        return {
            "account_id": int(row["account_id"]),
            "name": acc["name"] if acc else f"#{row['account_id']}",
            "reach": acc["reach"] if acc else None,
            "status": row["status"],
            "queue_position": int(row["queue_position"]) if row["queue_position"] is not None else None,
            "frozen_outgoing": frozen_out,
            "frozen_incoming": frozen_in,
            "frozen_total": frozen_out + frozen_in,
            "failure_reason": row["failure_reason"],
            "attempts": int(row["attempts"]),
            "skipped": skipped,
        }

    def _order_summary(self, tx, order) -> dict[str, Any]:
        queue = []
        for pos, tr in enumerate(self.storage.suspended_transfers(tx, int(order["id"])), start=1):
            src = self.storage.get_account(tx, int(tr["from_account_id"]))
            dst = self.storage.get_account(tx, int(tr["to_account_id"]))
            queue.append({
                "position": pos,
                "transfer_id": int(tr["id"]),
                "from_account_id": int(tr["from_account_id"]),
                "to_account_id": int(tr["to_account_id"]),
                "from_name": src["name"] if src else None,
                "to_name": dst["name"] if dst else None,
                "amount": float(tr["amount"]),
                "status": tr["status"],
            })
        accounts = [self._freeze_account_item(tx, r) for r in self.storage.list_freezes(tx, int(order["id"]))]
        failed = [a for a in accounts if a["status"] == "failed"]
        return {
            "id": int(order["id"]),
            "reach": order["reach"],
            "reason": order["reason"],
            "started_on": order["started_on"],
            "planned_recovery_on": order["planned_recovery_on"],
            "status": order["status"],
            "version": int(order["version"]),
            "last_error": order["last_error"],
            "recovered_at": order["recovered_at"],
            "created_by": order["created_by"],
            "accounts": accounts,
            "restore_queue": queue,
            "failed_accounts": failed,
        }

    def get_freeze_order(self, order_id: int) -> dict[str, Any]:
        with self.storage.transaction() as tx:
            order = self.storage.get_order(tx, order_id)
            if not order:
                raise DomainError("禁调令不存在", 404)
            return self._order_summary(tx, order)

    def list_freeze_orders(self) -> dict[str, Any]:
        """页面用：按河段列出禁调令、冻结账户、待恢复顺序和失败原因。"""
        with self.storage.transaction() as tx:
            orders = self.storage.list_orders(tx)
            return {"reaches": [self._order_summary(tx, o) for o in orders]}

    def list_accounts(self) -> list[dict[str, Any]]:
        with self.storage.transaction() as tx:
            result = []
            for row in self.storage.list_accounts(tx):
                item = dict(row)
                reserved = self._reserved_outgoing(tx, int(row["id"]))
                item["available"] = max(0.0, float(row["quota"]) - float(row["used"]) - reserved)
                item["freeze"] = self._freeze_view(tx, int(row["id"]))
                result.append(item)
        return result

    def list_transfers(self) -> list[dict[str, Any]]:
        with self.storage.transaction() as tx:
            return [dict(r) for r in self.storage.list_transfers(tx)]

    def audit(self) -> list[dict[str, Any]]:
        with self.storage.transaction() as tx:
            return [dict(r) for r in self.storage.audit_log(tx)]
