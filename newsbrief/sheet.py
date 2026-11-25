"""出表层：openpyxl 一次写 5 表 12 列（文档 §7），不做任何业务判断。

版式逐项对齐提供的定稿 xlsx（经 openpyxl 全量样式签名比对确认）：
主表类 3 表列宽 15×7 + 46/62/36 + 15×2、冻结 A2、表头 DDEBF7 浅蓝 + 仅加粗
（不显式指定字体名/字号/颜色）、表头不换行；数据区 Calibri 11、顶端对齐、
自动换行、无填充无边框。口径与稿外信息不冻结、A 列整列加粗、无表头填充；
评级留痕不冻结、只自定义前 4 列宽，表头同主表类。
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .model import COLS_12, SHEETS_5, TRACE_COLS_15

WIDTHS_12 = [15, 15, 15, 15, 15, 15, 15, 46, 62, 36, 15, 15]
WIDTHS_KOUSHI = [52, 92, 28, 28, 28, 28, 28, 28, 28]
WIDTHS_TRACE = [18, 46, 18, 18]            # 其余列保持默认宽度（基准未自定义）
HEAD_FILL = PatternFill("solid", fgColor="DDEBF7")
HEAD_FONT = Font(bold=True)                 # name/size/color 全部继承默认
BODY_ALIGN = Alignment(vertical="top", wrap_text=True)
KOU_FONT = Font(bold=True)                  # A 列项目名整列加粗


def _widths(ws, widths: list[int]) -> None:
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _head(ws, ncols: int) -> None:
    for c in range(1, ncols + 1):
        cell = ws.cell(1, c)
        cell.fill, cell.font = HEAD_FILL, HEAD_FONT
        # alignment 保持默认：基准表头 horizontal/vertical/wrap 均为 None


def _body(ws, nrows: int, ncols: int) -> None:
    for r in range(2, nrows + 1):
        for c in range(1, ncols + 1):
            ws.cell(r, c).alignment = BODY_ALIGN


def build_workbook(batch, path: str | Path, koushi_rows: list[list],
                   trace_rows: list[list]) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws_main = wb.active
    ws_main.title = SHEETS_5[0]
    ws_top = wb.create_sheet(SHEETS_5[1])
    ws_pool = wb.create_sheet(SHEETS_5[2])
    ws_kou = wb.create_sheet(SHEETS_5[3])
    ws_trace = wb.create_sheet(SHEETS_5[4])

    for ws in (ws_main, ws_top, ws_pool):
        ws.append(COLS_12)
    for it in batch.main:
        ws_main.append(it.row(main=True))
    for it in batch.main[:10]:
        ws_top.append(it.row(main=True))
    for it in batch.candidate:
        ws_pool.append(it.row(main=False))

    ws_kou.append(["项目", "内容"])
    # 附录逐条留痕是 6–9 列表格（08 样例口径表 5 列）；不再写死 2 列。
    for row in koushi_rows[1:]:
        ws_kou.append(list(row))
    ws_trace.append(TRACE_COLS_15)
    for r in trace_rows:
        ws_trace.append(r)

    # 主表类 3 表：冻结 A2 + 浅蓝表头 + 正文顶端换行
    for ws in (ws_main, ws_top, ws_pool):
        ws.freeze_panes = "A2"
        _widths(ws, WIDTHS_12)
        _head(ws, 12)
        _body(ws, ws.max_row, 12)

    # 口径与稿外信息：不冻结、5 列全顶端换行。
    # A 列三类：项目名区（前 N 行，仅加粗）/ 章节标题行（B 列空或「——」，
    # 加粗 + DDEBF7 底色）/ 留痕行（常规）。不是整列加粗。
    _widths(ws_kou, WIDTHS_KOUSHI)
    head_rows = 0
    for r in range(1, ws_kou.max_row + 1):
        a = ws_kou.cell(r, 1).value
        b = ws_kou.cell(r, 2).value
        if str(a or "").startswith("——"):
            break
        head_rows = r
    for r in range(1, ws_kou.max_row + 1):
        for c in range(1, 6):
            ws_kou.cell(r, c).alignment = BODY_ALIGN
        a = str(ws_kou.cell(r, 1).value or "")
        b = ws_kou.cell(r, 2).value
        if a.startswith("——"):
            ws_kou.cell(r, 1).font = KOU_FONT
            ws_kou.cell(r, 1).fill = HEAD_FILL
        elif r <= head_rows:
            ws_kou.cell(r, 1).font = KOU_FONT

    # 评级留痕：不冻结、只前 4 列自定义宽、表头浅蓝、正文顶端换行
    _widths(ws_trace, WIDTHS_TRACE)
    _head(ws_trace, 15)
    _body(ws_trace, ws_trace.max_row, 15)

    wb.save(p)
    return p
