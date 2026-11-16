"""Real persisted result consumers; the provider is an owned loopback fixture."""

from diagnosis_fake import respond, serve
from diagnosis_fixtures import text_payload
from test_diagnosis_lifecycle import admission, context as _context, deployment as _deployment
from test_diagnosis_validation import _corpus, _crashloop, _raw
from conftest import sign_in

from cloudops.entrypoints.worker import run_once
from cloudops.features.diagnosis.models import Analysis
from cloudops.features.diagnosis.projections import handoff_sections
from cloudops.features.incidents import handoff
from cloudops.features.investigations.models import InvestigationRun


context = _context
deployment = _deployment


def test_stored_result_handoff_comparison_and_saved_bytes(
    client, db, context, deployment, monkeypatch, tmp_path
):
    _corpus(tmp_path)
    db.get(InvestigationRun, context[1]).question = "memory"
    db.commit()
    _crashloop(db, context)
    with serve(
        lambda h: respond(
            h,
            text_payload(
                _raw(
                    runbook_citation_ids=["K001"],
                    unverified_checks=[{"check": "config", "reason": "Not collected"}],
                    missing_evidence=["Previous revision"],
                )
            ),
        )
    ) as server:
        deployment(server.url)
        identifier = admission(db, context)
        assert run_once()
        headers = sign_in(client)
        url = f"/api/incidents/{context[0]}"
        preview = client.get(url + "/handoff/preview")
        assert preview.status_code == 200, preview.text
        data = preview.json()
        assert data["schema_version"] == "HandoffSnapshot/v3"
        sections = data["snapshot"]
        assert sections["findings"]["items"][0]["analysis_id"] == str(identifier)
        assert sections["findings"]["items"][0]["result"]["summary"] in data["markdown"]
        assert sections["citations"]["items"][0]["locator"]["key"].startswith("v1_")
        assert sections["runbook_citations"]["items"][0]["reference"]["kind"] == "runbook"
        assert sections["unverified_checks"]["items"][0]["summary"] == "Not collected"
        assert sections["open_questions"]["items"][0]["summary"] == "Previous revision"
        earlier = db.get(InvestigationRun, context[1])
        other = InvestigationRun(
            incident_id=earlier.incident_id,
            requested_by=earlier.requested_by,
            state="failed",
            reason="Controlled unavailable",
            question="Why?",
            question_source="operator",
        )
        db.add(other)
        db.commit()
        comparison = client.get(
            url + f"/runs/{context[1]}/compare",
            params={"right_incident_id": str(context[0]), "right_run_id": str(other.id)},
        )
        assert comparison.status_code == 200, comparison.text
        pair = comparison.json()
        assert pair["left"]["analysis"]["result"]["run_id"] == str(context[1])
        assert pair["right"]["analysis"]["result"] is None
        resolved = client.post(
            url + "/lifecycle",
            headers=headers,
            json={"action": "resolve", "reason": "Controlled handoff"},
        )
        assert resolved.status_code == 200, resolved.text
        activities = client.get(url + "/activity").json()["items"]
        resolution = next(a["id"] for a in activities if a["kind"] == "resolve")
        params = {"resolution_id": resolution}
        saved = client.get(url + "/handoff", params=params).content
        saved_json = client.get(url + "/handoff/preview", params=params).json()
        assert (
            client.post(
                url + "/lifecycle",
                headers=headers,
                json={"action": "reopen", "reason": "Later work"},
            ).status_code
            == 200
        )
        monkeypatch.setattr(handoff, "render", lambda _: "changed")
        assert client.get(url + "/handoff", params=params).content == saved
        assert client.get(url + "/handoff/preview", params=params).json() == saved_json
        assert len(server.requests) == 1
        db.expire_all()
        db.get(Analysis, identifier).purpose = "evaluation"
        db.commit()
        assert handoff_sections(db, [context[1]])["findings"]["status"] == "unavailable"
