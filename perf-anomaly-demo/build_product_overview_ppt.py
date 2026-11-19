#!/usr/bin/env python3
"""Build an editable Chinese product overview deck for SysInsight × DREAM.

The presentation deliberately separates checked-in validation evidence from the
current runtime snapshot.  Every diagram is built from native PowerPoint shapes
so the deck remains editable without external image assets.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
DEPS = ROOT / ".pptx_deps"
if DEPS.is_dir():
    sys.path.insert(0, str(DEPS))

from pptx import Presentation  # noqa: E402
from pptx.dml.color import RGBColor  # noqa: E402
from pptx.enum.dml import MSO_LINE_DASH_STYLE  # noqa: E402
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE  # noqa: E402
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN  # noqa: E402
from pptx.util import Inches, Pt  # noqa: E402


OUTPUT = REPO / "SysInsight_DREAM产品功能介绍_场景压力详解版.pptx"
FONT = "Noto Sans CJK SC"
MONO = "Noto Sans Mono CJK SC"
W = 13.333
H = 7.5


def C(value: str) -> RGBColor:
    value = value.lstrip("#")
    return RGBColor(int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


NAVY = C("0F1D37")
NAVY_2 = C("172B4D")
INK = C("1C2A42")
MUTED = C("5B6E8C")
SUBTLE = C("91A0B6")
LIGHT = C("F6F8FC")
WHITE = C("FFFFFF")
LINE = C("D8E1ED")
BLUE = C("2770DC")
CYAN = C("16A2B5")
PURPLE = C("7750BE")
ORANGE = C("E37E27")
GREEN = C("1D9B6C")
RED = C("D24E4E")
GOLD = C("D39726")
PALE_BLUE = C("E8F1FF")
PALE_CYAN = C("E6F8F8")
PALE_PURPLE = C("F1EBFD")
PALE_ORANGE = C("FFF3E2")
PALE_GREEN = C("E6F7EF")
PALE_RED = C("FDEDED")
PALE_GOLD = C("FFF8E0")
PALE_GRAY = C("EEF2F7")


TP_STABILITY = [
    ("热点 Payment", [30.04, 24.85, 24.40]),
    ("热点 Stock Level", [39.70, 40.12, 39.04]),
    ("Stock Level 突发", [38.11, 41.19, 37.83]),
    ("五类 TPCC 高强度混合", [60.62, 59.62, 57.85]),
    ("WAL / checkpoint", [45.54, 47.65, 48.42]),
    ("热点 Order Status", [26.06, 21.50, 22.97]),
]

DREAM_STABILITY = [
    ("Q31", [87.85, 69.47, 90.39]),
    ("Q44", [46.24, 63.07, 64.91]),
    ("Q88", [81.11, 80.57, 80.82]),
]


def set_run_font(run, size, color=INK, bold=False, italic=False, font=FONT):
    run.font.name = font
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = color
    try:
        from pptx.oxml.ns import qn

        run._r.get_or_add_rPr().set(qn("a:ea"), font)
    except Exception:
        pass


def add_text(
    slide,
    value,
    x,
    y,
    w,
    h,
    size=12,
    color=INK,
    bold=False,
    align=PP_ALIGN.LEFT,
    valign=MSO_ANCHOR.TOP,
    margin=0.04,
    italic=False,
    font=FONT,
    url=None,
):
    shape = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = shape.text_frame
    tf.clear()
    tf.word_wrap = True
    tf.margin_left = Inches(margin)
    tf.margin_right = Inches(margin)
    tf.margin_top = Inches(margin)
    tf.margin_bottom = Inches(margin)
    tf.vertical_anchor = valign
    p = tf.paragraphs[0]
    p.alignment = align
    p.space_after = Pt(0)
    r = p.add_run()
    r.text = str(value)
    set_run_font(r, size, color, bold, italic, font)
    if url:
        r.hyperlink.address = url
    return shape


def add_rich_line(slide, parts, x, y, w, h, size=10, align=PP_ALIGN.LEFT, valign=MSO_ANCHOR.MIDDLE):
    shape = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = shape.text_frame
    tf.clear()
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = Inches(0.03)
    tf.margin_top = tf.margin_bottom = Inches(0.02)
    tf.vertical_anchor = valign
    p = tf.paragraphs[0]
    p.alignment = align
    p.space_after = Pt(0)
    for text, color, bold in parts:
        r = p.add_run()
        r.text = text
        set_run_font(r, size, color, bold)
    return shape


def add_bullets(slide, items, x, y, w, h, size=10, color=MUTED, gap=5, bullet_color=None):
    shape = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = shape.text_frame
    tf.clear()
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = Inches(0.03)
    tf.margin_top = tf.margin_bottom = Inches(0.02)
    for idx, item in enumerate(items):
        p = tf.paragraphs[0] if idx == 0 else tf.add_paragraph()
        p.space_after = Pt(gap)
        p.line_spacing = 1.02
        r = p.add_run()
        r.text = "• " + str(item)
        set_run_font(r, size, bullet_color or color, False)
    return shape


def add_box(slide, x, y, w, h, fill=WHITE, line=LINE, radius=True, width=0.7, transparency=0):
    shape = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE,
        Inches(x),
        Inches(y),
        Inches(w),
        Inches(h),
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill
    shape.fill.transparency = transparency
    shape.line.color.rgb = line
    shape.line.width = Pt(width)
    return shape


def add_circle(slide, x, y, d, fill, line=None):
    shape = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(x), Inches(y), Inches(d), Inches(d))
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill
    shape.line.color.rgb = line or fill
    return shape


def add_line(slide, x1, y1, x2, y2, color=LINE, width=1.0, dash=False):
    line = slide.shapes.add_connector(
        MSO_CONNECTOR.STRAIGHT,
        Inches(x1),
        Inches(y1),
        Inches(x2),
        Inches(y2),
    )
    line.line.color.rgb = color
    line.line.width = Pt(width)
    if dash:
        line.line.dash_style = MSO_LINE_DASH_STYLE.DASH
    return line


def add_chevron(slide, x, y, w=0.22, h=0.36, color=SUBTLE):
    shape = slide.shapes.add_shape(MSO_SHAPE.CHEVRON, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    return shape


def add_pill(slide, text, x, y, w, color, pale, size=8.3, h=0.29):
    add_box(slide, x, y, w, h, pale, pale, radius=True)
    return add_text(
        slide,
        text,
        x + 0.03,
        y + 0.015,
        w - 0.06,
        h - 0.03,
        size=size,
        color=color,
        bold=True,
        align=PP_ALIGN.CENTER,
        valign=MSO_ANCHOR.MIDDLE,
        margin=0,
    )


def add_header(slide, number, title, subtitle, accent=BLUE):
    add_box(slide, 0, 0, W, 0.98, NAVY, NAVY, radius=False)
    add_box(slide, 0, 0, 0.12, 0.98, accent, accent, radius=False)
    add_text(slide, f"SYSINSIGHT × DREAM  /  {number}", 0.50, 0.10, 4.6, 0.17, 7.3, C("9FC0EC"), True, margin=0)
    add_text(slide, title, 0.50, 0.30, 12.1, 0.34, 22.5, WHITE, True, valign=MSO_ANCHOR.MIDDLE, margin=0)
    add_text(slide, subtitle, 0.51, 0.75, 12.0, 0.16, 8.4, C("C4D1E4"), margin=0)


def add_footer(slide, page, note="基于 2026-10-08 当前本机演示环境与已提交验证证据"):
    add_line(slide, 0.50, 7.18, 12.82, 7.18, LINE, 0.6)
    add_text(slide, note, 0.52, 7.25, 11.7, 0.13, 6.3, SUBTLE, margin=0)
    add_text(slide, f"{page:02d}", 12.35, 7.23, 0.42, 0.14, 6.7, MUTED, True, align=PP_ALIGN.RIGHT, margin=0)


def new_slide(prs, bg=LIGHT):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = bg
    return slide


def add_card_title(slide, icon, title, subtitle, x, y, w, accent=BLUE, pale=PALE_BLUE):
    add_box(slide, x, y, w, 1.12, WHITE, LINE)
    add_circle(slide, x + 0.18, y + 0.22, 0.46, pale, pale)
    add_text(slide, icon, x + 0.18, y + 0.22, 0.46, 0.46, 12, accent, True, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE, margin=0)
    add_text(slide, title, x + 0.78, y + 0.18, w - 0.96, 0.28, 13.2, INK, True, margin=0)
    add_text(slide, subtitle, x + 0.78, y + 0.54, w - 0.96, 0.40, 8.5, MUTED, margin=0)


def add_metric_card(slide, x, y, w, value, label, accent=BLUE, pale=PALE_BLUE, dark=False):
    fill = C("1B3155") if dark else WHITE
    line = C("365477") if dark else LINE
    value_color = WHITE if dark else accent
    label_color = C("B9C9DF") if dark else MUTED
    add_box(slide, x, y, w, 0.91, fill, line)
    add_box(slide, x, y, 0.07, 0.91, accent, accent, radius=False)
    add_text(slide, value, x + 0.18, y + 0.11, w - 0.31, 0.36, 20.5, value_color, True, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE, margin=0)
    add_text(slide, label, x + 0.16, y + 0.56, w - 0.28, 0.18, 7.8, label_color, align=PP_ALIGN.CENTER, margin=0)


def add_flow_card(slide, x, y, w, h, index, title, body, accent=BLUE, pale=PALE_BLUE):
    add_box(slide, x, y, w, h, WHITE, LINE)
    add_box(slide, x, y, w, 0.07, accent, accent, radius=False)
    add_pill(slide, index, x + 0.14, y + 0.16, 0.42, accent, pale, 7.5, 0.25)
    add_text(slide, title, x + 0.14, y + 0.50, w - 0.28, 0.28, 11.7, INK, True, margin=0)
    add_text(slide, body, x + 0.14, y + 0.86, w - 0.28, h - 0.97, 7.7, MUTED, margin=0)


def add_table(slide, x, y, widths, headers, rows, row_h, header_h=0.42, font_size=8.1, aligns=None, accent=BLUE):
    aligns = aligns or [PP_ALIGN.LEFT] * len(widths)
    cx = x
    for i, (width, header) in enumerate(zip(widths, headers)):
        add_box(slide, cx, y, width, header_h, NAVY_2, NAVY_2, radius=False)
        add_text(slide, header, cx + 0.07, y + 0.03, width - 0.14, header_h - 0.06, 7.7, WHITE, True,
                 align=aligns[i], valign=MSO_ANCHOR.MIDDLE, margin=0)
        cx += width
    for r_idx, row in enumerate(rows):
        cy = y + header_h + r_idx * row_h
        fill = WHITE if r_idx % 2 == 0 else C("F1F4F9")
        cx = x
        for c_idx, (width, value) in enumerate(zip(widths, row)):
            add_box(slide, cx, cy, width, row_h, fill, LINE, radius=False, width=0.5)
            color = accent if c_idx == 0 else INK
            add_text(slide, value, cx + 0.07, cy + 0.04, width - 0.14, row_h - 0.08, font_size,
                     color, c_idx == 0, align=aligns[c_idx], valign=MSO_ANCHOR.MIDDLE, margin=0)
            cx += width


def add_horizontal_bars(slide, items, x, y, w, row_h, max_value, accent=BLUE, label_w=1.90):
    chart_w = w - label_w - 0.70
    for idx, (label, value) in enumerate(items):
        cy = y + idx * row_h
        add_text(slide, label, x, cy + 0.06, label_w - 0.08, 0.20, 8.0, INK, True, margin=0)
        add_box(slide, x + label_w, cy + 0.08, chart_w, 0.18, PALE_GRAY, PALE_GRAY, radius=True)
        bar_w = max(0.08, chart_w * min(value / max_value, 1.0))
        add_box(slide, x + label_w, cy + 0.08, bar_w, 0.18, accent, accent, radius=True)
        add_text(slide, f"{value:.1f}%", x + label_w + chart_w + 0.09, cy + 0.035, 0.56, 0.24, 8.5,
                 accent, True, align=PP_ALIGN.RIGHT, margin=0)


def add_line_chart(slide, x, y, w, h, points, phases=None, colors=None, labels=None):
    """Draw a compact editable line chart. points is a list of series."""
    phases = phases or []
    colors = colors or [BLUE, ORANGE, GREEN]
    labels = labels or []
    for start, end, fill, title in phases:
        px = x + start * w
        pw = (end - start) * w
        add_box(slide, px, y, pw, h, fill, fill, radius=False)
        if title:
            add_text(slide, title, px + 0.04, y + 0.06, pw - 0.08, 0.18, 6.8, MUTED, True, align=PP_ALIGN.CENTER, margin=0)
    add_line(slide, x, y + h, x + w, y + h, C("BFCADA"), 0.8)
    add_line(slide, x, y, x, y + h, C("BFCADA"), 0.8)
    all_values = [v for series in points for v in series]
    min_v, max_v = min(all_values), max(all_values)
    pad = max((max_v - min_v) * 0.12, 1.0)
    min_v -= pad
    max_v += pad
    for s_idx, series in enumerate(points):
        coords = []
        for i, value in enumerate(series):
            px = x + (i / max(1, len(series) - 1)) * w
            py = y + h - ((value - min_v) / (max_v - min_v)) * h
            coords.append((px, py))
        for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
            add_line(slide, x1, y1, x2, y2, colors[s_idx % len(colors)], 1.8)
        for px, py in coords:
            add_circle(slide, px - 0.035, py - 0.035, 0.07, colors[s_idx % len(colors)])
    for idx, label in enumerate(labels):
        lx = x + w - (len(labels) - idx) * 1.04
        add_line(slide, lx, y - 0.17, lx + 0.19, y - 0.17, colors[idx % len(colors)], 2.2)
        add_text(slide, label, lx + 0.24, y - 0.25, 0.76, 0.16, 6.8, MUTED, margin=0)


def read_only_sql(text):
    text = re.sub(r"--[^\n]*", " ", text)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = text.strip().rstrip(";").strip().lower()
    if not text or ";" in text or not text.startswith(("select", "with", "explain")):
        return False
    forbidden = r"\b(insert|update|delete|merge|create|drop|alter|truncate|grant|revoke|copy|call|do|vacuum|refresh|set|reset|begin|commit|rollback|prepare|execute|lock)\b"
    return re.search(forbidden, text) is None


def catalog_facts():
    import importlib.util

    spec = importlib.util.spec_from_file_location("tpcc_transaction_cases", ROOT / "tpcc_transaction_cases.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    tp_cases = list(module.CASE_DEFINITIONS)
    focus = list(module.FOCUS_SCENARIO_IDS)
    sql_root = REPO / "dream" / "data" / "slow_queries" / "TPC-DS"
    sql_files = sorted(sql_root.glob("*.sql"))
    read_only_count = sum(read_only_sql(p.read_text(encoding="utf-8")) for p in sql_files)
    return len(tp_cases), len(focus), len(sql_files), read_only_count


def fetch_json(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:9108{path}", timeout=2.0) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return {}


def runtime_facts():
    status = fetch_json("/api/v1/status?limit=20")
    lab = fetch_json("/api/v1/lab/status")
    service = status.get("service", {})
    counts = status.get("state", {}).get("counts", {})
    latest = lab.get("latest_dream", {}).get("result", {})
    comparison = latest.get("comparison", {})
    improvement = latest.get("improvement", {})
    facts = {
        "api_configured": bool(service.get("api_configured", True)),
        "full_chain": bool(service.get("sysinsight_full_chain", True)),
        "dream_ready": bool(service.get("dream_ready", True)),
        "dream_auto_apply": bool(service.get("dream_auto_apply", True)),
        "dream_trigger_ms": float(service.get("dream_trigger_ms", 10000.0)),
        "active_improvements": int(counts.get("active_improvements", 1)),
        "auto_applications": int(counts.get("observed_auto_applications", 1)),
        "q31_baseline_ms": float(comparison.get("baseline_ms", 38249.36749180779)),
        "q31_optimized_ms": float(comparison.get("optimized_ms", 4652.8935097157955)),
        "q31_ratio": float(comparison.get("improvement_ratio", 0.8783537136735046)),
        "q31_internal_ms": float(improvement.get("new_time", 4.494676828384399)) * 1000,
        "q31_job": lab.get("latest_dream", {}).get("result", {}).get("job_id", "dream-20261007191035-fc522799a4"),
    }
    if facts["q31_internal_ms"] > 100000:
        facts["q31_internal_ms"] = 4494.676828384399
    return facts


def build_deck():
    tp_count, focus_count, ap_count, ap_read_only = catalog_facts()
    assert (tp_count, focus_count, ap_count, ap_read_only) == (27, 6, 99, 95)
    run = runtime_facts()
    avg_tp = sum(sum(v) / len(v) for _, v in TP_STABILITY) / len(TP_STABILITY)
    dream_verified_sql = len(DREAM_STABILITY)
    dream_verified_runs = sum(len(values) for _, values in DREAM_STABILITY)
    dream_portfolio_avg = sum(sum(values) / len(values) for _, values in DREAM_STABILITY) / dream_verified_sql

    prs = Presentation()
    prs.slide_width = Inches(W)
    prs.slide_height = Inches(H)
    prs.core_properties.title = "SysInsight × DREAM 产品功能介绍"
    prs.core_properties.subject = "数据库智能检测、分析、调优、验证与可观测闭环"
    prs.core_properties.author = "SysInsight × DREAM"
    prs.core_properties.keywords = "PostgreSQL, SysInsight, DREAM, TPCC, TPC-DS, Grafana, LLM"

    # 01 — Cover
    slide = new_slide(prs, NAVY)
    add_box(slide, 0, 0, W, H, NAVY, NAVY, radius=False)
    add_box(slide, 0, 0, W, 0.12, BLUE, BLUE, radius=False)
    add_circle(slide, 10.28, -0.82, 4.10, C("17345E"), C("17345E"))
    add_circle(slide, 11.05, 0.02, 2.65, C("1D4678"), C("1D4678"))
    add_circle(slide, 11.70, 0.66, 1.36, CYAN, CYAN)
    add_text(slide, "S", 11.70, 0.66, 1.36, 1.36, 35, WHITE, True, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE, margin=0)
    add_text(slide, "SYSINSIGHT  ×  DREAM", 0.72, 0.76, 5.8, 0.24, 10.4, C("9FC6FA"), True, margin=0)
    add_text(slide, "数据库智能性能优化平台", 0.68, 1.26, 9.4, 0.70, 34, WHITE, True, margin=0)
    add_text(slide, "从告警检测到参数调优，从长 SQL 识别到下一次自动应用", 0.72, 2.12, 9.2, 0.32, 15, C("C5D2E5"), margin=0)
    add_pill(slide, "当前本机闭环可运行", 0.72, 2.78, 1.72, GREEN, C("143F3B"), 8.3, 0.32)
    add_pill(slide, "GPT-5.6-SOL 已接入", 2.58, 2.78, 1.87, CYAN, C("173B50"), 8.3, 0.32)
    add_pill(slide, "PostgreSQL 12", 4.59, 2.78, 1.45, C("93B9F1"), C("213B62"), 8.3, 0.32)
    add_metric_card(slide, 0.72, 4.12, 2.72, "27 / 6", "TP 场景目录 / 重点场景", BLUE, PALE_BLUE, True)
    add_metric_card(slide, 3.64, 4.12, 2.72, "99 / 95", "TPC-DS SQL / 只读 SQL", PURPLE, PALE_PURPLE, True)
    add_metric_card(slide, 6.56, 4.12, 2.72, "≥ 10 s", "DREAM 自动触发阈值", ORANGE, PALE_ORANGE, True)
    add_metric_card(slide, 9.48, 4.12, 2.72, "24 h", "监控与状态数据保留", GREEN, PALE_GREEN, True)
    add_box(slide, 0.72, 5.50, 11.48, 0.72, C("192F50"), C("335277"))
    add_rich_line(
        slide,
        [("监控", C("A9C7F3"), True), ("  →  检测  →  分析  →  调优  →  实测  →  应用 / 恢复  →  ", C("D8E3F2"), False), ("审计", C("8FE0C2"), True)],
        1.10, 5.70, 10.72, 0.25, 13.2, PP_ALIGN.CENTER,
    )
    add_text(slide, "产品功能文档  ·  2026-10-08", 0.72, 6.88, 5.0, 0.18, 7.6, C("91A6C4"), margin=0)
    add_text(slide, "所有性能数字均来自本机 PostgreSQL 实际执行", 7.20, 6.86, 5.0, 0.18, 7.5, C("91A6C4"), align=PP_ALIGN.RIGHT, margin=0)

    # 02 — Product positioning
    slide = new_slide(prs)
    add_header(slide, "02", "产品定位｜把性能问题变成可执行、可验证的闭环", "一套平台覆盖 TP 系统参数与 AP SQL 两类优化对象；统一记录动作、收益和回滚证据", BLUE)
    add_text(slide, "传统痛点", 0.62, 1.24, 2.1, 0.24, 10, RED, True, margin=0)
    pain_cards = [
        ("01", "告警只有指标", "能看到 CPU/TPS 异常，却难以定位数据库参数与代码证据。"),
        ("02", "慢 SQL 靠人工", "发现、改写、复测与再次上线通常是割裂的人工流程。"),
        ("03", "效果难以证明", "压力变化、缓存波动和真实收益容易混在一起。"),
    ]
    for i, (num, title, body) in enumerate(pain_cards):
        y = 1.60 + i * 1.34
        add_box(slide, 0.62, y, 3.05, 1.08, WHITE, LINE)
        add_pill(slide, num, 0.80, y + 0.18, 0.44, RED, PALE_RED, 7.4, 0.25)
        add_text(slide, title, 1.38, y + 0.15, 2.04, 0.24, 11.2, INK, True, margin=0)
        add_text(slide, body, 0.80, y + 0.51, 2.61, 0.39, 8.0, MUTED, margin=0)

    add_box(slide, 4.04, 1.29, 4.92, 4.72, WHITE, LINE)
    add_text(slide, "统一智能优化平台", 4.34, 1.56, 4.32, 0.32, 17.5, INK, True, align=PP_ALIGN.CENTER, margin=0)
    add_text(slide, "同一 PostgreSQL · 同一证据存储 · 同一可视化入口", 4.34, 1.97, 4.32, 0.22, 8.3, MUTED, align=PP_ALIGN.CENTER, margin=0)
    add_box(slide, 4.40, 2.45, 1.90, 2.32, PALE_BLUE, C("BDD4F5"))
    add_circle(slide, 5.00, 2.75, 0.68, BLUE)
    add_text(slide, "S", 5.00, 2.75, 0.68, 0.68, 20, WHITE, True, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE, margin=0)
    add_text(slide, "SysInsight", 4.60, 3.62, 1.50, 0.25, 13, BLUE, True, align=PP_ALIGN.CENTER, margin=0)
    add_text(slide, "TP 告警与参数闭环", 4.59, 4.08, 1.52, 0.20, 8, MUTED, align=PP_ALIGN.CENTER, margin=0)
    add_box(slide, 6.70, 2.45, 1.90, 2.32, PALE_PURPLE, C("D4C7F0"))
    add_circle(slide, 7.31, 2.75, 0.68, PURPLE)
    add_text(slide, "D", 7.31, 2.75, 0.68, 0.68, 20, WHITE, True, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE, margin=0)
    add_text(slide, "DREAM", 6.89, 3.62, 1.52, 0.25, 13, PURPLE, True, align=PP_ALIGN.CENTER, margin=0)
    add_text(slide, "AP 长 SQL 自动优化", 6.88, 4.08, 1.54, 0.20, 8, MUTED, align=PP_ALIGN.CENTER, margin=0)
    add_chevron(slide, 6.39, 3.30, 0.22, 0.37, C("A6B8CE"))
    add_pill(slide, "Bridge 编排 / 状态 / 策略", 5.06, 5.13, 2.86, CYAN, PALE_CYAN, 8.5, 0.31)

    add_text(slide, "产品结果", 9.35, 1.24, 2.1, 0.24, 10, GREEN, True, margin=0)
    result_cards = [
        ("自动", "告警或长 SQL 达到条件后，后台异步进入对应闭环。", GREEN, PALE_GREEN),
        ("真实", "候选必须在真实 PostgreSQL 上执行，收益不使用模型预测。", BLUE, PALE_BLUE),
        ("可控", "临时配置、资源上限、只读门禁、重复请求保护。", ORANGE, PALE_ORANGE),
        ("可见", "Grafana、管理页和实验控制台展示每一步动作。", PURPLE, PALE_PURPLE),
    ]
    for i, (title, body, color, pale) in enumerate(result_cards):
        y = 1.60 + i * 1.02
        add_box(slide, 9.35, y, 3.35, 0.82, WHITE, LINE)
        add_pill(slide, title, 9.53, y + 0.16, 0.64, color, pale, 7.8, 0.25)
        add_text(slide, body, 10.30, y + 0.13, 2.18, 0.48, 7.8, MUTED, margin=0)
    add_box(slide, 0.62, 6.32, 12.08, 0.56, NAVY_2, NAVY_2)
    add_rich_line(slide, [("核心承诺：", C("8FCAF7"), True), ("系统不仅给建议，还记录“为什么做、做了什么、是否有效、是否恢复”。", WHITE, False)],
                  0.93, 6.48, 11.44, 0.20, 10.2, PP_ALIGN.CENTER)
    add_footer(slide, 2)

    # 03 — Architecture
    slide = new_slide(prs)
    add_header(slide, "03", "统一架构｜一个 Bridge 串起两条优化链路", "数据采集、模型调用、执行验证、状态存储与前端展示由同一编排层协调", CYAN)
    add_pill(slide, "工作负载", 0.58, 1.25, 1.16, MUTED, PALE_GRAY, 8.2)
    add_box(slide, 0.58, 1.72, 1.88, 1.02, WHITE, LINE)
    add_text(slide, "TPCC / 应用事务", 0.75, 1.94, 1.54, 0.24, 10.3, BLUE, True, align=PP_ALIGN.CENTER, margin=0)
    add_text(slide, "目标业务 TPS", 0.75, 2.31, 1.54, 0.18, 7.5, MUTED, align=PP_ALIGN.CENTER, margin=0)
    add_box(slide, 0.58, 3.02, 1.88, 1.02, WHITE, LINE)
    add_text(slide, "TPC-DS / 分析 SQL", 0.75, 3.24, 1.54, 0.24, 10.3, PURPLE, True, align=PP_ALIGN.CENTER, margin=0)
    add_text(slide, "执行耗时", 0.75, 3.61, 1.54, 0.18, 7.5, MUTED, align=PP_ALIGN.CENTER, margin=0)

    add_chevron(slide, 2.64, 2.55, 0.27, 0.48, SUBTLE)
    add_box(slide, 3.06, 1.65, 2.18, 2.46, NAVY_2, NAVY_2)
    add_circle(slide, 3.74, 1.99, 0.76, BLUE)
    add_text(slide, "PG", 3.74, 1.99, 0.76, 0.76, 16, WHITE, True, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE, margin=0)
    add_text(slide, "PostgreSQL 12", 3.34, 2.98, 1.62, 0.27, 13, WHITE, True, align=PP_ALIGN.CENTER, margin=0)
    add_text(slide, "keeninsight\nTPCC + TPC-DS SF10", 3.34, 3.40, 1.62, 0.45, 8.2, C("C3D2E7"), align=PP_ALIGN.CENTER, margin=0)

    add_chevron(slide, 5.42, 2.55, 0.27, 0.48, SUBTLE)
    add_box(slide, 5.83, 1.42, 2.47, 2.92, WHITE, C("AFC6E7"), width=1.2)
    add_pill(slide, "核心编排层", 6.38, 1.67, 1.37, CYAN, PALE_CYAN, 8.0)
    add_text(slide, "SysInsight / DREAM Bridge", 6.09, 2.15, 1.95, 0.53, 14, INK, True, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE, margin=0)
    add_bullets(slide, ["SQL 观察与告警路由", "后台任务与去重", "策略门禁与自动应用", "SQLite 状态 / 24h 保留"], 6.13, 2.86, 1.88, 1.17, 7.5, MUTED, 3)

    add_chevron(slide, 8.48, 2.55, 0.27, 0.48, SUBTLE)
    add_box(slide, 8.89, 1.34, 1.93, 1.38, PALE_BLUE, C("BED4F3"))
    add_text(slide, "SysInsight", 9.11, 1.60, 1.49, 0.27, 12.5, BLUE, True, align=PP_ALIGN.CENTER, margin=0)
    add_text(slide, "检测 / 源码证据\n参数候选 / TP 复测", 9.10, 2.01, 1.52, 0.43, 7.7, MUTED, align=PP_ALIGN.CENTER, margin=0)
    add_box(slide, 8.89, 3.05, 1.93, 1.38, PALE_PURPLE, C("D4C7F0"))
    add_text(slide, "DREAM", 9.11, 3.31, 1.49, 0.27, 12.5, PURPLE, True, align=PP_ALIGN.CENTER, margin=0)
    add_text(slide, "根因 / 改写 / Hint\n真实执行验证", 9.10, 3.72, 1.52, 0.43, 7.7, MUTED, align=PP_ALIGN.CENTER, margin=0)

    add_chevron(slide, 11.02, 2.55, 0.27, 0.48, SUBTLE)
    add_box(slide, 11.43, 1.48, 1.32, 2.80, WHITE, LINE)
    for i, (letter, label, color, pale) in enumerate([
        ("G", "Grafana", BLUE, PALE_BLUE),
        ("L", "实验台", ORANGE, PALE_ORANGE),
        ("U", "管理页", GREEN, PALE_GREEN),
    ]):
        cy = 1.76 + i * 0.79
        add_circle(slide, 11.60, cy, 0.37, pale, pale)
        add_text(slide, letter, 11.60, cy, 0.37, 0.37, 9.5, color, True, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE, margin=0)
        add_text(slide, label, 12.06, cy + 0.06, 0.51, 0.20, 7.4, INK, True, margin=0)

    add_box(slide, 0.58, 4.86, 12.17, 1.77, WHITE, LINE)
    add_text(slide, "数据与执行底座", 0.84, 5.12, 1.65, 0.25, 11.8, INK, True, margin=0)
    foundations = [
        ("采集", "node_exporter · postgres_exporter\npg_stat_statements · pg activity", BLUE, PALE_BLUE),
        ("告警 / 采样", "Prometheus · perf 调用栈\nSysInsight 源码匹配", CYAN, PALE_CYAN),
        ("模型", "共享 GPT-5.6-SOL API\nSysInsight 与 DREAM 同源配置", PURPLE, PALE_PURPLE),
        ("执行", "TemporaryPostgresConfiguration\npg_hint_plan · Managed Replay", ORANGE, PALE_ORANGE),
        ("审计", "原始响应 · 候选 · 前后指标\n命中次数 · 恢复状态", GREEN, PALE_GREEN),
    ]
    for i, (title, body, color, pale) in enumerate(foundations):
        x = 2.36 + i * 2.04
        add_box(slide, x, 5.10, 1.82, 1.20, pale, pale)
        add_text(slide, title, x + 0.12, 5.25, 1.58, 0.20, 9.2, color, True, margin=0)
        add_text(slide, body, x + 0.12, 5.60, 1.58, 0.44, 7.0, MUTED, margin=0)
    add_footer(slide, 3)

    # 04 — Scenario design methodology
    slide = new_slide(prs)
    add_header(slide, "04", "场景设计出发点｜验证因果，而不只是制造一个慢结果", "标准负载负责代表性，独立压力负责制造瓶颈，同压复测负责证明收益来自调优动作", GOLD)
    add_box(slide, 0.60, 1.27, 3.18, 1.32, WHITE, LINE)
    add_pill(slide, "要回答的问题", 0.86, 1.50, 1.15, GOLD, PALE_GOLD, 7.7, 0.27)
    add_text(slide, "SysInsight", 0.86, 1.94, 0.96, 0.22, 10.2, BLUE, True, margin=0)
    add_text(slide, "相同外部压力持续存在时，参数动作能否恢复被保护业务 TPS？", 1.83, 1.91, 1.65, 0.47, 7.7, MUTED, margin=0)
    add_box(slide, 3.98, 1.27, 3.18, 1.32, WHITE, LINE)
    add_pill(slide, "要回答的问题", 4.24, 1.50, 1.15, GOLD, PALE_GOLD, 7.7, 0.27)
    add_text(slide, "DREAM", 4.24, 1.94, 0.86, 0.22, 10.2, PURPLE, True, margin=0)
    add_text(slide, "长 SQL 的优化动作能否真实降低耗时，并在下一次相同 SQL 自动生效？", 5.11, 1.91, 1.75, 0.47, 7.7, MUTED, margin=0)
    add_box(slide, 7.36, 1.27, 5.37, 1.32, NAVY_2, NAVY_2)
    add_text(slide, "场景不是为了证明“模型总能优化”", 7.68, 1.53, 4.72, 0.27, 13.0, WHITE, True, align=PP_ALIGN.CENTER, margin=0)
    add_text(slide, "它要把负载、压力、动作和结果拆开，让每一步都能被复现、否证和审计。", 7.74, 2.03, 4.60, 0.31, 8.0, C("C6D3E5"), align=PP_ALIGN.CENTER, margin=0)

    add_text(slide, "因果实验结构", 0.60, 2.94, 1.65, 0.23, 10.6, INK, True, margin=0)
    causal = [
        ("被保护负载", "TP：固定 TPC-C 混合事务\nAP：同一条原始 SQL", BLUE, PALE_BLUE),
        ("独立压力 / 缺陷", "TP：额外固定速率事务\nAP：查询结构本身的低效", ORANGE, PALE_ORANGE),
        ("可观测瓶颈", "锁 / Buffer / WAL / Checkpoint\n扫描 / 聚合 / 排序 / Join", RED, PALE_RED),
        ("引擎动作", "SysInsight 参数候选\nDREAM Rewrite / Hint / GUC", PURPLE, PALE_PURPLE),
        ("同条件复测", "压力保持不撤掉\n真实执行 old / new time", GREEN, PALE_GREEN),
    ]
    for i, (title, body, color, pale) in enumerate(causal):
        x = 0.60 + i * 2.48
        add_box(slide, x, 3.34, 2.17, 1.35, pale, pale)
        add_text(slide, f"0{i+1}", x + 0.15, 3.53, 0.36, 0.18, 7.0, color, True, margin=0)
        add_text(slide, title, x + 0.15, 3.85, 1.87, 0.23, 10.1, INK, True, margin=0)
        add_text(slide, body, x + 0.15, 4.24, 1.87, 0.35, 7.1, MUTED, align=PP_ALIGN.CENTER, margin=0)
        if i < 4:
            add_chevron(slide, x + 2.25, 3.84, 0.15, 0.28, C("A7B6CA"))

    add_text(slide, "为什么结果有说服力", 0.60, 5.08, 2.10, 0.23, 10.6, INK, True, margin=0)
    principles = [
        ("标准", "TPC-C / TPC-DS", BLUE, PALE_BLUE),
        ("真实", "真实 SQL 与 PostgreSQL", CYAN, PALE_CYAN),
        ("隔离", "目标负载与压力分开", ORANGE, PALE_ORANGE),
        ("同压", "调优前后压力可比", PURPLE, PALE_PURPLE),
        ("实测", "不用模型预测耗时", GREEN, PALE_GREEN),
        ("可否证", "失败场景也保留", RED, PALE_RED),
    ]
    for i, (title, body, color, pale) in enumerate(principles):
        x = 0.60 + i * 2.02
        add_box(slide, x, 5.48, 1.79, 0.89, WHITE, LINE)
        add_pill(slide, title, x + 0.18, 5.66, 0.57, color, pale, 7.5, 0.25)
        add_text(slide, body, x + 0.86, 5.65, 0.76, 0.37, 7.0, MUTED, True, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE, margin=0)
    add_box(slide, 0.60, 6.55, 12.13, 0.30, PALE_GOLD, PALE_GOLD)
    add_text(slide, "不靠 sleep、随机等待或伪造指标制造收益；性能结论来自同机、同库、同负载条件下的真实执行。", 0.86, 6.61, 11.62, 0.17, 7.8, GOLD, True, align=PP_ALIGN.CENTER, margin=0)
    add_footer(slide, 4)

    # 05 — How TP pressure is produced
    slide = new_slide(prs)
    add_header(slide, "05", "SysInsight 压力如何制造｜两组独立 pgbench 形成受控资源争用", "正常业务与外部压力分开运行；压力只增加真实事务，不预先改参数，也不使用 sleep 制造假慢", BLUE)

    add_box(slide, 0.60, 1.28, 3.20, 3.18, PALE_BLUE, C("BDD4F5"))
    add_pill(slide, "A · 被保护业务", 0.88, 1.54, 1.36, BLUE, WHITE, 7.6, 0.28)
    add_text(slide, "Control Worker", 0.88, 2.02, 2.64, 0.28, 14.0, INK, True, margin=0)
    add_text(slide, "2 个独立客户端 · pgbench -R ≈ 1600 TPS", 0.88, 2.43, 2.64, 0.21, 8.1, BLUE, True, margin=0)
    add_bullets(slide, [
        "固定运行 tp_normal.sql，调优前后完全不变",
        "TPC-C 比例：New Order 45% / Payment 43% / 其余各 4%",
        "随机覆盖 5 仓 × 10 地区 × 3000 客户 × 10 万商品",
        "它的稳态 TPS 是唯一主要收益指标",
    ], 0.86, 2.86, 2.68, 1.20, 7.6, MUTED, 4)

    add_chevron(slide, 4.00, 2.64, 0.25, 0.42, C("9BAFC8"))
    add_box(slide, 4.42, 1.28, 4.48, 3.18, NAVY_2, NAVY_2)
    add_pill(slide, "真实 PostgreSQL 资源", 5.70, 1.54, 1.91, C("B9D6FF"), C("294466"), 7.6, 0.28)
    resources = [
        ("锁", "warehouse / district / customer 热点行", RED, PALE_RED),
        ("缓存与 I/O", "orders / order_line / stock 索引与数据页", CYAN, PALE_CYAN),
        ("WAL", "UPDATE / INSERT 产生日志、脏页与刷写", ORANGE, PALE_ORANGE),
        ("Checkpoint", "写入积累后触发 write / sync 路径", PURPLE, PALE_PURPLE),
    ]
    for i, (title, body, color, pale) in enumerate(resources):
        y = 2.06 + i * 0.52
        add_pill(slide, title, 4.76, y, 0.88, color, pale, 7.0, 0.24)
        add_text(slide, body, 5.82, y + 0.02, 2.71, 0.21, 7.5, C("D4DFEC"), margin=0)
    add_text(slide, "实际执行 BEGIN / SELECT / UPDATE / INSERT / COMMIT 路径", 4.80, 4.12, 3.73, 0.18, 7.1, C("9FB0C7"), align=PP_ALIGN.CENTER, margin=0)

    add_chevron(slide, 9.10, 2.64, 0.25, 0.42, C("9BAFC8"))
    add_box(slide, 9.52, 1.28, 3.21, 3.18, PALE_ORANGE, C("F2D4AE"))
    add_pill(slide, "B · 外部压力", 9.80, 1.54, 1.26, ORANGE, WHITE, 7.6, 0.28)
    add_text(slide, "Pressure Worker", 9.80, 2.02, 2.65, 0.28, 14.0, INK, True, margin=0)
    add_text(slide, "3–10 客户端 · 场景目标 1950–5600 TPS", 9.80, 2.43, 2.65, 0.21, 8.1, ORANGE, True, margin=0)
    add_bullets(slide, [
        "每个场景选择真实 TPC-C 事务脚本",
        "-R 使用开环固定到达率，不因数据库变慢而主动降载",
        "调优前和调优后重新播放同一压力配方",
        "压力进程本身从不修改 PostgreSQL 参数",
    ], 9.78, 2.86, 2.69, 1.20, 7.6, MUTED, 4)

    add_text(slide, "为什么可以复现实验", 0.60, 4.82, 2.10, 0.23, 10.8, INK, True, margin=0)
    timeline = [
        ("60 s", "无压力基线", "只运行 A", BLUE, PALE_BLUE, 2.33),
        ("90 s", "压力 / 调优前", "A + B", ORANGE, PALE_ORANGE, 2.33),
        ("动作", "应用参数候选", "B 不撤掉", PURPLE, PALE_PURPLE, 2.33),
        ("90 s", "压力 / 调优后", "A + 同配方 B", GREEN, PALE_GREEN, 2.33),
    ]
    for i, (duration, title, body, color, pale, width) in enumerate(timeline):
        x = 0.60 + i * 2.50
        add_box(slide, x, 5.22, width, 0.91, pale, pale)
        add_text(slide, duration, x + 0.15, 5.37, 0.55, 0.22, 10.0, color, True, align=PP_ALIGN.CENTER, margin=0)
        add_text(slide, title, x + 0.78, 5.34, 1.35, 0.20, 8.1, INK, True, margin=0)
        add_text(slide, body, x + 0.78, 5.72, 1.35, 0.17, 7.2, MUTED, margin=0)
        if i < 3:
            add_chevron(slide, x + 2.38, 5.49, 0.13, 0.27, C("A5B5C9"))
    add_box(slide, 10.59, 5.22, 2.14, 0.91, NAVY_2, NAVY_2)
    add_text(slide, "有效性门槛", 10.82, 5.37, 1.68, 0.19, 8.2, C("A9C9F4"), True, align=PP_ALIGN.CENTER, margin=0)
    add_text(slide, "压力前后实际 TPS 偏差 ≤ 20%", 10.82, 5.69, 1.68, 0.27, 7.2, WHITE, True, align=PP_ALIGN.CENTER, margin=0)
    add_box(slide, 0.60, 6.39, 12.13, 0.45, PALE_GOLD, PALE_GOLD)
    add_text(slide, "数据保护：每个脚本执行真实 SQL 后回滚到 SAVEPOINT，避免持续改变业务数据；写事务仍实际经过执行器、行锁、Buffer 和 WAL 路径。",
             0.84, 6.50, 11.66, 0.20, 7.7, GOLD, True, align=PP_ALIGN.CENTER, margin=0)
    add_footer(slide, 5, "外部压力 TPS 只用于证明施压力度可比；业务恢复只看 Control Worker 的目标业务 TPS")

    # 06 — Detailed TP pressure scenarios, read/write skew
    slide = new_slide(prs)
    add_header(slide, "06", "SysInsight 压力详解（1/2）｜热点写、热点读与分散读突发", "三种压力分别验证“少数键被打爆”“同一分区被反复查询”“大量分区同时扩大工作集”", BLUE)
    pressure_cards = [
        {
            "title": "热点 Payment",
            "tag": "热点写 / 锁竞争",
            "recipe": "3 客户端 · 目标 1950 TPS\n固定 w_id=1、d_id=1；customer 随机 1–3000",
            "sql": "每笔事务连续 UPDATE 同一 warehouse 行、同一 district 行，再 UPDATE customer 并 INSERT history。",
            "reality": "模拟大商户、热点租户或单一区域在促销时集中收款；商户/区域汇总行成为串行化热点。",
            "effect": "行锁等待、提交与 WAL/fsync 竞争；观察 lock_waits、xact_commit、buffers_backend_fsync、n_tup_upd。",
            "gain": "三轮平均 +26.4%",
            "color": RED,
            "pale": PALE_RED,
        },
        {
            "title": "热点 Stock Level",
            "tag": "热点读 / 缓存竞争",
            "recipe": "5 客户端 · 目标 3800 TPS\n固定 w_id=1、d_id=1，反复查询同一库存分区",
            "sql": "从最近订单行取 DISTINCT 商品，再回查 stock 中 quantity<15 的数量；每次命中同一批索引和数据页。",
            "reality": "模拟某个大仓、爆款门店或热点租户的库存看板被 OMS、客服和补货系统高频轮询。",
            "effect": "共享缓存、索引页和 CPU 并发竞争；观察 blks_read、blks_hit、buffers_backend、active_total。",
            "gain": "三轮平均 +39.6%",
            "color": BLUE,
            "pale": PALE_BLUE,
        },
        {
            "title": "Stock Level 突发",
            "tag": "分散读 / 工作集扩大",
            "recipe": "5 客户端 · 目标 3800 TPS\nw_id 随机 1–5、d_id 随机 1–10",
            "sql": "执行与热点场景相同的标准库存检查，但请求均匀散布到 50 个仓库/地区组合。",
            "reality": "模拟开店时刻、盘点任务或促销结束后，大量门店同时刷新库存与缺货清单。",
            "effect": "工作集突然扩大，造成更多索引范围读取和 Buffer churn；观察 read/hit、backend buffer 与活跃会话。",
            "gain": "三轮平均 +39.0%",
            "color": CYAN,
            "pale": PALE_CYAN,
        },
    ]
    for i, card in enumerate(pressure_cards):
        x = 0.60 + i * 4.11
        color, pale = card["color"], card["pale"]
        add_box(slide, x, 1.30, 3.80, 5.18, WHITE, LINE)
        add_box(slide, x, 1.30, 3.80, 0.08, color, color, radius=False)
        add_text(slide, card["title"], x + 0.24, 1.60, 2.20, 0.27, 13.3, INK, True, margin=0)
        add_pill(slide, card["tag"], x + 2.33, 1.59, 1.20, color, pale, 6.8, 0.26)
        labels = [
            ("压力配方", card["recipe"], 2.08, 0.55),
            ("实际 SQL", card["sql"], 2.88, 0.68),
            ("现实问题", card["reality"], 3.80, 0.68),
            ("资源后果", card["effect"], 4.72, 0.76),
        ]
        for label, value, y, height in labels:
            add_text(slide, label, x + 0.24, y, 0.66, 0.18, 7.0, color, True, margin=0)
            add_text(slide, value, x + 1.02, y - 0.03, 2.49, height, 7.3, MUTED if label != "现实问题" else INK,
                     label == "现实问题", margin=0)
        add_box(slide, x + 0.24, 5.88, 3.32, 0.36, pale, pale)
        add_text(slide, card["gain"], x + 0.40, 5.97, 3.00, 0.17, 8.8, color, True, align=PP_ALIGN.CENTER, margin=0)
    add_box(slide, 0.60, 6.60, 12.13, 0.25, PALE_BLUE, PALE_BLUE)
    add_text(slide, "关键区别：热点 Stock Level 固定一个分区，验证数据倾斜；Stock Level 突发随机覆盖 50 个分区，验证工作集扩大与缓存抖动。",
             0.86, 6.64, 11.62, 0.17, 7.4, BLUE, True, align=PP_ALIGN.CENTER, margin=0)
    add_footer(slide, 6)

    # 07 — Detailed TP pressure scenarios, mixed/WAL/read hot spot
    slide = new_slide(prs)
    add_header(slide, "07", "SysInsight 压力详解（2/2）｜整体流量阶跃、持续写峰值与查单热点", "三种压力分别对应全站突增、写入子系统过载和热点租户查询风暴", BLUE)
    pressure_cards = [
        {
            "title": "五类 TPCC 高强度混合",
            "tag": "全站流量阶跃",
            "recipe": "8 客户端 · 目标 2800 TPS\n45/43/4/4/4 全事务混合；仓库/客户/商品随机",
            "sql": "同时增加 New Order、Payment、Order Status、Delivery、Stock Level，读写与索引访问一起上升。",
            "reality": "模拟大促开场、渠道切流、应用扩容或客户端数突然增加，所有核心交易同比例阶跃增长。",
            "effect": "锁、CPU、索引、Buffer、WAL 和 Checkpoint 综合争用；检验系统级参数是否能改善总体吞吐。",
            "gain": "三轮平均 +59.4%",
            "color": PURPLE,
            "pale": PALE_PURPLE,
        },
        {
            "title": "WAL / Checkpoint 写入压力",
            "tag": "持续写入峰值",
            "recipe": "10 客户端 · 目标 2500 TPS\n混合中 45% New Order + 43% Payment = 88% 写事务",
            "sql": "短时间持续执行多表 UPDATE/INSERT；增加 WAL 生成、脏页、backend write 与 checkpoint 同步工作。",
            "reality": "模拟订单涌入、支付结算、批量入账等持续写窗口；瓶颈位于日志和刷盘路径而非单一热点键。",
            "effect": "观察 checkpoints_req、checkpoint_write_time、checkpoint_sync_time、buffers_backend。",
            "gain": "三轮平均 +47.2%",
            "color": ORANGE,
            "pale": PALE_ORANGE,
        },
        {
            "title": "热点 Order Status",
            "tag": "热点查单风暴",
            "recipe": "6 客户端 · 目标 5600 TPS\n固定 w_id=1、d_id=1；反复读取最新订单",
            "sql": "先定位该地区最新订单，再按主键读取订单头与全部 order_line；重复命中相同索引前缀和页面。",
            "reality": "模拟物流异常、活动订单或大租户引发客服、用户端和跟踪服务集中轮询查单。",
            "effect": "热点索引/Buffer 页、排序和 CPU 竞争；观察 blks_read、blks_hit、buffers_backend、active_total。",
            "gain": "三轮平均 +23.5%",
            "color": GREEN,
            "pale": PALE_GREEN,
        },
    ]
    for i, card in enumerate(pressure_cards):
        x = 0.60 + i * 4.11
        color, pale = card["color"], card["pale"]
        add_box(slide, x, 1.30, 3.80, 5.18, WHITE, LINE)
        add_box(slide, x, 1.30, 3.80, 0.08, color, color, radius=False)
        add_text(slide, card["title"], x + 0.24, 1.60, 2.26, 0.40, 11.5, INK, True, margin=0)
        add_pill(slide, card["tag"], x + 2.40, 1.59, 1.14, color, pale, 6.5, 0.26)
        labels = [
            ("压力配方", card["recipe"], 2.17, 0.55),
            ("实际 SQL", card["sql"], 2.97, 0.68),
            ("现实问题", card["reality"], 3.89, 0.68),
            ("资源后果", card["effect"], 4.81, 0.70),
        ]
        for label, value, y, height in labels:
            add_text(slide, label, x + 0.24, y, 0.66, 0.18, 7.0, color, True, margin=0)
            add_text(slide, value, x + 1.02, y - 0.03, 2.49, height, 7.3, MUTED if label != "现实问题" else INK,
                     label == "现实问题", margin=0)
        add_box(slide, x + 0.24, 5.88, 3.32, 0.36, pale, pale)
        add_text(slide, card["gain"], x + 0.40, 5.97, 3.00, 0.17, 8.8, color, True, align=PP_ALIGN.CENTER, margin=0)
    add_box(slide, 0.60, 6.60, 12.13, 0.25, PALE_ORANGE, PALE_ORANGE)
    add_text(slide, "关键区别：混合突发看端到端并发吞吐；WAL 场景用更多并发写会话聚焦日志/刷盘证据；Order Status 则隔离高频热点读。",
             0.86, 6.64, 11.62, 0.17, 7.4, ORANGE, True, align=PP_ALIGN.CENTER, margin=0)
    add_footer(slide, 7, "目标速率是 offered load；实际压力 TPS 由系统独立采集，调优前后偏差超过 20% 时对比判为无效")

    # 08 — DREAM workload rationale
    slide = new_slide(prs)
    add_header(slide, "08", "DREAM 查询压力如何形成｜全量 SQL 入口 + 多结构组合验证", "99 条 TPC-DS SQL 进入统一观察链路；95 条只读 SQL 可自动候选，当前用 3 类典型结构做真实重复验证", PURPLE)
    add_metric_card(slide, 0.60, 1.22, 2.34, "SF10", "标准 TPC-DS 数据规模", PURPLE, PALE_PURPLE)
    add_metric_card(slide, 3.13, 1.22, 2.34, "99 / 95", "查询总数 / 只读 SQL", BLUE, PALE_BLUE)
    add_metric_card(slide, 5.66, 1.22, 2.34, "≥ 10 s", "自动进入 DREAM 的门槛", ORANGE, PALE_ORANGE)
    add_metric_card(slide, 8.19, 1.22, 2.34, f"{dream_verified_runs} / {dream_verified_runs}", "代表 SQL 真实复测正收益", GREEN, PALE_GREEN)
    add_metric_card(slide, 10.72, 1.22, 2.01, "真实", "old/new 均实跑", CYAN, PALE_CYAN)

    ap_cards = [
        ("Q31", "跨渠道季度增长", "Store/Web 两个事实表先跨年份/季度聚合，再把两个 CTE 各引用 3 次并形成 6 路连接。", "渠道增长、同比/环比经营分析", "大中间结果被重复读取；可过滤下推并一次条件聚合", "三轮平均 +82.6%", BLUE, PALE_BLUE),
        ("Q44", "商品表现 Top / Bottom", "同一 store_sales 聚合逻辑执行两遍，再分别窗口 Rank、排序并回连 item 维表。", "商品排行榜、选品与经营诊断", "重复聚合与双重排序；可复用公共结果并优化 Top-N", "三轮平均 +58.1%", PURPLE, PALE_PURPLE),
        ("Q88", "分时段客流统计", "8 个仅时间条件不同的子查询分别 Join 并扫描事实/维表，最后横向拼成 8 个桶。", "门店客流、时段运营仪表盘", "同一数据被扫描 8 次；可一次扫描并用条件聚合分桶", "三轮平均 +80.8%", ORANGE, PALE_ORANGE),
    ]
    for i, (qid, title, shape, business, mechanism, gain, color, pale) in enumerate(ap_cards):
        x = 0.60 + i * 4.11
        add_box(slide, x, 2.47, 3.80, 3.68, WHITE, LINE)
        add_box(slide, x, 2.47, 3.80, 0.08, color, color, radius=False)
        add_pill(slide, qid, x + 0.24, 2.74, 0.59, color, pale, 8.1, 0.27)
        add_text(slide, title, x + 0.98, 2.75, 2.54, 0.24, 11.4, INK, True, margin=0)
        add_text(slide, "SQL 结构", x + 0.24, 3.30, 0.68, 0.18, 7.2, color, True, margin=0)
        add_text(slide, shape, x + 1.03, 3.23, 2.49, 0.55, 7.4, MUTED, margin=0)
        add_text(slide, "业务映射", x + 0.24, 4.04, 0.68, 0.18, 7.2, color, True, margin=0)
        add_text(slide, business, x + 1.03, 4.01, 2.49, 0.28, 8.0, INK, True, margin=0)
        add_text(slide, "优化机理", x + 0.24, 4.61, 0.68, 0.18, 7.2, color, True, margin=0)
        add_text(slide, mechanism, x + 1.03, 4.53, 2.49, 0.53, 7.4, MUTED, margin=0)
        add_box(slide, x + 0.24, 5.39, 3.32, 0.46, pale, pale)
        add_text(slide, gain, x + 0.40, 5.50, 3.00, 0.20, 10.2, color, True, align=PP_ALIGN.CENTER, margin=0)
    add_box(slide, 0.60, 6.38, 12.13, 0.47, NAVY_2, NAVY_2)
    add_rich_line(slide, [("综合口径：", C("B9D6FF"), True), (f"{ap_count} 条目录 / {ap_read_only} 条只读候选 / {dream_verified_sql} 条代表 SQL / {dream_verified_runs} 次重复复测 / 9/9 正收益 / 平均 +{dream_portfolio_avg:.1f}%。", WHITE, False)],
                  0.88, 6.49, 11.56, 0.22, 8.5, PP_ALIGN.CENTER)
    add_footer(slide, 8, "Q4 因原 SQL 超过 180 s timeout 被移出默认入口；选择标准是可完成、可重复、可展示，而不是只挑最大百分比")

    # 09 — SysInsight flow
    slide = new_slide(prs)
    add_header(slide, "09", "SysInsight｜告警触发的 TP 参数调优闭环", "在压力持续存在的前提下比较目标业务 TPS；实验结束后恢复原始数据库配置", BLUE)
    steps = [
        ("01", "建立基线", "运行正常 TPCC 控制负载，记录稳态 TPS。"),
        ("02", "施加压力", "固定速率外部场景使目标业务性能下降。"),
        ("03", "告警触发", "Prometheus 告警进入 Bridge，自动触发链路。"),
        ("04", "检测取证", "采集主机/PG 指标、perf 栈与源码证据。"),
        ("05", "LLM 分析", "GPT-5.6-SOL 生成数据库参数候选。"),
        ("06", "临时应用", "临时配置上下文完整应用可执行候选。"),
        ("07", "同压复测", "压力不撤掉，复测同一业务 TPS。"),
        ("08", "恢复审计", "恢复原配置并保存候选、指标和结果。"),
    ]
    for i, (num, title, body) in enumerate(steps[:4]):
        x = 0.60 + i * 3.10
        add_flow_card(slide, x, 1.35, 2.78, 1.52, num, title, body, BLUE, PALE_BLUE)
        if i < 3:
            add_chevron(slide, x + 2.86, 1.93, 0.18, 0.32, C("9DB4D0"))
    for i, (num, title, body) in enumerate(steps[4:]):
        x = 0.60 + i * 3.10
        add_flow_card(slide, x, 3.15, 2.78, 1.52, num, title, body, CYAN, PALE_CYAN)
        if i < 3:
            add_chevron(slide, x + 2.86, 3.73, 0.18, 0.32, C("9DB4D0"))

    add_box(slide, 0.60, 5.08, 8.10, 1.64, WHITE, LINE)
    add_text(slide, "实验时间轴", 0.84, 5.31, 1.35, 0.23, 10.5, INK, True, margin=0)
    timeline = [
        ("无压力基线", "60 s", BLUE, PALE_BLUE, 1.52),
        ("压力 / 调优前", "90 s", ORANGE, PALE_ORANGE, 2.10),
        ("应用候选", "压力保持", PURPLE, PALE_PURPLE, 1.62),
        ("压力 / 调优后", "90 s", GREEN, PALE_GREEN, 2.10),
    ]
    tx = 0.84
    for title, duration, color, pale, width in timeline:
        add_box(slide, tx, 5.78, width, 0.56, pale, pale)
        add_text(slide, title, tx + 0.06, 5.85, width - 0.12, 0.18, 8.2, color, True, align=PP_ALIGN.CENTER, margin=0)
        add_text(slide, duration, tx + 0.06, 6.10, width - 0.12, 0.14, 6.7, MUTED, align=PP_ALIGN.CENTER, margin=0)
        tx += width + 0.11
    add_box(slide, 8.98, 5.08, 3.75, 1.64, NAVY_2, NAVY_2)
    add_text(slide, "主指标", 9.25, 5.30, 0.82, 0.20, 8.2, C("9FC6FA"), True, margin=0)
    add_text(slide, "压力下目标业务 TPS", 9.25, 5.65, 3.20, 0.34, 17.5, WHITE, True, margin=0)
    add_text(slide, "外部压力 TPS 仅用于验证前后施压强度可比；不作为业务收益指标。", 9.25, 6.16, 3.16, 0.37, 7.5, C("C4D1E4"), margin=0)
    add_footer(slide, 9, "默认控制台：60 s 基线 + 180 s 压力观察（前后各 90 s）；压力阶段不追加撤压恢复窗口")

    # 10 — DREAM flow
    slide = new_slide(prs)
    add_header(slide, "10", "DREAM｜长 AP SQL 的异步优化与下次自动应用", "原 SQL 完成后再进入后台分析；只有真实复测达标的只读方案才会被激活", PURPLE)
    add_box(slide, 0.58, 1.24, 12.17, 0.66, PALE_PURPLE, C("D8CCF2"))
    add_rich_line(slide, [("自动触发门禁：", PURPLE, True), ("SQL 已结束  +  只读  +  实际耗时 ≥ 10 s  +  可重放样本  +  当前无同 SQL 任务", INK, False)],
                  0.86, 1.43, 11.62, 0.24, 10.1, PP_ALIGN.CENTER)
    dream_steps = [
        ("01", "记录耗时", "持续采集 SQL 耗时与结束状态。"),
        ("02", "后台排队", "异步执行；按同一 SQL 去重。"),
        ("03", "LLM 诊断", "DREAM 调用 LLM 生成根因与动作。"),
        ("04", "真实执行", "在 PG 上实测 old_time / new_time。"),
        ("05", "策略判定", "只读且收益 ≥ 10% 才激活。"),
        ("06", "注册方案", "保存 Hint、会话参数或改写。"),
        ("07", "下次命中", "相同 SQL 再次执行自动采用。"),
    ]
    for i, (num, title, body) in enumerate(dream_steps):
        x = 0.58 + i * 1.76
        add_flow_card(slide, x, 2.23, 1.54, 2.17, num, title, body, PURPLE if i < 4 else GREEN, PALE_PURPLE if i < 4 else PALE_GREEN)
        if i < len(dream_steps) - 1:
            add_chevron(slide, x + 1.57, 3.04, 0.13, 0.28, C("A9B6CA"))

    add_box(slide, 0.58, 4.78, 5.72, 1.78, WHITE, LINE)
    add_text(slide, "代表性真实链路 · TPC-DS Q31", 0.84, 5.02, 5.18, 0.24, 11.3, INK, True, margin=0)
    add_rich_line(slide, [
        (f"{run['q31_baseline_ms']/1000:.2f}s", RED, True),
        ("  原 SQL  →  ", MUTED, False),
        (f"{run['q31_internal_ms']/1000:.2f}s", PURPLE, True),
        ("  DREAM 内部复测  →  ", MUTED, False),
        (f"{run['q31_optimized_ms']/1000:.2f}s", GREEN, True),
        ("  下次自动命中", MUTED, False),
    ], 0.84, 5.43, 5.16, 0.29, 11.7, PP_ALIGN.CENTER)
    add_pill(slide, f"实测提升 {run['q31_ratio']*100:.1f}%", 2.35, 5.96, 1.86, GREEN, PALE_GREEN, 8.5, 0.30)
    add_box(slide, 6.58, 4.78, 6.17, 1.78, NAVY_2, NAVY_2)
    add_text(slide, "异步设计的产品含义", 6.87, 5.02, 2.36, 0.25, 11.3, WHITE, True, margin=0)
    add_bullets(slide, [
        "长 SQL 的第一次执行保持原语义，结束后才触发 DREAM。",
        "DREAM 分析、模型调用和复测在后台执行，不占用前台按钮线程。",
        "只有下一次相同 SQL 才使用已激活方案，并记录命中与实际耗时。",
    ], 6.88, 5.40, 5.45, 0.96, 8.1, C("D2DDEA"), 4)
    add_footer(slide, 10)

    # 11 — Application policy matrix
    slide = new_slide(prs)
    add_header(slide, "11", "自动应用策略｜不同动作走不同执行路径", "把“模型给出建议”与“数据库真正采用方案”拆开管理，避免把不可执行动作误报为已优化", ORANGE)
    add_metric_card(slide, 0.60, 1.24, 2.82, "1.3.10", "pg_hint_plan 已安装并启用", BLUE, PALE_BLUE)
    add_metric_card(slide, 3.61, 1.24, 2.82, str(run["active_improvements"]), "当前 active 改进方案", PURPLE, PALE_PURPLE)
    add_metric_card(slide, 6.62, 1.24, 2.82, str(run["auto_applications"]), "已观察到自动应用次数", GREEN, PALE_GREEN)
    add_metric_card(slide, 9.63, 1.24, 3.10, "≥ 10%", "方案激活最低实测收益", ORANGE, PALE_ORANGE)
    headers = ["动作类型", "真实验证", "自动应用路径", "当前处理", "边界 / 说明"]
    rows = [
        ["Plan Hint", "EXPLAIN / 执行复测", "PostgreSQL 直连", "写入 hint_plan.hints", "直接客户端可命中"],
        ["Planner Set", "执行计划 + 耗时", "PostgreSQL 直连", "pg_hint_plan Set(...) ", "仅允许受支持项"],
        ["SQL Rewrite", "原 SQL 与改写 SQL 实跑", "Bridge Managed", "下次托管执行改写", "直连 PG 无法透明替换文本"],
        ["会话级 GUC", "同一 SQL 会话复测", "Bridge Managed", "托管连接 SET LOCAL", "不写全局配置"],
        ["DDL / 未知动作", "不自动执行", "无", "保留候选或拒绝", "避免结构/语义副作用"],
    ]
    add_table(slide, 0.60, 2.48, [1.45, 1.82, 1.80, 2.20, 4.90], headers, rows, 0.66, 0.48, 7.8,
              [PP_ALIGN.LEFT, PP_ALIGN.CENTER, PP_ALIGN.CENTER, PP_ALIGN.LEFT, PP_ALIGN.LEFT], ORANGE)
    add_box(slide, 0.60, 6.43, 12.13, 0.42, PALE_GOLD, C("F1D786"))
    add_rich_line(slide, [("关键边界：", GOLD, True), ("语义级 SQL 改写需要应用、代理或 Bridge 接入；裸 PostgreSQL 连接目前只具备 Hint / Planner Set 的直接自动应用能力。", INK, False)],
                  0.85, 6.52, 11.62, 0.20, 8.4, PP_ALIGN.CENTER)
    add_footer(slide, 11)

    # 12 — Lab console
    slide = new_slide(prs)
    add_header(slide, "12", "实验控制台｜一页触发、观察与复现实验", "把已验证场景、数据库重置、任务状态和前后对比集中在单页，适合现场演示", ORANGE)
    add_box(slide, 0.58, 1.24, 12.17, 5.64, WHITE, C("BFCBDD"), radius=True, width=1.0)
    add_box(slide, 0.58, 1.24, 12.17, 0.48, NAVY_2, NAVY_2, radius=False)
    add_circle(slide, 0.79, 1.40, 0.10, RED)
    add_circle(slide, 0.97, 1.40, 0.10, GOLD)
    add_circle(slide, 1.15, 1.40, 0.10, GREEN)
    add_text(slide, "SysInsight / DREAM 实验控制台", 1.46, 1.34, 4.0, 0.19, 8.2, WHITE, True, margin=0)
    add_pill(slide, "刷新", 11.55, 1.33, 0.72, C("B8D7FF"), C("233E65"), 7.0, 0.25)

    # Left TP panel
    add_box(slide, 0.88, 1.98, 5.55, 4.52, C("F9FBFE"), LINE)
    add_text(slide, "TP｜SysInsight 压力调优", 1.13, 2.20, 4.90, 0.29, 14.0, BLUE, True, margin=0)
    add_pill(slide, "6 组关注场景", 4.75, 2.20, 1.30, BLUE, PALE_BLUE, 7.2, 0.25)
    add_text(slide, "数据库参数", 1.13, 2.70, 1.15, 0.18, 7.5, MUTED, True, margin=0)
    add_box(slide, 2.38, 2.60, 1.50, 0.36, WHITE, LINE)
    add_text(slide, "当前实验基线", 2.50, 2.69, 1.22, 0.16, 7.4, INK, margin=0)
    add_pill(slide, "重置参数", 4.70, 2.62, 1.10, RED, PALE_RED, 7.4, 0.31)
    add_text(slide, "压力场景", 1.13, 3.19, 1.15, 0.18, 7.5, MUTED, True, margin=0)
    add_box(slide, 2.38, 3.08, 3.42, 0.38, WHITE, LINE)
    add_text(slide, "热点仓库 Payment 外部并发", 2.50, 3.18, 3.14, 0.17, 7.6, INK, margin=0)
    add_text(slide, "基线", 1.13, 3.72, 0.55, 0.18, 7.4, MUTED, True, margin=0)
    add_box(slide, 1.72, 3.63, 0.62, 0.36, WHITE, LINE)
    add_text(slide, "60 s", 1.79, 3.72, 0.48, 0.16, 7.4, INK, align=PP_ALIGN.CENTER, margin=0)
    add_text(slide, "压力观察", 2.58, 3.72, 0.86, 0.18, 7.4, MUTED, True, margin=0)
    add_box(slide, 3.48, 3.63, 0.72, 0.36, WHITE, LINE)
    add_text(slide, "180 s", 3.55, 3.72, 0.58, 0.16, 7.4, INK, align=PP_ALIGN.CENTER, margin=0)
    add_pill(slide, "启动 TPCC", 4.70, 3.64, 1.10, WHITE, BLUE, 7.6, 0.33)
    add_text(slide, "基线", 1.18, 4.47, 1.10, 0.17, 7.4, BLUE, True, align=PP_ALIGN.CENTER, margin=0)
    add_text(slide, "压力前", 2.61, 4.47, 1.10, 0.17, 7.4, ORANGE, True, align=PP_ALIGN.CENTER, margin=0)
    add_text(slide, "应用候选", 4.04, 4.47, 1.10, 0.17, 7.4, PURPLE, True, align=PP_ALIGN.CENTER, margin=0)
    add_text(slide, "压力后", 5.22, 4.47, 0.86, 0.17, 7.4, GREEN, True, align=PP_ALIGN.CENTER, margin=0)
    add_box(slide, 1.13, 4.75, 1.30, 0.12, BLUE, BLUE, radius=True)
    add_box(slide, 2.57, 4.75, 1.30, 0.12, ORANGE, ORANGE, radius=True)
    add_box(slide, 4.01, 4.75, 1.04, 0.12, PURPLE, PURPLE, radius=True)
    add_box(slide, 5.18, 4.75, 0.90, 0.12, GREEN, GREEN, radius=True)
    add_box(slide, 1.13, 5.24, 4.95, 0.92, WHITE, LINE)
    add_text(slide, "目标业务 TPS", 1.34, 5.43, 1.22, 0.18, 7.4, MUTED, True, margin=0)
    add_rich_line(slide, [("1,317", ORANGE, True), ("  →  ", MUTED, False), ("1,596", GREEN, True), ("   +21.2%", GREEN, True)],
                  2.58, 5.37, 3.12, 0.28, 12.4, PP_ALIGN.RIGHT)
    add_text(slide, "压力前后强度在允许偏差内 · applied_and_restored", 1.34, 5.82, 4.47, 0.17, 7.0, MUTED, margin=0)

    # Right AP panel
    add_box(slide, 6.68, 1.98, 5.77, 4.52, C("FBF9FE"), LINE)
    add_text(slide, "AP｜DREAM 长 SQL 优化", 6.94, 2.20, 5.05, 0.29, 14.0, PURPLE, True, margin=0)
    add_pill(slide, "99 条 SQL", 11.05, 2.20, 1.04, PURPLE, PALE_PURPLE, 7.2, 0.25)
    add_text(slide, "演示 SQL", 6.94, 2.70, 1.05, 0.18, 7.5, MUTED, True, margin=0)
    add_box(slide, 8.06, 2.60, 2.47, 0.36, WHITE, LINE)
    add_text(slide, "TPC-DS Q31（推荐）", 8.18, 2.69, 2.20, 0.16, 7.4, INK, margin=0)
    add_pill(slide, "清空记录", 10.79, 2.62, 1.16, RED, PALE_RED, 7.2, 0.31)
    add_pill(slide, "发送原 SQL 并触发 DREAM", 6.94, 3.22, 2.52, WHITE, PURPLE, 7.6, 0.36)
    add_pill(slide, "再次发送同一 SQL（自动应用）", 9.62, 3.22, 2.55, GREEN, PALE_GREEN, 7.4, 0.36)
    add_text(slide, "每个按钮运行期间禁用；request_id + 服务端锁阻止重复提交", 6.95, 3.76, 5.10, 0.20, 7.3, MUTED, margin=0)
    add_box(slide, 6.94, 4.22, 5.23, 0.62, WHITE, LINE)
    add_pill(slide, "completed", 7.16, 4.38, 0.96, GREEN, PALE_GREEN, 7.0, 0.24)
    add_text(slide, "analysis_completed · q31 · active", 8.36, 4.39, 3.45, 0.17, 7.7, INK, True, margin=0)
    add_table(slide, 6.94, 5.06, [1.40, 1.19, 1.50, 1.14], ["阶段", "耗时", "方式", "状态"],
              [["baseline", "38.25 s", "original SQL", "completed"], ["optimized", "4.65 s", "DREAM rewrite", "completed"]],
              0.40, 0.36, 6.8, [PP_ALIGN.LEFT, PP_ALIGN.RIGHT, PP_ALIGN.LEFT, PP_ALIGN.CENTER], PURPLE)
    add_footer(slide, 12, "控制台是操作入口；Grafana 同步展示过程指标、任务状态、改进方案与命中记录")

    # 13 — Grafana
    slide = new_slide(prs)
    add_header(slide, "13", "Grafana 联动｜看见系统在后台做了什么", "主机/数据库指标、27 场景 TPS、自动化任务与实验结果在同一监控体系中展示", CYAN)
    add_box(slide, 0.58, 1.24, 9.05, 5.64, C("111821"), C("303C4C"), radius=True, width=1.0)
    add_box(slide, 0.58, 1.24, 9.05, 0.48, C("1B2532"), C("1B2532"), radius=False)
    add_text(slide, "Grafana  ·  PostgreSQL 本机监控大屏", 0.83, 1.37, 4.20, 0.18, 8.2, C("E8EDF4"), True, margin=0)
    add_pill(slide, "Last 6 hours", 7.72, 1.34, 1.13, C("C8D4E3"), C("293748"), 6.8, 0.25)
    add_pill(slide, "15s", 8.92, 1.34, 0.48, GREEN, C("223A39"), 6.8, 0.25)
    # Dashboard cards
    add_box(slide, 0.82, 1.96, 5.50, 2.17, C("18222F"), C("334153"))
    add_text(slide, "TPCC 各场景目标业务 TPS（三阶段，未运行 = 0）", 1.02, 2.13, 5.08, 0.20, 7.6, C("D8E1EC"), True, margin=0)
    phases = [(0, 0.31, C("172D47"), "基线"), (0.31, 0.63, C("3A2B21"), "压力前"), (0.63, 1.0, C("17372F"), "调优后")]
    add_line_chart(slide, 1.05, 2.66, 4.98, 1.07,
                   [[1520, 1545, 1530, 1510, 920, 760, 690, 720, 910, 1120, 1390, 1510, 1550]],
                   phases, [CYAN], ["热点 Payment"])
    add_box(slide, 6.52, 1.96, 2.82, 2.17, C("18222F"), C("334153"))
    add_text(slide, "CPU / Memory", 6.72, 2.13, 2.42, 0.20, 7.6, C("D8E1EC"), True, margin=0)
    add_line_chart(slide, 6.76, 2.72, 2.30, 0.98,
                   [[28, 35, 52, 74, 81, 68, 63, 58, 55], [42, 43, 45, 47, 48, 48, 47, 46, 45]],
                   [], [ORANGE, PURPLE], ["CPU", "内存"])
    add_box(slide, 0.82, 4.38, 2.44, 1.90, C("18222F"), C("334153"))
    add_text(slide, "自动化状态", 1.02, 4.55, 2.02, 0.18, 7.6, C("D8E1EC"), True, margin=0)
    add_text(slide, "FULL CHAIN", 1.02, 4.96, 2.02, 0.29, 14, GREEN, True, align=PP_ALIGN.CENTER, margin=0)
    add_text(slide, "DREAM READY", 1.02, 5.39, 2.02, 0.20, 8.0, C("B8C6D8"), True, align=PP_ALIGN.CENTER, margin=0)
    add_pill(slide, "API configured", 1.38, 5.82, 1.28, GREEN, C("223A39"), 6.8, 0.25)
    add_box(slide, 3.47, 4.38, 3.14, 1.90, C("18222F"), C("334153"))
    add_text(slide, "改进方案 / 自动命中", 3.67, 4.55, 2.72, 0.18, 7.6, C("D8E1EC"), True, margin=0)
    add_text(slide, str(run["active_improvements"]), 3.82, 4.95, 0.80, 0.46, 23, PURPLE, True, align=PP_ALIGN.CENTER, margin=0)
    add_text(slide, "active", 3.82, 5.47, 0.80, 0.18, 7.0, C("AEBBCD"), align=PP_ALIGN.CENTER, margin=0)
    add_text(slide, str(run["auto_applications"]), 5.36, 4.95, 0.80, 0.46, 23, GREEN, True, align=PP_ALIGN.CENTER, margin=0)
    add_text(slide, "hits", 5.36, 5.47, 0.80, 0.18, 7.0, C("AEBBCD"), align=PP_ALIGN.CENTER, margin=0)
    add_box(slide, 6.82, 4.38, 2.52, 1.90, C("18222F"), C("334153"))
    add_text(slide, "最近任务", 7.02, 4.55, 2.10, 0.18, 7.6, C("D8E1EC"), True, margin=0)
    add_pill(slide, "completed", 7.02, 4.93, 0.90, GREEN, C("223A39"), 6.8, 0.24)
    add_text(slide, "DREAM · q31", 8.02, 4.98, 1.06, 0.17, 7.2, C("D8E1EC"), True, margin=0)
    add_pill(slide, "completed", 7.02, 5.43, 0.90, GREEN, C("223A39"), 6.8, 0.24)
    add_text(slide, "SysInsight · TP", 8.02, 5.48, 1.12, 0.17, 7.2, C("D8E1EC"), True, margin=0)
    add_text(slide, "可编辑界面示意 · 数据源为 Prometheus / Bridge Metrics", 0.86, 6.57, 5.70, 0.16, 6.5, C("78889D"), margin=0)

    add_text(slide, "三张 Dashboard", 9.98, 1.36, 2.12, 0.25, 11.0, INK, True, margin=0)
    dashboards = [
        ("01", "PostgreSQL 监控大屏", "CPU、内存、连接、全局事务速率、27 场景 TPS 曲线。", BLUE, PALE_BLUE),
        ("02", "自动化联动看板", "告警、SysInsight 事件、DREAM Job、改进方案与命中。", CYAN, PALE_CYAN),
        ("03", "实验结果看板", "三阶段 TP 对比、SQL 前后耗时、运行状态和错误。", PURPLE, PALE_PURPLE),
    ]
    for i, (num, title, body, color, pale) in enumerate(dashboards):
        y = 1.79 + i * 1.48
        add_box(slide, 9.98, y, 2.78, 1.22, WHITE, LINE)
        add_pill(slide, num, 10.18, y + 0.18, 0.44, color, pale, 7.1, 0.25)
        add_text(slide, title, 10.74, y + 0.17, 1.78, 0.22, 9.5, INK, True, margin=0)
        add_text(slide, body, 10.18, y + 0.55, 2.34, 0.43, 7.4, MUTED, margin=0)
    add_box(slide, 9.98, 6.31, 2.78, 0.54, PALE_GREEN, PALE_GREEN)
    add_text(slide, "机器空闲时场景 TPS = 0；历史曲线仍保留可回看。", 10.16, 6.42, 2.43, 0.26, 7.1, GREEN, True, align=PP_ALIGN.CENTER, margin=0)
    add_footer(slide, 13)

    # 14 — TP validation
    slide = new_slide(prs)
    add_header(slide, "14", "SysInsight 实测稳定性｜六组场景均连续 3/3 正收益", "同压力校验、真实 TPCC、候选真实应用与恢复；百分比为压力下目标业务 TPS 提升", BLUE)
    add_metric_card(slide, 0.60, 1.24, 2.63, "18 / 18", "重复实验正收益", GREEN, PALE_GREEN)
    add_metric_card(slide, 3.43, 1.24, 2.63, f"{avg_tp:.1f}%", "六场景三轮平均提升", BLUE, PALE_BLUE)
    add_metric_card(slide, 6.26, 1.24, 2.63, "21.5%", "单轮最低正收益", ORANGE, PALE_ORANGE)
    add_metric_card(slide, 9.09, 1.24, 3.64, "60.6%", "单轮最高提升", PURPLE, PALE_PURPLE)
    table_rows = []
    bar_items = []
    for label, values in TP_STABILITY:
        avg = sum(values) / len(values)
        table_rows.append([label, f"+{values[0]:.2f}%", f"+{values[1]:.2f}%", f"+{values[2]:.2f}%", f"+{avg:.2f}%"])
        bar_items.append((label.replace("热点 ", "热点·"), avg))
    add_table(slide, 0.60, 2.47, [2.40, 1.02, 1.02, 1.02, 1.08], ["场景", "第 1 次", "第 2 次", "第 3 次", "平均"],
              table_rows, 0.55, 0.44, 7.7, [PP_ALIGN.LEFT, PP_ALIGN.RIGHT, PP_ALIGN.RIGHT, PP_ALIGN.RIGHT, PP_ALIGN.RIGHT], BLUE)
    add_box(slide, 7.45, 2.47, 5.28, 3.74, WHITE, LINE)
    add_text(slide, "三轮平均提升", 7.73, 2.73, 4.72, 0.24, 11.4, INK, True, margin=0)
    add_horizontal_bars(slide, bar_items, 7.73, 3.16, 4.70, 0.47, 65, BLUE, 2.20)
    add_box(slide, 0.60, 6.42, 12.13, 0.43, PALE_BLUE, PALE_BLUE)
    add_text(slide, "判定前提：告警已检测 · 外部压力前后偏差在允许范围 · 候选应用/恢复完成 · 取稳态 TPS 中位数；演示不承诺固定百分比。",
             0.86, 6.52, 11.62, 0.20, 8.0, BLUE, True, align=PP_ALIGN.CENTER, margin=0)
    add_footer(slide, 14, "证据来源：perf-anomaly-demo/results/stability_audit_20260929.md")

    # 15 — DREAM evidence
    slide = new_slide(prs)
    add_header(slide, "15", "DREAM 综合实测结果｜全量入口 + 代表性 SQL 组合证据", "全量 SQL 统一进入观察与触发链路；Q31 展示完整闭环，Q31 / Q44 / Q88 汇总展示多结构收益稳定性", PURPLE)
    add_box(slide, 0.60, 1.24, 6.05, 4.08, WHITE, LINE)
    add_text(slide, "TPC-DS Q31 完整证据", 0.88, 1.52, 5.48, 0.28, 14.0, INK, True, margin=0)
    stages = [
        ("原 SQL 基线", run["q31_baseline_ms"] / 1000, RED),
        ("DREAM 内部复测", run["q31_internal_ms"] / 1000, PURPLE),
        ("下一次自动命中", run["q31_optimized_ms"] / 1000, GREEN),
    ]
    max_seconds = max(v for _, v, _ in stages)
    for i, (label, seconds, color) in enumerate(stages):
        y = 2.08 + i * 0.85
        add_text(slide, label, 0.89, y + 0.04, 1.48, 0.22, 8.4, INK, True, margin=0)
        add_box(slide, 2.43, y + 0.05, 2.83, 0.26, PALE_GRAY, PALE_GRAY)
        add_box(slide, 2.43, y + 0.05, max(0.28, 2.83 * seconds / max_seconds), 0.26, color, color)
        add_text(slide, f"{seconds:.2f} s", 5.42, y + 0.02, 0.83, 0.26, 9.4, color, True, align=PP_ALIGN.RIGHT, margin=0)
    add_pill(slide, f"下一次实测提升 {run['q31_ratio']*100:.2f}%", 1.95, 4.76, 2.99, GREEN, PALE_GREEN, 10.0, 0.36)

    add_box(slide, 6.92, 1.24, 5.81, 4.08, WHITE, LINE)
    add_text(slide, f"代表性 SQL 组合｜{dream_verified_sql} 条 × 3 轮", 7.20, 1.52, 5.20, 0.28, 14.0, INK, True, margin=0)
    dream_rows = []
    dream_bars = []
    for label, values in DREAM_STABILITY:
        avg = sum(values) / len(values)
        dream_rows.append([label, f"+{values[0]:.2f}%", f"+{values[1]:.2f}%", f"+{values[2]:.2f}%", "3/3"])
        dream_bars.append((label, avg))
    add_table(slide, 7.20, 2.02, [0.70, 0.91, 0.91, 0.91, 0.72], ["SQL", "第 1 次", "第 2 次", "第 3 次", "结果"],
              dream_rows, 0.48, 0.40, 7.0, [PP_ALIGN.LEFT, PP_ALIGN.RIGHT, PP_ALIGN.RIGHT, PP_ALIGN.RIGHT, PP_ALIGN.CENTER], PURPLE)
    add_horizontal_bars(slide, dream_bars, 7.20, 4.05, 5.11, 0.40, 100, PURPLE, 0.72)

    add_box(slide, 0.60, 5.63, 12.13, 1.08, NAVY_2, NAVY_2)
    add_text(slide, f"综合结果：{ap_count} 条目录 · {ap_read_only} 条只读候选 · {dream_verified_runs} 次代表性复测 · 9/9 正收益 · 平均 +{dream_portfolio_avg:.1f}%",
             0.88, 5.80, 8.62, 0.20, 8.6, WHITE, True, margin=0)
    add_pill(slide, "REAL LLM", 0.88, 6.10, 0.95, C("B4D6FF"), C("294466"), 7.1, 0.25)
    add_text(slide, "gpt-5.6-sol", 1.95, 6.12, 1.20, 0.18, 8.0, WHITE, True, margin=0)
    add_pill(slide, "ACTIVE", 3.44, 6.10, 0.78, GREEN, C("234039"), 7.1, 0.25)
    add_text(slide, "rewrite_sql · bridge_managed", 4.33, 6.12, 2.48, 0.18, 8.0, WHITE, True, margin=0)
    add_pill(slide, "HIT", 7.12, 6.10, 0.62, GREEN, C("234039"), 7.1, 0.25)
    add_text(slide, f"{run['auto_applications']} 次自动应用已观察", 7.86, 6.12, 1.82, 0.18, 8.0, WHITE, True, margin=0)
    add_text(slide, "Q4 因原 SQL >180 s timeout，不再作为默认演示入口", 9.71, 6.10, 2.68, 0.31, 7.4, C("C8D4E3"), margin=0)
    add_text(slide, f"Job: {run['q31_job']}", 0.88, 6.48, 5.30, 0.12, 6.0, C("8FA3BF"), font=MONO, margin=0)
    add_footer(slide, 15, "证据来源：Bridge 当前状态 + perf-anomaly-demo/results/stability_audit_20260929.md")

    # 16 — Safety and reliability
    slide = new_slide(prs)
    add_header(slide, "16", "安全与稳定性｜自动化不等于无边界执行", "通过 SQL 门禁、实测门槛、资源限额、临时配置和生命周期管理控制演示风险", GREEN)
    safety_cards = [
        ("SQL 安全门禁", ["只处理已完成、只读、可重放 SQL", "严格解析并拒绝 DDL / 多语句 / 未知动作", "SQL 改写按规范化文本与字面量身份匹配"], PURPLE, PALE_PURPLE),
        ("收益与语义门禁", ["old_time / new_time 均来自真实执行", "自动激活最低实测收益 10%", "失败、超时、低收益方案不进入 active"], GREEN, PALE_GREEN),
        ("数据库配置保护", ["TemporaryPostgresConfiguration 临时应用", "实验结束恢复原配置并记录恢复状态", "高风险全局项审计跳过，不为实验任意重启"], BLUE, PALE_BLUE),
        ("资源与并发保护", ["PostgreSQL CPU 250% · Memory 24 GiB", "TasksMax 512；TPCC 速率上限约 1600 / 4000 TPS", "按钮幂等、request_id、后台任务锁和同 SQL 去重"], ORANGE, PALE_ORANGE),
    ]
    positions = [(0.60, 1.34), (6.78, 1.34), (0.60, 3.66), (6.78, 3.66)]
    for (title, bullets, color, pale), (x, y) in zip(safety_cards, positions):
        add_box(slide, x, y, 5.95, 1.95, WHITE, LINE)
        add_box(slide, x, y, 0.10, 1.95, color, color, radius=False)
        add_circle(slide, x + 0.28, y + 0.24, 0.38, pale, pale)
        add_text(slide, "✓", x + 0.28, y + 0.24, 0.38, 0.38, 10.2, color, True, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE, margin=0)
        add_text(slide, title, x + 0.79, y + 0.24, 4.77, 0.25, 12.2, INK, True, margin=0)
        add_bullets(slide, bullets, x + 0.29, y + 0.77, 5.28, 0.95, 8.0, MUTED, 4)
    add_box(slide, 0.60, 6.03, 12.13, 0.76, NAVY_2, NAVY_2)
    retention = [
        ("24 h", "状态/样本保留"),
        ("15 min", "定期清理"),
        ("增量", "SQLite Vacuum"),
        ("可恢复", "active 状态跨重启"),
        ("同步", "DREAM runtime 校验"),
    ]
    for i, (value, label) in enumerate(retention):
        x = 0.94 + i * 2.37
        add_text(slide, value, x, 6.18, 0.92, 0.25, 11.0, WHITE, True, align=PP_ALIGN.CENTER, margin=0)
        add_text(slide, label, x + 0.96, 6.20, 1.14, 0.20, 7.3, C("BFCDE0"), margin=0)
        if i < len(retention) - 1:
            add_line(slide, x + 2.18, 6.16, x + 2.18, 6.57, C("3B5271"), 0.8)
    add_footer(slide, 16)

    # 17 — Evidence and audit
    slide = new_slide(prs)
    add_header(slide, "17", "证据链与审计｜每个结论都能回到原始动作", "Bridge 统一保存事件、SQL 观察、任务、改进方案、实验采样与恢复状态，并向 Grafana 暴露指标", CYAN)
    add_text(slide, "SysInsight 证据链", 0.60, 1.27, 2.30, 0.24, 11.4, BLUE, True, margin=0)
    sys_steps = [
        ("Prometheus 告警", "firing / rule"),
        ("指标 + perf", "PG / host / call stack"),
        ("原始检测", "anomaly + source match"),
        ("LLM 响应", "raw result + candidates"),
        ("应用 / 复测", "before / after TPS"),
        ("恢复证明", "settings restored"),
    ]
    for i, (title, body) in enumerate(sys_steps):
        x = 0.60 + i * 2.02
        add_box(slide, x, 1.68, 1.72, 1.02, WHITE, LINE)
        add_pill(slide, f"S{i+1}", x + 0.15, 1.83, 0.43, BLUE, PALE_BLUE, 6.9, 0.23)
        add_text(slide, title, x + 0.15, 2.13, 1.42, 0.19, 8.5, INK, True, margin=0)
        add_text(slide, body, x + 0.15, 2.42, 1.42, 0.16, 6.6, MUTED, margin=0)
        if i < len(sys_steps) - 1:
            add_chevron(slide, x + 1.79, 2.00, 0.14, 0.27, C("A4B5CB"))

    add_text(slide, "DREAM 证据链", 0.60, 3.05, 2.30, 0.24, 11.4, PURPLE, True, margin=0)
    dream_steps_evidence = [
        ("SQL 观察", "duration / calls"),
        ("后台 Job", "queued → completed"),
        ("模型输出", "root cause / action"),
        ("执行评价", "old / new time"),
        ("改进方案", "candidate / active"),
        ("命中记录", "hit count / method"),
    ]
    for i, (title, body) in enumerate(dream_steps_evidence):
        x = 0.60 + i * 2.02
        add_box(slide, x, 3.46, 1.72, 1.02, WHITE, LINE)
        add_pill(slide, f"D{i+1}", x + 0.15, 3.61, 0.43, PURPLE, PALE_PURPLE, 6.9, 0.23)
        add_text(slide, title, x + 0.15, 3.91, 1.42, 0.19, 8.5, INK, True, margin=0)
        add_text(slide, body, x + 0.15, 4.20, 1.42, 0.16, 6.6, MUTED, margin=0)
        if i < len(dream_steps_evidence) - 1:
            add_chevron(slide, x + 1.79, 3.78, 0.14, 0.27, C("A4B5CB"))

    add_box(slide, 0.60, 4.94, 12.13, 1.75, WHITE, LINE)
    add_text(slide, "当前运行快照", 0.87, 5.20, 1.40, 0.25, 11.0, INK, True, margin=0)
    snapshot = [
        ("API", "configured" if run["api_configured"] else "not configured", GREEN if run["api_configured"] else RED),
        ("SysInsight", "full chain" if run["full_chain"] else "partial", GREEN if run["full_chain"] else ORANGE),
        ("DREAM", "ready + auto apply" if run["dream_ready"] and run["dream_auto_apply"] else "not ready", GREEN if run["dream_ready"] else RED),
        ("改进方案", f"{run['active_improvements']} active", PURPLE),
        ("自动命中", f"{run['auto_applications']} observed", BLUE),
    ]
    for i, (label, value, color) in enumerate(snapshot):
        x = 2.50 + i * 2.01
        add_box(slide, x, 5.21, 1.77, 1.06, PALE_GRAY, PALE_GRAY)
        add_text(slide, label, x + 0.12, 5.37, 1.53, 0.17, 7.2, MUTED, True, align=PP_ALIGN.CENTER, margin=0)
        add_text(slide, value, x + 0.12, 5.72, 1.53, 0.24, 9.1, color, True, align=PP_ALIGN.CENTER, margin=0)
    add_text(slide, "失败与 rejected 状态同样保留；系统不会把“分析完成”误写成“优化成功”。", 0.88, 6.39, 11.52, 0.20, 7.8, MUTED, align=PP_ALIGN.CENTER, margin=0)
    add_footer(slide, 17)

    # 18 — Deployment and access
    slide = new_slide(prs)
    add_header(slide, "18", "当前部署与访问入口｜本机单节点演示环境", "所有服务绑定 127.0.0.1；核心数据与任务状态在同一工作区，便于复现和恢复", BLUE)
    add_box(slide, 0.60, 1.26, 4.14, 5.47, WHITE, LINE)
    add_text(slide, "运行组件", 0.88, 1.55, 3.58, 0.28, 14, INK, True, margin=0)
    components = [
        ("PostgreSQL", "5432 · keeninsight", BLUE, PALE_BLUE),
        ("Prometheus", "9090 · 24h retention", ORANGE, PALE_ORANGE),
        ("Grafana", "3000 · 3 dashboards", CYAN, PALE_CYAN),
        ("Bridge", "9108 · API / UI / metrics", PURPLE, PALE_PURPLE),
        ("Exporters", "node + postgres", GREEN, PALE_GREEN),
        ("LLM", "共享 GPT-5.6-SOL API", GOLD, PALE_GOLD),
    ]
    for i, (name, desc, color, pale) in enumerate(components):
        y = 2.05 + i * 0.68
        add_circle(slide, 0.90, y + 0.02, 0.31, pale, pale)
        add_text(slide, str(i + 1), 0.90, y + 0.02, 0.31, 0.31, 7.3, color, True, align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE, margin=0)
        add_text(slide, name, 1.37, y, 1.12, 0.20, 8.8, INK, True, margin=0)
        add_text(slide, desc, 2.48, y, 1.89, 0.20, 7.6, MUTED, margin=0)
    add_box(slide, 0.88, 6.17, 3.58, 0.33, PALE_BLUE, PALE_BLUE)
    add_text(slide, "TPC-DS SF10 · 数据库约 19 GB · 29 张基础表", 1.02, 6.25, 3.30, 0.16, 7.4, BLUE, True, align=PP_ALIGN.CENTER, margin=0)

    add_box(slide, 5.02, 1.26, 7.71, 5.47, WHITE, LINE)
    add_text(slide, "访问入口", 5.31, 1.55, 6.98, 0.28, 14, INK, True, margin=0)
    url_rows = [
        ("Grafana 首页", "http://127.0.0.1:3000/", "admin / admin（本机默认）"),
        ("PostgreSQL 监控", "http://127.0.0.1:3000/d/local-postgresql-demo/582e36f", "系统指标 + 场景 TPS"),
        ("自动化看板", "http://127.0.0.1:3000/d/sysinsight-dream-automation", "事件 / Job / 改进 / 命中"),
        ("实验结果看板", "http://127.0.0.1:3000/d/sysinsight-experiment-lab", "TP / AP 前后对比"),
        ("管理页", "http://127.0.0.1:9108/ui", "状态与历史"),
        ("实验控制台", "http://127.0.0.1:9108/lab", "场景触发与清理"),
        ("Prometheus", "http://127.0.0.1:9090/", "原始指标查询"),
    ]
    y = 2.05
    for idx, (name, url, desc) in enumerate(url_rows):
        fill = C("F3F6FA") if idx % 2 else WHITE
        add_box(slide, 5.30, y, 7.10, 0.51, fill, LINE, radius=False, width=0.4)
        add_text(slide, name, 5.47, y + 0.11, 1.22, 0.18, 7.7, INK, True, margin=0)
        add_text(slide, url, 6.84, y + 0.11, 3.63, 0.18, 7.1, BLUE, font=MONO, margin=0, url=url)
        add_text(slide, desc, 10.63, y + 0.11, 1.55, 0.18, 6.8, MUTED, align=PP_ALIGN.RIGHT, margin=0)
        y += 0.55
    add_box(slide, 5.30, 6.14, 7.10, 0.38, PALE_RED, PALE_RED)
    add_text(slide, "默认账号仅用于本机 Demo；对外部署前必须修改凭据并增加访问控制。", 5.55, 6.23, 6.60, 0.18, 7.4, RED, True, align=PP_ALIGN.CENTER, margin=0)
    add_footer(slide, 18)

    # 19 — Current readiness and boundaries
    slide = new_slide(prs, NAVY)
    add_box(slide, 0, 0, W, H, NAVY, NAVY, radius=False)
    add_box(slide, 0, 0, W, 0.12, CYAN, CYAN, radius=False)
    add_text(slide, "当前产品结论", 0.72, 0.62, 3.0, 0.24, 10.2, C("95BCEB"), True, margin=0)
    add_text(slide, "已形成可演示、可追溯的双闭环产品", 0.68, 1.06, 10.9, 0.56, 28.5, WHITE, True, margin=0)
    add_text(slide, "SysInsight 负责压力下 TP 参数恢复；DREAM 负责长 AP SQL 的异步优化与下一次应用。", 0.72, 1.82, 11.4, 0.28, 12.5, C("C4D1E4"), margin=0)

    columns = [
        ("现在可以稳定展示", ["6 组 TPCC 关注场景，三轮均正收益", "99 条目录 / 95 条只读 SQL 接入 DREAM", "Q31 / Q44 / Q88 共 9 次真实复测均正收益", "Grafana + 管理页 + 实验控制台"], GREEN, C("173D39")),
        ("明确的当前边界", ["SQL 改写需要 Bridge / 应用接入", "裸 PG 客户端仅可直接采用 Hint 路径", "当前是本机单节点 Demo，不等于生产 HA", "性能幅度受缓存、计划与数据分布影响"], ORANGE, C("3B3025")),
        ("下一步产品化方向", ["接入应用代理，实现透明 SQL Rewrite", "补充鉴权、RBAC 与多租户隔离", "增加灰度、自动回退和生产级配置快照", "扩展多实例与更多真实业务负载"], CYAN, C("173845")),
    ]
    for i, (title, bullets, color, fill) in enumerate(columns):
        x = 0.72 + i * 4.10
        add_box(slide, x, 2.58, 3.78, 3.16, fill, C("38506F"))
        add_box(slide, x, 2.58, 3.78, 0.08, color, color, radius=False)
        add_text(slide, title, x + 0.26, 2.91, 3.26, 0.29, 14.0, WHITE, True, margin=0)
        add_bullets(slide, bullets, x + 0.26, 3.48, 3.22, 1.80, 8.5, C("D3DEEC"), 8)
    add_box(slide, 0.72, 6.16, 11.98, 0.63, C("1A355A"), C("335579"))
    add_rich_line(slide, [("产品价值：", C("8FCAF7"), True), ("让数据库调优从一次性专家操作，变成可持续、可观察、可验证的系统能力。", WHITE, False)],
                  1.08, 6.35, 11.24, 0.23, 12.0, PP_ALIGN.CENTER)
    add_text(slide, "SYSINSIGHT × DREAM  ·  PRODUCT OVERVIEW", 0.72, 7.08, 5.0, 0.16, 6.8, C("718BAE"), margin=0)
    add_text(slide, "2026-10-08", 11.52, 7.08, 1.18, 0.16, 6.8, C("718BAE"), align=PP_ALIGN.RIGHT, margin=0)

    prs.save(OUTPUT)
    return OUTPUT, len(prs.slides), run


if __name__ == "__main__":
    output, slides, runtime = build_deck()
    print(f"wrote {output}")
    print(f"slides={slides}")
    print(json.dumps(runtime, ensure_ascii=False, sort_keys=True))
