import argparse
import json
import sys


def main():
    if sys.argv[1:2] == ["hybrid-lab"]:
        from .hybrid_lab import main as entry
        return entry(sys.argv[2:])
    if sys.argv[1:2] == ["equivalence-lab"]:
        from .equivalence import main as entry
        return entry(sys.argv[2:])
    if sys.argv[1:2] == ["pipeline"]:
        from .pipeline import main as entry
        return entry(sys.argv[2:])
    if sys.argv[1:2] == ["pipeline-lab"]:
        from .pipeline_lab import main as entry
        return entry(sys.argv[2:])
    parser = argparse.ArgumentParser(description="Headless paper-oriented eBPF experiment")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="read-only prerequisites and server ABI check")
    doctor.add_argument("--pg-config", default="/usr/lib/postgresql/12/bin/pg_config")
    collect = commands.add_parser("collect", help="attach only to explicitly named backend PIDs")
    collect.add_argument("--pg-config", default="/usr/lib/postgresql/12/bin/pg_config")
    collect.add_argument("--pid", type=int, action="append", required=True)
    collect.add_argument("--output", required=True, help="new private run directory")
    collect.add_argument("--duration", type=float, default=30)
    collect.add_argument("--report-seconds", type=float, default=1)
    collect.add_argument("--profile", choices=("boundary", "resident", "six-resident", "events",
                         "diagnostic", "diagnostic-lock", "diagnostic-wal", "diagnostic-system",
                         "core", "selected", "all"),
                         default="events")
    collect.add_argument("--detail-level", choices=("lightweight", "full"), default="lightweight",
                         help="lightweight: aggregate short SQL, entry-only counts, no scheduler hook")
    collect.add_argument("--scope", choices=("system", "query"), default="system",
                         help="system avoids per-SQL boundary traps; query is opt-in diagnosis scope")
    collect.add_argument("--query-output", choices=("aggregate", "all"))
    collect.add_argument("--metric-mode", choices=("counts", "timed"))
    collect.add_argument("--cpu-mode", choices=("off", "sched"))
    collect.add_argument("--selector", help="offline Lasso model, never auto-trained on test data")
    collect.add_argument("--capture-sql", action="store_true", help="opt-in sensitive, truncated SQL text")
    collect.add_argument("--long-query-ms", type=float, default=1000)
    collect.add_argument("--event-latency-ms", type=float, default=10)
    collect.add_argument("--event-bytes", type=int, default=1048576)
    collect.add_argument("--event-calls", type=int, default=1000)
    collect.add_argument("--alert-cooldown-ms", type=float, default=100,
                         help="per span/probe notification coalescing; counters are never sampled")
    collect.add_argument("--max-output-mb", type=float, default=128)
    collect.add_argument("--collector-cpu-budget", type=float, default=5,
                         help="collector user+system CPU / one CPU; NOT total BPF overhead")
    collect.add_argument("--cost-model", help="optional independently calibrated per-probe ns/call JSON")
    collect.add_argument("--overhead-budget", type=float, default=2,
                         help="estimated core-equivalent CPU percent, only with --cost-model")
    collect.add_argument("--kafka-bootstrap", help="optional broker list; requires kafka-python")
    collect.add_argument("--kafka-topic", default="keeninsight-ebpf")
    train = commands.add_parser("train-selector")
    train.add_argument("--input", required=True)
    train.add_argument("--output", required=True)
    train.add_argument("--alpha", type=float, default=.05)
    commands.add_parser("pipeline", help="resident eBPF -> original SysInsight -> apply/retest/restore")
    commands.add_parser("pipeline-lab", help="real closed loop in a disposable PostgreSQL cluster")
    commands.add_parser("equivalence-lab", help="original six TP cases: eBPF profiles to business TPS tuning")
    commands.add_parser("hybrid-lab", help="eBPF alarm -> original bounded perf/PG evidence -> original tuning")
    args = parser.parse_args()
    if args.command == "train-selector":
        from .selection import train_file
        print(json.dumps(train_file(args.input, args.output, args.alpha), indent=2))
    else:
        from .collector import collect_run, inspect_server
        if args.command == "doctor":
            print(json.dumps(inspect_server(args.pg_config), indent=2))
        else:
            collect_run(args)


if __name__ == "__main__":
    main()
