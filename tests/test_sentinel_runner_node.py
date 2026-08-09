# -*- coding: utf-8 -*-
from pathlib import Path
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import sentinel_runner
from core import openai_auth


def test_node_resolver_uses_nvm_when_background_path_has_no_node(tmp_path):
    """桌面后台 PATH 缺少 node 时，自动使用用户 nvm 安装的最高版本。"""
    node = tmp_path / ".nvm" / "versions" / "node" / "v24.13.0" / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_text("#!/bin/sh\n", encoding="utf-8")
    node.chmod(0o755)

    with patch.dict("os.environ", {}, clear=True), patch(
        "core.sentinel_runner.shutil.which", return_value=None
    ), patch.object(Path, "home", return_value=tmp_path):
        resolved = sentinel_runner._resolve_node_executable()

    assert resolved == str(node)


def test_runner_reuses_requirement_proof_for_encrypted_dx():
    challenge = {
        "token": "challenge-token",
        "turnstile": {"required": True, "dx": "encrypted"},
        "_requirements_proof": "exact-request-proof",
    }
    completed = SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"p": "pow", "t": "turnstile", "c": "challenge-token", "id": "did", "flow": "flow"}),
        stderr="",
    )

    captured = {}

    def fake_run(cmd, **kwargs):
        challenge_file = cmd[cmd.index("--challenge-file") + 1]
        captured["challenge"] = json.loads(Path(challenge_file).read_text(encoding="utf-8"))
        return completed

    with patch.object(sentinel_runner, "_ensure_runner_environment"), patch.object(
        sentinel_runner, "_resolve_node_executable", return_value="node"
    ), patch.object(sentinel_runner.subprocess, "run", side_effect=fake_run) as run:
        sentinel_runner.generate_sentinel_token(challenge, "flow", "did")

    cmd = run.call_args.args[0]
    env = run.call_args.kwargs["env"]
    assert env["SENTINEL_CHALLENGE_PROOF"] == "exact-request-proof"
    assert "_requirements_proof" not in captured["challenge"]


def test_runner_omits_empty_requirement_proof_argument():
    completed = SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"p": "pow", "c": "challenge-token", "id": "did", "flow": "flow"}),
        stderr="",
    )
    with patch.object(sentinel_runner, "_ensure_runner_environment"), patch.object(
        sentinel_runner, "_resolve_node_executable", return_value="node"
    ), patch.object(sentinel_runner.subprocess, "run", return_value=completed) as run:
        sentinel_runner.generate_sentinel_token({"token": "challenge-token"}, "flow", "did")
    assert "SENTINEL_CHALLENGE_PROOF" not in run.call_args.kwargs["env"]


def test_request_sentinel_token_keeps_exact_request_proof_for_runner():
    response = Mock()
    response.json.return_value = {"token": "challenge-token", "so": {"required": True}}
    response.raise_for_status.return_value = None
    session = Mock(device_id="did", sentinel_sid="sid", browser_profile={})
    session.get_sentinel_headers.return_value = {"content-type": "text/plain"}
    session.post.return_value = response

    with patch.object(openai_auth, "generate_requirements_token", return_value="exact-proof") as make_proof, patch.object(
        openai_auth, "build_sentinel_request_body", return_value="body"
    ) as make_body:
        result = openai_auth.request_sentinel_token(session, "oauth_create_account")

    make_proof.assert_called_once()
    make_body.assert_called_once_with("exact-proof", "did", "oauth_create_account")
    assert result["_requirements_proof"] == "exact-proof"


def test_build_sentinel_header_splits_required_so_from_main_token():
    session = Mock(device_id="did", sentinel_sid="sid", browser_profile={})
    session.auth_cookie_header.return_value = "oai-did=did"
    challenge = {
        "token": "challenge-token",
        "turnstile": {"required": True},
        "so": {"required": True},
    }
    generated = json.dumps({
        "p": "pow",
        "t": "turnstile",
        "c": "challenge-token",
        "id": "did",
        "flow": "oauth_create_account",
        "so": "observer",
    })

    with patch.object(openai_auth, "generate_sentinel_token", return_value=generated):
        sentinel_header, so_header = openai_auth.build_sentinel_header(
            session, challenge, "oauth_create_account"
        )

    assert "so" not in json.loads(sentinel_header)
    assert json.loads(so_header) == {
        "so": "observer",
        "c": "challenge-token",
        "id": "did",
        "flow": "oauth_create_account",
    }


def test_build_sentinel_header_fails_before_request_when_required_so_is_missing():
    session = Mock(device_id="did", sentinel_sid="sid", browser_profile={})
    session.auth_cookie_header.return_value = "oai-did=did"
    challenge = {"token": "challenge-token", "so": {"required": True}}
    generated = json.dumps({"p": "pow", "c": "challenge-token", "id": "did", "flow": "flow"})

    with patch.object(openai_auth, "generate_sentinel_token", return_value=generated):
        try:
            openai_auth.build_sentinel_header(session, challenge, "flow")
        except RuntimeError as exc:
            assert "要求 SO" in str(exc)
        else:
            raise AssertionError("missing required SO must stop before create_account")
