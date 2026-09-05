# -*- coding: utf-8 -*-
"""Production WSGI entry point used by the Docker image."""
import logging

from webui.app import create_app
from webui.auth import expected_auth_code, is_generated_code


app = create_app()

if is_generated_code():
    logging.getLogger(__name__).warning(
        "WEBUI_AUTH_CODE/AUTH_CODE is not configured; temporary auth code: %s",
        expected_auth_code(),
    )
