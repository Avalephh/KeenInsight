#!/usr/bin/env python3
"""Source-only release guard and a complete, categorized file inventory."""
import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1]
ROLES={
    "sysinsight_dream_bridge":"Service entry point; collector, queues, SQLite, API and retention",
    "sysinsight_live_chain":"Alert evidence → original SysInsight → candidate apply/retest/restore",
    "sysinsight_original_llm":"Original upstream LLAMBO prompt/parser integration and API audit",
    "sysinsight_detection":"Original source differential-profile and function matching adapter",
    "sysinsight_prometheus":"Prometheus alert/metric observations and canonical tuning input",
    "sysinsight_source_evidence":"PostgreSQL source matching and provenance construction",
    "sysinsight_pipeline":"Explicit case-artifact replay compatibility entry used by the bridge",
    "sysinsight_candidate_benchmark":"Candidate validation and controlled benchmark support",
    "pg_temporary_config":"Temporary PostgreSQL configuration lifecycle and restoration",
    "db_profile":"Database reference profile resolution",
    "dream_auto_apply":"Read-only SQL/action parser and automatic-application policy",
    "dream_live_adapter":"One real external DREAM job; diagnosis/evaluation/evidence contract",
    "lab_controller":"Bounded, idempotent TP/AP console experiments and replay",
    "tpcc_external_cases":"Pressure recipes and PostgreSQL/pgbench execution helpers",
    "tpcc_transaction_cases":"TPC-C transaction-oriented scene recipes",
    "run_demo":"Shared perf, process and baseline capture helpers; standalone diagnostic CLI",
    "keeninsight_portal":"Read-only presentation/data contracts and retained evidence",
    "sql_profiling":"Bounded, PID/execution-verified SQL CPU stack sampling",
    "sysinsight_shap":"Optional model training, model integrity and conservative attribution",
    "collect_shap_windows":"Optional acquisition of real TPS/perf training pairs",
}


def files():
    excluded={".git","__pycache__",".venv","node_modules","third_party",
              "results","logs","data","run","local","downloads","vendor"}
    for p in sorted(ROOT.rglob("*")):
        relative=p.relative_to(ROOT)
        if relative.parts[0]=="dream":continue
        if p.is_file() and not any(part in excluded for part in relative.parts):
            if p.suffix in {".pyc",".pyo"}:continue
            yield p


def role(path):
    parts=path.parts
    if parts[0]=="app":
        if len(parts)==2 and path.suffix==".py":
            return ROLES.get(path.stem)
        if path.name=="bridge_ui.html":return "Background-action management interface"
        if path.name=="experiment_lab.html":return "TP/AP experiment console"
        if parts[1]=="keeninsight_web":return "Live presentation page/style/data adapter; served by bridge"
        if parts[1]=="tpcc_cases":return "Rollback-protected transaction or pressure workload SQL"
        if parts[1]=="db_profiles":
            return "Reference-profile generator or declarative database/version manifest"
    if parts[0]=="monitoring":
        return "Deployment/runtime lifecycle" if len(parts)==2 else "Prometheus/Grafana/exporter configuration or dashboard"
    if parts[0]=="tests":return "Automated contract, security, retention or browser regression"
    if parts[0]=="scripts":return "Dependency setup, profile generation, verification or deployment entry point"
    if parts[0]=="integrations":return "Pinned external-source identity or reviewed compatibility patch"
    if parts[0]=="config":return "Portable configuration template without credentials"
    if parts[0]=="docs":return "Operator/developer documentation or source inventory"
    if parts[0]==".github":return "Non-destructive CI verification"
    if len(parts)==1:return "Project documentation, license, dependency lock or development configuration"
    return None


def check():
    problems=[];items=list(files())
    paths={p.relative_to(ROOT) for p in items}
    for p in items:
        relative=p.relative_to(ROOT)
        if not role(relative):problems.append(str(relative)+": no documented consumer or purpose")
        if p.stat().st_size>1024*1024:problems.append(str(relative)+": unexpectedly large source file")
        if p.suffix in {".sqlite",".sqlite3",".db",".pt",".pth",".joblib",".csv",".png",".pptx",".gz",".zip"}:
            problems.append(str(relative)+": generated/binary artifact")
        try:text=p.read_text()
        except UnicodeError:
            problems.append(str(relative)+": non-text content");continue
        if p.suffix==".py":
            try:ast.parse(text,filename=str(relative))
            except SyntaxError as exc:problems.append(str(relative)+": "+str(exc))
        if p.suffix==".json":
            try:json.loads(text)
            except ValueError:problems.append(str(relative)+": invalid JSON")
        if parts_are_product(relative):
            if re.search(r"/root/(?:new|keeninsight-postgres)|35\.212\.",text):
                problems.append(str(relative)+": workstation-specific path/endpoint")
            if re.search(r"\bsk"+r"-[A-Za-z0-9_-]{16,}",text):
                problems.append(str(relative)+": possible API credential (value suppressed)")
            if re.search(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----",text):
                problems.append(str(relative)+": private key material")
    app=ROOT/"app"
    modules={p.stem:p for p in app.glob("*.py")}
    reached=set()
    pending=["sysinsight_dream_bridge","dream_live_adapter","sysinsight_original_llm",
             "sysinsight_detection","collect_shap_windows","sysinsight_shap"]
    while pending:
        name=pending.pop()
        if name in reached:continue
        if name not in modules:
            problems.append("Missing runtime/CLI module: "+name);continue
        reached.add(name)
        for node in ast.walk(ast.parse(modules[name].read_text())):
            names=[]
            if isinstance(node,ast.ImportFrom) and node.module:names=[node.module.split(".")[0]]
            elif isinstance(node,ast.Import):names=[a.name.split(".")[0] for a in node.names]
            pending.extend(n for n in names if n in modules and n not in reached)
            if (isinstance(node,ast.BinOp) and isinstance(node.op,ast.Div)
                    and isinstance(node.left,ast.Name) and node.left.id=="ROOT"
                    and isinstance(node.right,ast.Constant) and isinstance(node.right.value,str)
                    and node.right.value.endswith(".py") and "/" not in node.right.value):
                child=Path(node.right.value).stem
                if child not in modules:
                    problems.append(name+": missing subprocess script "+node.right.value)
                elif child not in reached:pending.append(child)
    for name in set(modules)-reached:problems.append("Unreferenced application module: "+name)
    sql_names=set()
    for module_path in modules.values():
        for node in ast.walk(ast.parse(module_path.read_text())):
            if (isinstance(node,ast.Constant) and isinstance(node.value,str)
                    and node.value.endswith(".sql") and "\n" not in node.value):
                sql_names.add(Path(node.value).name)
    for sql in (app/"tpcc_cases").glob("*.sql"):
        if sql.name not in sql_names:
            problems.append("Unreferenced workload SQL: "+sql.name)
    required=["README.md","README.en.md","LICENSE","NOTICE","SECURITY.md",
              "docs/DEPLOYMENT.md","docs/ARCHITECTURE.md","docs/DEPENDENCIES.md",
              "docs/REPRODUCING.md",
              "docs/KNOWN_LIMITATIONS.md","docs/CONFIGURATION.md","docs/FILE_INVENTORY.md"]
    for name in required:
        if Path(name) not in paths:problems.append("Missing release document: "+name)
    for line in problems:print("FAIL",line,file=sys.stderr)
    if problems:return False
    print("PASS: {} categorized text files, {} reachable application modules; no embedded run artifacts or credentials detected".format(len(items),len(reached)))
    return True


def parts_are_product(path):
    return path.parts[0] in {"app","monitoring","config","integrations"}


def inventory():
    entries=[(p.relative_to(ROOT),role(p.relative_to(ROOT))) for p in files()]
    destination=ROOT/"docs/FILE_INVENTORY.md"
    if not any(p==Path("docs/FILE_INVENTORY.md") for p,_ in entries):
        entries.append((Path("docs/FILE_INVENTORY.md"),"Generated source-purpose inventory"))
    entries.sort(key=lambda row:str(row[0]))
    lines=["# 文件用途与保留依据","",
           "此清单覆盖发布目录中全部源文件、配置与文档；不包含 Git、外部依赖、缓存或运行记录。",
           "它证明文件有已识别用途，不代表所有分支都已在所有数据库版本和机器上执行。",
           "应用模块按静态导入和显式子进程入口校验；前端、SQL 与参考资料通过运行时路由/目录加载。",
           "可用 `python3 scripts/release_check.py --write-inventory` 更新。","",
           "| 文件 | 实际用途 / 消费方 |","|---|---|"]
    lines+=["| `{}` | {} |".format(p.as_posix(),purpose or "UNREVIEWED") for p,purpose in entries]
    destination.parent.mkdir(exist_ok=True)
    destination.write_text("\n".join(lines)+"\n")
    print("Wrote source-purpose inventory:",len(entries),"files")


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-inventory",action="store_true")
    args=parser.parse_args()
    if args.write_inventory:inventory()
    raise SystemExit(0 if check() else 1)
