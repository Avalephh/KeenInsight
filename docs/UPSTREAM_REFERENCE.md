# Upstream KeenInsight reference

The previous KeenInsight implementation is publicly available at
<https://github.com/Avalephh/KeenInsight>.  The repository has several
independent branches: `main`/`WorkloadTune_new` contain the Vue WorkloadTune
flow, `dev` contains the monitoring/pgbench experiment pipeline, `dream`
contains a separate SQL-agent prototype, and `db-replay` contains a separate
Go replay service.  Their exact compatibility status and boundaries are
recorded in [the branch audit](UPSTREAM_COMPATIBILITY_AUDIT.md).

This PostgreSQL demo uses the WorkloadTune flow as a compatibility reference
for the live `/api/tune/*` adapter, while the supplied `English.zip` HTML/CSS
remains the visual shell served at `/`.  The live pages are backed by the
current PostgreSQL API; the remote repository is reference material, not a
runtime dependency.  This keeps the demo reproducible offline and makes the
source boundary explicit for RAG retrieval.
