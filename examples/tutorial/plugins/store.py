"""存储服务：把数据落到一个 JSON 文件（``store``）。

这个插件演示三件事：

1. 服务是**普通对象**：`Store` 只负责读写，不关心谁在用；
2. 生命周期用 ``effect`` 表达：加载时读盘、卸载时落盘，不需要手动 close；
3. 配置从 ``ctx.plugin(Store, {"path": ...})`` 的第二个参数来。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cordis_py import Service


class Store(Service):
    """JSON 存储服务（``store``）。

    配置：``{"path": 数据文件路径}``。
    """

    provide = "store"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        cfg = config or {}
        self.path = Path(cfg.get("path") or "./todo-data.json")
        super().__init__(ctx, "store")
        # effect 主体立即执行；它返回的函数就是「卸载时做什么」。
        ctx.effect(self._open, "store.open")

    def _open(self) -> Any:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {}
        self.save()
        return self.save

    def save(self) -> None:
        """落盘。业务插件改完数据后调用一次即可。"""
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
