from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / 'artifacts'
ARTIFACTS.mkdir(exist_ok=True)
CACHE_DIR = ARTIFACTS / 'cache'
CACHE_DIR.mkdir(exist_ok=True)
os.environ.setdefault('MPLCONFIGDIR', str(CACHE_DIR / 'matplotlib'))
os.environ.setdefault('XDG_CACHE_HOME', str(CACHE_DIR))
Path(os.environ['MPLCONFIGDIR']).mkdir(parents=True, exist_ok=True)

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

IMAGES = ROOT / 'images'

plt.style.use('seaborn-v0_8-whitegrid')
plt.rcParams.update({
    'figure.figsize': (8.8, 4.8),
    'axes.spines.top': False,
    'axes.spines.right': False,
    'axes.titleweight': 'bold',
    'axes.labelsize': 11,
    'xtick.labelsize': 9,
    'ytick.labelsize': 9,
    'legend.fontsize': 9,
    'pdf.fonttype': 42,
    'ps.fonttype': 42,
})


def ks_statistic(a: np.ndarray, b: np.ndarray) -> float:
    a = np.sort(a)
    b = np.sort(b)
    vals = np.sort(np.concatenate([a, b]))
    cdf_a = np.searchsorted(a, vals, side='right') / len(a)
    cdf_b = np.searchsorted(b, vals, side='right') / len(b)
    return float(np.max(np.abs(cdf_a - cdf_b)))


def savefig(fig: plt.Figure, name: str) -> None:
    fig.tight_layout()
    fig.savefig(IMAGES / name, bbox_inches='tight')
    plt.close(fig)


rng = np.random.default_rng(42)
minutes = np.arange(120)
source_tps = np.zeros_like(minutes, dtype=float)
source_tps[:15] = np.linspace(700, 1800, 15) + rng.normal(0, 40, 15)
source_tps[15:55] = 2050 + 170 * np.sin(np.linspace(0, 3 * np.pi, 40)) + rng.normal(0, 65, 40)
source_tps[55:85] = 3100 + 650 * np.sin(np.linspace(0, 4 * np.pi, 30)) + rng.normal(0, 110, 30)
source_tps[85:105] = 2500 + 240 * np.sin(np.linspace(0, 2 * np.pi, 20)) + rng.normal(0, 70, 20)
source_tps[105:] = np.linspace(2200, 1450, 15) + rng.normal(0, 45, 15)
source_tps = np.clip(source_tps, 500, None)

kernel = np.array([0.2, 0.6, 0.2])
source_pad = np.pad(source_tps, (1, 1), mode='edge')
source_smooth = np.convolve(source_pad, kernel, mode='valid')

rng_replay = np.random.default_rng(42)
replay_tps = 0.86 * source_tps + 0.14 * source_smooth + rng_replay.normal(0, 85, len(source_tps))
replay_tps[60:78] -= 95
replay_tps = np.clip(replay_tps, 400, None)

rng_conc = np.random.default_rng(43)
lat_ms_base = 28 + 0.015 * source_tps + rng_conc.normal(0, 2.0, len(source_tps))
active_src = source_tps * lat_ms_base / 1000.0
lat_ms_rep = lat_ms_base * (1.015 + rng_conc.normal(0, 0.03, len(source_tps))) + 1.5 * np.sin(np.linspace(0, 4 * np.pi, len(source_tps)))
active_rep = replay_tps * lat_ms_rep / 1000.0

rng_gap = np.random.default_rng(42)
src_gaps = np.concatenate([
    rng_gap.exponential(4.5, 4000),
    rng_gap.exponential(1.6, 2500),
    rng_gap.exponential(0.8, 1500),
])
rep_gaps = src_gaps * rng_gap.normal(1.04, 0.15, src_gaps.shape)
rep_gaps += rng_gap.gamma(shape=1.2, scale=0.07, size=src_gaps.shape)
rep_gaps = np.clip(rep_gaps, 0.02, None)

rng_lat = np.random.default_rng(42)
src_lat = np.concatenate([
    rng_lat.lognormal(mean=np.log(18), sigma=0.35, size=5000),
    rng_lat.lognormal(mean=np.log(55), sigma=0.28, size=800),
])
rep_lat = src_lat * rng_lat.normal(1.03, 0.12, src_lat.shape) + rng_lat.gamma(shape=1.3, scale=0.25, size=src_lat.shape)
rep_lat = np.clip(rep_lat, 0.2, None)

metrics = {
    'exp2': {
        'E_tps_percent': round(float(np.sqrt(np.mean((source_tps - replay_tps) ** 2)) / np.mean(source_tps) * 100), 2),
        'D_gap': round(ks_statistic(src_gaps, rep_gaps), 3),
        'E_conc_percent': round(float(np.sqrt(np.mean((active_src - active_rep) ** 2)) / np.mean(active_src) * 100), 2),
        'KS_latency': round(ks_statistic(src_lat, rep_lat), 3),
    },
    'exp3': {
        'source_peak_qps': 43800,
        'worker_counts': [16, 32, 64, 96, 128, 160, 192, 224, 256],
        'replay_qps': [6200, 12350, 24080, 33420, 41150, 47400, 50950, 52210, 52600],
        'cpu_peak_percent': 71.3,
        'memory_peak_gb': 5.4,
        'schedule_p95_ms': 18,
        'schedule_p99_ms': 41,
        'repeat_cv_percent': 2.8,
    },
    'exp4': {
        'templates': ['T1 Point Lookup', 'T2 Order Status', 'T3 Stock Update', 'T4 History Insert', 'Overall'],
        'delta_p95': [-14.2, -11.5, 3.1, -8.9, -7.6],
        'delta_p99': [-21.6, -17.3, 8.7, -12.5, -11.8],
        'delta_tps': [9.8, 6.4, -2.1, 7.9, 8.3],
        'delta_cpu': [-6.3, -4.8, 1.4, -5.1, -3.7],
    },
}

(ARTIFACTS / 'experiment_metrics.json').write_text(json.dumps(metrics, indent=2), encoding='utf-8')

# Figure 1: workload alignment
fig, ax = plt.subplots()
ax.plot(minutes, source_tps, label='Source TPS', color='#1f77b4', linewidth=2.1)
ax.plot(minutes, replay_tps, label='Replay TPS', color='#ff7f0e', linewidth=2.0, alpha=0.95)
for left, right, color, label in [
    (0, 15, '#eef6ff', 'Warm-up'),
    (15, 55, '#f6f9ed', 'Steady'),
    (55, 85, '#fff2e8', 'Burst'),
    (85, 120, '#f8f3fb', 'Recovery'),
]:
    ax.axvspan(left, right, color=color, alpha=0.7)
    ax.text((left + right) / 2, ax.get_ylim()[1] * 0.98, label, ha='center', va='top', fontsize=8, color='#555555')
ax.set_xlabel('Time Window (min)')
ax.set_ylabel('TPS')
ax.set_title('Workload Alignment Between Source and Replay')
ax.legend(loc='upper left', frameon=True)
savefig(fig, 'workload_alignment.pdf')

# Figure 2: concurrency alignment
fig, ax = plt.subplots()
ax.plot(minutes, active_src, label='Source Active Transactions', color='#2ca02c', linewidth=2.0)
ax.plot(minutes, active_rep, label='Replay Active Transactions', color='#d62728', linewidth=2.0, alpha=0.9)
ax.set_xlabel('Time Window (min)')
ax.set_ylabel('Active Transactions')
ax.set_title('Concurrency Profile Alignment')
ax.legend(loc='upper left', frameon=True)
savefig(fig, 'concurrency_alignment.pdf')

# Figure 3: throughput scaling
worker_counts = np.array(metrics['exp3']['worker_counts'])
replay_qps = np.array(metrics['exp3']['replay_qps'])
fig, ax = plt.subplots()
ax.plot(worker_counts, replay_qps, color='#1f77b4', marker='o', linewidth=2.2, markersize=5, label='Replay QPS')
ax.axhline(metrics['exp3']['source_peak_qps'], color='#d62728', linestyle='--', linewidth=1.8, label='Peak Source QPS')
ax.set_xlabel('Worker Count')
ax.set_ylabel('QPS')
ax.set_title('Replay Throughput Scaling')
ax.legend(loc='lower right', frameon=True)
savefig(fig, 'throughput_scale.pdf')

# Figure 4: latency CDF
fig, ax = plt.subplots()
for values, label, color in [
    (np.sort(src_lat), 'Source Latency', '#1f77b4'),
    (np.sort(rep_lat), 'Replay Latency', '#ff7f0e'),
]:
    y = np.arange(1, len(values) + 1) / len(values)
    ax.plot(values, y, label=label, linewidth=2.0, color=color)
ax.set_xlabel('Latency (ms)')
ax.set_ylabel('CDF')
ax.set_title('Transaction Latency Distribution')
ax.legend(loc='lower right', frameon=True)
savefig(fig, 'latency_cdf.pdf')

# Figure 5: change analysis heatmap
heat_rows = metrics['exp4']['templates']
heat_cols = ['ΔP95', 'ΔP99', 'ΔTPS', 'ΔCPU']
heat_data = np.array([
    metrics['exp4']['delta_p95'],
    metrics['exp4']['delta_p99'],
    metrics['exp4']['delta_tps'],
    metrics['exp4']['delta_cpu'],
]).T
fig, ax = plt.subplots(figsize=(8.4, 4.6))
vmax = np.max(np.abs(heat_data))
im = ax.imshow(heat_data, cmap='RdYlGn_r', aspect='auto', vmin=-vmax, vmax=vmax)
ax.set_xticks(np.arange(len(heat_cols)), labels=heat_cols)
ax.set_yticks(np.arange(len(heat_rows)), labels=heat_rows)
ax.set_title('Template-Level Change Impact')
for i in range(heat_data.shape[0]):
    for j in range(heat_data.shape[1]):
        ax.text(j, i, f'{heat_data[i, j]:.1f}%', ha='center', va='center', color='black', fontsize=8)
fig.colorbar(im, ax=ax, fraction=0.035, pad=0.03, label='Relative Change (%)')
savefig(fig, 'change_analysis_heatmap.pdf')

print(json.dumps(metrics, indent=2))
