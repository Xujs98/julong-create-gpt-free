import unittest
from pathlib import Path

from core.codex_agent_service import _agent_registry_unsupported


class StatusClassificationTests(unittest.TestCase):
    def test_agent_registry_capability_error_is_unsupported(self):
        exc = RuntimeError('403 {"code":"agent_registry_not_enabled"}')
        self.assertTrue(_agent_registry_unsupported(exc))

    def test_other_agent_error_remains_regular_failure(self):
        self.assertFalse(_agent_registry_unsupported(RuntimeError("HTTP 500")))

    def test_ui_distinguishes_403_live_check_and_agent_capability(self):
        html = Path("webui/templates/index.html").read_text(encoding="utf-8")
        self.assertIn("查活: ${blocked ? '访问受阻' : '失败'}", html)
        self.assertIn("s === 'unsupported'", html)


if __name__ == "__main__":
    unittest.main()
