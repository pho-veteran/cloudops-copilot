# Knowledge index acceptance evidence

The original JSON receipts in this directory describe the initial PR candidate and its version-1 index configuration. They remain historical records; their test counts and API-only restart receipt do not establish later fixes or a PostgreSQL restart.

The follow-up lifecycle receipt and verification record describe the corrected candidate. Their `source_commit` identifies the commit containing the product implementation and lifecycle driver, before the evidence-only commit. Compare the source commit with the PR head to confirm that subsequent changes affect evidence only.

The lifecycle probe exercises real host CLI processes and a loopback HTTP API against an owned PostgreSQL container. Temporary files are controlled fixtures; the initial eight-guide import uses the repository release corpus. Interruption barriers wrap the normal CLI in disposable subprocesses before capture and after the activation transaction commits. They are test harness hooks, not product switches. PostgreSQL guard-loss and transactional rollback checks run separately as integration tests.

No browser, semantic retrieval, Run pinning, production deployment or live Kubernetes recovery is claimed for #59.
