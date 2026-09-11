"""交易审计：append-only 记录每一次交易尝试。

为什么必须有这一层：一旦系统能自己花钱，"事后能说清每一分钱是怎么花出去的"
就从"好习惯"变成"底线"。因此每次尝试——**包括被拒绝的和演练的**——都写一行，
并且从不修改/删除已有行。判定证据（模型原始结论、闸门实测值与上限）整份存入，
以便事后复盘"当时为什么放行"。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping, Optional

from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.services.trade.models import GateVerdict, TradeIntent, TradeOutcome


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _today_prefix() -> str:
    return datetime.now().strftime("%Y-%m-%d")


class TradeAuditStore:
    """``trade_attempts`` 表的读写封装（只插入、只查询，不更新、不删除）。"""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path

    def _ensure_schema(self) -> None:
        """确保建库建表完成（与 SqliteTaskRepository 的做法一致）。"""
        bootstrap_sqlite_storage(self.db_path, legacy_config_file=None)

    def append(
        self,
        *,
        intent: TradeIntent,
        verdict: GateVerdict,
        outcome: TradeOutcome,
        adapter: str,
        detail: str = "",
        external_ref: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> int:
        """写入一行审计记录，返回自增 id。"""
        self._ensure_schema()
        with sqlite_connection(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO trade_attempts (
                    created_at, idempotency_key, task_name, item_id, title, link,
                    seller, price, adapter, outcome, allowed, reasons_json,
                    checks_json, evidence_json, external_ref, detail, duration_ms
                ) VALUES (
                    :created_at, :idempotency_key, :task_name, :item_id, :title, :link,
                    :seller, :price, :adapter, :outcome, :allowed, :reasons_json,
                    :checks_json, :evidence_json, :external_ref, :detail, :duration_ms
                )
                """,
                {
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "idempotency_key": intent.idempotency_key,
                    "task_name": intent.task_name,
                    "item_id": intent.item_id,
                    "title": intent.title,
                    "link": intent.link,
                    "seller": intent.seller,
                    "price": float(intent.price) if intent.price is not None else None,
                    "adapter": adapter,
                    "outcome": outcome.value,
                    "allowed": int(verdict.allowed),
                    "reasons_json": _dumps(list(verdict.reasons)),
                    "checks_json": _dumps(verdict.checks),
                    "evidence_json": _dumps(dict(intent.evidence or {})),
                    "external_ref": external_ref,
                    "detail": detail,
                    "duration_ms": duration_ms,
                },
            )
            conn.commit()
            return int(cursor.lastrowid or 0)

    # ------------------------------------------------------------------ 查询
    def has_idempotency_key(self, key: str) -> bool:
        """该商品是否已经有过"实际成交"的记录（用于幂等去重）。

        只把 ``submitted`` 视为已占用：被拒绝或演练的记录不应阻止将来真正下单。
        """
        self._ensure_schema()
        with sqlite_connection(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM trade_attempts
                WHERE idempotency_key = ? AND outcome = ?
                LIMIT 1
                """,
                (key, TradeOutcome.SUBMITTED.value),
            ).fetchone()
        return row is not None

    def spent_today(self, *, include_dry_run: bool = False) -> float:
        """当日已成交金额。``include_dry_run`` 用于演练期预估预算消耗。"""
        self._ensure_schema()
        outcomes = [TradeOutcome.SUBMITTED.value]
        if include_dry_run:
            outcomes.append(TradeOutcome.DRY_RUN.value)
        placeholders = ",".join("?" for _ in outcomes)
        with sqlite_connection(self.db_path) as conn:
            row = conn.execute(
                f"""
                SELECT COALESCE(SUM(price), 0) AS total FROM trade_attempts
                WHERE created_at LIKE ? AND outcome IN ({placeholders})
                """,
                [f"{_today_prefix()}%", *outcomes],
            ).fetchone()
        return float(row["total"] or 0.0)

    def orders_today(self, *, include_dry_run: bool = False) -> int:
        """当日已成交单数。"""
        self._ensure_schema()
        outcomes = [TradeOutcome.SUBMITTED.value]
        if include_dry_run:
            outcomes.append(TradeOutcome.DRY_RUN.value)
        placeholders = ",".join("?" for _ in outcomes)
        with sqlite_connection(self.db_path) as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS total FROM trade_attempts
                WHERE created_at LIKE ? AND outcome IN ({placeholders})
                """,
                [f"{_today_prefix()}%", *outcomes],
            ).fetchone()
        return int(row["total"] or 0)

    def recent(self, limit: int = 50) -> list[dict]:
        """最近的交易尝试，供 UI/排查使用。"""
        self._ensure_schema()
        with sqlite_connection(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM trade_attempts
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    @staticmethod
    def _row_to_dict(row: Mapping[str, Any]) -> dict:
        payload = dict(row)
        for key in ("reasons_json", "checks_json", "evidence_json"):
            raw = payload.get(key)
            try:
                payload[key.replace("_json", "")] = json.loads(raw) if raw else None
            except (TypeError, ValueError):
                payload[key.replace("_json", "")] = raw
            payload.pop(key, None)
        payload["allowed"] = bool(payload.get("allowed"))
        return payload
