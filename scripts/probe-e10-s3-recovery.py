#!/usr/bin/env python3
"""Exercise one E10 S3 recovery action through the normal E07 product API.

The trusted lab manager owns faults, loads, writer lifecycle and resets. This driver only
signs in as an ordinary Operator over same-origin HTTPS, opens a manual incident, proposes
the qualified plan, reads its immutable review and approves it explicitly. It then saves
the execution and verification read responses privately for ``trial post-check``.
"""

import argparse
import json
import os
import secrets
import ssl
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_lab as LAB  # noqa: E402
from e10_campaign import CampaignError, RECOVERY_CASES, TrialAdmission, check_window  # noqa: E402
from cloudops.features.evaluation.hashing import content_sha256  # noqa: E402

ORIGIN = "https://localhost:8443"
OUT = LAB.LAB_DIR / "product"


def die(message):
    raise SystemExit(f"probe-e10-s3-recovery: {message}")


def guard():
    if LAB.PROJECT != LAB.RECOVERY_LAB_PROJECT:
        die(f"set CLOUDOPS_LAB_PROJECT={LAB.RECOVERY_LAB_PROJECT}.")
    if not (LAB.LAB_DIR / "owner.json").is_file():
        die("start the recovery lab through manage-local-lab.py first.")


def dc(*args, stdin=None, timeout=120):
    result = subprocess.run(
        [*LAB.COMPOSE, *args],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=LAB.ROOT,
        timeout=timeout,
    )
    if result.returncode != 0:
        die(f"compose {args[0]} failed (exit {result.returncode}).")
    return result.stdout


def password():
    path = OUT / "operator.password"
    if not path.exists():
        OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
        LAB.private_write(path, secrets.token_urlsafe(24))
    return path.read_text().strip()


def client():
    import httpx

    return httpx.Client(
        base_url=ORIGIN,
        verify=ssl.create_default_context(cafile=str(OUT / "ca.crt")),
        trust_env=False,
        timeout=30,
    )


def login(session):
    response = session.post(
        "/api/login",
        json={"username": "operator", "password": password()},
        headers={"Origin": ORIGIN},
    )
    if response.status_code != 200:
        die(f"operator sign-in returned {response.status_code}.")
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def save(name, value):
    LAB.private_write(OUT / name, json.dumps(value, indent=2) + "\n")
    return OUT / name


def phase_accounts(_args):
    os.umask(0o077)
    dc(
        "exec",
        "-T",
        "api",
        "python",
        "-m",
        "cloudops.entrypoints.cli",
        "account",
        "operator",
        "--role",
        "operator",
        "--password-stdin",
        stdin=password() + "\n",
    )
    dc("cp", "https:/data/caddy/pki/authorities/local/root.crt", str(OUT / "ca.crt"))
    os.chmod(OUT / "ca.crt", 0o600)
    print(json.dumps({"operator": "provisioned", "ca": str(OUT / "ca.crt")}))


def incident_request(workload_id):
    """A neutral operator request; scenario IDs, controls and labels stay out of the Run."""
    return {
        "workload_id": workload_id,
        "title": "Checkout requests are failing",
        "description": "Investigate the current checkout health and the supported recovery options.",
    }


def checkout_workload(session):
    uid = json.loads((LAB.LAB_DIR / "owner.json").read_text())["deployments"]["checkout"]
    items = [item for item in session.get("/api/workloads").json()["items"] if item["uid"] == uid]
    if len(items) != 1:
        die("the owned checkout UID is not exactly one configured workload.")
    return items[0]


def phase_baseline(_args):
    """Confirm the healthy recovery sample as the known-good baseline before any bad rollout."""
    if (LAB.LAB_DIR / "trial.json").exists() or (LAB.LAB_DIR / "fault.json").exists():
        die("confirm the known-good baseline only from the idle healthy target.")
    with client() as session:
        headers = login(session)
        workload = checkout_workload(session)
        first = session.get(f"/api/workloads/{workload['id']}/baseline-candidate")
        if first.status_code != 200:
            die(f"baseline candidate returned {first.status_code}: {first.text[:300]}")
        candidate = first.json()
        wait = (
            datetime.fromisoformat(candidate["confirm_after"])
            - datetime.fromisoformat(candidate["server_time"])
        ).total_seconds()
        time.sleep(max(0.0, wait) + 1)
        confirmed = session.post(
            f"/api/workloads/{workload['id']}/baseline",
            json={"candidate_id": candidate["id"], "digest": candidate["digest"]},
            headers=headers,
        )
        if confirmed.status_code != 201:
            die(f"baseline confirmation returned {confirmed.status_code}: {confirmed.text[:300]}")
        baseline = confirmed.json()
        path = save("baseline.json", baseline)
        print(
            json.dumps(
                {
                    "baseline_id": baseline["id"],
                    "digest": baseline["digest"],
                    "confirmed_at": baseline["confirmed_at"],
                    "path": str(path),
                },
                indent=2,
            )
        )


def phase_act(args):
    trial = json.loads((LAB.LAB_DIR / "trial.json").read_text())
    if trial.get("scenario_id") != args.scenario or trial.get("status") != "observed":
        die("act only on the active, observed trial for this scenario.")
    if args.scenario != "recovery-traffic-scale":
        die("only qualified manual scale needs no model analysis; other templates need E06 advice.")
    with client() as session:
        headers = login(session)
        workload = checkout_workload(session)
        if workload["uid"] != trial["target_uid"]:
            die("the configured workload UID differs from the trial target.")
        opened = session.post(
            "/api/incidents",
            json=incident_request(workload["id"]),
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        if opened.status_code != 201:
            die(f"incident admission returned {opened.status_code}: {opened.text[:300]}")
        incident = opened.json()
        # Approval requires the incident's admitted investigation Run to finish first.
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            run_view = session.get(
                f"/api/incidents/{incident['incident_id']}/runs/{incident['run_id']}"
            ).json()
            if run_view.get("state") not in {"queued", "collecting"}:
                break
            time.sleep(3)
        else:
            die("the incident's investigation Run did not finish.")
        replicas = trial["qualification"]["declared_resources"]["replicas"]
        deadline = time.monotonic() + 120
        while True:
            proposed = session.post(
                f"/api/incidents/{incident['incident_id']}/plans",
                json={"replicas": replicas, "rationale": "Scale to the qualified capacity."},
                headers=headers,
            )
            code = proposed.json().get("error", {}).get("code") if proposed.content else None
            if code != "target_cooldown" or time.monotonic() > deadline:
                break
            time.sleep(5)
        if proposed.status_code != 201:
            die(f"plan proposal returned {proposed.status_code}: {proposed.text[:300]}")
        plan = proposed.json()
        review = session.get(f"/api/plans/{plan['id']}").json()
        content = review["plan"]["content"]
        if (
            content.get("schema_version") != "RecoveryPlan/v2"
            or content.get("execution_capability") != "conditional_dispatch_v2"
            or content.get("verification_profile_digest")
            != trial["qualification"]["profile_digest"]
        ):
            die("the plan is not bound to the qualified verification profile.")
        decided = session.post(
            f"/api/plans/{plan['id']}/decision",
            json={
                "version": review["plan"]["version"],
                "digest": review["plan"]["digest"],
                "review_token": review["review_token"],
                "decision": "approve",
            },
            headers=headers,
        )
        if decided.status_code != 200:
            die(f"approval returned {decided.status_code}: {decided.text[:300]}")
        approved_at = time.time()
        execution = None
        deadline = time.monotonic() + args.wait
        while time.monotonic() < deadline:
            rows = session.get(f"/api/incidents/{incident['incident_id']}/executions").json()
            if rows:
                execution = session.get(f"/api/executions/{rows[0]['id']}").json()
                if execution["state"] not in {"queued", "executing", "verifying"}:
                    break
            time.sleep(5)
        if execution is None:
            die("no execution was admitted.")
        verification = session.get(f"/api/executions/{execution['id']}/verification").json()
        paths = {
            "execution": str(save(f"execution-{execution['id']}.json", execution)),
            "verification": str(save(f"verification-{execution['id']}.json", verification)),
        }
        print(
            json.dumps(
                {
                    "incident_id": incident["incident_id"],
                    "plan_id": plan["id"],
                    "plan_digest": review["plan"]["digest"],
                    "execution_id": execution["id"],
                    "execution_state": execution["state"],
                    "verification_state": (verification.get("result") or {}).get("state"),
                    "approved_to_final_seconds": round(time.time() - approved_at),
                    **paths,
                },
                indent=2,
            )
        )


S6_TEMPLATES = {
    "recovery-oom-memory": "set_memory_profile",
    "recovery-bad-rollout-restore": "restore_deployment_template",
    "recovery-traffic-scale": "scale_deployment",
}


def s6_trial():
    trial = json.loads((LAB.LAB_DIR / "trial.json").read_text())
    if (
        trial.get("status") != "observed"
        or trial.get("scenario_id") not in S6_TEMPLATES
        or not trial.get("qualification")
        or (trial.get("load") or {}).get("state") != "running"
    ):
        die("S6 requires one observed, qualified recovery trial with its bounded load running.")
    return trial


def product(response, statuses=(200,)):
    if response.status_code not in statuses:
        die(f"product request returned {response.status_code}.")
    return response.json()


def s6_open(args):
    trial = s6_trial()
    try:
        check_window(trial, datetime.fromisoformat(args.start), datetime.fromisoformat(args.end))
    except (CampaignError, ValueError):
        die("the window must cover this trial's fault and detection while its load is active.")
    with client() as session:
        headers = login(session)
        workload = checkout_workload(session)
        if workload["uid"] != trial["target_uid"]:
            die("the target UID changed.")
        request = incident_request(workload["id"])
        request["question"] = "What do the stored observations support about this workload?"
        request["window"] = {"start": args.start, "end": args.end}
        admitted = product(
            session.post(
                "/api/incidents",
                json=request,
                headers={**headers, "Idempotency-Key": trial["trial_id"]},
            ),
            (200, 201),
        )
        path = save(
            f"s6-incident-{trial['trial_id']}.json", {**admitted, "trial_id": trial["trial_id"]}
        )
        print(path)


def s6_admission(args):
    trial = s6_trial()
    if args.case_id not in RECOVERY_CASES[trial["scenario_id"]]:
        die("the case ID is outside this scenario's five planned slots.")
    incident = json.loads((OUT / f"s6-incident-{trial['trial_id']}.json").read_text())
    run = f"/api/incidents/{incident['incident_id']}/runs/{incident['run_id']}"
    with client() as session:
        login(session)
        analysis = product(session.get(f"{run}/analysis"))
    if analysis["status"] != "pending" or not analysis["analysis_id"]:
        die("the operational Analysis is not awaiting its first send.")
    if content_sha256(analysis["identity"]) != args.deployment_digest:
        die("the admitted deployment differs from the authorized deployment.")
    # The full profile digest is taken from the separately approved credential-free profile.
    admission = TrialAdmission(
        case_id=args.case_id,
        trial_id=trial["trial_id"],
        lab_run_id=trial["run_id"],
        lab_window_id=trial["window_id"],
        incident_id=incident["incident_id"],
        run_id=incident["run_id"],
        analysis_id=analysis["analysis_id"],
        workload_uid=trial["target_uid"],
        window_start=incident["window"]["start"],
        window_end=incident["window"]["end"],
        profile_digest=args.profile_digest,
        source_binding_sha256=trial["source_binding_sha256"],
    )
    print(save(f"s6-admission-{trial['trial_id']}.json", admission.model_dump(mode="json")))


def s6_action(args):
    trial = s6_trial()
    binding = TrialAdmission.model_validate_json(args.admission.read_bytes())
    if (
        str(binding.trial_id) != trial["trial_id"]
        or str(binding.lab_run_id) != trial["run_id"]
        or str(binding.lab_window_id) != trial["window_id"]
        or binding.workload_uid != trial["target_uid"]
        or binding.source_binding_sha256 != trial["source_binding_sha256"]
    ):
        die("the action does not belong to the active trial.")
    if args.phase != "s6-evidence":
        try:
            check_window(trial, binding.window_start, binding.window_end)
        except CampaignError:
            die("the active trial's change or expired load invalidates this action.")
    template = S6_TEMPLATES[trial["scenario_id"]]
    incident = f"/api/incidents/{binding.incident_id}"
    with client() as session:
        headers = login(session)
        if args.phase == "s6-propose":
            analysis = product(session.get(f"{incident}/runs/{binding.run_id}/analysis"))
            if analysis["analysis_id"] != str(binding.analysis_id) or not analysis.get("result"):
                die("the trial has no matching Investigator result.")
            result = analysis["result"]
            print(save(f"s6-investigator-{binding.case_id}.json", result))
            if result.get("candidate_template_id") != template:
                die("the Investigator did not advise this trial's qualified action.")
            selector = {
                "scale_deployment": lambda: {
                    "replicas": trial["qualification"]["declared_resources"]["replicas"]
                },
                "restore_deployment_template": lambda: {
                    "baseline_id": trial["known_good_baseline"]["id"]
                },
                "set_memory_profile": lambda: {"profile_id": "e10-s3-recovery-memory"},
            }[template]()
            plan = product(
                session.post(
                    f"{incident}/plans",
                    json={
                        "origin": "analysis_advice",
                        "run_id": str(binding.run_id),
                        "analysis_id": str(binding.analysis_id),
                        "template": template,
                        "rationale": "Apply the Investigator's qualified recovery recommendation.",
                        **selector,
                    },
                    headers=headers,
                ),
                (201,),
            )
            print(save(f"s6-plan-{binding.case_id}.json", plan))
            return

        if args.phase == "s6-evidence":
            executions = product(session.get(f"{incident}/executions"))
            matches = [item for item in executions if item["plan_id"] == str(args.plan)]
            if len(matches) != 1:
                die("no unique execution matches the reviewed plan.")
            execution = f"/api/executions/{matches[0]['id']}"
            print(save(f"s6-execution-{binding.case_id}.json", product(session.get(execution))))
            print(
                save(
                    f"s6-verification-{binding.case_id}.json",
                    product(session.get(f"{execution}/verification")),
                )
            )
            print(
                save(f"s6-incident-outcome-{binding.case_id}.json", product(session.get(incident)))
            )
            return

        review = product(session.get(f"/api/plans/{args.plan}"))
        plan = review["plan"]
        if (
            plan["digest"] != args.digest
            or plan["incident_id"] != str(binding.incident_id)
            or plan["source_analysis_id"] != str(binding.analysis_id)
            or plan["template"] != template
            or plan["content"]["workload"]["uid"] != binding.workload_uid
            or plan["content"].get("execution_capability") != "conditional_dispatch_v2"
            or plan["content"].get("verification_profile_digest")
            != trial["qualification"]["profile_digest"]
            or not review.get("review_token")
        ):
            die("the current review is not the exact approved candidate.")
        decision = product(
            session.post(
                f"/api/plans/{args.plan}/decision",
                json={
                    "version": plan["version"],
                    "digest": args.digest,
                    "review_token": review["review_token"],
                    "decision": "approve",
                },
                headers=headers,
            )
        )
        print(save(f"s6-approval-{binding.case_id}.json", decision))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="phase", required=True)
    sub.add_parser("accounts")
    sub.add_parser("baseline")
    act = sub.add_parser("act")
    act.add_argument("--scenario", required=True)
    act.add_argument("--wait", type=int, default=480)
    open_run = sub.add_parser("s6-open")
    open_run.add_argument("--start", required=True)
    open_run.add_argument("--end", required=True)
    admission = sub.add_parser("s6-admission")
    admission.add_argument("--case-id", required=True)
    admission.add_argument("--deployment-digest", required=True)
    admission.add_argument("--profile-digest", required=True)
    propose = sub.add_parser("s6-propose")
    approve = sub.add_parser("s6-approve")
    evidence = sub.add_parser("s6-evidence")
    for action in (propose, approve, evidence):
        action.add_argument("--admission", type=Path, required=True)
    for action in (approve, evidence):
        action.add_argument("--plan", type=UUID, required=True)
    approve.add_argument("--digest", required=True)
    args = parser.parse_args()
    guard()
    phases = {
        "accounts": phase_accounts,
        "baseline": phase_baseline,
        "act": phase_act,
        "s6-open": s6_open,
        "s6-admission": s6_admission,
        "s6-propose": s6_action,
        "s6-approve": s6_action,
        "s6-evidence": s6_action,
    }
    phases[args.phase](args)


if __name__ == "__main__":
    main()
