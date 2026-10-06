import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path

import app
from app import Handler, seed_demo


class HttpFreezeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = app.Database(Path(self.tmp.name) / "http.db")
        seed_demo(self.db)
        Handler.db = self.db
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def _request(self, method: str, path: str, body=None, user="u", role="dispatcher", raw=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json", "X-User": user, "X-Role": role})
        try:
            with urllib.request.urlopen(req) as resp:
                payload = resp.read()
                return resp.status, payload if raw else json.loads(payload)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_freeze_api_roles_conflict_and_page(self):
        status, page = self._request("GET", "/", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("污染预警禁调", page.decode())
        # Non-dispatcher cannot issue a freeze order.
        status, body = self._request("POST", "/api/freeze", {
            "reach": "干流北段", "started_on": "2026-10-06", "planned_end_on": "2026-10-10",
        }, role="viewer")
        self.assertEqual(status, 403)
        status, order = self._request("POST", "/api/freeze", {
            "reach": "干流北段", "reason": "污染", "started_on": "2026-10-06",
            "planned_end_on": "2026-10-10", "condition_note": "达标",
        }, user="d1")
        self.assertEqual(status, 201)
        # Duplicate active order is rejected.
        status, body = self._request("POST", "/api/freeze", {
            "reach": "干流北段", "started_on": "2026-10-06", "planned_end_on": "2026-10-11",
        }, user="d2")
        self.assertEqual(status, 409)
        # Withdrawal on the frozen reach is rejected through HTTP.
        status, body = self._request("POST", "/api/usage", {
            "account_id": 2, "amount": 5, "meter_event_id": "H1", "occurred_at": "2026-10-07",
        }, user="m1", role="meter")
        self.assertEqual(status, 409)
        self.assertIn("不接受新的取水确认", body["error"])
        # Optimistic conditions: first writer wins, second sees current state.
        status, updated = self._request("POST", f"/api/freeze/{order['id']}/conditions", {
            "planned_end_on": "2026-10-12", "condition_note": "72小时", "version": 1,
        }, user="d2")
        self.assertEqual(status, 200)
        status, loser = self._request("POST", f"/api/freeze/{order['id']}/conditions", {
            "planned_end_on": "2026-10-30", "condition_note": "旧版本写入", "version": 1,
        }, user="d3")
        self.assertEqual(status, 409)
        self.assertEqual(loser["current"]["version"], 2)
        self.assertEqual(loser["current"]["condition_note"], "72小时")
        # Board groups by reach.
        status, board = self._request("GET", "/api/freeze/board")
        self.assertEqual(status, 200)
        self.assertTrue(any(r["reach"] == "干流北段" for r in board["reaches"]))
        # Lift runs restoration and is idempotent.
        status, lifted = self._request("POST", f"/api/freeze/{order['id']}/lift",
                                       {"version": 2}, user="d1")
        self.assertEqual(status, 200)
        status, again = self._request("POST", f"/api/freeze/{order['id']}/restore", {}, user="d1")
        self.assertEqual(status, 200)
        self.assertEqual(again["executed"], [])


if __name__ == "__main__":
    unittest.main()
