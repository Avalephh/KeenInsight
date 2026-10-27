# KeenInsight

See [reproduction preparation](docs/REPRODUCING.md) for pinned downloads,
trusted model import, offline wheelhouses, extension builds and fresh TP/AP
data generation. Dependency preparation does not resolve the documented
TP candidate/TPS linkage and DREAM internal-evaluation collection limitations.

Traceable PostgreSQL performance diagnosis, controlled tuning experiments,
and live result presentation. [中文说明](README.md)

This clean source branch contains the application, static UI, configuration
templates, dependency locks, integration patches, tests and operator docs.
It deliberately excludes historical experiments, databases, logs, credentials,
pretrained models, virtual environments and monitoring distributions.

## What it does

- **SysInsight:** a Prometheus alert initiates real perf capture,
  source/function association, the original SysInsight/LLAMBO recommendation
  path, candidate validation and temporary apply/retest/restore evidence.
- **DREAM:** a completed replayable read-only analytical statement meeting the
  configured duration gate is diagnosed asynchronously. Validated hints can
  be published to PostgreSQL; rewrites/session actions apply through the
  bridge-managed execution path, with actual timings and hit records.
- **Presentation:** hosts, alerts, SQL executions, exact-job evidence,
  parameter/source references and empirical tuning knowledge. Grafana owns
  monitoring time series; there is no duplicate realtime-monitor page.
- **Evidence:** separate execution and job identities, actual model responses,
  optional SQL CPU stack samples, conservative SHAP availability, and bounded
  24-hour operational history.

Read [known limitations](docs/KNOWN_LIMITATIONS.md) before drawing conclusions:
the TP console's explicitly supplied candidate replay is not connected to that
alert's fresh LLM candidate, and DREAM internal-evaluation isolation has a known
gap. A completed job is not proof of improvement, correctness or safe production
operation.

## Check the source

```bash
git clone --branch opensource/clean-20261027 --single-branch \
  https://github.com/Avalephh/KeenInsight.git
cd KeenInsight
python3 -m pip install -r requirements-dev.txt
bash scripts/test.sh
```

Use Python 3.10/3.11 (the dev file adds TOML parsing for 3.10; 3.11 uses stdlib).
Node.js adds JavaScript syntax checks. Tests use temporary
fixtures and do not contact a live database or provider. Optional model/reference
tests are explicitly skipped when their separately installed assets are absent.

## Deployment outline

The reference deployment is Linux/systemd, PostgreSQL 12, pg_stat_statements,
pg_hint_plan, perf and pgbench, using separate SysInsight and DREAM Python
environments. Existing TP/AP workload data is an explicit prerequisite.
No install script initializes or replaces a database.

1. Review [security](SECURITY.md) and [deployment](docs/DEPLOYMENT.md).
2. Fetch the pinned external sources with `fetch_keeninsight_sources.sh`,
   `fetch_postgresql_source.sh`, `fetch_dream_source.py` and
   `fetch_flamegraph.sh`; generate reference assets using `build_profiles.sh`.
3. Prepare both Python environments using `setup_python.sh` and
   `setup_dream_python.sh`. Obtain trusted real model assets separately and run
   `check_dream_assets.py`.
4. Copy `config/env.example` to the private `monitoring/.env.local`, chmod 600,
   and configure the database, provider, model, API credential and Grafana
   initial password.
5. Run `bash scripts/deploy.sh doctor`, then, only on the intended test host,
   `sudo bash scripts/deploy.sh install`.

Local UI: `http://127.0.0.1:9108/keeninsight/`; lab: `/lab`; actions: `/ui`.
Grafana uses port 3000, Prometheus 9090. The bridge's mutation endpoints are
not authenticated: keep them on loopback, behind an authenticated proxy/tunnel.

## Layout and provenance

`app/` holds runtime code/UI; `monitoring/` holds lifecycle/configuration;
`integrations/` pins external source and patch identities; `config/` contains
templates; `scripts/`, `tests/`, `docs/` provide verification and documentation.
See the [complete file inventory](docs/FILE_INVENTORY.md).

The MIT license covers original project integration code, not external
dependencies or their upstream source covered by compatibility patches.
Some audited upstream snapshots do not provide a clear license; the source
and model fetch instructions are not a redistribution grant. See
[NOTICE](NOTICE) and [dependency terms](docs/DEPENDENCIES.md).
