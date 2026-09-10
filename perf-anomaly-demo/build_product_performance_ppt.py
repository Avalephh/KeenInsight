#!/usr/bin/env python3
"""Build the two-page product performance showcase from measured artifacts.

The deck intentionally reads all TPS and selected PostgreSQL values from the
recorded case_result.json/API result files.  It does not contain repair-result
presets.
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT.parent / "产品性能展示.pptx"
FONT = "Noto Sans CJK SC"


# The labels describe the measured scenarios; numeric values and API values
# are loaded below from the real artifacts.
CASE_SPECS = [
    {
        "id": "D01",
        "path": "results/tpcc_api_validation/20260910_230000_d01_expanded_api/d01_work_mem_sort/case_result.json",
        "scenario": "订单金额全量排序",
        "pressure": "全量排序与 TPCC 点查询并发；排序/哈希工作区被外部报表占用",
        "params": [("work_mem", "work_mem")],
        "adjustment": "增加排序/哈希工作区，缓解外部报表的内存竞争",
    },
    {
        "id": "D05",
        "path": "results/tpcc_api_validation/20260910_001000_d05_d08_expanded_api/d05_parallel_tuple_pressure/case_result.json",
        "scenario": "高基数聚合/结果传输",
        "pressure": "商品汇总产生大量分组结果；并行结果传输与 OLTP 争用",
        "params": [("parallel_tuple_cost", "parallel_tuple_cost")],
        "adjustment": "提高并行结果传输成本，避免高基数结果不必要地并行传输",
    },
    {
        "id": "D06",
        "path": "results/tpcc_api_validation/20260910_001000_d05_d08_expanded_api/d06_parallel_setup_pressure/case_result.json",
        "scenario": "重复聚合/启动开销",
        "pressure": "报表连续重复提交；并行 worker 启动与建链开销叠加",
        "params": [("parallel_setup_cost", "parallel_setup_cost")],
        "adjustment": "提高并行启动成本，压低重复报表频繁启动 worker 的开销",
    },
    {
        "id": "D08",
        "path": "results/tpcc_api_validation/20260910_001000_d05_d08_expanded_api/d08_parallel_worker_cap/case_result.json",
        "scenario": "大表连接/worker 争用",
        "pressure": "订单明细×商品表集中扫描；争用全局并行工作进程",
        "params": [("max_parallel_workers_per_gather", "max_parallel_workers_per_gather")],
        "adjustment": "降低单查询并行 worker 上限，减少全局并行进程争用",
    },
    {
        "id": "C05",
        "path": "results/tpcc_api_validation/20260910_003000_c05_c19_expanded_api/c05_jit_threshold/case_result.json",
        "scenario": "复杂表达式/JIT 编译",
        "pressure": "复杂表达式查询触发 JIT 编译/优化成本；短事务被拖慢",
        "params": [("jit", "jit"), ("jit_above_cost", "jit_above_cost")],
        "adjustment": "关闭 JIT 并提高 JIT 触发阈值，避免短事务承担编译/优化开销",
    },
    {
        "id": "C19",
        "path": "results/tpcc_api_validation/20260910_003000_c05_c19_expanded_api/c19_cpu_tuple_parallel/case_result.json",
        "scenario": "库存范围/元组成本误判",
        "pressure": "整仓范围扩大；每元组成本估计使规划器偏向串行位图路径",
        "params": [("cpu_tuple_cost", "cpu_tuple_cost")],
        "adjustment": "提高每元组 CPU 成本，让规划器重新评估扩大范围的执行路径",
    },
]


NAVY = RGBColor(17, 31, 56)
INK = RGBColor(28, 42, 66)
MUTED = RGBColor(91, 108, 132)
LIGHT = RGBColor(246, 248, 252)
WHITE = RGBColor(255, 255, 255)
LINE = RGBColor(218, 226, 237)
BLUE = RGBColor(48, 112, 221)
CYAN = RGBColor(36, 168, 185)
GREEN = RGBColor(27, 156, 111)
ORANGE = RGBColor(232, 132, 42)
RED = RGBColor(213, 83, 79)
PALE_BLUE = RGBColor(232, 241, 255)
PALE_CYAN = RGBColor(230, 248, 248)
PALE_GREEN = RGBColor(229, 247, 239)
PALE_ORANGE = RGBColor(255, 243, 226)


def fmt_num(value: float) -> str:
    return f"{value:,.2f}"


def fmt_param(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def previous_setting(pre_settings, key):
    value = pre_settings.get(key)
    if isinstance(value, dict):
        return value.get("setting")
    return value


def load_cases():
    cases = []
    for spec in CASE_SPECS:
        case_path = ROOT / spec["path"]
        case = json.loads(case_path.read_text(encoding="utf-8"))
        decision = case["decision"]
        api = case["api"]
        config = api["selected_configuration"]
        pre_settings = case["temporary_application"].get("pre_settings", {})
        artifact_path = Path(api["artifact"])
        api_artifact = json.loads(artifact_path.read_text(encoding="utf-8"))

        # These assertions keep the deck tied to actual completed API runs.
        assert decision["complete_case"] is True, spec["id"]
        assert decision["pressure_drop_over_20pct"] is True, spec["id"]
        assert decision["repair_rise_over_20pct"] is True, spec["id"]
        assert case["preset_case_definition_not_used_as_repair"] is True, spec["id"]
        assert len(api_artifact["llm_calls"]) >= 10, spec["id"]
        assert api["selection"]["parsed_acquisition_count"] == 5, spec["id"]

        key_values = []
        key_changes = []
        for label, key in spec["params"]:
            assert key in config, f"{spec['id']}: missing {key}"
            key_values.append(f"{label}={fmt_param(config[key])}")
            old_value = previous_setting(pre_settings, key)
            unit = " kB" if key == "work_mem" else ""
            if old_value is None:
                key_changes.append(f"{label} {fmt_param(config[key])}{unit}")
            else:
                key_changes.append(
                    f"{label} {fmt_param(old_value)}→{fmt_param(config[key])}{unit}"
                )

        baseline = float(decision["baseline_control_tps"])
        pressure = float(decision["pressure_control_tps"])
        repaired = float(decision["repaired_control_tps"])
        cases.append(
            {
                **spec,
                "case_path": case_path,
                "api_artifact_path": artifact_path,
                "title": case["title"],
                "external_event": case["external_event"],
                "apply_status": case["temporary_application"]["status"],
                "config_count": len(config),
                "candidate_count": api["selection"]["parsed_acquisition_count"],
                "key_values": key_values,
                "key_changes": key_changes,
                "baseline": baseline,
                "pressure_tps": pressure,
                "repaired": repaired,
                "pressure_retention": pressure / baseline,
                "pressure_drop": 1 - pressure / baseline,
                "recovery_ratio": repaired / pressure,
            }
        )
    return cases


def set_run_font(run, size, color, bold=False, italic=False):
    run.font.name = FONT
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = color
    # Set the East Asian font as well; this is honored by PowerPoint when the
    # deck is opened on a machine that has the selected CJK font.
    try:
        from pptx.oxml.ns import qn

        run._r.get_or_add_rPr().set(qn("a:ea"), FONT)
    except Exception:
        pass


def add_text(slide, value, x, y, w, h, size=12, color=INK, bold=False,
             align=PP_ALIGN.LEFT, valign=MSO_ANCHOR.TOP, margin=0.06,
             italic=False):
    shape = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = shape.text_frame
    tf.clear()
    tf.word_wrap = True
    tf.margin_left = Inches(margin)
    tf.margin_right = Inches(margin)
    tf.margin_top = Inches(margin)
    tf.margin_bottom = Inches(margin)
    tf.vertical_anchor = valign
    paragraph = tf.paragraphs[0]
    paragraph.alignment = align
    run = paragraph.add_run()
    run.text = str(value)
    set_run_font(run, size, color, bold, italic)
    return shape


def add_box(slide, x, y, w, h, fill, line=LINE, radius=True):
    shape = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE if radius else MSO_SHAPE.RECTANGLE,
        Inches(x), Inches(y), Inches(w), Inches(h),
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill
    shape.line.color.rgb = line
    shape.line.width = Pt(0.7)
    return shape


def add_header(slide, number, title, subtitle):
    add_box(slide, 0, 0, 13.333, 1.04, NAVY, NAVY, radius=False)
    add_text(slide, f"PERFORMANCE SHOWCASE  /  {number}", 0.52, 0.12, 2.4, 0.20,
             size=7.5, color=RGBColor(158, 190, 235), bold=True, margin=0)
    add_text(slide, title, 0.52, 0.31, 12.0, 0.40, size=24, color=WHITE,
             bold=True, valign=MSO_ANCHOR.MIDDLE, margin=0)
    add_text(slide, subtitle, 0.54, 0.78, 12.0, 0.18, size=9.5,
             color=RGBColor(193, 207, 228), margin=0)


def add_footer(slide, text, color=MUTED):
    add_text(slide, text, 0.52, 7.22, 12.2, 0.16, size=6.7, color=color, margin=0)


def build_deck(cases):
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]
    prs.core_properties.title = "产品性能展示：Prometheus / perf / SysInsight / GPT API"
    prs.core_properties.subject = "基于 PostgreSQL 12.22 + TPCC 实测数据的两页性能展示"
    prs.core_properties.author = "Codex"

    # Page 1: product loop and headline performance.
    slide = prs.slides.add_slide(blank)
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = LIGHT
    add_header(slide, "01", "产品功效｜从外部压力到可验证恢复",
               "PostgreSQL 12.22 + TPCC 本机实测  ·  真实 GPT API 配置  ·  结果来自已保存的复测证据")

    flow = [
        ("01", "外部压力", "TPCC 持续负载\n报表 / 连接 / 并行\n进入同一数据库", BLUE, PALE_BLUE),
        ("02", "可观测", "Prometheus 告警\n触发 perf 采样\n捕获真实热点", CYAN, PALE_CYAN),
        ("03", "原始检测", "SysInsight 原始函数\n做源函数关联\n形成 5 个候选", ORANGE, PALE_ORANGE),
        ("04", "API 决策", "调用 GPT API\n解析 response\n选择 1 个配置", RGBColor(125, 91, 191), RGBColor(241, 235, 253)),
        ("05", "应用复测", "临时会话应用\nTPCC 控制复测\n恢复后还原配置", GREEN, PALE_GREEN),
    ]
    xs = [0.52, 3.03, 5.54, 8.05, 10.56]
    for i, (num, title, body, accent, pale) in enumerate(flow):
        add_box(slide, xs[i], 1.35, 2.16, 1.50, WHITE, LINE)
        add_box(slide, xs[i], 1.35, 2.16, 0.08, accent, accent, radius=False)
        add_text(slide, num, xs[i] + 0.13, 1.51, 0.35, 0.20, size=8.5,
                 color=accent, bold=True, margin=0)
        add_text(slide, title, xs[i] + 0.13, 1.73, 1.82, 0.25, size=14,
                 color=INK, bold=True, margin=0)
        add_text(slide, body, xs[i] + 0.13, 2.08, 1.88, 0.62, size=9.2,
                 color=MUTED, margin=0)
        if i < len(flow) - 1:
            chevron = slide.shapes.add_shape(
                MSO_SHAPE.CHEVRON, Inches(xs[i] + 2.22), Inches(1.91),
                Inches(0.20), Inches(0.31)
            )
            chevron.fill.solid()
            chevron.fill.fore_color.rgb = RGBColor(178, 192, 211)
            chevron.line.fill.background()

    add_box(slide, 0.52, 3.12, 12.28, 0.52, NAVY, NAVY)
    add_text(slide, "产品功效：发现  →  归因  →  生成  →  应用  →  复测；把外部压力导致的退化变成可解释、可验证的调优闭环。",
             0.73, 3.22, 11.86, 0.29, size=11.2, color=WHITE, bold=True,
             valign=MSO_ANCHOR.MIDDLE, margin=0)

    min_recovery = min(c["recovery_ratio"] for c in cases) * 100
    max_recovery = max(c["recovery_ratio"] for c in cases) * 100
    kpis = [
        ("6", "纳入展示的实测场景", BLUE, PALE_BLUE, 22),
        ("6 / 6", "压力下降、修复上升均超过 20%", GREEN, PALE_GREEN, 19),
        ("5 → 1", "候选配置经原始选择函数收敛", ORANGE, PALE_ORANGE, 19),
        (f"{min_recovery:.2f}%–{max_recovery:.2f}%", "修复后 TPS ÷ 压力阶段 TPS", CYAN, PALE_CYAN, 13.5),
    ]
    for i, (value, label, accent, pale, value_size) in enumerate(kpis):
        x = 0.52 + i * 3.07
        add_box(slide, x, 3.88, 2.83, 1.08, WHITE, LINE)
        add_box(slide, x, 3.88, 0.09, 1.08, accent, accent, radius=False)
        add_text(slide, value, x + 0.20, 4.00, 2.48, 0.38, size=value_size,
                 color=accent, bold=True, align=PP_ALIGN.CENTER,
                 valign=MSO_ANCHOR.MIDDLE, margin=0)
        add_text(slide, label, x + 0.16, 4.49, 2.52, 0.27, size=8.3,
                 color=MUTED, align=PP_ALIGN.CENTER, margin=0)

    # A compact measured-average chart gives the audience the direction of
    # change without hiding the per-case values shown on page 2.
    avg_base = mean(c["baseline"] for c in cases)
    avg_pressure = mean(c["pressure_tps"] for c in cases)
    avg_repaired = mean(c["repaired"] for c in cases)
    add_box(slide, 0.52, 5.25, 12.28, 1.65, WHITE, LINE)
    add_text(slide, "六组简单均值（TPS）", 0.75, 5.42, 2.20, 0.24, size=11,
             color=INK, bold=True, margin=0)
    add_text(slide, "仅作展示汇总；单场景数据见下一页", 0.75, 5.70, 2.45, 0.18,
             size=7.2, color=MUTED, margin=0)
    chart_x, chart_w = 3.35, 8.55
    chart_values = [("基线", avg_base, BLUE), ("外部压力", avg_pressure, RED),
                    ("API 修复", avg_repaired, GREEN)]
    max_value = avg_base
    for row, (label, value, color) in enumerate(chart_values):
        y = 5.40 + row * 0.38
        add_text(slide, label, 2.72, y, 0.56, 0.22, size=8.1, color=MUTED,
                 align=PP_ALIGN.RIGHT, margin=0)
        add_box(slide, chart_x, y + 0.04, chart_w, 0.16, RGBColor(239, 243, 248), RGBColor(239, 243, 248), radius=False)
        add_box(slide, chart_x, y + 0.04, chart_w * value / max_value, 0.16, color, color, radius=False)
        add_text(slide, fmt_num(value), chart_x + chart_w + 0.12, y - 0.01, 1.06, 0.25,
                 size=8.5, color=color, bold=True, margin=0)
    add_text(slide, "恢复不是“回到基线”的承诺：本页指标表达的是相对压力阶段的 TPS 恢复比例；每组配置由真实 API response 解析得到。",
             0.75, 6.57, 11.75, 0.18, size=7.2, color=MUTED, margin=0)
    add_footer(slide, "实测环境：本机 PostgreSQL 12.22 / keeninsight_tpcc / TPCC；数据证据：tpcc_api_validation 六组 case_result.json + sysinsight_api/result.json")

    # Page 2: exact per-case evidence.
    slide = prs.slides.add_slide(blank)
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = LIGHT
    add_header(slide, "02", "六类压力场景｜压力特征 × 恢复方法 × 实测比例",
               "每一行都是一次真实 API 配置链路：5 个候选配置 → 原始选择函数选 1 个 → 临时应用 → TPCC 控制复测")

    x0 = 0.42
    widths = [1.72, 3.18, 3.72, 2.62, 0.92]
    headers = ["场景", "外部压力特征", "恢复方法 / API 关键配置", "控制 TPS：基线 → 压力 → 修复", "恢复比例"]
    cx = x0
    for width, header in zip(widths, headers):
        add_box(slide, cx, 1.22, width, 0.42, NAVY, NAVY, radius=False)
        add_text(slide, header, cx + 0.05, 1.29, width - 0.10, 0.24, size=8.0,
                 color=WHITE, bold=True, align=PP_ALIGN.CENTER,
                 valign=MSO_ANCHOR.MIDDLE, margin=0)
        cx += width + 0.02

    row_y = 1.69
    row_h = 0.78
    accents = [BLUE, CYAN, ORANGE, RGBColor(125, 91, 191), RED, GREEN]
    for i, (case, accent) in enumerate(zip(cases, accents)):
        fill = WHITE if i % 2 == 0 else RGBColor(241, 245, 250)
        cx = x0
        # Scenario cell.
        add_box(slide, cx, row_y, widths[0], row_h, fill, LINE, radius=False)
        add_box(slide, cx, row_y, 0.08, row_h, accent, accent, radius=False)
        add_text(slide, case["id"], cx + 0.15, row_y + 0.10, 0.55, 0.20, size=8.2,
                 color=accent, bold=True, margin=0)
        add_text(slide, case["scenario"], cx + 0.15, row_y + 0.33, widths[0] - 0.25, 0.31,
                 size=9.1, color=INK, bold=True, margin=0)
        cx += widths[0] + 0.02

        # Pressure cell.
        add_box(slide, cx, row_y, widths[1], row_h, fill, LINE, radius=False)
        pressure_text = f"{case['pressure']}\n压力后保留 {case['pressure_retention'] * 100:.2f}%（下降 {case['pressure_drop'] * 100:.2f}%）"
        add_text(slide, pressure_text, cx + 0.09, row_y + 0.10, widths[1] - 0.16, 0.58,
                 size=8.0, color=INK, margin=0)
        cx += widths[1] + 0.02

        # Method cell.
        add_box(slide, cx, row_y, widths[2], row_h, fill, LINE, radius=False)
        method_text = f"主要调整：{'; '.join(case['key_changes'])}\n目的：{case['adjustment']}"
        add_text(slide, method_text, cx + 0.09, row_y + 0.08, widths[2] - 0.16, 0.63,
                 size=7.45, color=INK, margin=0)
        cx += widths[2] + 0.02

        # TPS cell.
        add_box(slide, cx, row_y, widths[3], row_h, fill, LINE, radius=False)
        tps_text = f"{fmt_num(case['baseline'])}\n→ {fmt_num(case['pressure_tps'])}\n→ {fmt_num(case['repaired'])}"
        add_text(slide, tps_text, cx + 0.05, row_y + 0.09, widths[3] - 0.10, 0.57,
                 size=8.65, color=INK, bold=True, align=PP_ALIGN.CENTER,
                 valign=MSO_ANCHOR.MIDDLE, margin=0)
        cx += widths[3] + 0.02

        # Recovery cell.
        add_box(slide, cx, row_y, widths[4], row_h, PALE_GREEN, RGBColor(180, 226, 202), radius=False)
        add_text(slide, f"{case['recovery_ratio'] * 100:.2f}%", cx + 0.02, row_y + 0.22,
                 widths[4] - 0.04, 0.25, size=9.2, color=GREEN, bold=True,
                 align=PP_ALIGN.CENTER, valign=MSO_ANCHOR.MIDDLE, margin=0)
        row_y += row_h + 0.035

    add_box(slide, 0.42, 6.18, 12.48, 0.68, NAVY, NAVY)
    add_text(slide, "恢复比例定义：修复后控制 TPS ÷ 压力阶段控制 TPS。\n配置来自真实 GPT API response 的解析结果，不是预设修复值；每行的 26–27 项是完整 API 配置，关键参数仅用于说明主要调优方向。",
             0.64, 6.28, 12.05, 0.47, size=8.0, color=WHITE, margin=0)
    add_footer(slide, "数据证据：/root/new/perf-anomaly-demo/results/tpcc_api_validation/；d07 未达到修复上升 >20% 标准，未计入以上六组。", color=MUTED)

    prs.save(OUTPUT)
    return OUTPUT


if __name__ == "__main__":
    loaded = load_cases()
    output = build_deck(loaded)
    print(f"created {output}")
    for case in loaded:
        print(case["id"], case["baseline"], case["pressure_tps"], case["repaired"], case["recovery_ratio"])
