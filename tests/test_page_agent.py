import unittest
from unittest.mock import patch

from core.page_agent import PageAgent


class PageAgentTests(unittest.TestCase):
    def _agent(self, provider: str = "openai_compatible") -> PageAgent:
        # 绕过运行时配置门禁，仅验证动作解析与本地回退逻辑。
        agent = PageAgent.__new__(PageAgent)
        agent.config = {"provider": provider, "max_steps": 4}
        agent.mode = "takeover"
        return agent

    def test_parse_markdown_json_actions(self):
        parsed = PageAgent._parse_model_json(
            "说明如下：\n```json\n{\"actions\":[{\"type\":\"click\",\"selector\":\"#next\"}]}\n```"
        )
        self.assertEqual(parsed["actions"][0]["selector"], "#next")

    def test_model_error_falls_back_to_local_dom_actions(self):
        agent = self._agent()
        snapshot = {
            "inputs": [{"selector": "#email", "tag": "input", "name": "email", "disabled": False}],
            "buttons": [
                {"selector": "#google", "text": "Continue with Google", "disabled": False},
                {"selector": "#next", "text": "Continue", "disabled": False},
            ],
        }
        with patch.object(agent, "snapshot", return_value=snapshot), patch.object(
            agent, "_model_actions", side_effect=ValueError("bad json")
        ), patch.object(agent, "_execute", return_value=True):
            result = agent.assist(object(), "email", {"email": "user@example.test"}, force=True)

        self.assertEqual(result.reason, "model_fallback_local:ValueError")
        self.assertEqual([action.get("selector") for action in result.actions], ["#email", "#next"])
        self.assertEqual(result.executed, 2)

    def test_takeover_executes_only_one_action_per_html_snapshot(self):
        agent = self._agent(provider="local")
        snapshot = {
            "inputs": [
                {"selector": "#email", "tag": "input", "name": "email", "disabled": False, "valuePresent": False}
            ],
            "buttons": [{"selector": "#next", "text": "Continue", "disabled": False}],
        }
        with patch.object(agent, "snapshot", return_value=snapshot), patch.object(
            agent, "_execute", return_value=True
        ) as execute:
            result = agent.assist(
                object(), "email", {"email": "user@example.test"}, force=True, max_actions=1
            )

        self.assertEqual(result.executed, 1)
        self.assertEqual(result.executed_actions[0]["type"], "fill")
        execute.assert_called_once()

    def test_filled_email_snapshot_moves_to_click_action(self):
        agent = self._agent(provider="local")
        snapshot = {
            "inputs": [
                {"selector": "#email", "tag": "input", "name": "email", "disabled": False, "valuePresent": True}
            ],
            "buttons": [{"selector": "#next", "text": "Continue", "disabled": False}],
        }
        actions = agent._local_actions("email", snapshot, {"email": "user@example.test"})
        self.assertEqual(actions, [{"type": "click", "selector": "#next"}])

    def test_filled_otp_snapshot_moves_to_verify_action(self):
        agent = self._agent(provider="local")
        snapshot = {
            "inputs": [
                {"selector": "#otp", "tag": "input", "autocomplete": "one-time-code", "disabled": False, "valuePresent": True}
            ],
            "buttons": [{"selector": "#verify", "text": "Verify", "disabled": False}],
        }
        actions = agent._local_actions("otp", snapshot, {"otp": "123456"})
        self.assertEqual(actions, [{"type": "click", "selector": "#verify"}])

    def test_filled_password_snapshot_moves_to_continue_action(self):
        agent = self._agent(provider="local")
        snapshot = {
            "inputs": [
                {"selector": "#password", "tag": "input", "type": "password", "disabled": False, "valuePresent": True}
            ],
            "buttons": [{"selector": "#next", "text": "Continue", "disabled": False}],
        }
        actions = agent._local_actions("password", snapshot, {"password": "secret"})
        self.assertEqual(actions, [{"type": "click", "selector": "#next"}])


if __name__ == "__main__":
    unittest.main()
