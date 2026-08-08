# -*- coding: utf-8 -*-
from pathlib import Path
from unittest.mock import patch

from core import sentinel_runner


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
