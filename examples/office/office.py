"""办公三件套 —— 用 cordis-py 组装的最小子集。

把「做表 / 记账 / 会议纪要」三件事做成三个插件，挂到一个进程上：
每位同事一个**隔离作用域**（独立数据库与输出目录），服务之间靠 inject 声明依赖，
所有副作用（数据库连接、文件目录、事件监听）都是 effect，随插件卸载自动回收。

运行方式::

    python office.py demo                     # 一键演示：两位同事 × 三件套
    python office.py ledger add --item 打车费 --amount 56 --category 交通 --who 张伟
    python office.py ledger list --month 2026-10 --who 张伟
    python office.py ledger report --month 2026-10 --who 张伟
    python office.py table merge --inputs a.csv b.csv --title 设备汇总
    python office.py table make --csv data.csv --title 名册
    python office.py meeting notice --title 周例会 --when "10月13日 14:00" --place 三层会议室 \\
        --attendees 张伟,王芳 --agenda "回顾;安排"
    python office.py meeting minutes --file notes.txt --title 周例会 --attendees 张伟,王芳

扩展方式：在 ``plugins/`` 下新增一个 ``Service`` 子类，再在 ``make_workspace()``
里加一行 ``scope.plugin(...)`` 即可 —— 其它插件不需要任何改动。
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent  # python-cordis 根目录
for _p in (str(PROJECT_ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cordis_py import Context  # noqa: E402

from plugins import Docs, Ledger, Meetings, Store, Tables  # noqa: E402

SERVICE_NAMES = ("docs", "store", "tables", "ledger", "meetings")


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------

def make_workspace(root: Context, unit: str) -> Context:
    """给一位同事（unit）建隔离作用域并装配全部插件。"""
    scope = root
    for name in SERVICE_NAMES:
        scope = scope.isolate(name)  # 每个服务一份独立 label，互不串

    scope.plugin(Docs, {"out_dir": str(HERE / "out" / unit)})
    scope.plugin(Store, {"db": str(HERE / "data" / unit / "office.db")})
    scope.plugin(Tables)
    scope.plugin(Ledger)
    scope.plugin(Meetings)
    return scope


async def _settle(root: Context) -> None:
    """把全部 fiber 驱动到收敛（同一事件循环内跑，多轮覆盖级联唤醒）。"""
    for _ in range(4):
        for runtime in list(root.registry.values()):
            for fiber in list(runtime.fibers):
                await fiber.await_()
        await asyncio.sleep(0)


def boot(units: List[str]) -> Dict[str, Context]:
    """创建根上下文，为每个 unit 装配一个工作区，并等待全部就绪。"""
    root = Context()
    scopes = {unit: make_workspace(root, unit) for unit in units}
    asyncio.run(_settle(root))
    return scopes


def dispose_handle(handle: Any) -> None:
    """同步方式卸载一个插件 fiber（演示热插拔）。"""
    result = handle.dispose()
    if hasattr(result, "__await__"):
        asyncio.run(result)


# ---------------------------------------------------------------------------
# 演示数据
# ---------------------------------------------------------------------------

_DEVICES_A = """部门,资产编号,使用人,设备型号,登记日期
办公室公文组,GT-001,王芳,ThinkPad T14,2025-11
办公室公文组,GT-002,刘洋,ThinkPad T14,2025-11
办公室公文组,GT-003,陈静,DELL 3430,2026-03
"""

_DEVICES_B = """部门,资产编号,使用人,设备型号,登记日期
规划组,GH-001,李强,ThinkPad X1,2025-08
规划组,GH-002,赵敏,MateBook B3,2026-01
办公室公文组,GT-002,刘洋,ThinkPad T14,2025-11
"""

_MEETING_NOTES = """会议记录（2026年10月13日）
一、上周工作回顾
张伟介绍了架构评审的进展。
二、调研问卷回收
王芳：需要在本周五前完成四个部门问卷的回收与汇总，由李强协助核对。
会议决定：问卷统一使用修订版模板。
三、下阶段安排
李强负责在下周二前输出汇总分析初稿。
张伟需要在10月20日前完成评审意见答复。
"""


def run_demo() -> None:
    print("=" * 72)
    print("办公三件套演示：两位同事（张伟 / 王芳）共用一套进程，数据互相隔离")
    print("=" * 72)

    # 演示使用独立工作区，每次演示前重置（data/ 与 out/ 均为示例运行时目录）。
    for unit in ("张伟", "王芳"):
        for sub in ("data", "out"):
            target = HERE / sub / unit
            if target.exists():
                shutil.rmtree(target)

    scopes = boot(["张伟", "王芳"])
    zhang, wang = scopes["张伟"], scopes["王芳"]

    # -- 0. 全局扩展：审计插件（事件监听示例，挂在根上下文上） -----------------
    audit_log = HERE / "out" / "审计日志.tsv"
    audit_log.parent.mkdir(parents=True, exist_ok=True)
    if audit_log.exists():
        audit_log.unlink()

    def audit(ctx, config):
        path = Path(config["log"])

        def on_entry(entry):
            with path.open("a", encoding="utf-8") as f:
                f.write(f"{entry['created_at']}\t{entry['item']}\t{entry['amount']}\n")

        ctx.on("ledger/entry", on_entry)

    print("\n[0] 全局扩展：在根上下文挂一个审计插件（监听所有工作区的记账事件）")
    audit_handle = zhang.root.plugin(audit, {"log": str(audit_log)})
    asyncio.run(_settle(zhang.root))

    # -- 1. 做表 --------------------------------------------------------------
    print("\n[1] 做表：把两份『收表』合并成汇总台账（按资产编号去重）")
    merged = zhang.tables.merge(
        [
            {"source": "公文组", "text": _DEVICES_A},
            {"source": "规划组", "text": _DEVICES_B},
        ],
        title="终端设备台账汇总",
        key="资产编号",
    )
    print(f"    合并 {merged['rows']} 行，去重 {merged['duplicates']} 行，来源：{merged['sources']}")
    print(f"    输出：{merged['path']}")

    # -- 2. 记账 --------------------------------------------------------------
    print("\n[2] 记账：两位同事各自登记，账本互不干扰")
    zhang.ledger.add("打车费", 56.0, date="2026-10-08", category="交通", who="张伟")
    zhang.ledger.add("打印纸", 128.5, date="2026-10-09", category="办公", who="张伟")
    zhang.ledger.add("会议茶歇", 320.0, date="2026-10-09", category="会务", who="张伟")
    zhang.ledger.add("备用金退回", 500.0, date="2026-10-10", direction="收", category="其他", who="张伟")
    report_path = zhang.ledger.report("2026-10")
    s_zhang = zhang.ledger.summary("2026-10")
    print(f"    张伟：{s_zhang['count']} 笔，支出 {s_zhang['expense']} 元，净额 {s_zhang['net']} 元")

    wang.ledger.add("快递费", 18.0, date="2026-10-09", category="邮寄", who="王芳")
    s_wang = wang.ledger.summary("2026-10")
    print(f"    王芳：{s_wang['count']} 笔，支出 {s_wang['expense']} 元")
    print(f"    张伟月报：{report_path}")

    # -- 3. 会议 --------------------------------------------------------------
    print("\n[3] 会议：通知 + 纪要 + 待办")
    notice_path = zhang.meetings.notice(
        "第16期双周报例会",
        when="2026年10月13日（周二）14:00",
        place="三层会议室",
        attendees=["张伟", "王芳", "李强"],
        agenda=["上周工作回顾", "调研问卷回收情况", "下阶段工作安排"],
        host="张伟",
    )
    minutes = zhang.meetings.minutes(
        "第16期双周报例会",
        _MEETING_NOTES,
        when="2026年10月13日（周二）14:00",
        place="三层会议室",
        attendees=["张伟", "王芳", "李强"],
    )
    print(f"    通知：{notice_path}")
    print(f"    纪要：{minutes['path']}")
    print(f"    议定事项 {len(minutes['decisions'])} 条，待办 {len(minutes['todos'])} 项：")
    for todo in zhang.meetings.open_todos():
        print(f"      - [{todo['owner']}] {todo['item'][:32]}…（期限：{todo['due']}）")

    # -- 4. 隔离与热插拔校验 ----------------------------------------------------
    print("\n[4] 校验：隔离与热插拔")
    print(f"    张伟的账本：{zhang.store.db_path}")
    print(f"    王芳的账本：{wang.store.db_path}")
    print(f"    张伟待办 {len(zhang.meetings.open_todos())} 项；王芳待办 {len(wang.meetings.open_todos())} 项（应为 0）")

    lines_before = len(audit_log.read_text(encoding="utf-8").splitlines()) if audit_log.exists() else 0
    print(f"    全局审计日志当前 {lines_before} 行；卸载审计插件……")
    dispose_handle(audit_handle)
    asyncio.run(_settle(zhang.root))
    zhang.ledger.add("卸载后测试", 1.0, date="2026-10-11", category="测试", who="张伟")
    lines_after = len(audit_log.read_text(encoding="utf-8").splitlines()) if audit_log.exists() else 0
    print(f"    再记一笔后日志 {lines_after} 行（仍为 {lines_before} 行说明监听器已随插件回收 ✓）")

    print("\n演示结束。输出目录：", HERE / "out")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def read_text(path: str) -> str:
    """读文本文件（utf-8 / gbk 自动识别，兼容 Excel 导出的 CSV）。"""
    data = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _parse_list(value: Optional[str], sep: str = ",") -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.replace("，", ",").split(sep) if item.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="office", description="办公三件套（cordis-py 组装）")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("demo", help="一键演示（两位同事 × 做表/记账/会议）")

    # 公共参数：--who 指工作区（同事名），每个叶子命令都可带
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--who", default="默认", help="工作区（同事名），默认『默认』")

    # 记账 ------------------------------------------------------------------
    ledger = sub.add_parser("ledger", help="记账：登记 / 查询 / 月报")
    lsub = ledger.add_subparsers(dest="action")

    add = lsub.add_parser("add", parents=[common], help="登记一笔")
    add.add_argument("--item", required=True, help="事项")
    add.add_argument("--amount", required=True, type=float, help="金额")
    add.add_argument("--direction", default="支", choices=["支", "收"], help="收支")
    add.add_argument("--category", default="其他", help="分类")
    add.add_argument("--date", default=None, help="日期，默认今天")
    add.add_argument("--note", default="", help="备注")

    lst = lsub.add_parser("list", parents=[common], help="列出流水")
    lst.add_argument("--month", default=None, help="月份，如 2026-10")

    rep = lsub.add_parser("report", parents=[common], help="导出月度报表（xlsx）")
    rep.add_argument("--month", default=None, help="月份，如 2026-10")

    # 做表 ------------------------------------------------------------------
    table = sub.add_parser("table", help="做表：合并收表 / 生成表格")
    tsub = table.add_subparsers(dest="action")

    merge = tsub.add_parser("merge", parents=[common], help="合并多份收表（csv/tsv）")
    merge.add_argument("--inputs", nargs="+", required=True, help="输入文件（至少一个）")
    merge.add_argument("--title", default="合并汇总表", help="标题")
    merge.add_argument("--key", default=None, help="按该列去重")

    make = tsub.add_parser("make", parents=[common], help="把一份 CSV 生成规范 xlsx")
    make.add_argument("--csv", required=True, help="输入文件")
    make.add_argument("--title", default="表格", help="标题")

    # 会议 ------------------------------------------------------------------
    meeting = sub.add_parser("meeting", help="会议：通知 / 纪要")
    msub = meeting.add_subparsers(dest="action")

    notice = msub.add_parser("notice", parents=[common], help="生成会议通知（docx）")
    notice.add_argument("--title", required=True)
    notice.add_argument("--when", default="")
    notice.add_argument("--place", default="")
    notice.add_argument("--attendees", default="")
    notice.add_argument("--agenda", default="", help="议程，分号分隔")
    notice.add_argument("--host", default="")

    minutes = msub.add_parser("minutes", parents=[common], help="把原始记录整理成纪要（docx）")
    minutes.add_argument("--file", required=True, help="会议记录文本文件")
    minutes.add_argument("--title", required=True)
    minutes.add_argument("--when", default="")
    minutes.add_argument("--place", default="")
    minutes.add_argument("--attendees", default="")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command in (None, "demo"):
        run_demo()
        return 0

    who = getattr(args, "who", "默认") or "默认"
    scopes = boot([who])
    scope = scopes[who]

    if args.command == "ledger":
        if args.action == "add":
            entry = scope.ledger.add(
                args.item, args.amount,
                date=args.date, direction=args.direction,
                category=args.category, note=args.note, who=who,
            )
            print(f"已登记 #{entry['id']}：{entry['date']} {entry['item']} {entry['amount']} 元（{entry['direction']}）")
        elif args.action == "list":
            for row in scope.ledger.entries(args.month):
                print(f"{row['date']}  {row['direction']}  {row['amount']:>10.2f}  {row['category']:<6}  {row['item']}")
        elif args.action == "report":
            path = scope.ledger.report(args.month)
            summary = scope.ledger.summary(args.month)
            print(f"月报已生成：{path}")
            print(f"收入 {summary['income']} 元，支出 {summary['expense']} 元，净额 {summary['net']} 元")
        else:
            parser.parse_args(["ledger", "--help"])

    elif args.command == "table":
        if args.action == "merge":
            blocks = [{"source": Path(p).stem, "text": read_text(p)} for p in args.inputs]
            result = scope.tables.merge(blocks, title=args.title, key=args.key)
            print(f"合并完成：{result['rows']} 行（去重 {result['duplicates']} 行）")
            print(f"输出：{result['path']}")
        elif args.action == "make":
            from plugins.tables import parse_table_text
            rows = parse_table_text(read_text(args.csv))
            if not rows:
                print("输入为空")
                return 1
            path = scope.tables.make(args.title, rows[0], rows[1:])
            print(f"输出：{path}")
        else:
            parser.parse_args(["table", "--help"])

    elif args.command == "meeting":
        if args.action == "notice":
            path = scope.meetings.notice(
                args.title, when=args.when, place=args.place,
                attendees=_parse_list(args.attendees),
                agenda=[a for a in args.agenda.replace("；", ";").split(";") if a.strip()],
                host=args.host,
            )
            print(f"通知已生成：{path}")
        elif args.action == "minutes":
            result = scope.meetings.minutes(
                args.title, read_text(args.file),
                when=args.when, place=args.place,
                attendees=_parse_list(args.attendees),
            )
            print(f"纪要已生成：{result['path']}")
            print(f"议定事项 {len(result['decisions'])} 条，待办 {len(result['todos'])} 项")
            for todo in scope.meetings.open_todos():
                print(f"  - [{todo['owner']}] {todo['item']}（期限：{todo['due']}）")
        else:
            parser.parse_args(["meeting", "--help"])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
