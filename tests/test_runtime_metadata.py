from webui import config_editor


def test_config_exposes_runtime_environment_and_version(monkeypatch):
    monkeypatch.delenv("CONTAINER", raising=False)
    fields = {item["key"]: item for item in config_editor.get_config()}

    assert fields["APP_VERSION"]["value"] == "2026.09.06.2"
    assert fields["APP_VERSION"]["readonly"] is True
    assert fields["RUNTIME_ENVIRONMENT"]["value"] in {"本地环境", "Docker 环境"}
    assert fields["RUNTIME_ENVIRONMENT"]["readonly"] is True


def test_config_detects_docker_environment(monkeypatch):
    monkeypatch.setenv("CONTAINER", "docker")
    fields = {item["key"]: item for item in config_editor.get_config()}

    assert fields["RUNTIME_ENVIRONMENT"]["value"] == "Docker 环境"
