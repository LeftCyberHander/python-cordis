"""会议插件（``meetings``）：会议通知/议程生成 + 纪要整理 + 待办跟踪。

对应调研痛点：「会议组织与纪要整理」。

cordis 概念映射：
====================  =========================================================
``Service``           ``Meetings`` 构造即注册为 ``meetings`` 服务
``inject``            依赖 ``store``（会议与待办落库）与 ``docs``（docx 输出）
``waterfall``         纪要段落渲染走 ``meeting/minutes`` 钩子，可被其它插件改写
====================  =========================================================
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from cordis_py import Service

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    when_text TEXT NOT NULL DEFAULT '',
    place TEXT NOT NULL DEFAULT '',
    attendees TEXT NOT NULL DEFAULT '',
    agenda TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meeting_todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL,
    item TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT '待定',
    due TEXT NOT NULL DEFAULT '待定',
    done INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""

_DECISION_MARKERS = ("决定", "议定", "明确", "同意", "统一", "确定", "通过")
_ACTION_MARKERS = ("负责", "需要", "完成", "落实", "跟进", "推进", "输出", "梳理", "提交", "协助", "安排")
_DEADLINE_RE = re.compile(
    r"((?:\d{4}年)?\d{1,2}月\d{1,2}日(?:前|之前)?"
    r"|本周[一二三四五六日天]?|下周[一二三四五六日天]?|周[一二三四五六日天]"
    r"|本月|下月|月底|季度末|节前|放假前)"
)
_OWNER_PATTERNS = (
    # 「王芳：xxx」这种行首署名，通常整句就是该同事的待办
    re.compile(r"^(?P<n>[\u4e00-\u9fa5]{2,3})[：:]"),
    re.compile(r"由(?P<n>[\u4e00-\u9fa5]{2,3})(?:负责|牵头|落实|跟进|完成|协助)"),
    re.compile(r"(?P<n>[\u4e00-\u9fa5]{2,3})(?:负责|牵头)"),
)


def _clean(line: str) -> str:
    """去掉常见行首标记（列表符、序号、方括号），保留正文。"""
    text = line.strip()
    text = re.sub(r"^[\-*·•—>]+\s*", "", text)
    text = re.sub(r"^[（(]?\d+[)）.、]\s*", "", text)
    text = re.sub(r"^[一二三四五六七八九十]+[、.]\s*", "", text)
    return text.strip()


def extract_minutes(notes: str, attendees: Sequence[str] = ()) -> Dict[str, Any]:
    """把会议原始记录整理成 决议 / 待办 / 讨论要点 三段结构。

    规则提取（可人工复核）；接入 LLM 后可在此处换成模型调用。
    """
    decisions: List[str] = []
    todos: List[Dict[str, str]] = []
    discussion: List[str] = []

    for raw in notes.splitlines():
        text = _clean(raw)
        if not text or len(text) < 4:
            continue

        owner = ""
        for pattern in _OWNER_PATTERNS:
            m = pattern.search(text)
            if m:
                owner = m.group("n")
                break
        if not owner:
            for name in attendees:
                if name and name in text:
                    owner = name
                    break

        deadline = ""
        m = _DEADLINE_RE.search(text)
        if m:
            deadline = m.group(1)

        if any(k in text for k in _DECISION_MARKERS) and not any(k in text for k in ("负责", "需要")):
            decisions.append(text)
            continue

        is_action = any(k in text for k in _ACTION_MARKERS)
        if is_action and (owner or deadline):
            todos.append({"item": text, "owner": owner or "待定", "due": deadline or "待定"})
            continue

        discussion.append(text)

    return {"decisions": decisions, "todos": todos, "discussion": discussion}


class Meetings(Service):
    """会议服务（``meetings``）。"""

    provide = "meetings"
    inject = ["store", "docs"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx, "meetings")
        self.ctx.store.executescript(_SCHEMA)

    # -- 会议组织 ------------------------------------------------------------
    def notice(
        self,
        title: str,
        *,
        when: str = "",
        place: str = "",
        attendees: Sequence[str] = (),
        agenda: Sequence[str] = (),
        host: str = "",
        name: Optional[str] = None,
    ) -> Path:
        """生成会议通知（含议程）→ docx，并把会议登记入库。"""
        meeting_id = self.ctx.store.insert(
            "INSERT INTO meetings (title, when_text, place, attendees, agenda, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [title, when, place, "、".join(attendees), "；".join(agenda),
             time.strftime("%Y-%m-%d %H:%M:%S")],
        )
        blocks: List[Tuple[str, Any]] = [
            ("p", "各相关人员："),
            ("p", f"　　定于{when or '（时间待定）'}在{place or '（地点待定）'}召开{title}，现将有关事项通知如下："),
            ("p", f"　　一、会议时间：{when or '待定'}"),
            ("p", f"　　二、会议地点：{place or '待定'}"),
            ("p", f"　　三、参会人员：{'、'.join(attendees) if attendees else '待定'}"),
            ("p", f"　　四、会议主持：{host or '待定'}"),
        ]
        if agenda:
            blocks.append(("p", "　　五、会议议程："))
            blocks.extend(("p", f"　　　　{i}. {item}") for i, item in enumerate(agenda, 1))
        blocks.append(("p", "　　请参会人员准时到会并准备相关材料；如有调整请提前告知。"))
        blocks.append(("p", "　　特此通知。"))
        blocks.append(("p", "　　　　　　　　　　　　　　　　　　　　　　（会议组织方）"))
        blocks.append(("p", f"　　　　　　　　　　　　　　　　　　　　　　{time.strftime('%Y年%m月%d日')}"))
        path = self.ctx.docs.docx(f"关于召开{title}的通知", blocks, stem=f"会议通知-{title}", name=name)
        return path

    # -- 纪要整理 ------------------------------------------------------------
    def minutes(
        self,
        title: str,
        notes: str,
        *,
        when: str = "",
        place: str = "",
        attendees: Sequence[str] = (),
        name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """把原始记录整理成结构化纪要 → docx，并抽取待办入库。

        返回 ``{"path": ..., "decisions": [...], "todos": [...]}``。
        """
        result = extract_minutes(notes, attendees)
        meeting_id = self.ctx.store.insert(
            "INSERT INTO meetings (title, when_text, place, attendees, agenda, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [title, when, place, "、".join(attendees), "（纪要）", time.strftime("%Y-%m-%d %H:%M:%S")],
        )
        for todo in result["todos"]:
            self.ctx.store.insert(
                "INSERT INTO meeting_todos (meeting_id, item, owner, due, created_at) VALUES (?, ?, ?, ?, ?)",
                [meeting_id, todo["item"], todo["owner"], todo["due"], time.strftime("%Y-%m-%d %H:%M:%S")],
            )

        blocks: List[Tuple[str, Any]] = [
            ("p", f"会议时间：{when or '（未提供）'}"),
            ("p", f"会议地点：{place or '（未提供）'}"),
            ("p", f"参会人员：{'、'.join(attendees) if attendees else '（未提供）'}"),
            ("h", "一、议定事项"),
        ]
        blocks.extend([("bullets", list(result["decisions"]))]
                      if result["decisions"] else [("p", "　　（本次记录未包含明确决议）")])
        blocks.append(("h", "二、讨论要点"))
        blocks.extend([("bullets", list(result["discussion"][:8]))]
                      if result["discussion"] else [("p", "　　（无）")])
        blocks.append(("h", "三、待办事项"))
        if result["todos"]:
            rows = [[i, t["item"], t["owner"], t["due"]] for i, t in enumerate(result["todos"], 1)]
            blocks.append(("table", (["序号", "事项", "责任人", "期限"], rows, [700, 4200, 1300, 1500])))
        else:
            blocks.append(("p", "　　（未识别到明确待办）"))
        blocks.append(("p", "（本纪要由办公助手按规则自动整理，请与会人员核对后定稿）"))

        # waterfall 钩子：其它插件可在渲染前增删段落（此处演示扩展点）。
        blocks = self.ctx.waterfall("meeting/minutes/blocks", blocks, lambda: blocks)

        path = self.ctx.docs.docx(f"{title}会议纪要", blocks, stem=f"会议纪要-{title}", name=name)
        return {"path": path, "decisions": result["decisions"], "todos": result["todos"]}

    # -- 待办跟踪 ------------------------------------------------------------
    def open_todos(self) -> List[Dict[str, Any]]:
        """列出未完成的待办（跨会议）。"""
        return self.ctx.store.query(
            "SELECT t.id, t.item, t.owner, t.due, m.title AS meeting"
            " FROM meeting_todos t JOIN meetings m ON m.id = t.meeting_id"
            " WHERE t.done = 0 ORDER BY t.id"
        )

    def close_todo(self, todo_id: int) -> None:
        self.ctx.store.execute("UPDATE meeting_todos SET done = 1 WHERE id = ?", [todo_id])
