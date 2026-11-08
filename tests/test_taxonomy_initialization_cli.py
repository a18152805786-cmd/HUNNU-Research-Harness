"""The public command must fail without touching an existing frozen taxonomy."""

import contextlib
import hashlib
import io
import json
from unittest.mock import patch

from hunnu_harness import paths
from hunnu_harness.cli import main
from hunnu_harness.literature import topics


def test_cli_initializes_only_the_reviewed_missing_configuration(tmp_path):
    source = tmp_path / "reviewed.json"
    raw = b'{"domains":{"Reviewed domain":["Reviewed topic"]}}'
    source.write_bytes(raw)
    destination = tmp_path / "output" / "topic_taxonomy.json"
    output = io.StringIO()
    with patch.object(paths, "OUTPUT_ROOT", tmp_path), patch.object(topics, "TOPIC_TAXONOMY_JSON", destination), contextlib.redirect_stdout(output):
        code = main(["library-init-taxonomy", "--source", str(source), "--sha256", hashlib.sha256(raw).hexdigest(), "--json"])
    assert code == 0
    assert json.loads(output.getvalue())["Status"] == "INITIALIZED"
    assert destination.read_bytes() == raw
    assert source.read_bytes() == raw


def test_cli_returns_one_json_refusal_and_keeps_existing_bytes(tmp_path):
    source = tmp_path / "reviewed.json"
    raw = b'{"domains":{"Reviewed domain":["Reviewed topic"]}}'
    source.write_bytes(raw)
    destination = tmp_path / "topic_taxonomy.json"
    destination.write_bytes(b"existing frozen configuration")
    output = io.StringIO()
    with patch.object(paths, "OUTPUT_ROOT", tmp_path), patch.object(topics, "TOPIC_TAXONOMY_JSON", destination), contextlib.redirect_stdout(output):
        code = main(["library-init-taxonomy", "--source", str(source), "--sha256", hashlib.sha256(raw).hexdigest(), "--json"])
    assert code == 1
    payload = json.loads(output.getvalue())
    assert payload["Status"] == "REFUSED"
    assert "already exists and will not be replaced" in payload["Reason"]
    assert destination.read_bytes() == b"existing frozen configuration"
