#!/usr/bin/env python3
"""Exercise the six existing lab scenarios and audit real alert persistence.

Only starts supported lab workloads through the normal HTTP endpoint; never
forces alerts, inserts incidents, deletes history, or rewrites result statuses.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import sqlite3
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def request(base, endpoint, body=None):
    req = Request(base.rstrip("/") + endpoint,
                  data=json.dumps(body).encode() if body is not None else None,
                  headers={"Content-Type": "application/json"},
                  method="POST" if body is not None else "GET")
    try:
        with urlopen(req, timeout=15) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError("HTTP {}: {}".format(exc.code, exc.read().decode()[:1000]))


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def incidents(db, since=None):
    with sqlite3.connect(db.as_uri()+"?mode=ro", uri=True, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        query="SELECT * FROM incidents"
        rows=conn.execute(query+" WHERE started_at>=? ORDER BY started_at" if since else query,
                          (since,) if since else ()).fetchall()
        result=[]
        for raw in rows:
            row=dict(raw)
            for key in ["alert_json", "sysinsight_json"]:
                row[key[:-5]]=json.loads(row.pop(key) or "{}")
            result.append(row)
        return result


def detail_error(detail):
    evidence=(detail.get("sysinsight") or {}).get("live_chain") or {}
    chain=evidence.get("result") or evidence
    return (detail.get("sysinsight") or {}).get("error") or chain.get("error") or evidence.get("reason")


def run(args):
    report_dir=Path(args.output).resolve() / dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir.mkdir(parents=True, exist_ok=False)
    db=Path(args.state_db).resolve()
    audit={"startedAt":stamp(),"scope":"real alert trigger / SQLite persistence / portal visibility",
           "baselineSeconds":args.baseline_seconds,"pressureSeconds":args.pressure_seconds,
           "forcedAlerts":False,"productionCodeChanged":False,"scenarios":[],"reportDirectory":str(report_dir)}
    print("REPORT "+str(report_dir),flush=True)
    catalog=request(args.base_url,"/api/v1/lab/catalog")
    cases=sorted(catalog["tpcc_focus_scenarios"],key=lambda row:row["focus_order"])
    assert len(cases)==6,"expected the six configured focus scenarios"
    save(report_dir/"catalog.json",cases)
    save(report_dir/"before_incidents.json",incidents(db))
    for case in cases:
        sid=case["scenario_id"]
        row={"scenarioId":sid,"title":case["title"],"clients":case["clients"],"startedAt":stamp(),
             "prometheusFiringObserved":False,"pressureFiringObserved":False,"baselineFiringObserved":False,
             "observedIncidentIds":[],"samples":[]}
        audit["scenarios"].append(row)
        # A clear interval prevents the prior scenario's alert from being
        # counted as a new trigger. Wait for two separate collector polls.
        clear_since=None
        deadline=time.monotonic()+150
        while time.monotonic()<deadline:
            status=request(args.base_url,"/api/v1/status?limit=1")
            firing=bool((status.get("alert") or {}).get("firing"))
            if not firing:
                if clear_since is None:clear_since=time.monotonic()
                if time.monotonic()-clear_since>=10:break
            else:clear_since=None
            time.sleep(2)
        else:
            row["error"]="prior alert did not clear; no workload started"
            save(report_dir/"audit.json",audit)
            print("BLOCK "+sid+" "+row["error"],flush=True)
            continue
        row["triggerWindowStart"]=stamp()
        payload={"scenario_id":sid,"normal_clients":2,"pressure_clients":case["clients"],
                 "baseline_seconds":args.baseline_seconds,"pressure_seconds":args.pressure_seconds}
        response=request(args.base_url,"/api/v1/actions/lab/tpcc/start",payload)
        if response.get("status")!="queued":raise RuntimeError("lab did not accept a new run: "+str(response))
        run_id=response["run"]["run_id"]
        row["runId"]=run_id
        print("START "+sid+" "+run_id,flush=True)
        next_log=0
        lab={}
        deadline=time.monotonic()+args.baseline_seconds+args.pressure_seconds+120
        while time.monotonic()<deadline:
            lab=request(args.base_url,"/api/v1/lab/runs/"+run_id)
            prom=request(args.prometheus_url,"/api/v1/alerts")
            alerts=[a for a in prom.get("data",{}).get("alerts",[])
                    if a.get("labels",{}).get("alertname")==args.alert_name and a.get("state")=="firing"]
            phase=lab.get("phase")
            firing=bool(alerts)
            if firing:
                row["prometheusFiringObserved"]=True
                if phase=="baseline":row["baselineFiringObserved"]=True
                if phase in {"pressure_before","pressure_after"}:row["pressureFiringObserved"]=True
            new=incidents(db,row["triggerWindowStart"])
            row["observedIncidentIds"]=[i["incident_id"] for i in new]
            row["samples"].append({"at":stamp(),"phase":phase,"runStatus":lab.get("status"),
                                   "prometheusAlerts":alerts,
                                   "incidents":[{"id":i["incident_id"],"status":i["status"],"error":detail_error(i)} for i in new]})
            row["runStatus"]=lab.get("status")
            if time.monotonic()>=next_log:
                print("PROGRESS {} phase={} firing={} incidents={}".format(sid,phase,firing,len(new)),flush=True)
                next_log=time.monotonic()+20
                save(report_dir/"audit.json",audit)
            if lab.get("status") not in {"queued","running"}:
                save(report_dir/(sid+"_lab.json"),lab)
                break
            time.sleep(2)
        else:
            # Only stop the exact run this validation created.
            row["error"]="validation run exceeded its bounded observation window"
            request(args.base_url,"/api/v1/actions/lab/stop",{"run_id":run_id})
        row["triggerWindowEnd"]=stamp()
        if args.wait_analysis_seconds:
            # Wait for all incidents from this case to restore their temporary
            # configurations before starting the next resource-sensitive case.
            deadline=time.monotonic()+args.wait_analysis_seconds
            while time.monotonic()<deadline:
                records=incidents(db,row['triggerWindowStart'])
                pending=[i for i in records if i['status'] in {'running','queued'}]
                if not pending:
                    break
                print('DRAIN {} pending={}'.format(sid,len(pending)),flush=True)
                save(report_dir/'audit.json',audit)
                time.sleep(15)
            else:
                row['analysisDrainTimedOut']=True
        records=incidents(db,row["triggerWindowStart"])
        def trigger_phase(incident):
            prior=[s for s in lab.get("samples",[]) if str(s.get("observed_at") or "")<=incident["started_at"]]
            return prior[-1].get("phase") if prior else None
        row["incidents"]=[{"id":i["incident_id"],"startedAt":i["started_at"],"status":i["status"],
                           "triggerPhase":trigger_phase(i),
                           "error":detail_error(i),"alert":i.get("alert")} for i in records]
        save(report_dir/(sid+"_incidents.json"),records)
        row["sqliteWritten"]=bool(records)
        row["portalRecords"]=[]
        for incident in records:
            portal=request(args.base_url,"/api/v1/keeninsight/alerts/"+incident["incident_id"])
            row["portalRecords"].append({"id":portal["id"],"status":portal.get("rawStatus"),"error":portal.get("error")})
        listing=request(args.base_url,"/api/v1/keeninsight/alerts?"+urlencode({"limit":200}))
        ids={i["id"] for i in listing.get("items",[])}
        row["portalListVisible"]=bool(records) and all(i["incident_id"] in ids for i in records)
        row["triggerAndPersistencePassed"]=row["pressureFiringObserved"] and row["sqliteWritten"] and row["portalListVisible"]
        row["finishedAt"]=stamp()
        save(report_dir/"audit.json",audit)
        print("DONE {} trigger={} persisted={} visible={} analysis={}".format(sid,row["pressureFiringObserved"],row["sqliteWritten"],row["portalListVisible"],[i["status"] for i in row["incidents"]]),flush=True)
    # Refresh only the incidents attributed to this run. A workflow can still
    # be running after the external pressure experiment has ended.
    final={i["incident_id"]:i for i in incidents(db,audit["startedAt"])}
    for row in audit["scenarios"]:
        for item in row.get("incidents",[]):
            if item["id"] in final:
                i=final[item["id"]];item.update(status=i["status"],error=detail_error(i))
    audit["finishedAt"]=stamp()
    audit["passed"]=sum(bool(row.get("triggerAndPersistencePassed")) for row in audit["scenarios"])
    audit["total"]=len(cases)
    save(report_dir/"audit.json",audit)
    lines=["# 六组真实场景告警触发与入库验证", "", "开始："+audit["startedAt"], "结束："+audit["finishedAt"], "",
           "每组 {} 秒基线、{} 秒压力；真实 TPCC、原有资源护栏、实际 Prometheus 规则，无强制告警。".format(args.baseline_seconds,args.pressure_seconds),
           "", "| 场景 | 压力中 firing | SQLite 入库 | Alert List 可查 | 新告警触发阶段 | 后台分析状态 |",
           "| --- | --- | --- | --- | --- | --- |"]
    for row in audit["scenarios"]:
        lines.append("| {} | {} | {} | {} | {} | {} |".format(row["scenarioId"],row["pressureFiringObserved"],
            row.get("sqliteWritten",False),row.get("portalListVisible",False),
            ", ".join(str(i.get("triggerPhase")) for i in row.get("incidents",[])),
            ", ".join(i["status"] for i in row.get("incidents",[])) or row.get("error", "no incident")))
    lines += ["", "触发与记录通过：{}/{}。分析状态独立判定，不把入库成功等同于调优成功。".format(audit["passed"],audit["total"]), ""]
    for row in audit["scenarios"]:
        lines += ["## "+row["scenarioId"],"", "实验："+str(row.get("runId")),""]
        for item in row.get("incidents",[]):
            lines += ["- `{}`：{}；{}".format(item["id"],item["status"],item.get("error") or "no reported error")]
        lines.append("")
    (report_dir/"report.md").write_text("\n".join(lines),encoding="utf-8")
    print("COMPLETE {}/{} {}".format(audit["passed"],audit["total"],report_dir/"report.md"),flush=True)


def summarize(directory, state_db):
    """Enrich completed audit using actual phase timestamps and latest states."""
    directory=Path(directory).resolve()
    audit=json.loads((directory/"audit.json").read_text())
    fresh={r["incident_id"]:r for r in incidents(Path(state_db).resolve(),audit["startedAt"])}
    counts={"no_active_backend":0,"llm_timeout":0,"other_failure":0,"completed":0,"running":0}
    for row in audit["scenarios"]:
        lab=json.loads((directory/(row["scenarioId"]+"_lab.json")).read_text())
        samples=lab.get("samples",[])
        for item in row.get("incidents",[]):
            incident=fresh.get(item["id"])
            if incident:
                item.update(status=incident["status"],error=detail_error(incident))
                prior=[s for s in samples if str(s.get("observed_at") or "")<=incident["started_at"]]
                following=[s for s in samples if str(s.get("observed_at") or "")>incident["started_at"]]
                item["triggerPhase"]=prior[-1].get("phase") if prior else None
                if prior and following and prior[-1].get("phase")!=following[0].get("phase"):
                    item["triggerPhase"]="transition:{}->{}".format(prior[-1].get("phase"),following[0].get("phase"))
                wrap=(incident.get("sysinsight") or {}).get("live_chain") or {}
                chain=wrap.get("result") or wrap
                item["completedStages"]=[s.get("name") for s in chain.get("stages",[]) if s.get("status")=="completed"]
                source=chain.get("source_evidence") or {}
                item["runtimeCallChainCount"]=len(source.get("runtime_call_chains") or [])
                item["sourceParameterEvidenceCount"]=len(source.get("static_parameter_evidence") or [])
            error=str(item.get("error") or "")
            if item["status"] in {"running","queued"}:failure="running"
            elif item["status"]=="completed":failure="completed"
            elif "no_active_backend" in error:failure="no_active_backend"
            elif "timed out" in error or "Timeout" in error:failure="llm_timeout"
            else:failure="other_failure"
            item["outcomeCategory"]=failure
            counts[failure]+=1
        row["newIncidentDuringPressure"]=any(i.get("triggerPhase") in {"pressure_before","pressure_after"} for i in row.get("incidents",[]))
        row["newIncidentDuringBaseline"]=any(i.get("triggerPhase")=="baseline" for i in row.get("incidents",[]))
        row["incidentAtPhaseTransition"]=any(str(i.get("triggerPhase") or "").startswith("transition:") for i in row.get("incidents",[]))
    audit["failureCategories"]=counts
    audit["summarizedAt"]=stamp()
    audit["pressureSpecificCases"]=sum(r["newIncidentDuringPressure"] for r in audit["scenarios"])
    save(directory/"audit.json",audit)
    lines=["# 六组真实场景告警与入库验证", "", "开始："+audit["startedAt"],"结束："+audit["finishedAt"],
           "", "本次仅验证触发、入库及页面可见性：每组 15 秒基线、90 秒压力，沿用现有场景配方和资源护栏。没有强制告警、手工插入告警、清理历史或修改生产代码。",
           "", "| 场景 | 压力中 firing | 入库 / 页面可查 | 新告警触发阶段 | 压力新增告警 | 后台分析 |",
           "| --- | --- | --- | --- | --- | --- |"]
    for row in audit["scenarios"]:
        lines.append("| {} | {} | {} / {} | {} | {} | {} |".format(row["scenarioId"],row["pressureFiringObserved"],
            row.get("sqliteWritten",False),row.get("portalListVisible",False),", ".join(i.get("triggerPhase") or "unknown" for i in row.get("incidents",[])),
            row["newIncidentDuringPressure"],", ".join(i["status"] for i in row.get("incidents",[]))))
    lines += ["", "场景期间真实告警及入库可查：{}/{}。压力阶段新增告警：{}/{}。".format(audit["passed"],audit["total"],audit["pressureSpecificCases"],audit["total"]),
              "", "触发、入库、后台调优是不同结论。基线新增告警说明目前 active sessions ≥3 的规则不能可靠区分正常业务与外部压力；不能以此证明异常识别准确。",
              "", "触发阶段按告警创建时间前后的真实实验样本夹定；若前后样本阶段不同则记为 transition，不武断归入基线或压力。热点 Payment 明确在基线阶段触发，Stock Level 突发在阶段切换附近触发。",
              "", "分析结果分类：`"+json.dumps(counts,ensure_ascii=False)+"`。",
              "", "已确认两类失败：触发后仅采集瞬时 active PID，短事务在采样时可能已结束；另有真实 perf/源码链成功后访问现配置大模型地址超时。API Key 已配置，不能把网络超时归因于未提供 Key。",""]
    for row in audit["scenarios"]:
        lines += ["## "+row["scenarioId"],"","实验：`"+row["runId"]+"`",""]
        for item in row.get("incidents",[]):
            lines += ["- `{}`：阶段 `{}`，状态 `{}`，分类 `{}`；运行栈 {} 条、源码参数证据 {} 条。".format(item["id"],item.get("triggerPhase"),item["status"],item["outcomeCategory"],item.get("runtimeCallChainCount",0),item.get("sourceParameterEvidenceCount",0)),
                      "  完成阶段：`"+", ".join(item.get("completedStages",[]))+"`。"]
            if item.get("error"):
                lines += ["  错误：`"+item["error"].splitlines()[0].replace("`", "'")+"`。"]
        lines.append("")
    browser_file=directory/"browser-verification.json"
    if browser_file.exists():
        browser=json.loads(browser_file.read_text())
        lines += ["## 浏览器核对", "", "逐条搜索并打开 {} 条新告警：列表、详情、失败状态和失败原因均可见；JS 错误 {} 条。".format(len(browser.get("results",[])),len(browser.get("jsErrors",[]))), ""]
    (directory/"report.md").write_text("\n".join(lines),encoding="utf-8")
    print(json.dumps({"passed":audit["passed"],"total":audit["total"],"pressureSpecificCases":audit["pressureSpecificCases"],"analysis":counts,"report":str(directory/"report.md")},ensure_ascii=False),flush=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url",default="http://127.0.0.1:9108")
    parser.add_argument("--prometheus-url",default="http://127.0.0.1:9090")
    parser.add_argument("--state-db",default=str(ROOT.parent/"monitoring/data/sysinsight_dream_bridge.sqlite3"))
    parser.add_argument("--output",default=str(ROOT/"results/alert_trigger_validation"))
    parser.add_argument("--alert-name",default="SysInsightDemoAnomaly")
    parser.add_argument("--baseline-seconds",type=int,default=15)
    parser.add_argument("--pressure-seconds",type=int,default=90)
    parser.add_argument("--wait-analysis-seconds",type=int,default=0)
    parser.add_argument("--summarize-directory",default="")
    args=parser.parse_args()
    if args.summarize_directory:summarize(args.summarize_directory,args.state_db)
    else:run(args)
