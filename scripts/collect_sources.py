#!/usr/bin/env python3
"""Collect supplied paper/review/demo material into portable RAG source text.

The generated directory contains only text plus a small provenance header, so
the shipped artifact does not depend on the original workstation paths or on
binary archive/PDF readers at runtime.  Re-running this command is
content-idempotent at the RAG database layer.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable


TEXT_SUFFIXES = {
    ".bib",
    ".css",
    ".html",
    ".htm",
    ".js",
    ".md",
    ".py",
    ".sh",
    ".sql",
    ".tex",
    ".toml",
    ".txt",
    ".yml",
    ".yaml",
}


class VisibleText(HTMLParser):
    _ignored = {"script", "style", "svg", "noscript", "template"}
    _headings = {"h1", "h2", "h3", "h4", "h5", "h6", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._ignored:
            self.ignored_depth += 1
        if tag in self._headings or tag in {"p", "div", "li", "tr", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._ignored and self.ignored_depth:
            self.ignored_depth -= 1
        if tag in self._headings or tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def normalize(value: str) -> str:
    value = html.unescape(value).replace("\x00", " ")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in value.split("\n")]
    result: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if not blank:
                result.append("")
            blank = True
        else:
            result.append(line)
            blank = False
    return "\n".join(result).strip()


def visible_html(value: str) -> str:
    parser = VisibleText()
    parser.feed(value)
    return normalize("\n".join(parser.parts))


def safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return value or "source"


def write_source(
    output: Path,
    *,
    relative_name: str,
    source_key: str,
    source_type: str,
    title: str,
    uri: str,
    content: str,
) -> dict[str, str | int]:
    content = normalize(content)
    if not content:
        return {"status": "empty", "name": relative_name, "chars": 0}
    target = output / relative_name
    target.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"SOURCE_KEY: {source_key}\n"
        f"SOURCE_TYPE: {source_type}\n"
        f"SOURCE_TITLE: {title}\n"
        f"SOURCE_URI: {uri}\n"
        "---\n"
    )
    target.write_text(header + content + "\n", encoding="utf-8")
    return {
        "status": "written",
        "name": relative_name,
        "chars": len(content),
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "source_key": source_key,
        "source_type": source_type,
        "title": title,
        "uri": uri,
    }


def collect_zip(
    archive: Path,
    output: Path,
    *,
    source_type: str,
    group: str,
    suffixes: Iterable[str],
) -> list[dict[str, str | int]]:
    allowed = set(suffixes)
    result = []
    with zipfile.ZipFile(archive) as zipped:
        entries = [
            item for item in zipped.infolist()
            if not item.is_dir() and Path(item.filename).suffix.lower() in allowed
        ]
        for index, item in enumerate(entries, start=1):
            raw = zipped.read(item).decode("utf-8", "replace")
            if Path(item.filename).suffix.lower() in {".html", ".htm"}:
                raw = visible_html(raw)
            name = Path(item.filename).name
            relative = f"{group}/{index:03d}-{safe_name(name)}.txt"
            result.append(
                write_source(
                    output,
                    relative_name=relative,
                    source_key=f"archive:{source_type}:{item.filename}",
                    source_type=source_type,
                    title=name,
                    uri=f"{archive.name}!{item.filename}",
                    content=raw,
                )
            )
    return result


def collect_pdf(pdf: Path, output: Path) -> list[dict[str, str | int]]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise SystemExit("PDF collection requires pypdf; install requirements first") from exc
    reader = PdfReader(str(pdf))
    pages = [
        f"[Page {index}]\n{page.extract_text() or ''}"
        for index, page in enumerate(reader.pages, start=1)
    ]
    result = write_source(
        output,
        relative_name="review/pvldb-demo-review.txt",
        source_key="file:review/pvldb-demo-review.txt",
        source_type="review",
        title="PVLDB Demo review for KeenInsight",
        uri=pdf.name,
        content="\n\n".join(pages),
    )
    return [result]


def extract_pdf_text(raw: bytes) -> str:
    """Extract searchable text from a PDF held in memory."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise SystemExit("PDF collection requires pypdf; install requirements first") from exc
    reader = PdfReader(io.BytesIO(raw))
    pages = []
    for index, page in enumerate(reader.pages, start=1):
        page_text = normalize(page.extract_text() or "")
        if page_text:
            pages.append(f"[Page {index}]\n{page_text}")
    return normalize("\n\n".join(pages))


def collect_zip_pdf_text(
    archive: Path, output: Path
) -> list[dict[str, str | int]]:
    """Collect text-bearing PDF design assets without shipping binary copies."""
    result = []
    with zipfile.ZipFile(archive) as zipped:
        entries = [
            item
            for item in zipped.infolist()
            if not item.is_dir() and Path(item.filename).suffix.lower() == ".pdf"
        ]
        for index, item in enumerate(entries, start=1):
            try:
                content = extract_pdf_text(zipped.read(item))
            except Exception as exc:
                print(f"warning: could not extract {archive.name}!{item.filename}: {exc}")
                continue
            if not content:
                continue
            result.append(
                write_source(
                    output,
                    relative_name=f"supplemental/pdf-{index:03d}-{safe_name(Path(item.filename).name)}.txt",
                    source_key=f"archive:pdf-text:{item.filename}",
                    source_type="supplemental-pdf",
                    title=Path(item.filename).name,
                    uri=f"{archive.name}!{item.filename}",
                    content=content,
                )
            )
    return result


def collect_asset_inventory(
    archive: Path, output: Path, *, label: str
) -> dict[str, str | int]:
    """Record every supplied archive asset, including non-text diagrams."""
    rows = [f"Archive: {archive.name}", "Assets:"]
    with zipfile.ZipFile(archive) as zipped:
        for item in zipped.infolist():
            if item.is_dir():
                rows.append(f"- {item.filename} | directory")
                continue
            raw = zipped.read(item)
            suffix = Path(item.filename).suffix.lower() or "(none)"
            digest = hashlib.sha256(raw).hexdigest()
            rows.append(
                f"- {item.filename} | bytes={item.file_size} | suffix={suffix} | sha256={digest}"
            )
    return write_source(
        output,
        relative_name=f"supplemental/{label}-asset-inventory.txt",
        source_key=f"archive:asset-inventory:{archive.name}",
        source_type="supplemental",
        title=f"Asset inventory · {archive.name}",
        uri=archive.name,
        content="\n".join(rows),
    )


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def artifact_summary(path: Path, payload: dict[str, Any]) -> str:
    """Keep evaluation artifacts searchable without indexing the raw JSON."""
    lines = [f"Artifact: {path.as_posix()}"]
    if path.name in {"final-regression-pg12.json", "post-change-regression-pg12.json"}:
        lines.append(f"Protocol: {_compact(payload.get('protocol') or {})}")
        lines.append(f"Failures: {_compact(payload.get('failures') or [])}")
        for name, summary in (payload.get("summary") or {}).items():
            lines.append(
                f"Scenario {name}: effective={summary.get('effective_count')}; "
                f"rollback_completed={summary.get('rollback_completed_count')}; "
                f"median_improvement={_compact(summary.get('median_improvement_pct') or {})}; "
                f"p95_improvement={_compact(summary.get('p95_improvement_pct') or {})}"
            )
    elif path.name == "aa-control-results.json":
        lines.append(f"Status: {payload.get('status')}")
        lines.append(f"Protocol: {_compact(payload.get('protocol') or {})}")
        lines.append(f"Failures: {_compact(payload.get('failures') or [])}")
        for case in payload.get("cases") or []:
            comparison = case.get("comparison") or {}
            lines.append(
                f"Scenario {case.get('scenario')}: same_plan_signature="
                f"{comparison.get('same_plan_signature')}; same_result="
                f"{comparison.get('same_result')}; median_difference_pct="
                f"{comparison.get('median_difference_pct')}; p95_difference_pct="
                f"{comparison.get('p95_difference_pct')}"
            )
    elif path.name == "ui-aa-control.json":
        lines.append(f"Status: {payload.get('status')}")
        lines.append(f"Protocol: {_compact(payload.get('protocol') or {})}")
        lines.append(f"Failures: {_compact(payload.get('failures') or [])}")
        for case in payload.get("cases") or []:
            comparison = case.get("comparison") or {}
            lines.append(
                f"Scenario {case.get('scenario')}: same_plan_signature="
                f"{comparison.get('same_plan_signature')}; same_result="
                f"{comparison.get('same_result')}; median_difference_pct="
                f"{comparison.get('median_difference_pct')}; p95_difference_pct="
                f"{comparison.get('p95_difference_pct')}"
            )
    elif path.name == "preflight-pg12.json":
        lines.extend(
            [
                f"Status: {payload.get('status')}",
                f"Server: {_compact((payload.get('database') or {}).get('server') or {})}",
                f"Row counts: {_compact((payload.get('database') or {}).get('row_counts') or {})}",
                f"Clean checks: waiting_locks_api={payload.get('waiting_locks_api')}; "
                f"applied_actions={payload.get('applied_actions')}",
            ]
        )
    elif path.name == "stats-case.json":
        # The workload endpoint flattens observations into its top-level JSON
        # response; keep this summary aligned with that portable API shape.
        baseline = payload.get("baseline") or {}
        after = payload.get("after") or {}
        baseline_stats = baseline.get("statistics_measurement") or {}
        after_stats = after.get("statistics_measurement") or {}
        feedback = (payload.get("feedback") or {}).get("feedback") or {}
        outcome = feedback.get("outcome") or {}
        lines.extend(
            [
                f"Protocol: {_compact(payload.get('protocol') or {})}",
                f"Action: {_compact({'status': (payload.get('applied') or {}).get('status'), 'type': (payload.get('action') or {}).get('action_type'), 'target': (payload.get('action') or {}).get('target')})}",
                f"Baseline estimate/actual: {baseline_stats.get('estimated_rows')} / {baseline_stats.get('actual_rows')} (error_ratio={baseline_stats.get('estimate_error_ratio')})",
                f"After estimate/actual: {after_stats.get('estimated_rows')} / {after_stats.get('actual_rows')} (error_ratio={after_stats.get('estimate_error_ratio')})",
                f"Feedback: observed_effect={outcome.get('observed_effect')}; effective={outcome.get('effective')}; result_unchanged={outcome.get('result_unchanged')}",
                f"Clean probe: {_compact({key: ((payload.get('clean') or {}).get('after') or {}).get('stats_probe', {}).get(key) for key in ('row_count', 'common_rows', 'rare_rows', 'autovacuum_disabled_for_probe')})}",
            ]
        )
    else:
        lines.append(f"Summary: {_compact(payload)}")
    return "\n".join(lines)


def collect_artifact_summaries(
    root: Path, output: Path
) -> list[dict[str, str | int]]:
    result = []
    for relative in (
        "artifacts/final-regression-pg12.json",
        "artifacts/aa-control-results.json",
        "artifacts/ui-aa-control.json",
        "artifacts/preflight-pg12.json",
        "artifacts/pg16-validation-status.json",
        "artifacts/stats-case.json",
        "artifacts/post-change-regression-pg12.json",
    ):
        path = root / relative
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warning: could not summarize {relative}: {exc}")
            continue
        result.append(
            write_source(
                output,
                relative_name=f"supplemental/{safe_name(relative)}.txt",
                source_key=f"artifact:{relative}",
                source_type="evaluation-data",
                title=relative,
                uri=f"project://{relative}",
                content=artifact_summary(Path(relative), payload),
            )
        )
    return result


def project_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    for pattern in (
        "README.md",
        "docs.md",
        "docs/*.md",
        "app/*.py",
        "demo/*.py",
        "scripts/*.py",
        "scripts/*.sh",
        "app/static/*",
        "db/init/*.sql",
        "demo/*.sql",
        "tests/*.py",
        "requirements.txt",
        "pyproject.toml",
        "docker-compose.yml",
        "Dockerfile",
        ".env.example",
    ):
        paths.extend(root.glob(pattern))
    return sorted({path for path in paths if path.is_file()})


def collect_project(root: Path, output: Path) -> list[dict[str, str | int]]:
    result = []
    for path in project_files(root):
        relative = path.relative_to(root).as_posix()
        suffix = path.suffix.lower()
        source_type = "design" if suffix in {".md", ".yml", ".yaml"} else "implementation"
        if (
            suffix in {".yml", ".yaml"}
            or relative.startswith("db/")
            or relative == "demo/bootstrap.sql"
            or path.name in {
                "Dockerfile",
                ".env.example",
                "requirements.txt",
                "pyproject.toml",
            }
        ):
            source_type = "deployment"
        result.append(
            write_source(
                output,
                relative_name=f"project/{safe_name(relative)}.txt",
                source_key=f"project:{relative}",
                source_type=source_type,
                title=relative,
                uri=f"project://{relative}",
                content=path.read_text(encoding="utf-8", errors="replace"),
            )
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="rag_sources")
    parser.add_argument("--paper-zip", type=Path)
    parser.add_argument("--legacy-demo-zip", type=Path)
    parser.add_argument("--review-pdf", type=Path)
    parser.add_argument("--project-root", type=Path)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, str | int]] = [
        write_source(
            output,
            relative_name="README.txt",
            source_key="file:README.txt",
            source_type="design",
            title="RAG source directory",
            uri=f"{output.name}/README.txt",
            content=(
                "This directory contains portable, source-attributed material "
                "for KeenInsight RAG. It includes the supplied paper, review, "
                "legacy demo pages, current project design and implementation "
                "files, plus deployment and test references. The service also "
                "periodically indexes runtime PostgreSQL evidence."
            ),
        )
    ]
    if args.paper_zip:
        records.extend(
            collect_zip(
                args.paper_zip,
                output,
                source_type="paper",
                group="paper",
                suffixes={".tex", ".bib"},
            )
        )
        records.extend(collect_zip_pdf_text(args.paper_zip, output))
        records.append(collect_asset_inventory(args.paper_zip, output, label="paper"))
    if args.legacy_demo_zip:
        records.extend(
            collect_zip(
                args.legacy_demo_zip,
                output,
                source_type="legacy-demo",
                group="legacy-demo",
                suffixes={".html", ".htm"},
            )
        )
        records.append(
            collect_asset_inventory(args.legacy_demo_zip, output, label="legacy-demo")
        )
    if args.review_pdf:
        records.extend(collect_pdf(args.review_pdf, output))
    if args.project_root:
        records.extend(collect_project(args.project_root, output))
        records.extend(collect_artifact_summaries(args.project_root, output))
    manifest = {
        "format": "keeninsight-rag-sources-v1",
        "records": records,
        "count": len(records),
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(output), "count": len(records)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
