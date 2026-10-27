"""提醒插件：函数式插件的完整例子（不需要继承 Service）。

和 ``Todo`` 的区别：

- 它不提供服务，只**消费**别人的服务与事件，所以写成普通函数最省事；
- ``inject = ["todo"]`` 让它等到 ``todo`` 就绪再执行；
- 监听器是 ``ctx.on`` 注册的 effect，插件卸载时自动摘除，不会泄漏。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cordis_py import inject_decorator


@inject_decorator("todo")
def reminder(ctx: Any, config: Any) -> None:
    """配置：``{"who": 名字, "log": 可选日志路径}``。"""
    cfg = config or {}
    who = cfg.get("who") or "匿名"
    log_path = Path(cfg["log"]) if cfg.get("log") else None

    def on_added(item: Any) -> None:
        # 事件是**进程级**的：所有作用域的监听器默认都能收到。要限定自己这一份，
        # 就得在监听器里按业务字段过滤（这里用 owner）。
        if item.get("owner") != who:
            return
        line = f"[{who}] 新增待办 #{item['id']}：{item['text']}"
        print("  " + line)
        if log_path is not None:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def on_finished(item: Any) -> None:
        if item.get("owner") != who:
            return
        print(f"  [{who}] 完成待办 #{item['id']}：{item['text']}")

    ctx.on("todo/added", on_added)
    ctx.on("todo/finished", on_finished)

    # 启动时汇报一次当前积压
    pending = len(ctx.todo.listing(open_only=True))
    print(f"  [{who}] 提醒插件已就绪，当前有 {pending} 项未完成")
