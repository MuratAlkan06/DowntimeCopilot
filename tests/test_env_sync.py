"""tools/env_sync.py (`make infra-env`): Terraform outputs -> .env, no terraform or AWS needed (runner injected)."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("env_sync", ROOT / "tools" / "env_sync.py")
env_sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(env_sync)

OUTPUTS = {
    "sync_table": "downtime-copilot-sync-0a1b2c3d",
    "audio_bucket": "downtime-copilot-audio-0a1b2c3d",
    "guardrail_id": "gr0example0id",
    "guardrail_version": "1",
    "region": "us-east-1",
    "runtime_role_arn": "arn:aws:iam::ACCOUNT:role/downtime-copilot-runtime",
}
OLD_ENV = """# AWS credentials template
export AWS_DEFAULT_REGION=us-west-2
export AWS_ACCESS_KEY_ID=OLDKEYID
export AWS_SECRET_ACCESS_KEY=OLDSECRET
export AWS_SESSION_TOKEN=OLDTOKEN
# export GUARDRAIL_ID=commented-out-stays
export GUARDRAIL_ID=old-guardrail
export DRY_RUN=0
export VITAL_RANGE_SPO2="90,100"
AUDIO_BUCKET=downtime-copilot-audio-0a1b2c3d
export GUARDRAIL_ID=duplicate-guardrail
"""


def fake_terraform(outputs: dict, calls: list | None = None, returncode: int = 0):
    def run(cmd, cwd=None, env=None, **kw):
        if calls is not None:
            calls.append({"cmd": cmd, "cwd": cwd, "env": env})
        body = json.dumps({k: {"sensitive": False, "type": "string", "value": v} for k, v in outputs.items()})
        return subprocess.CompletedProcess(cmd, returncode, stdout=body if returncode == 0 else "",
                                           stderr="" if returncode == 0 else "Error: backend not initialized")
    return run


def test_upsert_rewrites_ours_drops_static_keys_and_keeps_everything_else():
    values = env_sync.wanted(OUTPUTS, "downtime-runtime")
    text, changes = env_sync.upsert(OLD_ENV, values)
    lines = text.splitlines()
    assert "export AWS_DEFAULT_REGION=us-east-1" in lines
    assert "export GUARDRAIL_ID=gr0example0id" in lines
    assert "export AUDIO_BUCKET=downtime-copilot-audio-0a1b2c3d" in lines  # bare assignment normalized in place
    for kept in ("# AWS credentials template", "# export GUARDRAIL_ID=commented-out-stays", "export DRY_RUN=0",
                 'export VITAL_RANGE_SPO2="90,100"'):
        assert kept in lines
    assert not any(k in text for k in ("OLDKEYID", "OLDSECRET", "OLDTOKEN", "old-guardrail", "duplicate-guardrail"))
    assert lines.index("export GUARDRAIL_ID=gr0example0id") < lines.index("export DRY_RUN=0")  # first slot kept
    tail = lines[lines.index(env_sync.HEADER):]
    assert tail == [env_sync.HEADER, "export AWS_PROFILE=downtime-runtime", "export GUARDRAIL_VERSION=1",
                    "export SYNC_TABLE=downtime-copilot-sync-0a1b2c3d"]
    assert sorted(changes) == sorted([
        ("AWS_DEFAULT_REGION", "updated"), ("AWS_ACCESS_KEY_ID", "removed"), ("AWS_SECRET_ACCESS_KEY", "removed"),
        ("AWS_SESSION_TOKEN", "removed"), ("GUARDRAIL_ID", "updated"), ("AUDIO_BUCKET", "updated"),
        ("GUARDRAIL_ID", "removed duplicate"), ("AWS_PROFILE", "added"), ("GUARDRAIL_VERSION", "added"),
        ("SYNC_TABLE", "added")])
    again, changes2 = env_sync.upsert(text, values)
    assert again == text and {a for _, a in changes2} == {"unchanged"}


def test_main_writes_env_and_prints_names_never_values(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "SHELLKEYID")
    monkeypatch.setenv("AWS_PROFILE", "someone-else")
    env_file = tmp_path / ".env"
    env_file.write_text(OLD_ENV)
    calls = []
    rc = env_sync.main(["--env-file", str(env_file), "--infra-dir", str(tmp_path)], run=fake_terraform(OUTPUTS, calls))
    assert rc == 0
    (call,) = calls
    assert call["cmd"][1:] == ["output", "-json"] and call["cwd"] == tmp_path
    assert call["env"]["AWS_PROFILE"] == "downtime-admin" and "AWS_ACCESS_KEY_ID" not in call["env"]
    out = capsys.readouterr().out
    assert ".env: SYNC_TABLE added" in out and ".env: AWS_SESSION_TOKEN removed" in out
    for value in [*OUTPUTS.values(), "OLDKEYID", "OLDSECRET", "OLDTOKEN", "old-guardrail"]:
        if value != "1":
            assert value not in out
    assert "export AWS_PROFILE=downtime-runtime" in env_file.read_text().splitlines()


def test_new_env_file_is_private(tmp_path):
    env_file = tmp_path / ".env"
    assert env_sync.main(["--env-file", str(env_file)], run=fake_terraform(OUTPUTS)) == 0
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert env_file.read_text().startswith(env_sync.HEADER + "\n")


@pytest.mark.parametrize("outputs, returncode", [({}, 0), ({**OUTPUTS, "guardrail_version": ""}, 0), (OUTPUTS, 1)])
def test_missing_outputs_or_terraform_failure_leave_env_untouched(tmp_path, capsys, outputs, returncode):
    env_file = tmp_path / ".env"
    env_file.write_text(OLD_ENV)
    assert env_sync.main(["--env-file", str(env_file)], run=fake_terraform(outputs, returncode=returncode)) == 1
    assert env_file.read_text() == OLD_ENV
    assert "env_sync:" in capsys.readouterr().err


def test_wanted_rejects_values_with_shell_metacharacters():
    outputs = dict(OUTPUTS)
    first = next(iter(env_sync.OUTPUTS.values()))
    outputs[first] = "safe\nexport PWNED=1"
    with pytest.raises(env_sync.SyncError) as exc:
        env_sync.wanted(outputs, "downtime-runtime")
    assert first in str(exc.value)
    assert "PWNED" not in str(exc.value)
