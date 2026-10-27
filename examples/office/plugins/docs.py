"""文档输出服务：把结构化数据渲染成 .xlsx / .docx。

- 仅依赖标准库（zipfile + XML），与 survey 示例的 docx 写出器同一路子，
  不引入 openpyxl / python-docx；
- 注册为 ``docs`` 服务，供 tables / ledger / meetings 等业务插件注入。

cordis 概念映射：
====================  =========================================================
``Service``           ``Docs``：构造即 ``ctx.provide("docs", self)``
``inject``            tables / ledger / meetings 声明依赖，未就绪时自动 PENDING
``effect``            每个输出文件的落盘都登记为 effect（卸载时可回收）
====================  =========================================================
"""

from __future__ import annotations

import os
import re
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from cordis_py import Service


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _esc(text: Any) -> str:
    """XML 文本转义（含控制字符剔除）。"""
    s = str(text)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _col_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA。"""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _cell_ref(row: int, col: int) -> str:
    return f"{_col_letter(col)}{row}"


def _display_width(value: Any) -> int:
    """估算显示宽度：中日韩字符按 2 个字符宽度算。"""
    width = 0
    for ch in str(value):
        width += 2 if ord(ch) > 0x2E80 else 1
    return width


def _safe_name(filename: str) -> str:
    """把标题裁剪成安全的文件名片段。"""
    name = re.sub(r'[\\/:*?"<>|\s]+', "-", str(filename)).strip("-")
    return name[:60] or "输出"


# ---------------------------------------------------------------------------
# 极简 xlsx 写出器
# ---------------------------------------------------------------------------

_CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
{sheets}
</Types>"""

_SHEET_OVERRIDE = ('<Override PartName="/xl/worksheets/sheet{idx}.xml" '
                   'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets>{sheets}</sheets>
</workbook>"""

_WORKBOOK_SHEET = '<sheet name="{name}" sheetId="{idx}" r:id="rId{idx}"/>'

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
{rels}
<Relationship Id="rIdS" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

_WORKBOOK_REL_ITEM = ('<Relationship Id="rId{idx}" '
                      'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                      'Target="worksheets/sheet{idx}.xml"/>')

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2">
<font><sz val="11"/><name val="等线"/></font>
<font><b/><sz val="11"/><name val="等线"/></font>
</fonts>
<fills count="3">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFDCE6F1"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _sheet_xml(header: Sequence[Any], rows: Sequence[Sequence[Any]], widths: Sequence[int]) -> str:
    cols = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
        for i, w in enumerate(widths)
    )
    parts: List[str] = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']
    if cols:
        parts.append(f"<cols>{cols}</cols>")
    parts.append("<sheetData>")

    def _cell(row_no: int, col_no: int, value: Any, style: int = 0) -> str:
        ref = _cell_ref(row_no, col_no)
        s = f' s="{style}"' if style else ""
        if isinstance(value, bool):
            value = int(value)
        if isinstance(value, (int, float)):
            return f'<c r="{ref}"{s}><v>{value}</v></c>'
        text = _esc(value)
        return f'<c r="{ref}"{s} t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'

    row_no = 1
    if header:
        cells = "".join(_cell(row_no, i, h, style=1) for i, h in enumerate(header))
        parts.append(f'<row r="{row_no}">{cells}</row>')
        row_no += 1
    for row in rows:
        cells = "".join(_cell(row_no, i, v) for i, v in enumerate(row))
        parts.append(f'<row r="{row_no}">{cells}</row>')
        row_no += 1
    parts.append("</sheetData></worksheet>")
    return "".join(parts)


def write_xlsx_file(path: Path, sheets: Sequence[Dict[str, Any]]) -> Path:
    """把若干张表写进一个 xlsx 工作簿。

    每张表： ``{"name": 工作表名, "header": [...], "rows": [[...], ...]}``。
    表头自动加粗并填充底色，列宽按内容估算。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    used_names = set()
    sheet_defs: List[Dict[str, Any]] = []
    for i, spec in enumerate(sheets, start=1):
        name = str(spec.get("name") or f"Sheet{i}")
        name = re.sub(r"[\[\]:*?/\\]", "-", name)[:28] or f"Sheet{i}"
        base, n = name, 2
        while name in used_names:
            name = f"{base}{n}"
            n += 1
        used_names.add(name)
        sheet_defs.append({
            "idx": i,
            "name": name,
            "header": list(spec.get("header") or []),
            "rows": [list(r) for r in (spec.get("rows") or [])],
        })

    overrides = "".join(_SHEET_OVERRIDE.format(idx=s["idx"]) for s in sheet_defs)
    workbook_sheets = "".join(
        _WORKBOOK_SHEET.format(name=_esc(s["name"]), idx=s["idx"]) for s in sheet_defs
    )
    workbook_rels = "".join(_WORKBOOK_REL_ITEM.format(idx=s["idx"]) for s in sheet_defs)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT.format(sheets=overrides))
        z.writestr("_rels/.rels", _RELS)
        z.writestr("xl/workbook.xml", _WORKBOOK.format(sheets=workbook_sheets))
        z.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS.format(rels=workbook_rels))
        z.writestr("xl/styles.xml", _STYLES)
        for spec in sheet_defs:
            all_rows = ([spec["header"]] if spec["header"] else []) + spec["rows"]
            ncols = max((len(r) for r in all_rows), default=0)
            widths: List[int] = []
            for c in range(ncols):
                widest = max((_display_width(r[c]) for r in all_rows if c < len(r)), default=8)
                widths.append(max(8, min(widest + 2, 60)))
            z.writestr(f"xl/worksheets/sheet{spec['idx']}.xml",
                       _sheet_xml(spec["header"], spec["rows"], widths))
    return path


# ---------------------------------------------------------------------------
# 极简 docx 写出器
# ---------------------------------------------------------------------------

_DOCX_CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

_DOCX_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""


def _run(text: str, *, size: int = 24, bold: bool = False, font: str = "仿宋") -> str:
    b = "<w:b/>" if bold else ""
    return (
        f'<w:r><w:rPr><w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" '
        f'w:eastAsia="{font}"/>{b}<w:sz w:val="{size}"/></w:rPr>'
        f'<w:t xml:space="preserve">{_esc(text)}</w:t></w:r>'
    )


def _para(text: str, *, size: int = 24, bold: bool = False, font: str = "仿宋",
          align: Optional[str] = None, spacing: int = 120) -> str:
    jc = f'<w:jc w:val="{align}"/>' if align else ""
    return (
        f'<w:p><w:pPr><w:spacing w:after="{spacing}"/>{jc}</w:pPr>'
        f'{_run(text, size=size, bold=bold, font=font)}</w:p>'
    )


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]], widths: Sequence[int]) -> str:
    borders = "".join(
        f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="7F7F7F"/>'
        for side in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)

    def _cell(text: Any, w: int, bold: bool = False) -> str:
        return (
            f'<w:tc><w:tcPr><w:tcW w:w="{w}" w:type="dxa"/></w:tcPr>'
            f'{_para(str(text), size=21, bold=bold, spacing=40)}</w:tc>'
        )

    trs = []
    if headers:
        trs.append("<w:tr>" + "".join(_cell(h, w, bold=True) for h, w in zip(headers, widths)) + "</w:tr>")
    for row in rows:
        trs.append("<w:tr>" + "".join(_cell(v, w) for v, w in zip(row, widths)) + "</w:tr>")
    return (
        f'<w:tbl><w:tblPr><w:tblBorders>{borders}</w:tblBorders></w:tblPr>'
        f'<w:tblGrid>{grid}</w:tblGrid>{"".join(trs)}</w:tbl>'
        + _para("", spacing=60)
    )


def write_docx_file(path: Path, title: Optional[str], blocks: Sequence[Tuple[str, Any]]) -> Path:
    """把区块序列写成一个 docx。

    ``blocks`` 支持：
      ``("h", 文本)``            小节标题
      ``("p", 文本)``            正文段落
      ``("bullets", [文本, ...])`` 无序列表
      ``("table", (表头, 数据行))`` 表格
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body: List[str] = []
    if title:
        body.append(_para(title, size=32, bold=True, font="黑体", align="center", spacing=240))
    for kind, content in blocks:
        if kind == "h":
            body.append(_para(str(content), size=26, bold=True, font="黑体", spacing=100))
        elif kind == "p":
            body.append(_para(str(content)))
        elif kind == "bullets":
            for item in content:
                body.append(_para(f"· {item}"))
        elif kind == "table":
            headers, rows, widths = content
            body.append(_table(headers, rows, widths))
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f'<w:body>{"".join(body)}</w:body></w:document>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _DOCX_CT)
        z.writestr("_rels/.rels", _DOCX_RELS)
        z.writestr("word/document.xml", doc)
    return path


# ---------------------------------------------------------------------------
# cordis 服务
# ---------------------------------------------------------------------------

class Docs(Service):
    """文档输出服务（``docs``）。

    配置： ``{"out_dir": 输出目录}``。所有文件落在 ``out_dir`` 下，
    每次写出返回实际路径，业务插件把路径透传给调用方（同事）。
    """

    provide = "docs"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        cfg = config or {}
        self.out_dir = Path(cfg.get("out_dir") or "./out")
        super().__init__(ctx, "docs")
        # effect：目录随插件加载而准备、随卸载而清理登记（演示副作用归属）。
        ctx.effect(lambda: (self.out_dir.mkdir(parents=True, exist_ok=True), lambda: None)[1],
                   "docs.outDir")

    def _path(self, stem: str, suffix: str, name: Optional[str] = None) -> Path:
        if name:
            return self.out_dir / name
        stamp = time.strftime("%H%M%S")
        return self.out_dir / f"{_safe_name(stem)}-{stamp}{suffix}"

    def xlsx(self, sheets: Iterable[Dict[str, Any]], *, stem: str = "表格",
             name: Optional[str] = None) -> Path:
        """sheets 形如 [{"name": 表名, "header": [...], "rows": [...]}]。"""
        return write_xlsx_file(self._path(stem, ".xlsx", name), list(sheets))

    def docx(self, title: Optional[str], blocks: Sequence[Tuple[str, Any]], *,
             stem: str = "文档", name: Optional[str] = None) -> Path:
        return write_docx_file(self._path(stem, ".docx", name), title, list(blocks))
