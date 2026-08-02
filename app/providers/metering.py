"""
token / 成本计量: 每次调用写 usage.db, 按 today/7d/30d 汇总
"""

import logging
import sqlite3
import threading
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from typing import Iterator, Optional

from app.providers.base import ChatRequest, Usage, estimateUsage

##############################################################################################################################

logger = logging.getLogger("PromptMaster.providers.metering")

USAGE_RANGES = ("today", "7d", "30d")

RANGE_DAYS = {"today": 1, "7d": 7, "30d": 30}

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    tokensIn INTEGER NOT NULL DEFAULT 0,
    tokensOut INTEGER NOT NULL DEFAULT 0,
    costUsd REAL,
    estimated INTEGER NOT NULL DEFAULT 0,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_at ON usage(at);
CREATE INDEX IF NOT EXISTS idx_usage_provider ON usage(provider, model);
"""


def computeCost(usage: Usage, pricing: Optional[dict]) -> Optional[float]:
    """
    按每 1M token 单价计算成本; 价格未知返回 None
    """
    if not pricing:
        return None
    try:
        inputPrice = float(pricing.get("input") or 0.0)
        outputPrice = float(pricing.get("output") or 0.0)
    except (TypeError, ValueError):
        return None
    return (usage.tokensIn / 1_000_000) * inputPrice + (usage.tokensOut / 1_000_000) * outputPrice


def nowLocalIso() -> str:
    return datetime.now().astimezone().isoformat(timespec = "seconds")


class UsageStore:
    """
    SQLite 计量库 (WAL); 只做计量, 不做配额
    """
    def __init__(self, dbPath):
        self.dbPath = Path(dbPath)
        self._lock = threading.Lock()
        self.ensureSchema()

    def connect(self) -> sqlite3.Connection:
        self.dbPath.parent.mkdir(parents = True, exist_ok = True)
        connection = sqlite3.connect(self.dbPath.as_posix(), timeout = 10)
        connection.row_factory = sqlite3.Row
        return connection

    def ensureSchema(self) -> None:
        try:
            with self._lock, closing(self.connect()) as connection, connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(SCHEMA)
        except sqlite3.Error as exc:
            logger.error("初始化计量库 %s 失败: %s", self.dbPath.as_posix(), exc)

    ######################################################################

    def record(self, provider: str, model: str, usage: Usage, pricing: Optional[dict] = None) -> Optional[dict]:
        """
        记录一次调用; provider 未上报 usage 时 usage.estimated 为 True
        """
        costUsd = usage.costUsd if usage.costUsd is not None else computeCost(usage, pricing)
        row = {
            "id": uuid4().hex,
            "provider": provider,
            "model": model,
            "tokensIn": int(usage.tokensIn or 0),
            "tokensOut": int(usage.tokensOut or 0),
            "costUsd": costUsd,
            "estimated": bool(usage.estimated),
            "at": nowLocalIso(),
        }
        try:
            with self._lock, closing(self.connect()) as connection, connection:
                connection.execute(
                    "INSERT INTO usage (id, provider, model, tokensIn, tokensOut, costUsd, estimated, at)"
                    " VALUES (:id, :provider, :model, :tokensIn, :tokensOut, :costUsd, :estimated, :at)",
                    {**row, "estimated": int(row["estimated"])},
                )
        except sqlite3.Error as exc:
            logger.error("写入计量记录失败: %s", exc)
            return None
        return row

    ######################################################################

    def summary(self, rangeName: str = "today") -> dict:
        """
        返回区间总量与按日序列 (缺失日期补 0)
        """
        if rangeName not in USAGE_RANGES:
            rangeName = "today"
        days = RANGE_DAYS[rangeName]
        end = date.today()
        start = end - timedelta(days = days - 1)
        startIso = datetime.combine(start, datetime.min.time()).astimezone().isoformat(timespec = "seconds")

        totals = {"calls": 0, "tokensIn": 0, "tokensOut": 0, "costUsd": 0.0, "estimatedCalls": 0}
        daily = {}
        try:
            with self._lock, closing(self.connect()) as connection:
                rows = connection.execute(
                    "SELECT substr(at, 1, 10) AS day, COUNT(*) AS calls,"
                    " COALESCE(SUM(tokensIn), 0) AS tokensIn,"
                    " COALESCE(SUM(tokensOut), 0) AS tokensOut,"
                    " COALESCE(SUM(costUsd), 0) AS costUsd,"
                    " COALESCE(SUM(estimated), 0) AS estimatedCalls"
                    " FROM usage WHERE at >= ? GROUP BY day ORDER BY day",
                    (startIso,),
                ).fetchall()
        except sqlite3.Error as exc:
            logger.error("读取计量数据失败: %s", exc)
            rows = []

        for row in rows:
            entry = {
                "date": row["day"],
                "calls": int(row["calls"]),
                "tokensIn": int(row["tokensIn"]),
                "tokensOut": int(row["tokensOut"]),
                "costUsd": round(float(row["costUsd"] or 0.0), 6),
                "estimatedCalls": int(row["estimatedCalls"]),
            }
            daily[entry["date"]] = entry
            totals["calls"] += entry["calls"]
            totals["tokensIn"] += entry["tokensIn"]
            totals["tokensOut"] += entry["tokensOut"]
            totals["costUsd"] += entry["costUsd"]
            totals["estimatedCalls"] += entry["estimatedCalls"]

        series = []
        for offset in range(days):
            day = (start + timedelta(days = offset)).isoformat()
            series.append(daily.get(day, {
                "date": day, "calls": 0, "tokensIn": 0, "tokensOut": 0, "costUsd": 0.0, "estimatedCalls": 0,
            }))

        totals["costUsd"] = round(totals["costUsd"], 6)
        return {
            "range": rangeName,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "totals": totals,
            "series": series,
        }


##############################################################################################################################

def recordStream(store: Optional[UsageStore], chunks: Iterator, request: ChatRequest, pricing: Optional[dict] = None) -> Iterator:
    """
    透传 provider 增量, 并在流结束 (含异常) 时记账; 缺失 usage 时按字符估算
    """
    content = []
    usage = None
    failed = False
    try:
        for chunk in chunks:
            if chunk.delta:
                content.append(chunk.delta)
            if chunk.usage is not None:
                usage = chunk.usage
            yield chunk
    except BaseException:
        failed = True
        raise
    finally: # 请求完全失败且没有产生内容时不计账 (避免把失败重试算成 token 消耗)
        if store is not None and not (failed and usage is None and not content):
            final = usage if usage is not None else estimateUsage(request, "".join(content))
            store.record(request.provider, request.model, final, pricing)

##############################################################################################################################