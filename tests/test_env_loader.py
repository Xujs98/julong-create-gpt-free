# -*- coding: utf-8 -*-
import errno
from pathlib import Path
from unittest.mock import patch

from config import env_loader


def test_write_env_values_falls_back_for_bind_mounted_env(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("WEBUI_AUTH_CODE=old\nKEEP=value\n", encoding="utf-8")

    def raise_busy(_target):
        raise OSError(errno.EBUSY, "Device or resource busy")

    with patch.object(env_loader, "_ENV_PATH", env_path), patch.object(env_loader, "load_env") as load_env, patch.object(
        Path, "replace", side_effect=raise_busy
    ):
        assert env_loader.write_env_values({"WEBUI_AUTH_CODE": "new", "ADDED": "value"}) == [
            "WEBUI_AUTH_CODE",
            "ADDED",
        ]

    assert env_path.read_text(encoding="utf-8") == 'WEBUI_AUTH_CODE="new"\nKEEP=value\n\n# ---- updated by WebUI / config.env_loader ----\nADDED="value"\n'
    assert not env_path.with_suffix(".env.tmp").exists()
    load_env.assert_called_once_with(override=True)


def test_write_env_values_keeps_atomic_errors(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("KEY=old\n", encoding="utf-8")
    with patch.object(env_loader, "_ENV_PATH", env_path), patch.object(
        Path, "replace", side_effect=OSError(errno.EACCES, "permission denied")
    ):
        try:
            env_loader.write_env_values({"KEY": "new"})
        except OSError as exc:
            assert exc.errno == errno.EACCES
        else:
            raise AssertionError("non-EBUSY replace errors must propagate")
