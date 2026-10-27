"""做表插件（``tables``）：把零散数据变成规范表格，合并多份"收表"。

对应调研里两条高频痛点：
- 「数据收集与汇总（收表、合并）」
- 「报表编制（手工做表）」

cordis 概念映射：
====================  =========================================================
``Service``           ``Tables`` 构造即注册为 ``tables`` 服务
``inject``            依赖 ``docs``（文档输出），未就绪自动 PENDING
====================  =========================================================
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from cordis_py import Service


def _detect_delimiter(text: str) -> str:
    """按首行猜分隔符：Tab > 逗号 > 分号。"""
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    if "\t" in first:
        return "\t"
    if "," in first:
        return ","
    if ";" in first:
        return ";"
    return "\t"


def parse_table_text(text: str) -> List[List[str]]:
    """把粘贴/CSV 文本解析成二维表（首行视为表头）。"""
    delimiter = _detect_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows: List[List[str]] = []
    for raw in reader:
        cells = [str(c).strip() for c in raw]
        if not any(cells):
            continue
        rows.append(cells)
    return rows


class Tables(Service):
    """做表服务（``tables``）。"""

    provide = "tables"
    inject = ["docs"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx, "tables")

    # -- 基本能力 ------------------------------------------------------------
    def make(
        self,
        title: str,
        header: Sequence[str],
        rows: Sequence[Sequence[Any]],
        *,
        name: Optional[str] = None,
    ) -> Path:
        """单张规范表格 → xlsx。"""
        sheet = {"name": title, "header": list(header), "rows": [list(r) for r in rows]}
        return self.ctx.docs.xlsx([sheet], stem=title, name=name)

    def merge(
        self,
        blocks: Sequence[Dict[str, str]],
        *,
        title: str = "合并汇总表",
        key: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """合并多份"收表"（每份一段 CSV/TSV 文本）。

        ``blocks``： ``[{"source": 来源名, "text": 表格文本}, ...]``
        ``key``：按某一列去重（保留首次出现，重复计入 ``duplicates``）。

        返回 ``{"path": ..., "rows": n, "duplicates": n, "sources": {...}}``。
        """
        headers: List[str] = []
        parsed: List[Tuple[str, Dict[str, int], List[List[str]]]] = []
        counts: Dict[str, int] = {}

        for block in blocks:
            rows = parse_table_text(block.get("text", ""))
            if not rows:
                continue
            header, data = rows[0], rows[1:]
            index = {}
            for i, h in enumerate(header):
                if h not in headers:
                    headers.append(h)
                index.setdefault(h, i)
            parsed.append((block.get("source", ""), index, data))
            counts[block.get("source", "")] = counts.get(block.get("source", ""), 0) + len(data)

        out_header = ["来源"] + headers
        out_rows: List[List[str]] = []
        seen = set()
        duplicates = 0
        for source, index, data in parsed:
            for row in data:
                record = {
                    h: (row[index[h]] if h in index and index[h] < len(row) else "")
                    for h in headers
                }
                if key:
                    mark = record.get(key, "")
                    if mark:
                        if mark in seen:
                            duplicates += 1
                            continue
                        seen.add(mark)
                out_rows.append([source] + [record[h] for h in headers])

        detail = {"name": title, "header": out_header, "rows": out_rows}
        stats = {
            "name": "来源统计",
            "header": ["来源", "行数"],
            "rows": [[s, c] for s, c in counts.items()] + [["合计", len(out_rows)]],
        }
        path = self.ctx.docs.xlsx([detail, stats], stem=title, name=name)
        return {"path": path, "rows": len(out_rows), "duplicates": duplicates, "sources": counts}
