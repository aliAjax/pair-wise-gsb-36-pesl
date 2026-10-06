"""存储层：SQLite 表结构、迁移与全部 SQL，业务判定不写在本层。

存储层提供两类入口：
- ``store.transaction()``：开启 ``BEGIN IMMEDIATE`` 写事务，返回的连接同时也是上下文管理器；
- ``store.connect()``：只读/普通连接。

判定层的每个方法把 ``tx``（事务连接）传给这里的 CRUD 函数，
从而保证"判定 + 写入"在同一个事务里完成，并发提交只有一方能写入。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

DEFAULT_DB = Path(__file__).resolve().parent.parent / "water_rights.db"

ACTIVE_ORDER_STATES = ("active", "recovering")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = str(path)
        self._init_schema()

    # ------------------------------------------------------------------ 连接
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """独占写事务：余额/状态判断与写入必须在同一个事务内。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ 建表
    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    region TEXT NOT NULL,
                    reach TEXT NOT NULL DEFAULT '',
                    holder TEXT NOT NULL,
                    priority INTEGER NOT NULL CHECK(priority BETWEEN 1 AND 5),
                    valid_from TEXT NOT NULL,
                    valid_to TEXT NOT NULL,
                    quota REAL NOT NULL CHECK(quota >= 0),
                    used REAL NOT NULL DEFAULT 0 CHECK(used >= 0),
                    created_at TEXT NOT NULL,
                    CHECK(valid_from <= valid_to)
                );
                CREATE TABLE IF NOT EXISTS transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_account_id INTEGER NOT NULL REFERENCES accounts(id),
                    to_account_id INTEGER NOT NULL REFERENCES accounts(id),
                    amount REAL NOT NULL CHECK(amount > 0),
                    effective_date TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_by TEXT NOT NULL,
                    approved_by TEXT,
                    created_at TEXT NOT NULL,
                    approved_at TEXT,
                    executed_at TEXT,
                    suspended_order_id INTEGER REFERENCES freeze_orders(id)
                );
                CREATE TABLE IF NOT EXISTS usage_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    meter_event_id TEXT NOT NULL,
                    amount REAL NOT NULL CHECK(amount > 0),
                    occurred_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(account_id, meter_event_id)
                );
                CREATE TABLE IF NOT EXISTS season_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    region TEXT NOT NULL,
                    month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
                    max_fraction REAL NOT NULL CHECK(max_fraction > 0 AND max_fraction <= 1),
                    note TEXT NOT NULL DEFAULT '',
                    UNIQUE(region, month)
                );
                CREATE TABLE IF NOT EXISTS impact_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_region TEXT NOT NULL,
                    target_region TEXT NOT NULL,
                    min_source_fraction REAL NOT NULL CHECK(min_source_fraction >= 0 AND min_source_fraction <= 1),
                    note TEXT NOT NULL DEFAULT '',
                    UNIQUE(source_region, target_region)
                );
                CREATE TABLE IF NOT EXISTS freeze_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reach TEXT NOT NULL UNIQUE,
                    reason TEXT NOT NULL DEFAULT '',
                    started_on TEXT NOT NULL,
                    planned_recovery_on TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    recovered_at TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    CHECK(started_on <= planned_recovery_on),
                    CHECK(status IN ('active','recovering','recovered'))
                );
                CREATE TABLE IF NOT EXISTS account_freezes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES freeze_orders(id),
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    queue_position INTEGER,
                    status TEXT NOT NULL DEFAULT 'frozen',
                    failure_reason TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    UNIQUE(order_id, account_id),
                    CHECK(status IN ('frozen','recovered','failed'))
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """给既有数据库补齐禁调令相关列。"""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
        if "reach" not in cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN reach TEXT NOT NULL DEFAULT ''")
        # 老数据没有河段概念时，河段与地区同名。
        conn.execute("UPDATE accounts SET reach=region WHERE reach IS NULL OR reach=''")
        tcols = {r["name"] for r in conn.execute("PRAGMA table_info(transfers)").fetchall()}
        if "executed_at" not in tcols:
            conn.execute("ALTER TABLE transfers ADD COLUMN executed_at TEXT")
        if "suspended_order_id" not in tcols:
            conn.execute("ALTER TABLE transfers ADD COLUMN suspended_order_id INTEGER")
        # 同一河段只允许一条生效中的禁调令（recovered 状态保留历史）。
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_freeze_reach_active "
            "ON freeze_orders(reach) WHERE status IN ('active','recovering')"
        )

    # ------------------------------------------------------------------ 审计
    def audit(self, tx: sqlite3.Connection, actor: str, action: str, entity_type: str,
              entity_id: int | None, details: dict[str, Any]) -> None:
        tx.execute(
            "INSERT INTO audit_log(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (actor, action, entity_type, entity_id, json.dumps(details, ensure_ascii=False), utcnow()),
        )

    # ------------------------------------------------------------------ 账户
    def insert_account(self, tx: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
        cur = tx.execute(
            "INSERT INTO accounts(name,region,reach,holder,priority,valid_from,valid_to,quota,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (data["name"], data["region"], data["reach"], data["holder"], data["priority"],
             data["valid_from"], data["valid_to"], data["quota"], utcnow()),
        )
        return dict(self.get_account(tx, cur.lastrowid))

    def get_account(self, tx: sqlite3.Connection, account_id: int) -> sqlite3.Row | None:
        return tx.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()

    def list_accounts(self, tx: sqlite3.Connection) -> list[sqlite3.Row]:
        return tx.execute("SELECT * FROM accounts ORDER BY id").fetchall()

    def list_accounts_by_reach(self, tx: sqlite3.Connection, reach: str) -> list[sqlite3.Row]:
        return tx.execute("SELECT * FROM accounts WHERE reach=? ORDER BY id", (reach,)).fetchall()

    def move_quota(self, tx: sqlite3.Connection, source_id: int, target_id: int, amount: float) -> None:
        tx.execute("UPDATE accounts SET quota=quota-? WHERE id=?", (amount, source_id))
        tx.execute("UPDATE accounts SET quota=quota+? WHERE id=?", (amount, target_id))

    def add_used(self, tx: sqlite3.Connection, account_id: int, amount: float) -> None:
        tx.execute("UPDATE accounts SET used=used+? WHERE id=?", (amount, account_id))

    def set_impact_rule(self, tx: sqlite3.Connection, source_region: str, target_region: str,
                        min_source_fraction: float, note: str) -> None:
        tx.execute(
            "INSERT INTO impact_rules(source_region,target_region,min_source_fraction,note) VALUES(?,?,?,?)"
            " ON CONFLICT(source_region,target_region) DO UPDATE SET"
            " min_source_fraction=excluded.min_source_fraction,note=excluded.note",
            (source_region, target_region, min_source_fraction, note),
        )

    def get_impact_rule(self, tx: sqlite3.Connection, source_region: str,
                        target_region: str) -> sqlite3.Row | None:
        return tx.execute(
            "SELECT * FROM impact_rules WHERE source_region=? AND target_region=?",
            (source_region, target_region),
        ).fetchone()

    def get_season_rule(self, tx: sqlite3.Connection, region: str, month: int) -> sqlite3.Row | None:
        return tx.execute(
            "SELECT max_fraction FROM season_rules WHERE region=? AND month=?", (region, month)
        ).fetchone()

    def set_season_rule(self, tx: sqlite3.Connection, region: str, month: int,
                        max_fraction: float, note: str) -> None:
        tx.execute(
            "INSERT INTO season_rules(region,month,max_fraction,note) VALUES(?,?,?,?)"
            " ON CONFLICT(region,month) DO UPDATE SET max_fraction=excluded.max_fraction,note=excluded.note",
            (region, month, max_fraction, note),
        )

    # ------------------------------------------------------------------ 转让
    def insert_transfer(self, tx: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
        cur = tx.execute(
            "INSERT INTO transfers(from_account_id,to_account_id,amount,effective_date,created_by,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (data["from_account_id"], data["to_account_id"], data["amount"],
             data["effective_date"], data["created_by"], utcnow()),
        )
        return dict(self.get_transfer(tx, cur.lastrowid))

    def get_transfer(self, tx: sqlite3.Connection, transfer_id: int) -> sqlite3.Row | None:
        return tx.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()

    def list_transfers(self, tx: sqlite3.Connection) -> list[sqlite3.Row]:
        return tx.execute("SELECT * FROM transfers ORDER BY id DESC").fetchall()

    def pending_total(self, tx: sqlite3.Connection, account_id: int,
                      exclude_id: int | None = None) -> float:
        sql = "SELECT COALESCE(SUM(amount),0) total FROM transfers WHERE from_account_id=? AND status='pending'"
        params: list[Any] = [account_id]
        if exclude_id is not None:
            sql += " AND id<>?"
            params.append(exclude_id)
        return float(tx.execute(sql, params).fetchone()["total"])

    def mark_transfer(self, tx: sqlite3.Connection, transfer_id: int, status: str,
                      approved_by: str | None = None, approved_at: str | None = None,
                      executed_at: str | None = None, order_id: int | None = None) -> None:
        tx.execute(
            "UPDATE transfers SET status=?, approved_by=COALESCE(?,approved_by),"
            " approved_at=COALESCE(?,approved_at), executed_at=COALESCE(?,executed_at),"
            " suspended_order_id=? WHERE id=?",
            (status, approved_by, approved_at, executed_at, order_id, transfer_id),
        )

    def suspended_transfers(self, tx: sqlite3.Connection, order_id: int) -> list[sqlite3.Row]:
        return tx.execute(
            "SELECT * FROM transfers WHERE status='suspended' AND suspended_order_id=?"
            " ORDER BY COALESCE(approved_at,created_at), id",
            (order_id,),
        ).fetchall()

    def month_usage(self, tx: sqlite3.Connection, account_id: int, month_key: str) -> float:
        return float(tx.execute(
            "SELECT COALESCE(SUM(amount),0) total FROM usage_records"
            " WHERE account_id=? AND substr(occurred_at,1,7)=?",
            (account_id, month_key),
        ).fetchone()["total"])

    def insert_usage(self, tx: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
        cur = tx.execute(
            "INSERT INTO usage_records(account_id,meter_event_id,amount,occurred_at,actor,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (data["account_id"], data["meter_event_id"], data["amount"],
             data["occurred_at"], data["actor"], utcnow()),
        )
        return dict(tx.execute("SELECT * FROM usage_records WHERE id=?", (cur.lastrowid,)).fetchone())

    # ------------------------------------------------------------------ 禁调令
    def insert_order(self, tx: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
        cur = tx.execute(
            "INSERT INTO freeze_orders(reach,reason,started_on,planned_recovery_on,status,created_by,created_at)"
            " VALUES(?,?,?,?,'active',?,?)",
            (data["reach"], data["reason"], data["started_on"], data["planned_recovery_on"],
             data["created_by"], utcnow()),
        )
        return dict(self.get_order(tx, cur.lastrowid))

    def get_order(self, tx: sqlite3.Connection, order_id: int) -> sqlite3.Row | None:
        return tx.execute("SELECT * FROM freeze_orders WHERE id=?", (order_id,)).fetchone()

    def get_order_by_reach(self, tx: sqlite3.Connection, reach: str) -> sqlite3.Row | None:
        return tx.execute(
            "SELECT * FROM freeze_orders WHERE reach=? AND status IN ('active','recovering')",
            (reach,),
        ).fetchone()

    def list_orders(self, tx: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
        return tx.execute("SELECT * FROM freeze_orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def touch_order(self, tx: sqlite3.Connection, order_id: int, **fields: Any) -> None:
        allowed = {"reason", "planned_recovery_on", "status", "recovered_at", "last_error", "version"}
        sets, params = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"未知禁调令字段: {key}")
            sets.append(f"{key}=?")
            params.append(value)
        if not sets:
            return
        params.append(order_id)
        tx.execute(f"UPDATE freeze_orders SET {', '.join(sets)} WHERE id=?", params)

    def upsert_freeze(self, tx: sqlite3.Connection, order_id: int, account_id: int,
                      queue_position: int | None, status: str = "frozen") -> None:
        tx.execute(
            "INSERT INTO account_freezes(order_id,account_id,queue_position,status,updated_at)"
            " VALUES(?,?,?,?,?)"
            " ON CONFLICT(order_id,account_id) DO UPDATE SET queue_position=excluded.queue_position,"
            " status=excluded.status, updated_at=excluded.updated_at",
            (order_id, account_id, queue_position, status, utcnow()),
        )

    def get_freeze(self, tx: sqlite3.Connection, order_id: int,
                   account_id: int) -> sqlite3.Row | None:
        return tx.execute(
            "SELECT * FROM account_freezes WHERE order_id=? AND account_id=?",
            (order_id, account_id),
        ).fetchone()

    def list_freezes(self, tx: sqlite3.Connection, order_id: int) -> list[sqlite3.Row]:
        return tx.execute(
            "SELECT * FROM account_freezes WHERE order_id=? ORDER BY COALESCE(queue_position,999999), id",
            (order_id,),
        ).fetchall()

    def touch_freeze(self, tx: sqlite3.Connection, order_id: int, account_id: int,
                     status: str | None = None, failure_reason: str | None = None,
                     queue_position: int | None = None, bump_attempt: bool = False) -> None:
        sets = ["updated_at=?"]
        params: list[Any] = [utcnow()]
        if status is not None:
            sets.append("status=?")
            params.append(status)
        if failure_reason is not None:
            sets.append("failure_reason=?")
            params.append(failure_reason)
        if queue_position is not None:
            sets.append("queue_position=?")
            params.append(queue_position)
        if bump_attempt:
            sets.append("attempts=attempts+1")
        params.extend([order_id, account_id])
        tx.execute(
            f"UPDATE account_freezes SET {', '.join(sets)} WHERE order_id=? AND account_id=?",
            params,
        )

    def active_freeze_for_account(self, tx: sqlite3.Connection, account_id: int) -> sqlite3.Row | None:
        return tx.execute(
            "SELECT f.*, o.reach AS reach, o.status AS order_status FROM account_freezes f"
            " JOIN freeze_orders o ON o.id=f.order_id"
            " WHERE f.account_id=? AND o.status IN ('active','recovering') LIMIT 1",
            (account_id,),
        ).fetchone()

    def audit_log(self, tx: sqlite3.Connection) -> list[sqlite3.Row]:
        return tx.execute("SELECT * FROM audit_log ORDER BY id DESC").fetchall()
