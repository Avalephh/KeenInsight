# Security policy

This is a privileged, single-host research/demo service, not a hardened
multi-tenant database management plane. Run it only against a disposable
database or an explicitly approved test environment.

- The bridge serves experiment/reset/apply endpoints without application
  authentication. Keep it bound to loopback. Use an authenticated reverse
  proxy or SSH tunnel, not a public port-forward.
- Use least privilege where possible. Parameter changes, perf, peer-authenticated
  workers, systemd resource limits and extension setup need additional privileges.
  The current Linux deployment typically uses a root supervisor and postgres
  workers; this is not an endorsement for production deployment.
- LLM inputs can contain SQL literals, schema names, plan information and source
  excerpts. Obtain approval before sending sensitive database information to a
  remote provider.
- Store API credentials and Grafana's initial password only in the private
  environment file or a secret manager. Do not commit credentials, auth.json,
  SQL captures, logs, dumps or generated worker configuration.
- Model files may deserialize Python/PyTorch/joblib objects. Only use trusted
  sources and verify integrity. File hashes do not make an untrusted pickle safe.
- A parser/allowlist is defense in depth, not a complete sandbox or formal SQL
  equivalence proof. Performance changes may regress latency, durability or
  correctness. Inspect the documented policies and restoration evidence.
- Reset/clear/rollback controls mutate state. Experiment scripts execute real
  SQL and produce real resource pressure; they are not unit tests.

Report a suspected vulnerability privately to the repository maintainer
through an available private security advisory channel. Do not post credentials,
database contents or exploitable deployment details in public issues. No
response-time or production-security guarantee is offered.
