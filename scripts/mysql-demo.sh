#!/usr/bin/env bash
set -euo pipefail
export SYSINSIGHT_GPT_BASE_URL="${SYSINSIGHT_GPT_BASE_URL:-https://api.gpt.ge/v1}"
export SYSINSIGHT_GPT_MODEL="${SYSINSIGHT_GPT_MODEL:-gpt-6.1-sol}"
export SYSINSIGHT_SHAP_MODEL_DIR="${SYSINSIGHT_SHAP_MODEL_DIR:-/root/new/perf-anomaly-demo/models/sysinsight_mysql_tps}"
export MYSQL_SOURCE_ROOT="${MYSQL_SOURCE_ROOT:-/root/mysql-src/src}"
exec /usr/bin/python3 /root/new/perf-anomaly-demo/sysinsight_dream_bridge.py \
  --db keeninsight_tpcc --db-user keeninsight --host 127.0.0.1 --port 3306 \
  --db-schema keeninsight_tpcc --dbms mysql --pg-version 8.0.36 \
  --alert-name SysInsightMySQLDemoAnomaly \
  --state-db /root/new/monitoring/data/mysql_demo_service.sqlite3 \
  --output /root/new/monitoring/data/mysql_demo_service \
  --http-listen 0.0.0.0 --http-port 8101 --http-port-zh 8104 --http-port-zh-lab 8105 \
  --public-host 123.56.83.97 --poll-interval 5 --stats-limit 300 --active-limit 100 \
  --slow-limit 10 --slow-mean-ms 10000 --slow-max-ms 10000 \
  --sample-retention-days 1 --sample-max-rows 500000 \
  --sample-maintenance-interval 900 --sample-maintenance-batch 50000 \
  --sysinsight-chain-timeout 1800 --sysinsight-api-timeout 1200 \
  --sysinsight-perf-seconds 10 --sysinsight-perf-frequency 300 \
  --sysinsight-candidate-count 1 --sysinsight-measure-repeats 2 \
  --sysinsight-sql-timeout 90 \
  --sysinsight-normal-profile /root/new/perf-anomaly-demo/results/mysql_normal_profile.csv \
  --sysinsight-stackcollapse /root/new/perf-anomaly-demo/vendor/FlameGraph/stackcollapse-perf.pl \
  --sysinsight-full-chain
