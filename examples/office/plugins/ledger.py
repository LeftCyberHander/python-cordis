"""记账插件（``ledger``）：台账登记、月度汇总、导出报表。

对应调研痛点：「台账登记（手工记账）」。

cordis 概念映射：
====================  =========================================================
``Service``           ``Ledger`` 构造即注册为 ``ledger`` 服务
``inject``            依赖 ``store``（落库）与 ``docs``（导出 xlsx）
``isolate``           每个同事/部门一份独立数据库（账本互不干扰）
``effect``            数据库连接随插件加载打开、随卸载关闭
====================  =========================================================
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from cordis_py import Service

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    item TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT '支',
    amount REAL NOT NULL,
    category TEXT NOT NULL DEFAULT '其他',
    who TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


class Ledger(Service):
    """记账服务（``ledger``）。"""

    provide = "ledger"
    inject = ["store", "docs"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx, "ledger")
        self.ctx.store.executescript(_SCHEMA)

    # -- 写 ------------------------------------------------------------------
    def add(
        self,
        item: str,
        amount: float,
        *,
        date: Optional[str] = None,
        direction: str = "支",
        category: str = "其他",
        who: str = "",
        note: str = "",
    ) -> Dict[str, Any]:
        """登记一笔。``direction`` 取 ``"支"``（支出）或 ``"收"``（收入）。"""
        if direction not in ("支", "收"):
            raise ValueError("direction 只能是 '支' 或 '收'")
        entry = {
            "date": date or time.strftime("%Y-%m-%d"),
            "item": str(item),
            "direction": direction,
            "amount": round(float(amount), 2),
            "category": str(category),
            "who": str(who),
            "note": str(note),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        entry["id"] = self.ctx.store.insert(
            "INSERT INTO ledger_entries (date, item, direction, amount, category, who, note, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [entry["date"], entry["item"], entry["direction"], entry["amount"],
             entry["category"], entry["who"], entry["note"], entry["created_at"]],
        )
        # 事件：其它插件（审计、通知等）可挂监听，业务代码零改动。
        self.ctx.emit("ledger/entry", dict(entry))
        return entry

    # -- 读 ------------------------------------------------------------------
    def entries(self, month: Optional[str] = None) -> List[Dict[str, Any]]:
        """列出流水；``month`` 形如 ``"2026-10"``。"""
        if month:
            return self.ctx.store.query(
                "SELECT * FROM ledger_entries WHERE date LIKE ? ORDER BY date, id",
                [f"{month}%"],
            )
        return self.ctx.store.query("SELECT * FROM ledger_entries ORDER BY date, id")

    def summary(self, month: Optional[str] = None) -> Dict[str, Any]:
        """汇总：收入 / 支出 / 净额 + 分类小计。"""
        rows = self.entries(month)
        income = round(sum(r["amount"] for r in rows if r["direction"] == "收"), 2)
        expense = round(sum(r["amount"] for r in rows if r["direction"] == "支"), 2)
        by_category: Dict[str, float] = {}
        for r in rows:
            if r["direction"] == "支":
                by_category[r["category"]] = round(by_category.get(r["category"], 0) + r["amount"], 2)
        return {
            "month": month or "全部",
            "count": len(rows),
            "income": income,
            "expense": expense,
            "net": round(income - expense, 2),
            "by_category": dict(sorted(by_category.items(), key=lambda kv: -kv[1])),
        }

    # -- 导出 ----------------------------------------------------------------
    def report(self, month: Optional[str] = None, *, name: Optional[str] = None) -> Path:
        """月度报表 → xlsx（明细 + 分类汇总两张表）。"""
        rows = self.entries(month)
        summary = self.summary(month)
        detail = {
            "name": "明细",
            "header": ["日期", "事项", "收/支", "金额", "分类", "经办", "备注"],
            "rows": [
                [r["date"], r["item"], r["direction"], r["amount"], r["category"], r["who"], r["note"]]
                for r in rows
            ],
        }
        categories = {
            "name": "分类汇总",
            "header": ["分类", "支出合计"],
            "rows": [[k, v] for k, v in summary["by_category"].items()]
            + [["支出合计", summary["expense"]], ["收入合计", summary["income"]], ["净额", summary["net"]]],
        }
        title = f"台账报表-{month or '全部'}"
        return self.ctx.docs.xlsx([detail, categories], stem=title, name=name)
