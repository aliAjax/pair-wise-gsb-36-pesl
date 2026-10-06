"""兼容入口：历史代码从 ``app`` 导入 Database/DomainError/seed_demo 并运行 CLI。

分层实现见 :mod:`waterrights.storage`（存储）、:mod:`waterrights.domain`（判定）、
:mod:`waterrights.api`（接口）；页面在 ``static/index.html``。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from waterrights.api import build_server
from waterrights.domain import DomainError, WaterRightsService, parse_date  # noqa: F401
from waterrights.storage import DEFAULT_DB, Storage  # noqa: F402


class Database(WaterRightsService):
    """向后兼容的旧名称；判定层实现全部业务方法。"""


def seed_demo(db: Database) -> dict[str, int]:
    if db.list_accounts():
        return {str(a["name"]): int(a["id"]) for a in db.list_accounts()}
    upstream = db.create_account("alice", {"name": "北区水库", "region": "upstream", "reach": "north-canal",
                                            "holder": "北区水务公司", "priority": 1,
                                            "valid_from": "2026-01-01", "valid_to": "2026-12-31",
                                            "quota": 1000}, "editor")
    downstream = db.create_account("alice", {"name": "河口灌区", "region": "downstream", "reach": "north-canal",
                                              "holder": "河口合作社", "priority": 2,
                                              "valid_from": "2026-01-01", "valid_to": "2026-12-31",
                                              "quota": 500}, "editor")
    # 不同河段的账户：该河段无禁调令时照旧运行，恢复时作为跨河段对手方出现。
    db.create_account("alice", {"name": "西岸水厂", "region": "west", "reach": "west-canal",
                                "holder": "西岸供水公司", "priority": 2,
                                "valid_from": "2026-01-01", "valid_to": "2026-12-31",
                                "quota": 400}, "editor")
    db.set_season_rule("alice", "upstream", 7, 0.35, "夏季上限", "editor")
    db.set_impact_rule("alice", "upstream", "downstream", 0.4, "保障河口最小生态流量", "editor")
    db.record_usage("meter-01", {"account_id": upstream["id"], "amount": 100,
                                 "meter_event_id": "UP-2026-0001", "occurred_at": "2026-03-01"}, "meter")
    return {"北区水库": int(upstream["id"]), "河口灌区": int(downstream["id"])}


def main() -> None:
    parser = argparse.ArgumentParser(description="跨区域水资源使用权分配与转让服务（含污染预警禁调令）")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8007")))
    parser.add_argument("--db", default=os.getenv("WATER_DB", str(DEFAULT_DB)))
    parser.add_argument("--init", action="store_true", help="创建数据库并写入示例账户")
    args = parser.parse_args()
    if args.init:
        db = Database(args.db)
        seed_demo(db)
        print(f"initialized database at {args.db}")
        return
    server = build_server(args.db, args.port)
    print(f"water-rights listening on http://127.0.0.1:{args.port} (db={args.db})")
    server.serve_forever()


if __name__ == "__main__":
    main()
