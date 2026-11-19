import os
import re
import zipfile
from pathlib import Path


STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
LEGACY = STATIC / "legacy"


def test_original_english_page_set_is_complete_and_wired():
    expected = {
        "template.html",
        "host-list.html",
        "realtime-monitor.html",
        "alert-list.html",
        "alert-detail.html",
        "alert-rules.html",
        "code-source.html",
        "manual.html",
    }
    assert {path.name for path in LEGACY.glob("*.html")} == expected
    for removed in ("sql-log.html", "sql-detail.html", "dba-rules.html", "tuning-knowledge.html"):
        assert not (LEGACY / removed).exists()
    for path in LEGACY.glob("*.html"):
        content = path.read_text(encoding="utf-8")
        assert '<style>' in content
        assert '/static/legacy-runtime.js' in content
        assert '/static/legacy-pages.js' in content


def _strip_removed_sql_menu(html):
    html = re.sub(
        r"\n\s*<div class=\"menu-group-title\">SQL level</div>.*?(?=\n\s*<div class=\"menu-group-title\">System Settings</div>)",
        "",
        html,
        flags=re.S,
    )
    return re.sub(
        r"\n\s*<div class=\"menu-item\" onclick=\"location\.href='tuning-knowledge\.html'\">.*?Tuning-derived Knowledge\s*</div>",
        "",
        html,
        flags=re.S,
    )


def test_legacy_shell_matches_supplied_archive_before_adapters():
    archive = Path(os.environ.get("KI_LEGACY_ARCHIVE", "/root/demo/English.zip"))
    if not archive.is_file():
        return
    with zipfile.ZipFile(archive) as zipped:
        for item in zipped.infolist():
            if item.is_dir() or not item.filename.lower().endswith(".html"):
                continue
            name = Path(item.filename).name
            if not (LEGACY / name).is_file():
                continue
            expected = zipped.read(item.filename).decode("utf-8", "replace")
            actual = (LEGACY / name).read_text(encoding="utf-8")
            for script in (
                '\n    <script src="/static/legacy-runtime.js"></script>',
                '\n    <script src="/static/legacy-pages.js"></script>',
            ):
                actual = actual.replace(script, "")
            actual = _strip_removed_sql_menu(actual)
            expected = _strip_removed_sql_menu(expected)
            def normalize(value):
                value = value.replace("\r\n", "\n").replace("\r", "\n")
                return "\n".join(line for line in value.split("\n") if line.strip())

            assert normalize(actual) == normalize(expected)


def test_advanced_console_is_preserved_separately():
    assert (STATIC / "console.html").is_file()
    assert (STATIC / "console-wait.html").is_file()
    assert (STATIC / "console.js").is_file()
    assert (STATIC / "console.css").is_file()
    assert 'href="/static/console.css' in (STATIC / "console.html").read_text(encoding="utf-8")
    assert "/api/" in (STATIC / "console.js").read_text(encoding="utf-8")
    wait = (STATIC / "console-wait.html").read_text(encoding="utf-8")
    assert "/api/health" in wait
    assert "/api/dashboard" in wait
    assert "/api/dashboard/metrics?limit=1" in wait
    assert "/console/home" in wait
    assert "/api/dashboard/metrics?limit=120" in (STATIC / "console.js").read_text(encoding="utf-8")
    assert 'id="tpsChart"' in (STATIC / "console.html").read_text(encoding="utf-8")
    assert 'id="latencyChart"' in (STATIC / "console.html").read_text(encoding="utf-8")


def test_legacy_page_exposes_the_live_anomaly_lab():
    runtime = (STATIC / "legacy-runtime.js").read_text(encoding="utf-8")
    console = (STATIC / "console.html").read_text(encoding="utf-8")
    script = (STATIC / "console.js").read_text(encoding="utf-8")
    assert "ki-anomaly-entry" in runtime
    assert "/console#workloads" in runtime
    assert 'id="normalLoadSelect"' in console
    assert 'id="normalLoadDataSource"' in console
    assert 'id="tpsChartLegend"' in console
    assert 'id="workloadTpsLegend"' in console
    assert 'id="anomalyDropZone"' in console
    assert 'id="workloadReportList"' in console
    assert "triggerAnomaly" in script
    assert "/api/normal-loads/start" in script
    assert "/auto-resolve" in script
    assert "stableChartTrend" in script
    assert "detectChartEvents" in script
    assert "preserveEvents" in script


def test_legacy_adapter_uses_compact_live_projection_and_typed_pages():
    runtime = (STATIC / "legacy-runtime.js").read_text(encoding="utf-8")
    pages = (STATIC / "legacy-pages.js").read_text(encoding="utf-8")
    assert "/dashboard/metrics" in runtime
    assert "/incidents" in pages
    assert "/source-analysis/knowledge" in pages
    assert "/rag/search" in pages


def test_live_shell_does_not_leave_fixture_targets_visible():
    runtime = (STATIC / "legacy-runtime.js").read_text(encoding="utf-8")
    pages = (STATIC / "legacy-pages.js").read_text(encoding="utf-8")
    console = (STATIC / "console.html").read_text(encoding="utf-8")
    assert "clearFixtureTargets" in runtime
    assert "ki-live-workspace" in runtime
    assert "ki-live-pending" in runtime
    assert 'id="tpsChart"' in console
    assert 'id="connectionsChart"' in console
    assert 'id="metricGrid"' not in console
    assert "metricsCanvas" not in console
    assert "overviewActivity" not in console
    assert "ki-live-pending" in pages


def test_live_adapters_keep_filters_and_rule_tables_on_persisted_data():
    pages = (STATIC / "legacy-pages.js").read_text(encoding="utf-8")
    assert "state.rules.filter" in pages
    assert "classList.remove('ki-live-pending')" in pages
    assert "sourceName(item.source_type)" in pages
