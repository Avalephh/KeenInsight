# 文件用途与保留依据

此清单覆盖发布目录中全部源文件、配置与文档；不包含 Git、外部依赖、缓存或运行记录。
它证明文件有已识别用途，不代表所有分支都已在所有数据库版本和机器上执行。
应用模块按静态导入和显式子进程入口校验；前端、SQL 与参考资料通过运行时路由/目录加载。
可用 `python3 scripts/release_check.py --write-inventory` 更新。

| 文件 | 实际用途 / 消费方 |
|---|---|
| `.gitattributes` | Project documentation, license, dependency lock or development configuration |
| `.github/workflows/test.yml` | Non-destructive CI verification |
| `.gitignore` | Project documentation, license, dependency lock or development configuration |
| `CONTRIBUTING.md` | Project documentation, license, dependency lock or development configuration |
| `LICENSE` | Project documentation, license, dependency lock or development configuration |
| `Makefile` | Project documentation, license, dependency lock or development configuration |
| `NOTICE` | Project documentation, license, dependency lock or development configuration |
| `README.en.md` | Project documentation, license, dependency lock or development configuration |
| `README.md` | Project documentation, license, dependency lock or development configuration |
| `SECURITY.md` | Project documentation, license, dependency lock or development configuration |
| `app/bridge_ui.html` | Background-action management interface |
| `app/collect_shap_windows.py` | Optional acquisition of real TPS/perf training pairs |
| `app/db_profile.py` | Database reference profile resolution |
| `app/db_profiles/build_pg_source_association.py` | Reference-profile generator or declarative database/version manifest |
| `app/db_profiles/build_profile_artifacts.py` | Reference-profile generator or declarative database/version manifest |
| `app/db_profiles/mysql/8.0.36/profile.json` | Reference-profile generator or declarative database/version manifest |
| `app/db_profiles/postgresql/12/profile.json` | Reference-profile generator or declarative database/version manifest |
| `app/db_profiles/postgresql/13/profile.json` | Reference-profile generator or declarative database/version manifest |
| `app/db_profiles/postgresql/14/profile.json` | Reference-profile generator or declarative database/version manifest |
| `app/dream_auto_apply.py` | Read-only SQL/action parser and automatic-application policy |
| `app/dream_live_adapter.py` | One real external DREAM job; diagnosis/evaluation/evidence contract |
| `app/experiment_lab.html` | TP/AP experiment console |
| `app/keeninsight_portal.py` | Read-only presentation/data contracts and retained evidence |
| `app/keeninsight_web/alert-detail.html` | Live presentation page/style/data adapter; served by bridge |
| `app/keeninsight_web/alert-list.html` | Live presentation page/style/data adapter; served by bridge |
| `app/keeninsight_web/alert-rules.html` | Live presentation page/style/data adapter; served by bridge |
| `app/keeninsight_web/code-source.html` | Live presentation page/style/data adapter; served by bridge |
| `app/keeninsight_web/dba-rules.html` | Live presentation page/style/data adapter; served by bridge |
| `app/keeninsight_web/host-list.html` | Live presentation page/style/data adapter; served by bridge |
| `app/keeninsight_web/manual.html` | Live presentation page/style/data adapter; served by bridge |
| `app/keeninsight_web/original-layouts.js` | Live presentation page/style/data adapter; served by bridge |
| `app/keeninsight_web/portal.css` | Live presentation page/style/data adapter; served by bridge |
| `app/keeninsight_web/portal.js` | Live presentation page/style/data adapter; served by bridge |
| `app/keeninsight_web/sql-detail.html` | Live presentation page/style/data adapter; served by bridge |
| `app/keeninsight_web/sql-log.html` | Live presentation page/style/data adapter; served by bridge |
| `app/keeninsight_web/template.html` | Live presentation page/style/data adapter; served by bridge |
| `app/keeninsight_web/tuning-knowledge.html` | Live presentation page/style/data adapter; served by bridge |
| `app/lab_controller.py` | Bounded, idempotent TP/AP console experiments and replay |
| `app/pg_temporary_config.py` | Temporary PostgreSQL configuration lifecycle and restoration |
| `app/run_demo.py` | Shared perf, process and baseline capture helpers; standalone diagnostic CLI |
| `app/sql_profiling.py` | Bounded, PID/execution-verified SQL CPU stack sampling |
| `app/sysinsight_candidate_benchmark.py` | Candidate validation and controlled benchmark support |
| `app/sysinsight_detection.py` | Original source differential-profile and function matching adapter |
| `app/sysinsight_dream_bridge.py` | Service entry point; collector, queues, SQLite, API and retention |
| `app/sysinsight_live_chain.py` | Alert evidence → original SysInsight → candidate apply/retest/restore |
| `app/sysinsight_original_llm.py` | Original upstream LLAMBO prompt/parser integration and API audit |
| `app/sysinsight_pipeline.py` | Explicit case-artifact replay compatibility entry used by the bridge |
| `app/sysinsight_prometheus.py` | Prometheus alert/metric observations and canonical tuning input |
| `app/sysinsight_shap.py` | Optional model training, model integrity and conservative attribution |
| `app/sysinsight_source_evidence.py` | PostgreSQL source matching and provenance construction |
| `app/tpcc_cases/c01_sort.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c01_sort_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c02_distinct.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c02_distinct_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c03_parallel_join.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c03_parallel_join_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c04_parallel_cost.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c04_parallel_cost_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c05_jit.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c05_jit_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c06_maintenance.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c06_maintenance_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c07_temp_buffers.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c07_temp_buffers_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c08_parallel_contention.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c08_parallel_contention_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c09_window_work_mem.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c09_window_work_mem_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c10_window_spill.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c10_window_spill_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c11_random_page_cost.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c11_random_page_cost_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c12_hash_join_spill.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c12_hash_join_spill_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c13_double_sort.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c13_double_sort_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c14_parallel_cost_heavy.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c14_parallel_cost_heavy_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c15_maintenance_heavy.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c15_maintenance_heavy_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c16_union_reconciliation.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c16_union_reconciliation_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c17_intersect_reconciliation.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c17_intersect_reconciliation_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c18_except_reconciliation.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c18_except_reconciliation_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c19_cpu_tuple_parallel.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/c19_cpu_tuple_parallel_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/d01_work_mem_sort.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/d01_work_mem_sort_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/d02_parallel_worker_contention.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/d02_parallel_worker_contention_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/d03_parallel_scan_threshold.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/d03_parallel_scan_threshold_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/d04_nestloop_misestimation.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/d04_nestloop_misestimation_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/d05_parallel_tuple_pressure.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/d05_parallel_tuple_pressure_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/d06_parallel_setup_pressure.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/d06_parallel_setup_pressure_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/d07_temp_buffer_pressure.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/d07_temp_buffer_pressure_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/d08_parallel_worker_cap.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/d08_parallel_worker_cap_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/d09_jit_expression_pressure.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/d09_jit_expression_pressure_tuned.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/normal.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/tp_autovacuum_churn.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/tp_delivery_burst.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/tp_new_order_burst.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/tp_new_order_hot.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/tp_normal.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/tp_order_status_burst.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/tp_order_status_hot.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/tp_payment_burst.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/tp_payment_hot.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/tp_read_mix_burst.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/tp_remote_new_order.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/tp_remote_payment.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/tp_stock_level_burst.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_cases/tp_stock_level_hot.sql` | Rollback-protected transaction or pressure workload SQL |
| `app/tpcc_external_cases.py` | Pressure recipes and PostgreSQL/pgbench execution helpers |
| `app/tpcc_transaction_cases.py` | TPC-C transaction-oriented scene recipes |
| `config/dream.example.json` | Portable configuration template without credentials |
| `config/env.example` | Portable configuration template without credentials |
| `docs/API.md` | Operator/developer documentation or source inventory |
| `docs/ARCHITECTURE.md` | Operator/developer documentation or source inventory |
| `docs/CONFIGURATION.md` | Operator/developer documentation or source inventory |
| `docs/DEPENDENCIES.md` | Operator/developer documentation or source inventory |
| `docs/DEPLOYMENT.md` | Operator/developer documentation or source inventory |
| `docs/FILE_INVENTORY.md` | Operator/developer documentation or source inventory |
| `docs/KNOWN_LIMITATIONS.md` | Operator/developer documentation or source inventory |
| `docs/RELEASE_VALIDATION.md` | Operator/developer documentation or source inventory |
| `external-sources.lock` | Project documentation, license, dependency lock or development configuration |
| `integrations/dream/assets.json` | Pinned external-source identity or reviewed compatibility patch |
| `integrations/dream/local-integration.patch` | Pinned external-source identity or reviewed compatibility patch |
| `integrations/dream/rcrank-compatibility.patch` | Pinned external-source identity or reviewed compatibility patch |
| `integrations/sources.json` | Pinned external-source identity or reviewed compatibility patch |
| `monitoring/codex_api_launcher.py` | Deployment/runtime lifecycle |
| `monitoring/config/grafana/grafana.ini` | Prometheus/Grafana/exporter configuration or dashboard |
| `monitoring/config/grafana/provisioning/dashboards/dashboards.yml` | Prometheus/Grafana/exporter configuration or dashboard |
| `monitoring/config/grafana/provisioning/datasources/prometheus.yml` | Prometheus/Grafana/exporter configuration or dashboard |
| `monitoring/config/postgres_exporter/queries.yaml` | Prometheus/Grafana/exporter configuration or dashboard |
| `monitoring/config/prometheus/prometheus.yml` | Prometheus/Grafana/exporter configuration or dashboard |
| `monitoring/config/prometheus/rules/sysinsight-demo.yml` | Prometheus/Grafana/exporter configuration or dashboard |
| `monitoring/dashboards/local-postgresql-demo.json` | Prometheus/Grafana/exporter configuration or dashboard |
| `monitoring/dashboards/sysinsight-dream-automation.json` | Prometheus/Grafana/exporter configuration or dashboard |
| `monitoring/dashboards/sysinsight-experiment-lab.json` | Prometheus/Grafana/exporter configuration or dashboard |
| `monitoring/deployment.py` | Deployment/runtime lifecycle |
| `monitoring/install_open_source.sh` | Deployment/runtime lifecycle |
| `monitoring/prepare_runtime.py` | Deployment/runtime lifecycle |
| `monitoring/process_control.py` | Deployment/runtime lifecycle |
| `monitoring/start.sh` | Deployment/runtime lifecycle |
| `monitoring/status.sh` | Deployment/runtime lifecycle |
| `monitoring/stop.sh` | Deployment/runtime lifecycle |
| `monitoring/versions.lock` | Deployment/runtime lifecycle |
| `package-lock.json` | Project documentation, license, dependency lock or development configuration |
| `package.json` | Project documentation, license, dependency lock or development configuration |
| `postgresql-source.lock` | Project documentation, license, dependency lock or development configuration |
| `requirements-dev.txt` | Project documentation, license, dependency lock or development configuration |
| `requirements-dream.lock.txt` | Project documentation, license, dependency lock or development configuration |
| `requirements-sysinsight.txt` | Project documentation, license, dependency lock or development configuration |
| `scripts/build_profiles.sh` | Dependency setup, profile generation, verification or deployment entry point |
| `scripts/check_dream_assets.py` | Dependency setup, profile generation, verification or deployment entry point |
| `scripts/deploy.sh` | Dependency setup, profile generation, verification or deployment entry point |
| `scripts/fetch_dream_source.py` | Dependency setup, profile generation, verification or deployment entry point |
| `scripts/fetch_flamegraph.sh` | Dependency setup, profile generation, verification or deployment entry point |
| `scripts/fetch_keeninsight_sources.sh` | Dependency setup, profile generation, verification or deployment entry point |
| `scripts/fetch_postgresql_source.sh` | Dependency setup, profile generation, verification or deployment entry point |
| `scripts/release_check.py` | Dependency setup, profile generation, verification or deployment entry point |
| `scripts/setup_dream_python.sh` | Dependency setup, profile generation, verification or deployment entry point |
| `scripts/setup_python.sh` | Dependency setup, profile generation, verification or deployment entry point |
| `scripts/test.sh` | Dependency setup, profile generation, verification or deployment entry point |
| `tests/browser.cjs` | Automated contract, security, retention or browser regression |
| `tests/test_bridge_retention.py` | Automated contract, security, retention or browser regression |
| `tests/test_codex_api_launcher.py` | Automated contract, security, retention or browser regression |
| `tests/test_deployment.py` | Automated contract, security, retention or browser regression |
| `tests/test_dream_auto_apply.py` | Automated contract, security, retention or browser regression |
| `tests/test_keeninsight_portal.py` | Automated contract, security, retention or browser regression |
| `tests/test_release_contracts.py` | Automated contract, security, retention or browser regression |
| `tests/test_sql_profiling_and_shap.py` | Automated contract, security, retention or browser regression |
