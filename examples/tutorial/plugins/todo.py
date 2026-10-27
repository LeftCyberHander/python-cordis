"""待办服务：真正的业务逻辑（``todo``）。

要点：

- ``inject = ["store"]``：声明依赖后，fiber 会等 ``store`` 就绪才执行构造函数，
  不需要写任何重试代码；
- 方法体里的 ``self.ctx`` 指向**调用方**上下文，因此 ``self.ctx.emit`` 发出的
  事件归调用方所在作用域；
- 每个写操作都发一个事件，别的插件可以监听而不必改这里一行代码。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from cordis_py import Service


class Todo(Service):
    """待办清单（``todo``）。"""

    provide = "todo"
    inject = ["store"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx, "todo")
        # 依赖在构造期取一次并缓存。方法体里的 `self.ctx` 是**调用方**上下文，
        # 若在方法里写 `self.ctx.store`，调用方也必须声明 inject store 才拿得到。
        self._store = self.ctx.store
        self.items: List[Dict[str, Any]] = self._store.data.setdefault("todos", [])

    # -- 写 ------------------------------------------------------------------
    def add(self, text: str, owner: str = "") -> Dict[str, Any]:
        item = {
            "id": max((i["id"] for i in self.items), default=0) + 1,
            "text": str(text),
            "owner": str(owner),
            "done": False,
        }
        self.items.append(item)
        self._store.save()
        self.ctx.emit("todo/added", dict(item))
        return item

    def finish(self, item_id: int) -> Dict[str, Any]:
        for item in self.items:
            if item["id"] == item_id and not item["done"]:
                item["done"] = True
                self._store.save()
                self.ctx.emit("todo/finished", dict(item))
                return item
        raise KeyError(f"没有这条待办：{item_id}")

    # -- 读 ------------------------------------------------------------------
    def listing(self, owner: Optional[str] = None, open_only: bool = False) -> List[Dict[str, Any]]:
        result = list(self.items)
        if owner is not None:
            result = [i for i in result if i["owner"] == owner]
        if open_only:
            result = [i for i in result if not i["done"]]
        return result

    def summary(self) -> Dict[str, int]:
        return {
            "total": len(self.items),
            "open": sum(1 for i in self.items if not i["done"]),
            "done": sum(1 for i in self.items if i["done"]),
        }
