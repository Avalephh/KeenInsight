#!/usr/bin/env python3
"""
PerfCollectorPG  –  PostgreSQL 版 perf 函数热点采集器

在 CH-Benchmark 运行期间，对所有 postgres 工作进程执行 perf record，
输出格式与 KeenInsight DifferentialProfiling 兼容：

  异常文件 (abnormal):  TSV
      Cycles\tFunction\tSampling Rate (%)\tAbsolute Count

  基线文件 (baseline):  CSV
      Function, Min Sampling Rate (%), Max Sampling Rate (%), Average Sampling Rate (%)
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Dict, List, Optional, Tuple


class PerfCollectorPG:

    def __init__(self, output_dir: str = "/root/KeenInsight/one/performance"):
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

    # ─── 核心：采集 ─────────────────────────────────────────────────────────

    def collect(self, duration: int = 10, frequency: int = 99) -> Optional[str]:
        """
        对 postgres 主进程及其所有子进程执行 perf record。
        使用 -p 主进程PID + --inherit 来捕获子进程。
        返回 perf.data 文件路径，失败返回 None。
        """
        # 只取 postgres 主进程 PID (postmaster)
        main_pid = self._get_pg_main_pid()
        if not main_pid:
            print("[PerfPG] 未找到 postgres 主进程")
            return None

        ts = int(time.time())
        data_file = os.path.join(self.output_dir, f"perf_pg_{ts}.data")

        # -a (system-wide) 比 -p 更可靠，perf report 会把进程名记录下来
        # 之后 parse_report 时只过滤 postgres 相关行
        cmd = (f"perf record -a -F {frequency} -g "
               f"-o {data_file} -- sleep {duration}")
        print(f"[PerfPG] {cmd}")
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if not os.path.exists(data_file) or os.path.getsize(data_file) == 0:
            print(f"[PerfPG] 采集失败: {r.stderr.strip()[:200]}")
            return None
        print(f"[PerfPG] 采集完成: {data_file} ({os.path.getsize(data_file)} bytes)")
        return data_file

    # ─── 解析 perf report ────────────────────────────────────────────────────

    def parse_report(self, data_file: str) -> List[Tuple[str, float, int]]:
        """
        用 perf report --stdio 解析，返回 [(function, pct, samples), ...]。
        只保留 comm=postgres 且 DSO=postgres 的用户态函数。
        """
        cmd = f"perf report -i {data_file} --stdio --no-children -n -g none 2>/dev/null"
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True)

        results: List[Tuple[str, float, int]] = []
        for line in r.stdout.splitlines():
            # 格式:  5.48%  434  postgres  postgres  [.] PrepareSortSupportFromIndexRel
            m = re.match(
                r'\s+([\d.]+)%\s+(\d+)\s+postgres\s+postgres\s+\[.\]\s+(.+)', line
            )
            if m:
                pct = float(m.group(1))
                samples = int(m.group(2))
                func = m.group(3).strip()
                if '(' in func:
                    func = func.split('(')[0]
                if func and pct >= 0.5:
                    results.append((func, pct, samples))
        return results

    # ─── 写入异常 profile (TSV) ──────────────────────────────────────────────

    def write_abnormal_profile(self, data_file: str, output_path: Optional[str] = None) -> str:
        """
        解析 perf.data → 写入 KeenInsight 异常 profile TSV。
        格式: Cycles\tFunction\tSampling Rate (%)\tAbsolute Count
        """
        entries = self.parse_report(data_file)
        if not output_path:
            output_path = os.path.join(self.output_dir, "chbench_abnormal_functions.txt")

        with open(output_path, "w") as f:
            f.write("Cycles\tFunction\tSampling Rate (%)\tAbsolute Count\n")
            for func, pct, samples in entries:
                f.write(f"{samples}\t{func}\t{pct:.2f}%\t{samples}\n")

        print(f"[PerfPG] 异常 profile 写入: {output_path} ({len(entries)} 个函数)")
        return output_path

    # ─── 写入基线 profile (CSV) ──────────────────────────────────────────────

    def write_baseline_profile(self, data_file: str, output_path: Optional[str] = None) -> str:
        """
        解析 perf.data → 写入 KeenInsight 基线 profile CSV。
        由于单次采集没有 min/max，我们用 pct ± 10% 模拟区间。
        格式: Function,Min Sampling Rate (%),Max Sampling Rate (%),Average Sampling Rate (%)
        """
        entries = self.parse_report(data_file)
        if not output_path:
            output_path = os.path.join(self.output_dir, "chbench_normal_functions.txt")

        with open(output_path, "w") as f:
            f.write("Function,Min Sampling Rate (%),Max Sampling Rate (%),Average Sampling Rate (%)\n")
            for func, pct, _ in entries:
                lo = max(0, pct * 0.8)
                hi = pct * 1.2
                f.write(f"{func},{lo:.2f},{hi:.2f},{pct:.2f}\n")

        print(f"[PerfPG] 基线 profile 写入: {output_path} ({len(entries)} 个函数)")
        return output_path

    # ─── 辅助 ────────────────────────────────────────────────────────────────

    @staticmethod
    def _get_pg_pids() -> List[int]:
        r = subprocess.run(["pgrep", "postgres"], capture_output=True, text=True)
        pids = []
        for line in r.stdout.strip().splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
        return pids

    @staticmethod
    def _get_pg_main_pid() -> Optional[int]:
        """获取 postmaster 主进程 PID。"""
        r = subprocess.run(["pgrep", "-x", "postgres"], capture_output=True, text=True)
        for line in r.stdout.strip().splitlines():
            line = line.strip()
            if line.isdigit():
                return int(line)
        return None


# ─── CLI 测试 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    c = PerfCollectorPG()
    print("开始采集 5 秒 ...")
    df = c.collect(duration=5, frequency=99)
    if df:
        entries = c.parse_report(df)
        print(f"\n采集到 {len(entries)} 个函数热点:")
        for func, pct, samples in entries[:15]:
            print(f"  {pct:6.2f}%  {samples:5d}  {func}")
        c.write_abnormal_profile(df)
        c.write_baseline_profile(df)
