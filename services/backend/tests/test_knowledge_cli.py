from __future__ import annotations

import json

import pytest

from cloudops.entrypoints.cli import main
from knowledge_fixtures import write_corpus


def test_knowledge_cli_index_and_status_json(db, tmp_path, monkeypatch, capsys):
    write_corpus(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        ["cloudops", "knowledge-index", "--root", str(tmp_path), "--json"],
    )
    main()
    output = capsys.readouterr().out.strip().splitlines()
    assert len(output) == 1
    receipt = json.loads(output[0])
    assert receipt["outcome"] == "indexed"
    assert receipt["document_count"] == 1
    assert receipt["generation_id"]

    monkeypatch.setattr("sys.argv", ["cloudops", "knowledge-status", "--json"])
    main()
    status = json.loads(capsys.readouterr().out.strip())
    assert status["state"] == "ready"
    assert status["active_generation"]["document_count"] == 1


def test_knowledge_cli_failure_is_safe_and_nonzero(db, tmp_path, monkeypatch, capsys):
    (tmp_path / "runbooks").mkdir()
    (tmp_path / "runbooks/catalog.json").write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["cloudops", "knowledge-index", "--root", str(tmp_path), "--json"],
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert "traceback" not in captured.err.lower()
    assert json.loads(captured.out)["outcome"] == "failed"
