from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from cloudops.features.knowledge.corpus import CorpusError, capture_corpus, metadata_digest
from knowledge_fixtures import document, write_corpus


@pytest.mark.parametrize("case", ["source-path", "unknown-field", "duplicate-key"])
def test_rejection_does_not_echo_private_input_values(tmp_path, case):
    marker = "/private/cloudops-sensitive-canary"
    write_corpus(tmp_path)
    path = tmp_path / "runbooks/catalog.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if case == "source-path":
        manifest["documents"][0]["source_path"] = marker + ".md"
    elif case == "unknown-field":
        manifest[marker] = "untrusted content"
    if case == "duplicate-key":
        path.write_text('{"' + marker + '":1,"' + marker + '":2}', encoding="utf-8")
    else:
        path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(CorpusError) as error:
        capture_corpus(tmp_path)
    assert marker not in str(error.value)


def test_valid_corpus_captures_exact_bytes_and_deterministic_digest(tmp_path):
    write_corpus(
        tmp_path,
        [document()],
        {"fixture-guide": "# Fixture guide\n\nExact body.\n"},
    )
    captured = capture_corpus(tmp_path)
    assert captured.documents[0].content == b"# Fixture guide\n\nExact body.\n"
    assert (
        captured.documents[0].content_sha256
        == hashlib.sha256(captured.documents[0].content).hexdigest()
    )
    repeated = capture_corpus(tmp_path)
    assert repeated.source_digest == captured.source_digest
    assert repeated.documents[0].metadata_sha256 == captured.documents[0].metadata_sha256


def test_moved_filename_and_empty_family_are_valid(tmp_path):
    write_corpus(
        tmp_path,
        [
            document(
                source_path="runbooks/renamed-guide.md",
                applicability={
                    "kind": "Deployment",
                    "symptom_families": [],
                    "recovery_templates": [],
                },
            )
        ],
    )
    captured = capture_corpus(tmp_path)
    assert captured.documents[0].source_path == "runbooks/renamed-guide.md"
    assert captured.documents[0].applicability["symptom_families"] == []


def test_metadata_digest_changes_with_metadata_not_content(tmp_path):
    write_corpus(tmp_path)
    first = capture_corpus(tmp_path).documents[0]
    write_corpus(
        tmp_path,
        [document(title="Different title")],
        {"fixture-guide": "# Fixture guide\n\nFixture body.\n"},
    )
    second = capture_corpus(tmp_path).documents[0]
    assert first.content_sha256 == second.content_sha256
    assert first.metadata_sha256 != second.metadata_sha256
    assert metadata_digest(first) == first.metadata_sha256


@pytest.mark.parametrize(
    "mutate",
    [
        lambda row: row.update(unknown="field"),
        lambda row: row.update(guide_kind="not-a-kind"),
        lambda row: row.update(version=1),
        lambda row: row["applicability"].update(kind="Service"),
        lambda row: row["applicability"].update(symptom_families=["unknown"]),
        lambda row: row["applicability"].update(recovery_templates=["unknown"]),
        lambda row: row.update(tags=["tag", "tag"]),
        lambda row: row["provenance"].update(source_revision="not-a-sha"),
        lambda row: row["provenance"].update(reviewed_at="not-a-date"),
        lambda row: row.update(source_path="/runbooks/fixture-guide.md"),
        lambda row: row.update(source_path="runbooks/../fixture-guide.md"),
        lambda row: row.update(source_path="runbooks\\fixture-guide.md"),
        lambda row: row.update(source_path="other/fixture-guide.md"),
    ],
)
def test_invalid_documents_are_rejected(tmp_path, mutate):
    row = document()
    mutate(row)
    write_corpus(tmp_path, [row])
    with pytest.raises(CorpusError):
        capture_corpus(tmp_path)


def test_duplicate_key_version_and_source_path_are_rejected(tmp_path):
    write_corpus(tmp_path, [document(), document()])
    with pytest.raises(CorpusError, match="duplicate document key/version"):
        capture_corpus(tmp_path)

    write_corpus(
        tmp_path / "same-file",
        [
            document(document_key="one"),
            document(document_key="two", source_path="runbooks/fixture-guide.md"),
        ],
    )
    with pytest.raises(CorpusError, match="duplicate source_path"):
        capture_corpus(tmp_path / "same-file")


def test_duplicate_json_keys_are_rejected(tmp_path):
    (tmp_path / "runbooks").mkdir()
    (tmp_path / "runbooks/catalog.json").write_text(
        '{"schema_version":"RunbookCatalog/v1","schema_version":"RunbookCatalog/v1","documents":[]}',
        encoding="utf-8",
    )
    with pytest.raises(CorpusError, match="duplicate JSON key"):
        capture_corpus(tmp_path)


def test_invalid_utf8_and_oversize_file_are_rejected(tmp_path):
    write_corpus(tmp_path)
    (tmp_path / "runbooks/fixture-guide.md").write_bytes(b"\xff\xfe")
    with pytest.raises(CorpusError, match="UTF-8"):
        capture_corpus(tmp_path)

    (tmp_path / "runbooks/fixture-guide.md").write_bytes(b"x" * (256 * 1024 + 1))
    with pytest.raises(CorpusError, match="256 KiB"):
        capture_corpus(tmp_path)


def test_empty_corpus_is_valid(tmp_path):
    write_corpus(tmp_path, [])
    captured = capture_corpus(tmp_path)
    assert captured.documents == ()
    assert captured.source_digest


def test_catalog_must_be_strict_json_object(tmp_path):
    (tmp_path / "runbooks").mkdir()
    (tmp_path / "runbooks/catalog.json").write_text("[]", encoding="utf-8")
    with pytest.raises(CorpusError, match="catalog must be an object"):
        capture_corpus(tmp_path)


def test_source_must_be_regular_file(tmp_path):
    write_corpus(tmp_path)
    (tmp_path / "runbooks/fixture-guide.md").unlink()
    (tmp_path / "runbooks/fixture-guide.md").mkdir()
    with pytest.raises(CorpusError, match="not regular"):
        capture_corpus(tmp_path)


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="symlink unavailable")
def test_symlink_source_is_rejected_when_supported(tmp_path):
    write_corpus(tmp_path)
    original = tmp_path / "runbooks/fixture-guide.md"
    target = tmp_path / "runbooks/target.md"
    target.write_bytes(original.read_bytes())
    original.unlink()
    try:
        original.symlink_to(target)
    except OSError:
        pytest.skip("host cannot create symlinks")
    with pytest.raises(CorpusError, match="symlink or reparse"):
        capture_corpus(tmp_path)


def test_source_digest_changes_with_exact_content_only(tmp_path):
    write_corpus(tmp_path, [document()], {"fixture-guide": "# Fixture guide\n\nA\n"})
    first = capture_corpus(tmp_path)
    write_corpus(tmp_path, [document()], {"fixture-guide": "# Fixture guide\n\nA\r\n"})
    second = capture_corpus(tmp_path)
    assert first.source_digest != second.source_digest
    assert first.documents[0].content_sha256 != second.documents[0].content_sha256


def test_recovery_kind_requires_template(tmp_path):
    row = document(
        guide_kind="recovery_template",
        applicability={
            "kind": "Deployment",
            "symptom_families": ["oom_restarts"],
            "recovery_templates": [],
        },
    )
    write_corpus(tmp_path, [row])
    with pytest.raises(CorpusError, match="recovery guide must declare"):
        capture_corpus(tmp_path)


def test_manifest_unknown_field_is_rejected(tmp_path):
    write_corpus(tmp_path)
    path = tmp_path / "runbooks/catalog.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["unknown"] = True
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(CorpusError, match="unknown fields"):
        capture_corpus(tmp_path)


def test_tag_bounds_allow_64_characters_but_not_65(tmp_path):
    write_corpus(tmp_path, [document(tags=[])])
    assert capture_corpus(tmp_path).documents[0].tags == ()
    write_corpus(tmp_path, [document(tags=["a" * 64])])
    assert capture_corpus(tmp_path).documents[0].tags == ("a" * 64,)
    write_corpus(tmp_path, [document(tags=["a" * 65])])
    with pytest.raises(CorpusError, match="entry over 64"):
        capture_corpus(tmp_path)


def test_metadata_array_order_is_part_of_the_digest(tmp_path):
    write_corpus(tmp_path, [document(tags=["a", "b"])])
    first = capture_corpus(tmp_path).documents[0]
    write_corpus(
        tmp_path,
        [document(tags=["b", "a"])],
        {"fixture-guide": "# Fixture guide\n\nFixture body.\n"},
    )
    second = capture_corpus(tmp_path).documents[0]
    assert first.content_sha256 == second.content_sha256
    assert first.metadata_sha256 != second.metadata_sha256
