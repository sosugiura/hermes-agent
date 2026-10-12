"""Config-directory Keychain lookup regression tests (no real secrets)."""
import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest

from agent.anthropic_adapter import _read_claude_code_credentials_from_keychain

pytestmark = pytest.mark.allow_macos_keychain


def payload(token):
    return MagicMock(returncode=0, stdout=json.dumps({"claudeAiOauth": {
        "accessToken": token, "refreshToken": "test-refresh", "expiresAt": 9999999999999,
    }}))


@pytest.mark.macos_only
def test_explicit_config_directory_uses_hashed_service(monkeypatch):
    directory = "/example/.claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", directory)
    with patch("agent.anthropic_adapter.subprocess.run", return_value=payload("test-configured")) as run:
        creds = _read_claude_code_credentials_from_keychain()
    assert creds["accessToken"] == "test-configured"
    suffix = hashlib.sha256(directory.encode()).hexdigest()[:8]
    assert run.call_args.args[0][3] == f"Claude Code-credentials-{suffix}"
    assert run.call_count == 1


@pytest.mark.macos_only
def test_missing_scoped_entry_falls_back_to_legacy(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/example/.claude")
    with patch("agent.anthropic_adapter.subprocess.run", side_effect=[MagicMock(returncode=1), payload("test-legacy")]) as run:
        creds = _read_claude_code_credentials_from_keychain()
    assert creds["accessToken"] == "test-legacy"
    assert run.call_args.args[0][3] == "Claude Code-credentials"


@pytest.mark.macos_only
def test_unset_config_directory_uses_legacy(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    with patch("agent.anthropic_adapter.subprocess.run", return_value=payload("test-legacy")) as run:
        assert _read_claude_code_credentials_from_keychain()["accessToken"] == "test-legacy"
    assert run.call_count == 1
    assert run.call_args.args[0][3] == "Claude Code-credentials"
