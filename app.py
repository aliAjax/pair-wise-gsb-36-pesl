"""Interface layer: HTTP server and demo seeding.

Business logic lives in :mod:`service`, decisions in :mod:`freeze_policy`,
persistence in :mod:`storage`. This module only parses requests, dispatches
to the service facade, and serialises responses.
"""
from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# Backward-compatible surface: existing tests and imports use ``app.Database``
# / ``app.DomainError``.
from service import DEFAULT_DB, Database, DomainError  # noqa: F401

ROOT = Path(__file__).resolve().parent


def seed_demo(db: Database) -> dict[str, int]:
    if db.list_accounts():
        return {a["name"]: int(a["id"]) for a in db.list_accounts()}
    upstream = db.create_account("alice", {"name": "北区水库", "region": "upstream", "reach": "干流北段",
                                           "holder": "北区水务公司", "priority": 1,
                                           "valid_from": "2026-01-01", "valid_to": "2026-12-31",
                                           "quota": 1000}, "editor")
    downstream = db.create_account("alice", {"name": "河口灌区", "region": "downstream", "reach": "干流北段",
                                             "holder": "河口合作社", "priority": 2,
                                             "valid_from": "2026-01-01", "valid_to": "2026-12-31",
                                             "quota": 500}, "editor")
    other = db.create_account("alice", {"name": "西区水厂", "region": "west", "reach": "支流西段",
                                        "holder": "西区供水公司", "priority": 2,
                                        "valid_from": "2026-01-01", "valid_to": "2026-12-31",
                                        "quota": 400}, "editor")
    db.set_season_rule("alice", "upstream", 7, 0.35, "夏季上限", "editor")
    db.set_impact_rule("alice", "upstream", "downstream", 0.4, "保障河口最小生态流量", "editor")
    db.record_usage("meter-01", {"account_id": upstream["id"], "amount": 100,
                                 "meter_event_id": "UP-2026-0001", "occurred_at": "2026-03-01"}, "meter")
    return {"北区水库": int(upstream["id"]), "河口灌区": int(downstream["id"]), "西区水厂": int(other["id"])}


class Handler(BaseHTTPRequestHandler):
    db: Database
    server_version = "WaterRights/1.0"

    def _send(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self) -> None:
        data = (ROOT / "static" / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise DomainError("请求体不是合法 JSON") from exc

    def _auth(self) -> tuple[str, str]:
        return self.headers.get("X-User", "anonymous"), self.headers.get("X-Role", "viewer")

    def _error(self, exc: Exception) -> None:
        payload: dict[str, Any] = {"error": str(exc)}
        current = getattr(exc, "current", None)
        if current is not None:
            # Losing writer keeps its own input and sees the current state.
            payload["current"] = current
        self._send(payload, getattr(exc, "status", 400))

    def do_GET(self) -> None:
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        try:
            if parsed.path in {"/", "/index.html"}:
                return self._html()
            if parsed.path == "/api/health":
                return self._send({"ok": True})
            if parsed.path == "/api/accounts":
                return self._send({"accounts": self.db.list_accounts()})
            if parsed.path == "/api/transfers":
                return self._send({"transfers": self.db.list_transfers()})
            if parsed.path == "/api/audit":
                return self._send({"audit": self.db.audit()})
            if parsed.path == "/api/freeze/board":
                return self._send(self.db.freeze_board())
            if parsed.path.startswith("/api/freeze/"):
                order_id = int(parsed.path.rsplit("/", 1)[-1])
                return self._send(self.db.get_freeze_order(order_id))
            if parsed.path.startswith("/api/accounts/") and parsed.path.endswith("/available"):
                account_id = int(parsed.path.split("/")[3])
                return self._send(self.db.available(account_id))
            if parsed.path == "/api/drought/simulate":
                q = parse_qs(parsed.query)
                return self._send(self.db.simulate_drought(float(q.get("supply", ["0"])[0]),
                                                           float(q.get("reduction", ["0"])[0])))
            raise DomainError("接口不存在", 404)
        except (ValueError, DomainError) as exc:
            self._error(exc)

    def do_POST(self) -> None:
        from urllib.parse import urlparse

        parsed = urlparse(self.path)
        try:
            actor, role = self._auth()
            body = self._body()
            parts = [p for p in parsed.path.split("/") if p]
            if parts == ["api", "accounts"]:
                return self._send(self.db.create_account(actor, body, role), 201)
            if parts == ["api", "rules", "season"]:
                return self._send(self.db.set_season_rule(actor, str(body.get("region", "")),
                                                          int(body.get("month", 0)),
                                                          body.get("max_fraction"),
                                                          str(body.get("note", "")), role), 201)
            if parts == ["api", "rules", "impact"]:
                return self._send(self.db.set_impact_rule(actor, str(body.get("source_region", "")),
                                                          str(body.get("target_region", "")),
                                                          body.get("min_source_fraction"),
                                                          str(body.get("note", "")), role), 201)
            if parts == ["api", "transfers"]:
                return self._send(self.db.create_transfer(actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "transfers"] and parts[3] == "approve":
                return self._send(self.db.approve_transfer(int(parts[2]), actor, role))
            if len(parts) == 4 and parts[:2] == ["api", "transfers"] and parts[3] == "reject":
                return self._send(self.db.reject_transfer(int(parts[2]), actor, role))
            if parts == ["api", "transfers", "settle"]:
                return self._send(self.db.execute_transfers(
                    actor, body.get("as_of"), role))
            if parts == ["api", "usage"]:
                return self._send(self.db.record_usage(actor, body, role), 201)
            if parts == ["api", "freeze"]:
                return self._send(self.db.create_freeze_order(actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "freeze"] and parts[3] == "conditions":
                return self._send(self.db.update_freeze_conditions(int(parts[2]), actor, body, role))
            if len(parts) == 4 and parts[:2] == ["api", "freeze"] and parts[3] == "lift":
                return self._send(self.db.lift_freeze(int(parts[2]), actor, body, role))
            if len(parts) == 4 and parts[:2] == ["api", "freeze"] and parts[3] == "restore":
                return self._send(self.db.restore_freeze(int(parts[2]), actor, role))
            raise DomainError("接口不存在", 404)
        except (ValueError, TypeError, DomainError) as exc:
            self._error(exc)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[water] {self.address_string()} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="跨区域水资源使用权分配与转让服务")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8007")))
    parser.add_argument("--db", default=os.getenv("WATER_DB", str(DEFAULT_DB)))
    parser.add_argument("--init", action="store_true", help="创建数据库并写入示例账户")
    args = parser.parse_args()
    db = Database(args.db)
    if args.init:
        seeded = seed_demo(db)
        print(f"initialized database at {args.db} with accounts {sorted(seeded)}")
        return
    Handler.db = db
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"water-rights listening on http://127.0.0.1:{args.port} (db={args.db})")
    server.serve_forever()


if __name__ == "__main__":
    main()
