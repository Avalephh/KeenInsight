# Contributing

1. Start from the clean source branch and create a feature branch.
2. Read `docs/ARCHITECTURE.md`, `docs/KNOWN_LIMITATIONS.md` and `SECURITY.md`.
3. Install `requirements-dev.txt`, then run `bash scripts/test.sh`. No production database, provider API or real
   pressure workload is contacted by this test command.
4. Keep runtime data, weights, external checkouts and operator credentials
   outside Git. Update `docs/FILE_INVENTORY.md` when adding a file.
5. Add regression coverage for behavior changes. Preserve exact Job/SQL/run
   association and unknown-vs-zero semantics. Do not turn failures, skipped
   stages or model confidence into fabricated measured success.
6. For live experiments, document machine/DB versions, actual pressure,
   baseline and warm controls, candidate provenance, restoration, failures
   and number of independent LLM runs. Do not check raw operational data into
   the source repository.
7. Update external source locks and patches deliberately. Test `git apply
   --check` against the pinned commit and review upstream license changes.

The project currently uses a small Python service and static HTML/CSS/JS;
avoid introducing a frontend build system or duplicated monitoring stack
without a concrete need.
