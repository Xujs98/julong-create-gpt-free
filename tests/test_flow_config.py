from config.env_loader import SECRET_ENV_KEYS
from webui.config_editor import EDITABLE_FIELDS


def test_flow_and_registration_password_are_editable_from_webui():
    fields = {item["key"]: item for item in EDITABLE_FIELDS}

    assert fields["REGISTER_PASSWORD"]["secret"] is True
    assert fields["FLOW_TRIGGER_URL"]["type"] == "str"
    assert fields["FLOW_TRIGGER_BEARER"]["secret"] is True
    assert fields["FLOW_TRIGGER_COOKIE"]["secret"] is True
    assert fields["FLOW_TRIGGER_PAYLOAD"]["type"] == "str"
    assert fields["FLOW_TRIGGER_TIMEOUT"]["type"] == "int"


def test_flow_credentials_and_registration_password_are_secret_env_keys():
    assert "REGISTER_PASSWORD" in SECRET_ENV_KEYS
    assert "FLOW_TRIGGER_BEARER" in SECRET_ENV_KEYS
    assert "FLOW_TRIGGER_COOKIE" in SECRET_ENV_KEYS
