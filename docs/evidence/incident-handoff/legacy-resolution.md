# Incident handoff

````json
{
  "activity": [
    {
      "id": "9299ee55-47b1-4d99-b26a-095b01ad3af8",
      "kind": "note",
      "text": "Original human note · Tiếng Việt preserved.\n<script>fixture-only</script>\n```human backticks stay intact```",
      "actor_id": "6baa2d7c-97ba-4052-b488-8845d1058d81",
      "created_at": "2026-11-04T10:34:26.697657+00:00"
    },
    {
      "id": "6dc0e723-3004-49b6-b70f-d0ab95a056a0",
      "kind": "acknowledge",
      "text": "Original acknowledgment",
      "actor_id": "6baa2d7c-97ba-4052-b488-8845d1058d81",
      "created_at": "2026-11-04T10:34:26.705761+00:00"
    },
    {
      "id": "196a90ae-3445-42c4-929a-4c87284066e5",
      "kind": "resolve",
      "text": "Legacy resolution before structured-renderer upgrade",
      "actor_id": "6baa2d7c-97ba-4052-b488-8845d1058d81",
      "created_at": "2026-11-04T10:34:26.716417+00:00"
    }
  ],
  "incident": {
    "id": "6a471a69-7c26-466c-8176-37d23df718ac",
    "state": "resolved",
    "title": "Issue38 legacy immutable export acceptance",
    "origin": {
      "kind": "operator",
      "source_id": null
    },
    "severity": {
      "value": null,
      "provenance": "not_provided"
    },
    "workload": {
      "id": "311ef95d-1724-49d4-b911-4b255b676d56",
      "uid": "4a6e99ae-c87d-4605-b621-3f0fdb01d6a9",
      "name": "checkout",
      "active": true,
      "health": "unknown",
      "source": "live_kubernetes",
      "cluster": "local-k3s",
      "namespace": "demo",
      "observation": {
        "ready": null,
        "desired": null,
        "reasons": [
          "not_observed"
        ],
        "coverage": "unknown",
        "freshness": "unknown",
        "refreshing": false,
        "age_seconds": null,
        "evaluated_at": "2026-11-04T10:34:26.722629Z",
        "latest_outcome": null,
        "last_attempt_at": null,
        "last_failure_at": null,
        "last_success_at": null,
        "effective_health": "unknown",
        "poll_lag_seconds": null,
        "last_failure_reason": null,
        "stale_after_seconds": 60,
        "refresh_remaining_seconds": null
      },
      "retirement_reason": null
    },
    "created_at": "2026-11-04T10:34:23.522606Z",
    "created_by": "6baa2d7c-97ba-4052-b488-8845d1058d81",
    "description": "Actual persisted acceptance data; collection remains explicitly synthetic.",
    "workload_id": "311ef95d-1724-49d4-b911-4b255b676d56",
    "alert_signal": "none",
    "last_seen_at": null,
    "first_seen_at": null
  },
  "executions": [],
  "limitations": "Stored observations and human notes only. No new diagnosis was generated. Historical unknown execution outcomes and target locks remain independent of incident resolution.",
  "investigations": {
    "items": [
      {
        "id": "74657a5e-3322-494a-a240-07f21d97d3f6",
        "state": "insufficient_evidence",
        "origin": {
          "kind": "operator",
          "source_id": null
        },
        "reason": "Fixture collection saved. Live evidence and AI diagnosis are unavailable; no recovery plan exists.",
        "window": {
          "end": "2026-11-04T10:34:23.524084Z",
          "start": "2026-11-04T10:04:23.524084Z",
          "source": "legacy_default"
        },
        "attempts": [
          {
            "error": null,
            "state": "succeeded",
            "number": 1,
            "started_at": "2026-11-04T10:34:24.078403Z",
            "finished_at": "2026-11-04T10:34:26.127106Z"
          }
        ],
        "evidence": [
          {
            "id": "6df0afa6-464d-46b7-a4aa-8faea1f57613",
            "run_id": "74657a5e-3322-494a-a240-07f21d97d3f6",
            "sha256": "f42d773539d04d66c1555d468f7feb2f0d9d8e32e8b0a97519de0b9b07de9c1e",
            "source": "development_fixture",
            "payload": {
              "label": "Synthetic development evidence; no live cluster was contacted.",
              "workload": "local-k3s/demo/checkout",
              "observation": "Live workload health and telemetry are unavailable."
            },
            "observed_at": "2026-11-04T10:34:26.119866Z",
            "collected_at": "2026-11-04T10:34:26.119866Z",
            "workload_uid": "4a6e99ae-c87d-4605-b621-3f0fdb01d6a9",
            "schema_version": 1
          }
        ],
        "question": "Issue38 legacy immutable export acceptance",
        "recovery": "unavailable",
        "diagnosis": "unavailable",
        "created_at": "2026-11-04T10:34:23.522606Z",
        "finished_at": "2026-11-04T10:34:26.135497Z",
        "incident_id": "6a471a69-7c26-466c-8176-37d23df718ac",
        "outcome_code": null,
        "queue_reason": null,
        "requested_by": "6baa2d7c-97ba-4052-b488-8845d1058d81",
        "question_source": "title_default"
      }
    ],
    "next_offset": null
  }
}
````
