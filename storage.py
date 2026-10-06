"""Storage layer: SQLite schema, migrations and row-level SQL only.

No business decisions live here; ``service.Database`` orchestrates these
helpers inside transactions.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "water_rights.db"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Transfer lifecycle:
#   pending -> approved -> executed
#                  \-> suspended (frozen by a freeze order) -> executed on restore
#   pending -> rejected
RESERVED_STATUSES = ("pending", "approved")  # outgoing amounts that still sit in quota


class Storage:
    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB):
        self.path = str(path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

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
                    executed_by TEXT,
                    suspended_at TEXT,
                    freeze_order_id INTEGER REFERENCES freeze_orders(id),
                    restore_seq INTEGER,
                    restore_state TEXT,
                    restore_error TEXT,
                    restore_attempts INTEGER NOT NULL DEFAULT 0,
                    restored_at TEXT,
                    last_error TEXT
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
                    reach TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    started_on TEXT NOT NULL,
                    planned_end_on TEXT NOT NULL,
                    condition_note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_by TEXT,
                    updated_at TEXT,
                    lifted_by TEXT,
                    lifted_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_freeze_active_per_reach
                    ON freeze_orders(reach) WHERE status='active';
                CREATE TABLE IF NOT EXISTS freeze_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES freeze_orders(id),
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    status TEXT NOT NULL DEFAULT 'frozen',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    restored_at TEXT,
                    UNIQUE(order_id, account_id)
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
        def columns(table: str) -> set[str]:
            return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

        account_cols = columns("accounts")
        if "reach" not in account_cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN reach TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE accounts SET reach=region WHERE reach=''")
        transfer_cols = columns("transfers")
        add = {
            "executed_at": "TEXT",
            "executed_by": "TEXT",
            "suspended_at": "TEXT",
            "freeze_order_id": "INTEGER",
            "restore_seq": "INTEGER",
            "restore_state": "TEXT",
            "restore_error": "TEXT",
            "restore_attempts": "INTEGER NOT NULL DEFAULT 0",
            "restored_at": "TEXT",
            "last_error": "TEXT",
        }
        for name, decl in add.items():
            if name not in transfer_cols:
                conn.execute(f"ALTER TABLE transfers ADD COLUMN {name} {decl}")
        # Historical 'approved' rows already moved quota at approval time; mark
        # them executed so restoration never touches them twice.
        conn.execute(
            "UPDATE transfers SET status='executed', executed_at=approved_at "
            "WHERE status='approved' AND executed_at IS NULL"
        )

    # ----- audit -----------------------------------------------------------
    def audit(self, conn: sqlite3.Connection, actor: str, action: str, entity_type: str,
              entity_id: int | None, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (actor, action, entity_type, entity_id, json.dumps(details, ensure_ascii=False), utcnow()),
        )

    def list_audit(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM audit_log ORDER BY id DESC").fetchall()

    # ----- accounts --------------------------------------------------------
    def insert_account(self, conn: sqlite3.Connection, data: dict[str, Any]) -> sqlite3.Cursor:
        return conn.execute(
            "INSERT INTO accounts(name,region,reach,holder,priority,valid_from,valid_to,quota,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (data["name"], data["region"], data["reach"], data["holder"], data["priority"],
             data["valid_from"], data["valid_to"], data["quota"], utcnow()),
        )

    def get_account(self, conn: sqlite3.Connection, account_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()

    def list_accounts(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()

    def accounts_by_reach(self, conn: sqlite3.Connection, reach: str) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM accounts WHERE reach=? ORDER BY id", (reach,)).fetchall()

    def add_used(self, conn: sqlite3.Connection, account_id: int, amount: float) -> None:
        conn.execute("UPDATE accounts SET used=used+? WHERE id=?", (amount, account_id))

    def move_quota(self, conn: sqlite3.Connection, source_id: int, target_id: int, amount: float) -> None:
        conn.execute("UPDATE accounts SET quota=quota-? WHERE id=?", (amount, source_id))
        conn.execute("UPDATE accounts SET quota=quota+? WHERE id=?", (amount, target_id))

    # ----- rules -----------------------------------------------------------
    def upsert_season_rule(self, conn: sqlite3.Connection, region: str, month: int,
                           max_fraction: float, note: str) -> None:
        conn.execute(
            """INSERT INTO season_rules(region,month,max_fraction,note) VALUES(?,?,?,?)
               ON CONFLICT(region,month) DO UPDATE SET max_fraction=excluded.max_fraction,note=excluded.note""",
            (region, month, max_fraction, note),
        )

    def upsert_impact_rule(self, conn: sqlite3.Connection, source_region: str, target_region: str,
                           min_source_fraction: float, note: str) -> None:
        conn.execute(
            """INSERT INTO impact_rules(source_region,target_region,min_source_fraction,note) VALUES(?,?,?,?)
               ON CONFLICT(source_region,target_region) DO UPDATE
               SET min_source_fraction=excluded.min_source_fraction,note=excluded.note""",
            (source_region, target_region, min_source_fraction, note),
        )

    def get_impact_rule(self, conn: sqlite3.Connection, source_region: str,
                        target_region: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM impact_rules WHERE source_region=? AND target_region=?",
            (source_region, target_region),
        ).fetchone()

    def get_season_rule(self, conn: sqlite3.Connection, region: str, month: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT max_fraction FROM season_rules WHERE region=? AND month=?", (region, month)
        ).fetchone()

    # ----- transfers -------------------------------------------------------
    def insert_transfer(self, conn: sqlite3.Connection, data: dict[str, Any]) -> sqlite3.Cursor:
        return conn.execute(
            "INSERT INTO transfers(from_account_id,to_account_id,amount,effective_date,created_by,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (data["from"], data["to"], data["amount"], data["effective_date"], data["created_by"], utcnow()),
        )

    def get_transfer(self, conn: sqlite3.Connection, transfer_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()

    def list_transfers(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM transfers ORDER BY id DESC").fetchall()

    def sum_outgoing(self, conn: sqlite3.Connection, account_id: int, statuses: tuple[str, ...],
                     exclude_id: int | None = None) -> float:
        sql = f"SELECT COALESCE(SUM(amount),0) total FROM transfers WHERE from_account_id=? AND status IN ({','.join('?' * len(statuses))})"
        params: list[Any] = [account_id, *statuses]
        if exclude_id is not None:
            sql += " AND id<>?"
            params.append(exclude_id)
        return float(conn.execute(sql, params).fetchone()["total"])

    def sum_suspended(self, conn: sqlite3.Connection, account_id: int, direction: str,
                      exclude_id: int | None = None) -> float:
        column = "from_account_id" if direction == "out" else "to_account_id"
        sql = f"SELECT COALESCE(SUM(amount),0) total FROM transfers WHERE {column}=? AND status='suspended'"
        params: list[Any] = [account_id]
        if exclude_id is not None:
            sql += " AND id<>?"
            params.append(exclude_id)
        return float(conn.execute(sql, params).fetchone()["total"])

    def approved_unexecuted_by_reach(self, conn: sqlite3.Connection, reach: str) -> list[sqlite3.Row]:
        return conn.execute(
            """SELECT t.* FROM transfers t JOIN accounts a ON a.id=t.from_account_id
               WHERE a.reach=? AND t.status='approved' AND t.executed_at IS NULL
               ORDER BY t.effective_date, t.approved_at, t.id""",
            (reach,),
        ).fetchall()

    def due_approved_transfers(self, conn: sqlite3.Connection, as_of: str) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM transfers WHERE status='approved' AND executed_at IS NULL"
            " AND effective_date<=? ORDER BY effective_date,id",
            (as_of,),
        ).fetchall()

    def suspended_for_order(self, conn: sqlite3.Connection, order_id: int) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM transfers WHERE freeze_order_id=? AND status='suspended' ORDER BY restore_seq",
            (order_id,),
        ).fetchall()

    def unfinished_source_accounts(self, conn: sqlite3.Connection, order_id: int) -> list[sqlite3.Row]:
        return conn.execute(
            """SELECT from_account_id AS account_id, MIN(restore_seq) AS first_seq
               FROM transfers
               WHERE freeze_order_id=? AND status='suspended'
                 AND (restore_state IS NULL OR restore_state='failed')
               GROUP BY from_account_id ORDER BY first_seq""",
            (order_id,),
        ).fetchall()

    def unfinished_for_account(self, conn: sqlite3.Connection, order_id: int, account_id: int) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM transfers WHERE freeze_order_id=? AND from_account_id=? AND status='suspended'"
            " AND (restore_state IS NULL OR restore_state='failed') ORDER BY restore_seq",
            (order_id, account_id),
        ).fetchall()

    def next_restore_seq(self, conn: sqlite3.Connection, order_id: int) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(restore_seq),0) m FROM transfers WHERE freeze_order_id=?", (order_id,)
        ).fetchone()
        return int(row["m"]) + 1

    def suspend_transfer(self, conn: sqlite3.Connection, transfer_id: int, order_id: int, seq: int) -> None:
        conn.execute(
            "UPDATE transfers SET status='suspended',suspended_at=?,freeze_order_id=?,restore_seq=?,"
            "restore_state=NULL,restore_error=NULL WHERE id=?",
            (utcnow(), order_id, seq, transfer_id),
        )

    def mark_executed(self, conn: sqlite3.Connection, transfer_id: int, actor: str, restored: bool) -> None:
        if restored:
            conn.execute(
                "UPDATE transfers SET status='executed',executed_at=?,executed_by=?,"
                "restore_state='done',restore_error=NULL,restored_at=COALESCE(restored_at,?) WHERE id=?",
                (utcnow(), actor, utcnow(), transfer_id),
            )
        else:
            conn.execute(
                "UPDATE transfers SET status='executed',executed_at=?,executed_by=?,"
                "restore_state=NULL,restore_error=NULL WHERE id=?",
                (utcnow(), actor, transfer_id),
            )

    def mark_restore_failed(self, conn: sqlite3.Connection, transfer_id: int, reason: str) -> None:
        conn.execute(
            "UPDATE transfers SET restore_state='failed',restore_error=?,"
            "restore_attempts=restore_attempts+1 WHERE id=?",
            (reason, transfer_id),
        )

    def mark_execute_failed(self, conn: sqlite3.Connection, transfer_id: int, reason: str) -> None:
        conn.execute("UPDATE transfers SET last_error=? WHERE id=?", (reason, transfer_id))

    def suspend_during_run(self, conn: sqlite3.Connection, transfer_id: int, order_id: int, seq: int) -> None:
        conn.execute(
            "UPDATE transfers SET status='suspended',suspended_at=?,freeze_order_id=?,restore_seq=?,"
            "last_error=NULL WHERE id=?",
            (utcnow(), order_id, seq, transfer_id),
        )

    # ----- usage -----------------------------------------------------------
    def month_usage(self, conn: sqlite3.Connection, account_id: int, month: str) -> float:
        return float(conn.execute(
            "SELECT COALESCE(SUM(amount),0) total FROM usage_records WHERE account_id=? AND substr(occurred_at,1,7)=?",
            (account_id, month),
        ).fetchone()["total"])

    def insert_usage(self, conn: sqlite3.Connection, data: dict[str, Any]) -> sqlite3.Cursor:
        return conn.execute(
            "INSERT INTO usage_records(account_id,meter_event_id,amount,occurred_at,actor,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (data["account_id"], data["meter_event_id"], data["amount"],
             data["occurred_at"], data["actor"], utcnow()),
        )

    # ----- freeze orders & freeze accounts ---------------------------------
    def insert_order(self, conn: sqlite3.Connection, data: dict[str, Any]) -> sqlite3.Cursor:
        return conn.execute(
            "INSERT INTO freeze_orders(reach,reason,started_on,planned_end_on,condition_note,status,"
            "version,created_by,created_at) VALUES(?,?,?,?,?,'active',1,?,?)",
            (data["reach"], data["reason"], data["started_on"], data["planned_end_on"],
             data["condition_note"], data["created_by"], utcnow()),
        )

    def get_order(self, conn: sqlite3.Connection, order_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM freeze_orders WHERE id=?", (order_id,)).fetchone()

    def get_active_order(self, conn: sqlite3.Connection, reach: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM freeze_orders WHERE reach=? AND status='active' ORDER BY id DESC", (reach,)
        ).fetchone()

    def list_active_orders(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM freeze_orders WHERE status='active' ORDER BY reach").fetchall()

    def list_orders(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM freeze_orders ORDER BY id DESC").fetchall()

    def update_conditions(self, conn: sqlite3.Connection, order_id: int, planned_end_on: str,
                          condition_note: str, actor: str) -> None:
        conn.execute(
            "UPDATE freeze_orders SET planned_end_on=?,condition_note=?,version=version+1,"
            "updated_by=?,updated_at=? WHERE id=?",
            (planned_end_on, condition_note, actor, utcnow(), order_id),
        )

    def lift_order(self, conn: sqlite3.Connection, order_id: int, actor: str) -> None:
        conn.execute(
            "UPDATE freeze_orders SET status='lifted',version=version+1,lifted_by=?,lifted_at=?,"
            "updated_by=?,updated_at=? WHERE id=?",
            (actor, utcnow(), actor, utcnow(), order_id),
        )

    def upsert_freeze_account(self, conn: sqlite3.Connection, order_id: int, account_id: int) -> None:
        conn.execute(
            "INSERT INTO freeze_accounts(order_id,account_id,created_at) VALUES(?,?,?)"
            " ON CONFLICT(order_id,account_id) DO NOTHING",
            (order_id, account_id, utcnow()),
        )

    def list_freeze_accounts(self, conn: sqlite3.Connection, order_id: int | None = None) -> list[sqlite3.Row]:
        if order_id is None:
            return conn.execute("SELECT * FROM freeze_accounts ORDER BY order_id,id").fetchall()
        return conn.execute(
            "SELECT * FROM freeze_accounts WHERE order_id=? ORDER BY id", (order_id,)
        ).fetchall()

    def get_freeze_account(self, conn: sqlite3.Connection, order_id: int,
                           account_id: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM freeze_accounts WHERE order_id=? AND account_id=?", (order_id, account_id)
        ).fetchone()

    def touch_freeze_account(self, conn: sqlite3.Connection, order_id: int, account_id: int,
                             error: str | None) -> None:
        conn.execute(
            "UPDATE freeze_accounts SET attempts=attempts+1,last_error=? WHERE order_id=? AND account_id=?",
            (error, order_id, account_id),
        )

    def restore_freeze_account_if_done(self, conn: sqlite3.Connection, order_id: int, account_id: int) -> None:
        pending = conn.execute(
            "SELECT COUNT(*) c FROM transfers WHERE freeze_order_id=? AND from_account_id=?"
            " AND status='suspended' AND (restore_state IS NULL OR restore_state='failed')",
            (order_id, account_id),
        ).fetchone()["c"]
        if pending == 0:
            conn.execute(
                "UPDATE freeze_accounts SET status='restored',restored_at=COALESCE(restored_at,?),"
                "last_error=NULL WHERE order_id=? AND account_id=?",
                (utcnow(), order_id, account_id),
            )
