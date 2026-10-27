"""用 cordis 组织调研问卷的题库与组卷（可直接运行的最小闭环）。

真实痛点（实测数据见 README_调研.md）：`D:/auto/调研/` 下 5 份问卷的
勾选项集合**交集占并集 71%**，绝大部分题目逐字重复，组间差异只集中在
「岗位职责说明」和少数几道部门特有题上。本文件把这份重复收敛成
「一份题库 + 每部门一个隔离作用域」。

cordis 概念 → 本文件用法：

=================  ========================================================
cordis 概念        在问卷场景里的角色
=================  ========================================================
``Service``        ``QuestionBank`` / ``Assembler``：构造即注册，隔离即派生副本
``isolate()``      每个部门一个作用域，各自 ``drop``/``add`` 互不污染
``inject``         组卷器声明依赖题库；题库未就绪时自动 PENDING，无需轮询
``effect``         每次加减题都是一个 effect，卸载该部门时自动逆序撤销
``waterfall``      钩子 ``survey/filter``：任何插件都能改写或剔除某道题
=================  ========================================================

运行::

    python examples/survey/question_bank.py            # 打印四份问卷
    python examples/survey/question_bank.py --docx DIR # 输出 .docx
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from cordis_py import Context, Service

# 各部门定义：名称、岗位职责说明、以及要剔除的母版题号、要追加的特有题
DEPTS: List[tuple] = [
    {
        "dept": "办公室公文组",
        "lead": "公文运转及OA系统维护，密级公文管理，公文核稿，档案管理等",
        "drop": [],
        "add": [("5.3", "公文流转的审批环节有几层？", ["1层", "2层", "3层及以上", "不清楚"])],
    },
    {
        "dept": "规划组",
        "lead": "规划管理（长中期发展）、架构管理、内部信息化管理",
        "drop": ["3.2", "4.2"],
        "add": [
            ("5.1", "有哪些数据指标量化工作成果？", ["有", "无", "不清楚"]),
            ("5.2", "现有指标主要由什么方式产生？",
             ["系统自动生成", "系统导出后人工整理", "手工台账／Excel", "凭经验估算"]),
        ],
    },
    {
        "dept": "数据治理组",
        "lead": "数据共享管理、数据质量与数据安全、监管大数据仓库统筹与规划",
        "drop": ["4.3"],
        "add": [("5.4", "数据质量问题主要集中在哪些环节？",
                 ["采集", "传输", "存储", "加工", "共享"])],
    },
    {
        "dept": "办公室综合组",
        "lead": "会议组织、后勤保障、跨部门协调",
        "drop": ["3.3"],
        "add": [],
    },
]

MASTER: Dict[str, tuple] = {
    "1.1": ("本组工作有哪些制度文件支撑？", ["有", "无", "不清楚"]),
    "2.1": ("当你需要确认「这件事该怎么走流程」时，通常去哪里找依据？",
            ["有一份统一的、大家都知道的文件，直接翻开就能查到",
             "有几份文件，但分散在不同地方，要问一下才知道看哪份",
             "主要靠问同事或问领导，文件不太看得懂或找不到",
             "一般不问，按自己的经验办",
             "不清楚"]),
    "2.2": ("是否 90% 以上的事项有线上资料可以确认流程？", ["是", "否"]),
    "3.1": ("线上化的载体是什么？", []),
    "3.2": ("哪些环节目前仍主要在线下完成？（多选）",
            ["审批与签批（纸质签批）", "数据收集与汇总（收表、合并）",
             "报表编制（手工做表）", "台账登记（手工记账）",
             "交接与签字确认", "会议组织与纪要整理",
             "基本都已线上化", "其他：__________"]),
    "3.3": ("当前线上化的主要障碍（多选）",
            ["系统功能不足", "多系统重复录入", "数据不同步", "线下重复进行", "其他："]),
    "4.1": ("本组目前使用 AI／智能化工具的情况是？",
            ["多个环节已在常态化使用", "个别同事自己在用",
             "试过，但已经停用", "没有使用", "不清楚"]),
    "4.2": ("尚未使用智能化的主要原因（多选）",
            ["不知道有何工具", "认为业务不适合", "数据敏感不敢用",
             "无IT支持", "现有工具够用", "其他："]),
    "4.3": ("你希望 Agent 优先帮你做什么？（多选）",
            ["文档撰写／润色", "数据整理／报表", "会议纪要", "信息检索／答疑",
             "代码／脚本", "图像／图纸处理"]),
    "4.4": ("你更需要哪种形态的智能化工具？",
            ["一键配置，开箱即用", "可自行配置，更灵活", "两者都需要", "无所谓"]),
}


class QuestionBank(Service):
    """题库服务。构造即 ``ctx.provide('bank', self)``。

    每调用一次 ``ctx.isolate('bank')`` 得到一份独立副本——这正是
    "各部门在同一套母版上加减题"的语义：改动互不外溢。
    """

    provide = "bank"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        self.dropped: set = set()
        self.extra: Dict[str, tuple] = {}
        super().__init__(ctx, "bank")

    def drop(self, qid: str) -> Callable[[], Any]:
        """剔除一道题。返回的 disposer 会随本 fiber 卸载自动执行。"""
        def _setup() -> Callable[[], None]:
            self.dropped.add(qid)
            return lambda: self.dropped.discard(qid)

        return self.ctx.effect(_setup, f"bank.drop({qid})")

    def add(self, qid: str, title: str, options: List[str]) -> Callable[[], Any]:
        """追加一道本组特有题。"""
        def _setup() -> Callable[[], None]:
            self.extra[qid] = (title, list(options))
            return lambda: self.extra.pop(qid, None)

        return self.ctx.effect(_setup, f"bank.add({qid})")

    def questions(self) -> List[tuple]:
        """产出该组的完整题目序列：``(题号, 题干, 选项)``，已剔除不适用题。"""
        merged: Dict[str, tuple] = {k: v for k, v in MASTER.items() if k not in self.dropped}
        merged.update(self.extra)
        return [(qid, title, opts) for qid, (title, opts) in sorted(merged.items())]


class Assembler(Service):
    """组卷器：把题库渲染成可发出去的问卷。

    ``inject = ['bank']`` 让它自动等到题库就绪才激活——不写任何轮询代码。
    """

    provide = "assembler"
    inject = ["bank"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        self.dept = "未命名部门"
        self.lead = ""
        super().__init__(ctx, "assembler")

    def build(self) -> List[str]:
        """走 ``waterfall`` 钩子，让插件可以逐题改写；返回 None 即剔除该题。"""
        bank: QuestionBank = self.ctx.bank
        lines = [f"【{self.dept}】调研问卷"]
        if self.lead:
            lines.append(f"岗位说明：{self.lead}")
        lines.append("")

        for qid, title, opts in bank.questions():
            # waterfall：监听器可返回新题干，或返回 None 剔除该题。
            # 注意端口约定：终端回调以"零参"形式被调用（events.py 中 `inner()`），
            # 故用闭包捕获 payload，而不是从形参取数据。
            payload = {"qid": qid, "title": title, "options": list(opts), "dept": self.dept}
            result = self.ctx.waterfall("survey/filter", payload, lambda: payload)
            if result is None:
                continue
            lines.append(f"{qid} {result['title']}")
            for opt in result["options"]:
                lines.append(f"    □ {opt}")
            lines.append("")
        return lines


async def _settle_scope(scope: Context) -> None:
    """把作用域内的 fiber 驱动到收敛。

    fiber 之间的唤醒是链式的（提供方激活 → 通知依赖方 → 调度其重载），
    必须全部跑在**同一个**事件循环里：跨 ``asyncio.run`` 会把链条断掉，
    被调度到已关闭循环上的重载任务会带着 CancelledError 醒来。
    多跑几轮是为了覆盖「依赖方排在提供方之前」的注册顺序。
    """
    for _ in range(3):
        for runtime in list(scope.registry.values()):
            for fiber in list(runtime.fibers):
                await fiber.await_()
        await asyncio.sleep(0)


def build_all(outdir: Optional[str] = None) -> Dict[str, List[str]]:
    """为每个部门建一个隔离作用域并组卷；``outdir`` 非空时同时写 .docx。"""
    root = Context()

    result: Dict[str, List[str]] = {}
    for spec in DEPTS:
        # 两个服务都各自隔离：同一进程里挂多份「题库 + 组卷器」实例，
        # 未隔离的服务名会在第二次注册时撞键（service has been registered）。
        scope = root.isolate("bank").isolate("assembler")
        # 类插件路径（对应 fiber.ts:204-211）：Fiber 实例化类并取 init()。
        scope.plugin(QuestionBank)
        scope.plugin(Assembler)

        # 等题库与组卷器两个 fiber 收敛（inject 满足后会自行激活）。
        asyncio.run(_settle_scope(scope))

        bank: QuestionBank = scope.bank
        asm: Assembler = scope.assembler
        asm.dept, asm.lead = spec["dept"], spec["lead"]

        # 部门差异化：全部是 effect，作用域卸载时自动逆序撤销。
        for qid in spec["drop"]:
            bank.drop(qid)
        for qid, title, opts in spec["add"]:
            bank.add(qid, title, opts)

        lines = asm.build()
        result[spec["dept"]] = lines

        if outdir:
            os.makedirs(outdir, exist_ok=True)
            write_docx(os.path.join(outdir, f"调研问卷-{spec['dept']}.docx"), lines)

    return result


# -- 最小 docx 写出器（仅依赖标准库，无需 python-docx）-----------------------------

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _para(text: str, size: int = 21, bold: bool = False) -> str:
    b = "<w:b/>" if bold else ""
    return (
        f'<w:p><w:pPr><w:spacing w:after="80"/></w:pPr>'
        f'<w:r><w:rPr>{b}<w:sz w:val="{size}"/></w:rPr>'
        f'<w:t xml:space="preserve">{_esc(text)}</w:t></w:r></w:p>'
    )


def write_docx(path: str, lines: List[str]) -> str:
    """把组卷结果写成 .docx。docx 本质是 zip + XML，标准库即可完成。"""
    import zipfile

    body = []
    for line in lines:
        if not line.strip():
            continue
        if line.startswith("【"):
            body.append(_para(line, 32, bold=True))
        elif line.startswith("    □"):
            body.append(_para(line.strip(), 20))
        elif line.startswith("岗位说明"):
            body.append(_para(line, 18))
        else:
            body.append(_para(line, 21, bold=True))
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f'<w:body>{"".join(body)}</w:body></w:document>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/document.xml", doc)
    return path


def main() -> None:
    argv = sys.argv[1:]
    outdir = argv[argv.index("--docx") + 1] if "--docx" in argv else None
    if outdir:
        os.system("cls" if os.name == "nt" else "clear")

    result = build_all(outdir)

    if outdir is None:
        for dept, lines in result.items():
            print("=" * 70)
            for ln in lines:
                print(ln)
            print()
        print("=" * 70)

    total_q = sum(1 for _ in result)
    print(f"部门数：{total_q}    题库母版：{len(MASTER)} 题（1 份）")
    for dept, lines in result.items():
        n = sum(1 for ln in lines if ln[:1].isdigit())
        print(f"  {dept:<12s} 实际出卷 {n} 题")
    if outdir:
        print(f"\n.docx 已写入：{os.path.abspath(outdir)}")


if __name__ == "__main__":
    main()