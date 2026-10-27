"""存储服务：给业务插件用的 SQLite 轻量封装（``store``）。

- 连接的生命周期用 ``effect`` 管理：插件加载即打开、卸载即关闭；
- 每个隔离作用域（isolate）一份独立数据库文件 —— 「每人/每部门一个账本」；
- 线程安全：CLI 与将来接 Web 时都可能跨线程调用，连接开 ``check_same_thread=False``
  并加一把可重入锁。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from cordis_py import Service


class Store(Service):
    """SQLite 存储服务（``store``）。

    配置： ``{"db": 数据库文件路径}``。
    """

    provide = "store"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        cfg = config or {}
        self.db_path = Path(cfg.get("db") or "./data/office.db")
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        super().__init__(ctx, "store")

        def _open() -> Any:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._conn = conn
            return self._close

        self.ctx.effect(_open, "store.open")

    def _close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        """执行单条写入语句并提交。"""
        with self._lock:
            assert self._conn is not None, "store 未打开（插件未加载？）"
            self._conn.execute(sql, tuple(params))
            self._conn.commit()

    def executescript(self, sql: str) -> None:
        """执行多语句脚本（建表等）。"""
        with self._lock:
            assert self._conn is not None, "store 未打开（插件未加载？）"
            self._conn.executescript(sql)
            self._conn.commit()

    def insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        """执行 INSERT 并返回行号。"""
        with self._lock:
            assert self._conn is not None, "store 未打开（插件未加载？）"
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        """查询并返回 dict 列表。"""
        with self._lock:
            assert self._conn is not None, "store 未打开（插件未加载？）"
            rows = self._conn.execute(sql, tuple(params)).fetchall()
            return [dict(row) for row in rows]

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        """查询单个值（无结果时返回 None）。"""
        rows = self.query(sql, params)
        if not rows:
            return None
        return next(iter(rows[0].values()))
