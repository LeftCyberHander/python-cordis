"""教程入口：装配插件、跑 CLI。

运行方式::

    python main.py demo
    python main.py add "买牛奶" --who 张三
    python main.py list --who 张三
    python main.py list --who 张三 --open
    python main.py done 1 --who 张三

三个人名对应三个**隔离作用域**：各自独立的数据文件、独立的 ``store``/``todo``
实例，同一份代码、同一个进程。
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
for _p in (str(PROJECT_ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cordis_py import Context  # noqa: E402

from plugins import Store, Todo, reminder  # noqa: E402

# 需要隔离的服务名：每个用户一份独立实例。
ISOLATED = ("store", "todo")


async def make_scope(root: Context, who: str) -> Context:
    """给一个用户建隔离作用域并装配全部插件。"""
    scope = root
    for name in ISOLATED:
        scope = scope.isolate(name)
    await scope.plugin(Store, {"path": str(HERE / "data" / who / "todo.json")})
    await scope.plugin(Todo)
    await scope.plugin(reminder, {"who": who})
    return scope


async def boot(users: List[str]) -> Dict[str, Context]:
    """创建根上下文，为每个用户装配工作区（共享同一个 root）。"""
    root = Context()
    return {who: await make_scope(root, who) for who in users}


def run_demo() -> None:
    for who in ("张三", "李四"):
        target = HERE / "data" / who
        if target.exists():
            shutil.rmtree(target)

    print("=" * 64)
    print("两个用户、同一份代码、同一个进程，数据完全隔离")
    print("=" * 64)

    scopes = asyncio.run(boot(["张三", "李四"]))
    zhang, li = scopes["张三"], scopes["李四"]

    print("\n[1] 张三登记两条，李四登记一条")
    zhang.todo.add("买牛奶", owner="张三")
    zhang.todo.add("写周报", owner="张三")
    li.todo.add("订会议室", owner="李四")

    print("\n[2] 各自的清单互不干扰")
    for who, scope in scopes.items():
        print(f"  {who}: {scope.todo.summary()}  {scope.store.path}")

    print("\n[3] 完成一条，事件照常派发")
    zhang.todo.finish(1)
    print(f"  张三剩余未完成：{len(zhang.todo.listing(open_only=True))} 项")

    print("\n[4] 隔离验证：两个 store 是不同的对象")
    print(f"  张三是李四的 store 吗？{zhang.store is li.store}")

    print("\n演示结束。数据在 data/ 下。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="todo", description="cordis-py 教程示例")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("demo", help="一键演示（两个用户 + 事件 + 隔离）")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--who", default="默认", help="用户（工作区）")

    add = sub.add_parser("add", parents=[common], help="新增待办")
    add.add_argument("text", help="待办内容")

    lst = sub.add_parser("list", parents=[common], help="列出待办")
    lst.add_argument("--open", action="store_true", help="只看未完成")

    done = sub.add_parser("done", parents=[common], help="标记完成")
    done.add_argument("id", type=int, help="待办编号")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in (None, "demo"):
        run_demo()
        return 0

    who = args.who or "默认"
    scope = asyncio.run(boot([who]))[who]

    if args.command == "add":
        item = scope.todo.add(args.text, owner=who)
        print(f"已添加 #{item['id']}：{item['text']}")
    elif args.command == "list":
        rows = scope.todo.listing(owner=who if who != "默认" else None, open_only=args.open)
        if not rows:
            print("（空）")
        for row in rows:
            mark = "x" if row["done"] else " "
            print(f"[{mark}] #{row['id']}  {row['text']}")
    elif args.command == "done":
        item = scope.todo.finish(args.id)
        print(f"已完成 #{item['id']}：{item['text']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
