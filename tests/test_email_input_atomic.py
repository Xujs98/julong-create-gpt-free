from unittest.mock import Mock, patch

from core import roxy_registration


def test_type_email_address_uses_atomic_fill_and_verifies_value():
    driver = Mock()
    element = Mock()
    with patch.object(roxy_registration, "_find_visible_email_input_js", return_value=element), \
         patch.object(roxy_registration, "_current_email_input_value", return_value="user@example.test"):
        roxy_registration._type_email_address(driver, "user@example.test", timeout=1)
    element.fill.assert_called_once_with("user@example.test")
