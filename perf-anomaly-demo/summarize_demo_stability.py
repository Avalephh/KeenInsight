#!/usr/bin/env python3
"""Summarize the measured stability audit without treating pending runs as pass."""
import argparse
import datetime as dt
import json
from pathlib import Path
import statistics


def load(path):
    try:return json.loads(Path(path).read_text())
    except (OSError,ValueError):return {}


def render(tp,ap_reports):
    lines=['# TP / AP 真实场景复验','',
           '更新：'+dt.datetime.now().astimezone().isoformat(),'',
           '只汇总实际记录。未完成不判通过；历史参数回放不冒充现场新 LLM 调优。','',
           '## TP 场景','',
           '| 场景 | 轮次 | 基线 TPS | 压力前 TPS | 压力后 TPS | 增益 | 实际外压变化 | 对比有效 | 候选来源 | 状态 |',
           '|---|---:|---:|---:|---:|---:|---:|---|---|---|']
    def number(value):
        return '{:.2f}'.format(value) if isinstance(value,(float,int)) else '—'
    for row in tp.get('tp',[]):
        c=row.get('comparison',{})
        lines.append('| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |'.format(
            row['scenarioId'],row['round'],number(c.get('baseline_tps')),number(c.get('pressure_before_tps')),
            number(c.get('pressure_after_tps')),number(100*c['tuning_gain_ratio'])+'%' if isinstance(c.get('tuning_gain_ratio'),(int,float)) else '—',
            number(100*c['external_pressure_delta_ratio'])+'%' if isinstance(c.get('external_pressure_delta_ratio'),(int,float)) else '—',
            str(c.get('comparison_valid','待判定')),
            row.get('candidateSourceType','待记录'),row['status']))
    lines+=['','TP 完成状态：'+str(tp.get('status','运行中'))+'。','',
            '三段曲线另需核对基线稳定性、压力造成的降幅和实际外压偏差；仅正收益不等于全链路稳定。','',
            '### TP 真实性边界','',
            '控制台真实执行 pgbench、应用参数并恢复，但当前候选来自历史验证文件。'
            '告警链会独立运行新 LLM 分析，其输出并未接入这次控制台 TPS 测量，不能将两者拼成一次完整在线调优证明。','',
            '已发现的无效或不完整对照：','']
    warnings=[]
    for row in tp.get('tp',[]):
        c=row.get('comparison',{})
        if c.get('comparison_valid') is False:
            warnings.append('- {} 第 {} 轮：实际外压不一致，不能将表面 TPS 增幅计为有效收益。'.format(row['scenarioId'],row['round']))
        if isinstance(c.get('pressure_drop_ratio'),(int,float)) and c['pressure_drop_ratio']<0:
            warnings.append('- {} 第 {} 轮：压力前 TPS 高于基线，未形成可信的“正常→下降→恢复”三段对照。'.format(row['scenarioId'],row['round']))
    lines+=warnings or ['尚无已记录的无效对照；仍需等待完整重复实验。']
    lines+=['',
            '## AP 场景','',
            '| SQL | 轮次 | 原始耗时 s | 预热原始耗时 s | 优化实际重放 s | 保守加速比 | 新 LLM Job | 触发方式 | 状态 |',
            '|---|---:|---:|---:|---|---:|---|---|---|']
    all_rows=[]
    cleanup_used=False
    for report in ap_reports:
        all_rows+=report.get('ap',[])
        directory=report.get('directory')
        if directory and ((Path(directory)/'internal_reentry_guard.json').exists()
                          or (Path(directory)/'internal_reentry_cleanup.json').exists()):
            cleanup_used=True
    for row in all_rows:
        replay=[r.get('duration_ms',0)/1000 for r in row.get('replays',[]) if r.get('status')=='completed']
        lines.append('| {} | {} | {} | {} | {} | {} | {} | {} | {} |'.format(
            row['sqlId'],row['round'],number(row.get('baseline',{}).get('duration_ms',0)/1000) if row.get('baseline') else '—',
            number(row['warmOriginal']['duration_ms']/1000) if row.get('warmOriginal',{}).get('status')=='completed' else '—',
            ' / '.join(number(v) for v in replay) or '—',number(row.get('speedup')),
            row.get('jobId','未创建'),row.get('triggerMode','未记录'),row['status']))
    stable=[]
    variable=[]
    for sid in dict.fromkeys(row['sqlId'] for row in all_rows):
        group=[r for r in all_rows if r['sqlId']==sid]
        jobs=[r.get('jobId') for r in group if r.get('jobId')]
        passed=[r for r in group if r.get('status')=='passed']
        if len(group)>=3 and len(passed)==len(group) and len(jobs)==len(set(jobs))==len(group):
            minimum=min(r['speedup'] for r in passed)
            maximum=max(r['speedup'] for r in passed)
            if maximum/minimum<=1.25:stable.append((sid,minimum))
            else:variable.append((sid,minimum,maximum))
    lines+=['','## 当前可证明的稳定 AP 候选','']
    lines+=['- {}：{} 次均为新 LLM 调优，最差保守加速比 {:.2f} 倍。'.format(
        sid,len([r for r in all_rows if r['sqlId']==sid]),gain) for sid,gain in sorted(stable,key=lambda v:v[1],reverse=True)]
    if not stable:lines+=['尚未有至少 3 次新 LLM 调优均达到两倍保守加速且幅度相近的已完成候选，不能提前推荐。']
    lines+=['','“相近结果”另外要求三轮最高/最低保守加速比不超过 1.25；持续正收益但幅度差别很大的 SQL 单列，不混称稳定倍数。']
    if variable:
        lines+=['','### 重复均有收益，但增益幅度不相近','']
        lines+=['- {}：保守加速 {:.2f}～{:.2f} 倍，不按相近结果推荐。'.format(*item)
                for item in variable]
    if cleanup_used:
        lines+=['','### AP 自动链路未通过项','',
            '已实证：DREAM 评估执行的改写 SQL 会被当作业务 SQL 再次自动调优。'
            'Q67 出现连续多层派生任务，记录与父子 SQL 匹配证据均保留。','',
            '本轮启用了仅针对测试派生任务的终止/清理守护，不修改产品调度逻辑。'
            '后续性能结果可用于挑选改写案例，但不能据此宣称当前产品的自动触发隔离已经正常。'
            '原始调优 Job 不被此守护终止；所有清理见同目录 internal_reentry_guard.json。']
    lines+=['','AP “保守加速”取首次基线和预热后原 SQL 中较快者作对照，再与两次优化实际重放中位数比较。',
            '只有单条原 SQL 执行完成且超过 10 秒才进入自动触发候选；超时、短 SQL、未发布和低收益均保留。',
            '前三个 Q88 复验为显式发起的新诊断，不冒充自动阈值触发。自动触发另以 automatic_completed_sql 标识。',
            '原始 Job JSON 保留模型诊断与动作响应、结果集一致性检查及应用验证；结果集一致不等于形式化等价证明。',
            '没有使用人为等待、估算 TPS 或伪造 LLM 输出。人工探索诊断与自动超阈值触发分别记录，不能混称。','']
    return '\n'.join(lines)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--tp',required=True)
    p.add_argument('--ap',action='append',default=[]);p.add_argument('--output',required=True)
    a=p.parse_args();Path(a.output).write_text(render(load(a.tp),[load(v) for v in a.ap]))
