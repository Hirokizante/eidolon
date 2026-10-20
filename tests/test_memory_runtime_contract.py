"""Protect isolation and redaction of the opt-in installed-runtime source probe."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "memory_runtime_contract", Path(__file__).parents[1] / "scripts/memory_runtime_contract.py")
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)


def test_runtime_probe_drops_credentials_and_preserves_virtualenv_interpreter(tmp_path, monkeypatch):
    real_python = tmp_path / "base-python"
    real_python.write_text("synthetic interpreter placeholder")
    python = tmp_path / "runtime/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(real_python)
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-credential")
    monkeypatch.setenv("HINDSIGHT_API_DATABASE_URL", "postgresql://private:secret@live/db")
    seen = []

    def execute(command, **kwargs):
        seen.append(command)
        # Resolving the interpreter symlink would lose the virtualenv's packages.
        assert command[0] == str(python)
        assert command[1:3] == ["-I", "-c"]
        assert kwargs["env"] == {
            "PATH": "/usr/bin:/bin",
            "HINDSIGHT_API_DATABASE_URL": "postgresql://synthetic:synthetic@127.0.0.1:1/synthetic",
        }
        assert Path(kwargs["cwd"]).is_dir()
        assert not (Path(kwargs["cwd"]) / ".env").exists()
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({
            "status": "passed", "role": "old"}), stderr="")

    monkeypatch.setattr(contract.subprocess, "run", execute)
    assert contract.run_runtime(python, "old", 10) == {"status": "passed", "role": "old"}
    assert len(seen) == 1


@pytest.mark.parametrize("failure", ["invalid_output", "timeout", "inconsistent_status"])
def test_runtime_probe_never_echoes_subprocess_errors_or_connection_details(
        tmp_path, monkeypatch, failure):
    python = tmp_path / "python"
    python.write_text("synthetic interpreter placeholder")
    private = "postgresql://private:secret@live/db"

    def execute(command, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 10, output=private, stderr=private)
        output = private if failure == "invalid_output" else json.dumps({
            "status": "passed", "role": "old"})
        return subprocess.CompletedProcess(command, 1, stdout=output, stderr=private)

    monkeypatch.setattr(contract.subprocess, "run", execute)
    result = contract.run_runtime(python, "old", 10)
    assert result["status"] == "failed"
    assert "secret" not in json.dumps(result)
    assert "postgresql" not in json.dumps(result)
