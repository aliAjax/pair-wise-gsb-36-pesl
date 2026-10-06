"""接口层：标准库 HTTP 路由，只做请求解析、身份透传与 JSON 序列化。"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .domain import DomainError, WaterRightsService

ROOT = Path(__file__).resolve().parent.parent


class Handler(BaseHTTPRequestHandler):
    service: WaterRightsService
    server_version = "WaterRights/1.1"

    # ------------------------------------------------------------- 输出
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

    def _fail(self, exc: Exception) -> None:
        if isinstance(exc, DomainError):
            payload: dict[str, Any] = {"error": str(exc)}
            if exc.current is not None:
                # 并发冲突时把当前状态回给后写的一方，原记录保持不变。
                payload["current"] = exc.current
            self._send(payload, exc.status)
        else:
            self._send({"error": f"服务器内部错误: {exc}"}, 500)

    # ------------------------------------------------------------- GET
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        svc = self.service
        try:
            if parsed.path in {"/", "/index.html"}:
                return self._html()
            if parsed.path == "/api/health":
                return self._send({"ok": True})
            if parsed.path == "/api/accounts":
                return self._send({"accounts": svc.list_accounts()})
            if parsed.path == "/api/transfers":
                return self._send({"transfers": svc.list_transfers()})
            if parsed.path == "/api/audit":
                return self._send({"audit": svc.audit()})
            if parsed.path == "/api/freezes":
                return self._send(svc.list_freeze_orders())
            if parsed.path.startswith("/api/freezes/"):
                parts = [p for p in parsed.path.split("/") if p]
                if len(parts) == 3:
                    return self._send(svc.get_freeze_order(int(parts[2])))
            if parsed.path.startswith("/api/accounts/") and parsed.path.endswith("/available"):
                account_id = int(parsed.path.split("/")[3])
                return self._send(svc.available(account_id))
            if parsed.path == "/api/drought/simulate":
                q = parse_qs(parsed.query)
                return self._send(svc.simulate_drought(
                    float(q.get("supply", ["0"])[0]), float(q.get("reduction", ["0"])[0])))
            raise DomainError("接口不存在", 404)
        except (ValueError, DomainError) as exc:
            self._fail(exc)

    # ------------------------------------------------------------- POST
    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            actor, role = self._auth()
            body = self._body()
            svc = self.service
            parts = [p for p in parsed.path.split("/") if p]
            if parts == ["api", "accounts"]:
                return self._send(svc.create_account(actor, body, role), 201)
            if parts == ["api", "rules", "season"]:
                return self._send(svc.set_season_rule(actor, str(body.get("region", "")),
                    int(body.get("month", 0)), body.get("max_fraction"), str(body.get("note", "")), role), 201)
            if parts == ["api", "rules", "impact"]:
                return self._send(svc.set_impact_rule(actor, str(body.get("source_region", "")),
                    str(body.get("target_region", "")), body.get("min_source_fraction"),
                    str(body.get("note", "")), role), 201)
            if parts == ["api", "transfers"]:
                return self._send(svc.create_transfer(actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "transfers"] and parts[3] == "approve":
                return self._send(svc.approve_transfer(int(parts[2]), actor, role))
            if len(parts) == 4 and parts[:2] == ["api", "transfers"] and parts[3] == "reject":
                return self._send(svc.reject_transfer(int(parts[2]), actor, role))
            if len(parts) == 4 and parts[:2] == ["api", "transfers"] and parts[3] == "execute":
                return self._send(svc.execute_transfer(int(parts[2]), actor, role))
            if parts == ["api", "usage"]:
                return self._send(svc.record_usage(actor, body, role), 201)
            if parts == ["api", "freezes"]:
                return self._send(svc.issue_freeze_order(actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "freezes"] and parts[3] == "recover":
                return self._send(svc.recover_freeze_order(int(parts[2]), actor, role))
            raise DomainError("接口不存在", 404)
        except (ValueError, TypeError, DomainError) as exc:
            self._fail(exc)

    # ------------------------------------------------------------- PATCH
    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        try:
            actor, role = self._auth()
            body = self._body()
            parts = [p for p in parsed.path.split("/") if p]
            # 调度员当天修改恢复条件：乐观版本控制，旧版本只能看到当前状态。
            if len(parts) == 3 and parts[:2] == ["api", "freezes"]:
                return self._send(self.service.update_freeze_order(int(parts[2]), actor, body, role))
            raise DomainError("接口不存在", 404)
        except (ValueError, TypeError, DomainError) as exc:
            self._fail(exc)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[water] {self.address_string()} - {fmt % args}")


def build_server(db_path: str, port: int) -> ThreadingHTTPServer:
    Handler.service = WaterRightsService(db_path)
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
