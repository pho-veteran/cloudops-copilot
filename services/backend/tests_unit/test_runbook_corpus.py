"""Author-side RunbookCatalog/v1 preflight tests; no DB/network/import runtime."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
TITLE = "Investigate a fixture symptom"


@pytest.fixture
def checker(monkeypatch):
    scripts = ROOT / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "runbook_check", scripts / "check-runbook-corpus.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def document(**overrides):
    value = {
        "document_key": "fixture-guide",
        "version": "1",
        "title": TITLE,
        "source_path": "runbooks/fixture-guide.md",
        "guide_kind": "diagnosis",
        "tags": ["fixture"],
        "applicability": {
            "kind": "Deployment",
            "symptom_families": ["oom_restarts"],
            "recovery_templates": [],
        },
        "provenance": {
            "source_revision": "6c8eea37e982f3dc6de2755e7a482eb16aaf1f16",
            "reviewed_at": "2026-11-11",
        },
    }
    value.update(overrides)
    return value


def write_corpus(root: Path, documents: list[dict], contents: dict[str, str] | None = None):
    (root / "runbooks").mkdir(parents=True)
    (root / "runbooks/catalog.json").write_text(
        json.dumps({"schema_version": "RunbookCatalog/v1", "documents": documents}),
        encoding="utf-8",
    )
    for row in documents:
        path = root / row["source_path"]
        try:
            path.resolve().relative_to(root.resolve())
        except ValueError:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            (contents or {}).get(row["document_key"], f"# {row['title']}\n\nFixture.\n"),
            encoding="utf-8",
        )


def test_valid_schema_fixture_allows_empty_family(checker, tmp_path):
    write_corpus(
        tmp_path,
        [
            document(
                applicability={
                    "kind": "Deployment",
                    "symptom_families": [],
                    "recovery_templates": [],
                }
            )
        ],
    )
    report = checker.check_corpus(tmp_path, release=False)
    assert report.valid, report.errors
    assert report.documents[0]["symptom_families"] == []


def test_release_corpus_is_valid(checker):
    report = checker.check_corpus(ROOT)
    assert report.valid, report.errors
    assert len(report.documents) == 8
    assert {row["guide_kind"] for row in report.documents} == {
        "diagnosis",
        "recovery_template",
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda row: row.pop("provenance"),
        lambda row: row.update(applicability=None),
        lambda row: row.update(version=1),
        lambda row: row.update(guide_kind="other"),
        lambda row: row.update(source_path="/absolute/guide.md"),
        lambda row: row.update(source_path="runbooks/../escape.md"),
        lambda row: row.update(tags=["one", "one"]),
        lambda row: row.update(
            applicability={
                "kind": "Deployment",
                "symptom_families": ["oom_restarts", "oom_restarts"],
                "recovery_templates": [],
            }
        ),
        lambda row: row.update(
            guide_kind="recovery_template",
            applicability={
                "kind": "Deployment",
                "symptom_families": ["oom_restarts"],
                "recovery_templates": ["set_memory_profile", "set_memory_profile"],
            },
        ),
        lambda row: row.update(
            applicability={
                "kind": "Deployment",
                "symptom_families": ["unknown_family"],
                "recovery_templates": [],
            }
        ),
        lambda row: row.update(
            applicability={
                "kind": "Deployment",
                "symptom_families": ["oom_restarts"],
                "recovery_templates": ["unknown_template"],
            }
        ),
        lambda row: row.update(
            provenance={
                "source_revision": "not-a-sha",
                "reviewed_at": "2026-11-11",
            }
        ),
        lambda row: row.update(
            provenance={
                "source_revision": "6c8eea37e982f3dc6de2755e7a482eb16aaf1f16",
                "reviewed_at": "not-a-date",
            }
        ),
        lambda row: row.update(unknown="rejected"),
        lambda row: row.update(sha256="a" * 64),
    ],
)
def test_invalid_required_or_owned_fields_are_rejected(checker, tmp_path, mutate):
    row = document()
    mutate(row)
    write_corpus(tmp_path, [row])
    report = checker.check_corpus(tmp_path, release=False)
    assert not report.valid
    assert report.errors


def test_unknown_root_schema_is_rejected(checker, tmp_path):
    write_corpus(tmp_path, [document()])
    catalog = json.loads((tmp_path / "runbooks/catalog.json").read_text(encoding="utf-8"))
    catalog["schema_version"] = "RunbookCatalog/v2"
    (tmp_path / "runbooks/catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    report = checker.check_corpus(tmp_path, release=False)
    assert not report.valid
    assert any("schema_version" in error for error in report.errors)


def test_duplicate_json_key_is_rejected_before_overwrite(checker, tmp_path):
    (tmp_path / "runbooks").mkdir()
    (tmp_path / "runbooks/catalog.json").write_text(
        '{"schema_version":"RunbookCatalog/v1","schema_version":"RunbookCatalog/v1",'
        '"documents":[]}',
        encoding="utf-8",
    )
    report = checker.check_corpus(tmp_path, release=False)
    assert not report.valid
    assert any("duplicate JSON key" in error for error in report.errors)


def test_duplicate_document_tuple_is_rejected(checker, tmp_path):
    first = document()
    second = document(source_path="runbooks/fixture-guide-2.md")
    write_corpus(tmp_path, [first, second])
    report = checker.check_corpus(tmp_path, release=False)
    assert not report.valid
    assert any("duplicate document" in error for error in report.errors)

    invalid_copy = document(title="Wrong title")
    write_corpus(tmp_path / "invalid-copy", [first, invalid_copy])
    report = checker.check_corpus(tmp_path / "invalid-copy", release=False)
    assert not report.valid
    assert any("duplicate document" in error for error in report.errors)

    shared = document(document_key="fixture-guide-2")
    write_corpus(tmp_path / "shared", [document(), shared])
    report = checker.check_corpus(tmp_path / "shared", release=False)
    assert not report.valid
    assert any("duplicate source_path" in error for error in report.errors)

    alias = document(document_key="fixture-guide-3", source_path="runbooks//fixture-guide.md")
    write_corpus(tmp_path / "alias", [document(), alias])
    report = checker.check_corpus(tmp_path / "alias", release=False)
    assert not report.valid
    assert any("duplicate source_path" in error for error in report.errors)


def test_diagnosis_guide_cannot_declare_recovery_template(checker, tmp_path):
    row = document(
        applicability={
            "kind": "Deployment",
            "symptom_families": ["oom_restarts"],
            "recovery_templates": ["set_memory_profile"],
        }
    )
    write_corpus(tmp_path, [row])
    report = checker.check_corpus(tmp_path, release=False)
    assert not report.valid
    assert any("empty recovery_templates" in error for error in report.errors)


def test_recovery_guide_requires_template(checker, tmp_path):
    row = document(
        guide_kind="recovery_template",
        applicability={
            "kind": "Deployment",
            "symptom_families": ["oom_restarts"],
            "recovery_templates": [],
        },
    )
    write_corpus(tmp_path, [row])
    report = checker.check_corpus(tmp_path, release=False)
    assert not report.valid
    assert any("at least one template" in error for error in report.errors)


def test_missing_file_and_malformed_utf8_are_rejected(checker, tmp_path):
    row = document()
    write_corpus(tmp_path, [row])
    (tmp_path / row["source_path"]).unlink()
    report = checker.check_corpus(tmp_path, release=False)
    assert not report.valid
    assert any("does not exist" in error for error in report.errors)

    (tmp_path / row["source_path"]).write_bytes(b"# Fixture\n\xff\n")
    report = checker.check_corpus(tmp_path, release=False)
    assert not report.valid
    assert any("valid UTF-8" in error for error in report.errors)


def test_symlink_or_junction_traversal_is_rejected(checker, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text(f"# {TITLE}\n\nOutside.\n", encoding="utf-8")
    (tmp_path / "runbooks").mkdir()
    link = tmp_path / "runbooks/fixture-guide.md"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"platform cannot create symlink: {exc}")
    row = document()
    (tmp_path / "runbooks/catalog.json").write_text(
        json.dumps({"schema_version": "RunbookCatalog/v1", "documents": [row]}),
        encoding="utf-8",
    )
    report = checker.check_corpus(tmp_path, release=False)
    assert not report.valid
    assert any("symlink" in error or "reparse" in error for error in report.errors)


def test_reparse_point_is_rejected_without_os_symlink(checker, tmp_path, monkeypatch):
    write_corpus(tmp_path, [document()])
    monkeypatch.setattr(
        checker,
        "_is_reparse",
        lambda path: path.name == "fixture-guide.md",
    )
    report = checker.check_corpus(tmp_path, release=False)
    assert not report.valid
    assert any("symlink" in error or "reparse" in error for error in report.errors)


def test_file_and_corpus_bounds_are_enforced(checker, tmp_path):
    row = document()
    content = f"# {TITLE}\n" + ("x" * (checker.MAX_FILE_BYTES + 1))
    write_corpus(tmp_path, [row], {row["document_key"]: content})
    report = checker.check_corpus(tmp_path, release=False)
    assert not report.valid
    assert any("256 KiB" in error for error in report.errors)

    second = tmp_path / "second"
    write_corpus(second, [row])
    prefix = f"# {TITLE}\n".encode("utf-8")
    filler = b"x" * (checker.MAX_FILE_BYTES - len(prefix))
    (second / row["source_path"]).write_bytes(prefix + filler)
    assert checker.check_corpus(second, release=False).valid


def test_metadata_bounds_are_enforced(checker, tmp_path):
    cases = [
        ("document_key", "a" * checker.MAX_KEY, "a" * (checker.MAX_KEY + 1)),
        ("version", "v" * checker.MAX_VERSION, "v" * (checker.MAX_VERSION + 1)),
        ("title", "T" * checker.MAX_TITLE, "T" * (checker.MAX_TITLE + 1)),
    ]
    for field, accepted, rejected in cases:
        base = tmp_path / field.replace("_", "-")
        row = document(**{field: accepted})
        write_corpus(base / "ok", [row], {row["document_key"]: f"# {row['title']}\n"})
        assert checker.check_corpus(base / "ok", release=False).valid
        bad = document(**{field: rejected})
        write_corpus(base / "bad", [bad], {bad["document_key"]: f"# {bad['title']}\n"})
        assert not checker.check_corpus(base / "bad", release=False).valid

    for count, accepted in ((16, True), (17, False)):
        base = tmp_path / f"tags-{count}"
        row = document(tags=[f"t{i}" for i in range(count)])
        write_corpus(base, [row])
        assert checker.check_corpus(base, release=False).valid is accepted

    for length, accepted in ((64, True), (65, False)):
        base = tmp_path / f"tag-{length}"
        row = document(tags=["t" * length])
        write_corpus(base, [row])
        assert checker.check_corpus(base, release=False).valid is accepted

    long_path = "runbooks/" + ("s" * 100) + ".md"
    row = document(source_path=long_path)
    write_corpus(tmp_path / "path-ok", [row], {row["document_key"]: f"# {TITLE}\n"})
    assert checker.check_corpus(tmp_path / "path-ok", release=False).valid
    too_long = document(source_path="runbooks/" + ("s" * 245) + ".md")
    write_corpus(tmp_path / "path-bad", [document()])
    catalog = json.loads((tmp_path / "path-bad/runbooks/catalog.json").read_text())
    catalog["documents"] = [too_long]
    (tmp_path / "path-bad/runbooks/catalog.json").write_text(json.dumps(catalog))
    assert not checker.check_corpus(tmp_path / "path-bad", release=False).valid


def test_document_cap_and_section_warning(checker, tmp_path):
    rows = []
    for index in range(100):
        key = f"fixture-{index}"
        rows.append(
            document(
                document_key=key,
                title=f"Fixture {index}",
                source_path=f"runbooks/{key}.md",
                applicability={
                    "kind": "Deployment",
                    "symptom_families": [],
                    "recovery_templates": [],
                },
            )
        )
    write_corpus(tmp_path / "max", rows)
    assert checker.check_corpus(tmp_path / "max", release=False).valid

    over = tmp_path / "over"
    write_corpus(
        over,
        rows
        + [
            document(
                document_key="fixture-100",
                title="Fixture 100",
                source_path="runbooks/fixture-100.md",
            )
        ],
    )
    assert not checker.check_corpus(over, release=False).valid

    long_section = tmp_path / "section"
    write_corpus(
        long_section,
        [document()],
        {"fixture-guide": f"# {TITLE}\n" + ("x" * 5000)},
    )
    report = checker.check_corpus(long_section, release=False)
    assert report.valid
    assert any("section exceeds" in warning for warning in report.warnings)
    rows = []
    contents = {}
    for index in range(34):
        key = f"fixture-{index}"
        title = f"Fixture {index}"
        row = document(
            document_key=key,
            title=title,
            source_path=f"runbooks/{key}.md",
            applicability={
                "kind": "Deployment",
                "symptom_families": [],
                "recovery_templates": [],
            },
        )
        rows.append(row)
        prefix = f"# {title}\n".encode("utf-8")
        contents[key] = (prefix + b"x" * (checker.MAX_FILE_BYTES - len(prefix))).decode()
    write_corpus(tmp_path, rows, contents)
    report = checker.check_corpus(tmp_path, release=False)
    assert not report.valid
    assert any("8 MiB" in error for error in report.errors)


def test_titled_and_reference_links_are_checked(checker, tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/link.md").write_text("# Link target\n", encoding="utf-8")
    (tmp_path / "docs/a_(b).md").write_text("# Parenthesized target\n", encoding="utf-8")
    row = document()
    write_corpus(
        tmp_path,
        [row],
        {"fixture-guide": f'# {TITLE}\n\n[Doc](../docs/link.md "Title")\n'},
    )
    assert checker.check_corpus(tmp_path, release=False).valid

    write_corpus(
        tmp_path / "parenthesized",
        [row],
        {"fixture-guide": f"# {TITLE}\n\n[Doc](../docs/a_(b).md)\n"},
    )
    (tmp_path / "parenthesized/docs").mkdir()
    (tmp_path / "parenthesized/docs/a_(b).md").write_text("# Target\n", encoding="utf-8")
    assert checker.check_corpus(tmp_path / "parenthesized", release=False).valid

    write_corpus(
        tmp_path / "missing-reference",
        [row],
        {"fixture-guide": f"# {TITLE}\n\n[Missing]: ../docs/missing.md\n"},
    )
    report = checker.check_corpus(tmp_path / "missing-reference", release=False)
    assert not report.valid
    assert any("does not exist" in error for error in report.errors)


def test_version_conflict_and_new_version(checker, tmp_path):
    baseline = tmp_path / "baseline"
    write_corpus(baseline, [document()])
    original = (baseline / "runbooks/fixture-guide.md").read_bytes()

    candidate = tmp_path / "candidate"
    write_corpus(candidate, [document()], {"fixture-guide": f"# {TITLE}\n\nChanged.\n"})
    conflict = checker.check_corpus(candidate, release=False, baseline_root=baseline)
    assert not conflict.valid
    assert conflict.version_conflicts
    assert (baseline / "runbooks/fixture-guide.md").read_bytes() == original

    write_corpus(
        tmp_path / "v2",
        [document(version="2")],
        {"fixture-guide": f"# {TITLE}\n\nChanged.\n"},
    )
    updated = checker.check_corpus(tmp_path / "v2", release=False, baseline_root=baseline)
    assert updated.valid, updated.errors
    assert updated.documents[0]["version"] == "2"


def test_metadata_change_requires_new_version(checker, tmp_path):
    baseline = tmp_path / "baseline"
    write_corpus(baseline, [document()])
    changed = document(tags=["fixture", "changed"])
    write_corpus(tmp_path / "candidate", [changed])
    report = checker.check_corpus(tmp_path / "candidate", release=False, baseline_root=baseline)
    assert not report.valid
    assert report.version_conflicts


def test_release_coverage_rejects_missing_guide_and_wrong_mapping(checker, tmp_path):
    source = ROOT / "runbooks"
    copy = tmp_path / "runbooks"
    (tmp_path / "docs").mkdir()
    for name in (
        "kubernetes-evidence.md",
        "prometheus-metrics.md",
        "workload-baselines.md",
        "job-dispatch.md",
        "foundation.md",
    ):
        (tmp_path / "docs" / name).write_text("# Linked documentation\n", encoding="utf-8")
    shutil.copytree(source, copy)
    manifest = json.loads((copy / "catalog.json").read_text(encoding="utf-8"))
    manifest["documents"] = manifest["documents"][:-1]
    (copy / "catalog.json").write_text(json.dumps(manifest), encoding="utf-8")
    report = checker.check_corpus(tmp_path)
    assert not report.valid
    assert any("eight guides" in error for error in report.errors)

    shutil.rmtree(copy)
    shutil.copytree(source, copy)
    manifest = json.loads((copy / "catalog.json").read_text(encoding="utf-8"))
    manifest["documents"][0]["applicability"]["symptom_families"] = ["image_pull"]
    (copy / "catalog.json").write_text(json.dumps(manifest), encoding="utf-8")
    report = checker.check_corpus(tmp_path)
    assert not report.valid
    assert any("wrong symptom family" in error for error in report.errors), report.errors


def test_broken_local_link_and_title_mismatch_are_rejected(checker, tmp_path):
    row = document()
    write_corpus(
        tmp_path,
        [row],
        {"fixture-guide": f"# {TITLE}\n\n[Missing](../docs/missing.md)\n"},
    )
    report = checker.check_corpus(tmp_path, release=False)
    assert not report.valid
    assert any("does not exist" in error for error in report.errors)

    write_corpus(tmp_path / "title", [row], {"fixture-guide": "# Wrong title\n"})
    report = checker.check_corpus(tmp_path / "title", release=False)
    assert not report.valid
    assert any("H1" in error for error in report.errors)


def test_json_output_is_safe_and_single_document(checker, tmp_path, capsys):
    write_corpus(
        tmp_path,
        [document(source_path="runbooks/missing.md")],
    )
    (tmp_path / "runbooks/missing.md").unlink()
    exit_code = checker.main(["--root", str(tmp_path), "--no-release", "--json"])
    captured = capsys.readouterr()
    assert exit_code == 1
    payload = json.loads(captured.out)
    assert payload["valid"] is False
    assert str(tmp_path) not in captured.out
    assert "Fixture" not in captured.out
