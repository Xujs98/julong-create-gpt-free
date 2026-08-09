# -*- coding: utf-8 -*-
import json
from pathlib import Path

from core.fingerprint_profile import (
    fingerprint_key_hash,
    get_or_create_browser_profile,
    load_browser_profile,
    profile_path,
    save_browser_profile,
)


def test_account_profile_is_stable_and_distinct(tmp_path: Path):
    first = get_or_create_browser_profile("User@Example.com", profile_dir=tmp_path)
    second = get_or_create_browser_profile(" user@example.com ", profile_dir=tmp_path)
    other = get_or_create_browser_profile("other@example.com", profile_dir=tmp_path)

    assert first == second
    assert first["fingerprint_id"] == fingerprint_key_hash("user@example.com")
    assert first["fingerprint_id"] != other["fingerprint_id"]
    assert first["canvas_seed"] != other["canvas_seed"]
    assert first["audio_seed"] != other["audio_seed"]
    assert profile_path("user@example.com", tmp_path).exists()


def test_profile_file_is_atomic_and_excludes_runtime_secrets(tmp_path: Path):
    profile = save_browser_profile(
        "user@example.com",
        {
            "screen_width": 1440,
            "react_container_key": "runtime-only",
            "registration_password": "PASSWORD",
            "access_token": "TOKEN",
            "canvas_seed": "abc",
        },
        profile_dir=tmp_path,
    )
    assert "react_container_key" not in profile
    assert "registration_password" not in profile
    assert "access_token" not in profile
    loaded = load_browser_profile("user@example.com", profile_dir=tmp_path)
    assert loaded == profile

    raw = json.loads(profile_path("user@example.com", tmp_path).read_text(encoding="utf-8"))
    serialized = json.dumps(raw, ensure_ascii=False).casefold()
    assert "password" not in serialized
    assert "token" not in serialized
    assert not list(tmp_path.glob("*.tmp"))
