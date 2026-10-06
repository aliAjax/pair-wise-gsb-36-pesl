import tempfile
import threading
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo


class FreezeOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.accounts = seed_demo(self.db)
        self.frozen = self.accounts["北区水库"]      # reach 干流北段
        self.frozen_to = self.accounts["河口灌区"]   # reach 干流北段
        self.free = self.accounts["西区水厂"]        # reach 支流西段

    def tearDown(self):
        self.tmp.cleanup()

    def _approve(self, transfer_id):
        return self.db.approve_transfer(transfer_id, "bob", "reviewer")

    def _transfer(self, source, target, amount, day="2026-10-08", actor="alice"):
        return self.db.create_transfer(actor, {
            "from_account_id": source, "to_account_id": target,
            "amount": amount, "effective_date": day,
        }, "editor")

    def test_freeze_blocks_transfer_and_usage_but_spares_other_reaches(self):
        order = self.db.create_freeze_order("dispatcher-1", {
            "reach": "干流北段", "reason": "氨氮超标", "started_on": "2026-10-06",
            "planned_end_on": "2026-10-10", "condition_note": "连续48小时达标",
        })
        self.assertEqual(order["status"], "active")
        # No outgoing transfer from a frozen account, no new usage confirmation.
        with self.assertRaisesRegex(DomainError, "禁调期内不能发起"):
            self._transfer(self.frozen, self.free, 10)
        with self.assertRaisesRegex(DomainError, "禁调期内不接受新的取水确认"):
            self.db.record_usage("m1", {"account_id": self.frozen_to, "amount": 5,
                                       "meter_event_id": "X-1", "occurred_at": "2026-10-07"}, "meter")
        # Accounts without a freeze record keep running normally.
        ok = self.db.record_usage("m1", {"account_id": self.free, "amount": 5,
                                         "meter_event_id": "W-1", "occurred_at": "2026-10-07"}, "meter")
        self.assertEqual(ok["amount"], 5)
        # A transfer out of the unaffected reach into the frozen reach is allowed.
        incoming = self._transfer(self.free, self.frozen_to, 10)
        self.assertEqual(incoming["status"], "pending")

    def test_approved_transfers_suspend_and_restore_in_original_order(self):
        # Source quota 1000, used 100, min retention 0.4 -> floor 400.
        t1 = self._transfer(self.frozen, self.frozen_to, 100, "2026-10-08")
        t2 = self._transfer(self.frozen, self.frozen_to, 200, "2026-10-09")
        self._approve(t1["id"])
        self._approve(t2["id"])
        order = self.db.create_freeze_order("d1", {
            "reach": "干流北段", "started_on": "2026-10-06",
            "planned_end_on": "2026-10-10", "condition_note": "达标",
        })
        self.assertEqual([s["restore_seq"] for s in order["suspended"]], [1, 2])
        details = self.db.get_freeze_order(order["id"])
        self.assertEqual([t["status"] for t in details["queue"]], ["suspended", "suspended"])
        # Both sides see frozen amounts and queue positions.
        src = self.db.available(self.frozen)
        self.assertEqual(src["frozen_outgoing"], 300)
        self.assertEqual(len(src["restore_queue"]), 2)
        dst = self.db.available(self.frozen_to)
        self.assertEqual(dst["frozen_incoming"], 300)
        # The suspended 300 still lives in quota but is committed to the queue,
        # so it must not appear as spendable available balance.
        self.assertAlmostEqual(src["available"], 1000 - 100 - 300)
        self.assertEqual(src["reserved_outgoing"], 0)  # approved moved to suspended
        # Inbound transfers from unaffected reaches are not blocked.
        incoming = self._transfer(self.free, self.frozen_to, 10, "2026-10-08")
        self.assertEqual(self._approve(incoming["id"])["status"], "approved")
        # Lift and restore: quota moves exactly once, in original order.
        quota_before = self.db.available(self.frozen)["quota"]
        report = self.db.lift_freeze(order["id"], "d1")["restoration"]
        self.assertEqual(report["executed"], [t1["id"], t2["id"]])
        self.assertEqual(report["failed"], [])
        self.assertAlmostEqual(self.db.available(self.frozen)["quota"], quota_before - 300)
        after = self.db.get_freeze_order(order["id"])
        self.assertTrue(all(t["status"] == "executed" for t in after["queue"]))
        self.assertEqual({a["account_id"] for a in after["accounts"]}, {self.frozen, self.frozen_to})
        self.assertTrue(all(a["status"] == "restored" for a in after["accounts"] if a["account_id"] == self.frozen))

    def test_pending_approval_blocked_during_freeze(self):
        waiting = self._transfer(self.frozen, self.free, 10, "2026-10-08")
        self.db.create_freeze_order("d1", {"reach": "干流北段",
                                           "started_on": "2026-10-06", "planned_end_on": "2026-10-10"})
        with self.assertRaisesRegex(DomainError, "暂缓批准"):
            self._approve(waiting["id"])

    def test_recovery_retries_only_unfinished_and_never_double_processes(self):
        t1 = self._transfer(self.frozen, self.frozen_to, 350, "2026-10-08")
        t2 = self._transfer(self.frozen, self.frozen_to, 50, "2026-10-09")
        self._approve(t1["id"])
        self._approve(t2["id"])
        order = self.db.create_freeze_order("d1", {
            "reach": "干流北段", "started_on": "2026-10-06",
            "planned_end_on": "2026-10-10",
        })
        quota_start = float(self.db.available(self.frozen)["quota"])
        # First restore fails: quota 900, used 100, t1=350 leaves 450 < floor 400?
        # 900-100-350 = 450 >= 400, so force failure by registering extra usage
        # after lift on the unrestricted account... instead simulate via a
        # stricter rule: raise retention to 0.95 before recovery.
        self.db.set_impact_rule("alice", "upstream", "downstream", 0.95, "应急", "editor")
        self.db.lift_freeze(order["id"], "d1")
        order_view = self.db.get_freeze_order(order["id"])
        failed = [t for t in order_view["queue"] if t["restore_state"] == "failed"]
        self.assertEqual([t["id"] for t in failed], [t1["id"]])
        # t2 must keep its place and not have jumped past t1.
        self.assertIsNone(order_view["queue"][1]["restore_state"])
        self.assertEqual(self.db.available(self.frozen)["quota"], quota_start)
        # Relax the rule; rerun processes only the unfinished account/items.
        self.db.set_impact_rule("alice", "upstream", "downstream", 0.4, "", "editor")
        rerun = self.db.restore_freeze(order["id"], "d1")
        self.assertEqual(sorted(rerun["executed"]), [t1["id"], t2["id"]])
        self.assertEqual(rerun["failed"], [])
        # A third run finds nothing unfinished and must not move quota again.
        again = self.db.restore_freeze(order["id"], "d1")
        self.assertEqual(again["executed"], [])
        self.assertAlmostEqual(self.db.available(self.frozen)["quota"], quota_start - 400)

    def test_settlement_executes_unfrozen_and_suspends_frozen(self):
        early = self._transfer(self.frozen, self.frozen_to, 60, "2026-10-08")
        self._approve(early["id"])
        # Settlement before the freeze executes normally.
        report = self.db.execute_transfers("bob", "2026-10-08", "reviewer")
        self.assertEqual(report["executed"], [early["id"]])
        # Another transfer is approved but not due until after the warning.
        held = self._transfer(self.frozen, self.frozen_to, 40, "2026-10-20")
        self._approve(held["id"])
        free_out = self._transfer(self.free, self.frozen_to, 20, "2026-10-18")
        self._approve(free_out["id"])
        order = self.db.create_freeze_order("d1", {
            "reach": "干流北段", "started_on": "2026-10-15", "planned_end_on": "2026-10-25",
        })
        # The already-approved future-dated transfer is captured into the queue.
        self.assertEqual([s["transfer_id"] for s in order["suspended"]], [held["id"]])
        # Settlement during the ban executes the unaffected-reach transfer only.
        report = self.db.execute_transfers("bob", "2026-10-21", "reviewer")
        self.assertEqual(report["executed"], [free_out["id"]])
        self.assertEqual(report["suspended"], [])
        self.assertEqual(self.db.get_freeze_order(order["id"])["queue"][0]["id"], held["id"])

    def test_conditions_use_optimistic_version_and_duplicate_order_rejected(self):
        order = self.db.create_freeze_order("d1", {
            "reach": "干流北段", "started_on": "2026-10-06", "planned_end_on": "2026-10-10",
        })
        self.assertEqual(order["version"], 1)
        updated = self.db.update_freeze_conditions(order["id"], "d2", {
            "planned_end_on": "2026-10-12", "condition_note": "72小时", "version": 1,
        })
        self.assertEqual(updated["version"], 2)
        # Stale version loses: its input is not written, current state returned.
        try:
            self.db.update_freeze_conditions(order["id"], "d3", {
                "planned_end_on": "2026-10-20", "condition_note": "过期写入", "version": 1,
            })
            self.fail("expected conflict")
        except DomainError as exc:
            self.assertEqual(exc.status, 409)
            self.assertEqual(exc.current["version"], 2)
            self.assertEqual(exc.current["condition_note"], "72小时")
        # Only one active freeze order per reach.
        with self.assertRaisesRegex(DomainError, "已有生效中的禁调令"):
            self.db.create_freeze_order("d4", {
                "reach": "干流北段", "started_on": "2026-10-06", "planned_end_on": "2026-10-11",
            })

    def test_concurrent_freeze_orders_single_writer(self):
        errors: list[Exception] = []

        def create(reason: str) -> None:
            try:
                self.db.create_freeze_order(reason, {
                    "reach": "干流北段", "started_on": "2026-10-06",
                    "planned_end_on": "2026-10-10", "reason": reason,
                })
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=create, args=(f"d{i}",)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].status, 409)
        board = self.db.freeze_board()
        active = [o for r in board["reaches"] for o in r["orders"] if o["status"] == "active" and r["reach"] == "干流北段"]
        self.assertEqual(len(active), 1)

    def test_board_groups_by_reach_with_failures(self):
        # Approve a transfer, then freeze the reach and attempt a failing restore.
        self.db.set_impact_rule("alice", "upstream", "downstream", 0.4, "", "editor")
        t1 = self._transfer(self.frozen, self.frozen_to, 300, "2026-10-08")
        self._approve(t1["id"])
        order = self.db.create_freeze_order("d1", {
            "reach": "干流北段", "reason": "污染", "started_on": "2026-10-06",
            "planned_end_on": "2026-10-10", "condition_note": "达标",
        })
        self.db.set_impact_rule("alice", "upstream", "downstream", 0.95, "应急", "editor")
        self.db.lift_freeze(order["id"], "d1")
        board = self.db.freeze_board()
        reaches = {r["reach"]: r["orders"] for r in board["reaches"]}
        self.assertIn("干流北段", reaches)
        view = next(o for o in reaches["干流北段"] if o["id"] == order["id"])
        self.assertEqual(len(view["accounts"]), 2)  # both accounts on the reach
        self.assertEqual(view["failures"][0]["transfer_id"], t1["id"])
        self.assertIn("最小留存", view["failures"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
