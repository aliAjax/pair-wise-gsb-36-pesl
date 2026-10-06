import tempfile
import threading
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo


class FreezeOrderFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "freeze.db")
        self.ids = seed_demo(self.db)
        self.a = self.ids["北区水库"]   # north-canal, quota 900 after seeded usage 100
        self.b = self.ids["河口灌区"]   # north-canal, quota 500
        self.c = next(x["id"] for x in self.db.list_accounts() if x["name"] == "西岸水厂")  # west-canal

    def tearDown(self):
        self.tmp.cleanup()

    def _transfer(self, src, dst, amount, date_="2026-06-01"):
        return self.db.create_transfer("alice", {
            "from_account_id": src, "to_account_id": dst, "amount": amount,
            "effective_date": date_}, "editor")

    def _approve(self, tid, reviewer="bob"):
        return self.db.approve_transfer(tid, reviewer, "reviewer")

    def _issue(self, reach="north-canal", **over):
        payload = {"reach": reach, "reason": "化工厂泄漏",
                   "started_on": "2026-06-05", "planned_recovery_on": "2026-06-10"}
        payload.update(over)
        return self.db.issue_freeze_order("dispatcher-1", payload, "editor")

    # ---------------------------------------------------------------- 冻结
    def test_order_blocks_transfer_and_usage_and_freezes_approved(self):
        t1 = self._approve(self._transfer(self.a, self.b, 100)["id"])
        order = self._issue()
        # 已批准转让被暂停，且冻结额、待恢复顺位对双方可见。
        self.assertEqual(t1["status"], "approved")
        suspended = [q for q in order["restore_queue"] if q["transfer_id"] == t1["id"]]
        self.assertEqual(len(suspended), 1)
        self.assertEqual(suspended[0]["position"], 1)
        av_a = self.db.available(self.a)["freeze"]
        av_b = self.db.available(self.b)["freeze"]
        self.assertTrue(av_a["blocked"])
        self.assertEqual(av_a["frozen_outgoing"], 100)
        self.assertEqual(av_b["frozen_incoming"], 100)
        self.assertEqual(av_a["restore_position"], 1)
        # 禁调期内不能转出，也不接受新的取水确认。
        with self.assertRaisesRegex(DomainError, "不能发起转出"):
            self._transfer(self.a, self.b, 5)
        with self.assertRaisesRegex(DomainError, "取水确认"):
            self.db.record_usage("meter-01", {"account_id": self.a, "amount": 5,
                                "meter_event_id": "X-1", "occurred_at": "2026-06-06"}, "meter")

    def test_account_without_order_runs_normally(self):
        self._issue()
        # west-canal 没有禁调记录：取水、转出、审批全部照常。
        self.db.record_usage("meter-02", {"account_id": self.c, "amount": 30,
                            "meter_event_id": "W-1", "occurred_at": "2026-06-06"}, "meter")
        t = self._transfer(self.c, self.b, 20)  # 对手方冻结不影响发起（仅转出方被管）
        approved = self._approve(t["id"])
        self.assertEqual(approved["status"], "suspended")  # 批准时对手方在禁调，排队等恢复
        view = self.db.available(self.c)["freeze"]
        self.assertFalse(view["blocked"])
        self.assertEqual(view["frozen_outgoing"], 20)
        self.assertIsNone(view["restore_position"])  # 非河段账户不占该令恢复顺位

    def test_approval_during_order_queues_in_original_order(self):
        self._issue()
        t1 = self._transfer(self.c, self.b, 20)   # 非冻结方发往冻结河段
        t2 = self._transfer(self.c, self.b, 30)
        # 冻结账户的新转让直接被拒；只有非冻结方的申请可以排队。
        with self.assertRaisesRegex(DomainError, "不能发起转出"):
            self._transfer(self.a, self.b, 10)
        a1 = self._approve(t1["id"])
        a2 = self._approve(t2["id"])
        self.assertEqual(a1["status"], "suspended")
        self.assertEqual(a2["status"], "suspended")
        board = self.db.get_freeze_order(1)
        positions = {q["transfer_id"]: q["position"] for q in board["restore_queue"]}
        self.assertEqual(positions[t1["id"]], 1)
        self.assertEqual(positions[t2["id"]], 2)

    # ---------------------------------------------------------------- 并发
    def test_concurrent_issue_only_one_writes(self):
        results: list = []
        errors: list = []

        def issue(reach):
            try:
                results.append(self._issue(reach=reach, reason=f"by-{reach}"))
            except DomainError as exc:
                errors.append(exc)

        t1 = threading.Thread(target=issue, args=("north-canal",))
        t2 = threading.Thread(target=issue, args=("north-canal",))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].status, 409)
        # 输的一方保留原记录视角：响应里带当前状态。
        self.assertEqual(errors[0].current["reach"], "north-canal")

    def test_optimistic_version_conflict(self):
        order = self._issue()
        self.db.update_freeze_order(order["id"], "dispatcher-1",
                                    {"version": order["version"], "planned_recovery_on": "2026-06-12"}, "editor")
        # 另一个调度员拿着旧版本当天再改：写入失败并看到当前状态。
        with self.assertRaises(DomainError) as cm:
            self.db.update_freeze_order(order["id"], "dispatcher-2",
                                        {"version": order["version"], "planned_recovery_on": "2026-06-13"}, "editor")
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(cm.exception.current["planned_recovery_on"], "2026-06-12")
        self.assertEqual(cm.exception.current["version"], 2)

    # ---------------------------------------------------------------- 恢复
    def test_recovery_executes_queue_in_order_and_is_idempotent(self):
        t1 = self._approve(self._transfer(self.a, self.b, 100)["id"])
        t2 = self._approve(self._transfer(self.a, self.b, 50)["id"])
        self._issue()
        qa, qb = self.db.available(self.a), self.db.available(self.b)
        quota_a_before, quota_b_before = qa["quota"], qb["quota"]
        result = self.db.recover_freeze_order(1, "dispatcher-1", "editor")
        self.assertEqual(result["status"], "recovered")
        self.assertEqual(result["executed_transfers"], [t1["id"], t2["id"]])
        self.assertEqual(self.db.available(self.a)["quota"], quota_a_before - 150)
        self.assertEqual(self.db.available(self.b)["quota"], quota_b_before + 150)
        # 再跑一次：只看到已恢复结果，不能重复划转。
        again = self.db.recover_freeze_order(1, "dispatcher-1", "editor")
        self.assertTrue(again["idempotent"])
        self.assertEqual(self.db.available(self.a)["quota"], quota_a_before - 150)

    def test_recovery_partial_failure_retries_only_unfinished_account(self):
        t1 = self._approve(self._transfer(self.a, self.b, 300)["id"])  # 顺位 1
        t2 = self._approve(self._transfer(self.a, self.b, 50)["id"])   # 顺位 2
        self._issue()
        # 恢复前调度员收紧了下游最小留存：A 配额 900，300 转出后剩 600 < 900*0.9 → 顺位1失败
        self.db.set_impact_rule("dispatcher-1", "upstream", "downstream", 0.9, "恢复条件收紧", "editor")
        result = self.db.recover_freeze_order(1, "dispatcher-1", "editor")
        self.assertEqual(result["status"], "recovering")
        failed = {a["account_id"]: a for a in result["accounts"] if a["status"] == "failed"}
        self.assertIn(self.a, failed)
        self.assertIn("最小留存", failed[self.a]["failure_reason"])
        # 两条转让都没有被划转。
        statuses = {t["id"]: t["status"] for t in self.db.list_transfers()}
        self.assertEqual(statuses[t1["id"]], "suspended")
        self.assertEqual(statuses[t2["id"]], "suspended")
        # 放宽条件后重试：顺位 1、2 全部补办成功。
        self.db.set_impact_rule("dispatcher-1", "upstream", "downstream", 0.4, "恢复条件解除", "editor")
        again = self.db.recover_freeze_order(1, "dispatcher-1", "editor")
        self.assertEqual(again["status"], "recovered")
        self.assertEqual(sorted(again["executed_transfers"]), sorted([t1["id"], t2["id"]]))
        statuses = {t["id"]: t["status"] for t in self.db.list_transfers()}
        self.assertEqual(statuses[t1["id"]], "executed")
        self.assertEqual(statuses[t2["id"]], "executed")

    def test_board_groups_by_reach_with_failed_reasons(self):
        self._issue()
        board = self.db.list_freeze_orders()
        self.assertEqual(len(board["reaches"]), 1)
        reach = board["reaches"][0]
        self.assertEqual(reach["reach"], "north-canal")
        names = {a["name"] for a in reach["accounts"]}
        self.assertEqual(names, {"北区水库", "河口灌区"})
        self.assertTrue(all(a["failure_reason"] == "" for a in reach["accounts"]))


if __name__ == "__main__":
    unittest.main()
