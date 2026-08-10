import unittest
from unittest.mock import Mock, patch

from core.page_agent import PageAgent, PageAgentConfigError, _post_model_request


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

    def test_challenge_stage_uses_immediate_local_fast_path(self):
        """完全接管遇到验证盾时直接调用 iframe 处理，不等待模型请求。"""
        agent = self._agent(provider="openai_compatible")
        driver = Mock()
        driver.click_challenge_frame.return_value = True
        snapshot = {
            "challenge_frames": [
                {"selector": '[data-page-agent-id="page-agent-challenge-0"]', "tag": "iframe"}
            ]
        }

        with patch.object(agent, "_model_actions") as model_actions:
            result = agent.assist(
                driver,
                "challenge",
                {},
                force=True,
                snapshot=snapshot,
                max_actions=1,
            )

        model_actions.assert_not_called()
        driver.click_challenge_frame.assert_called_once_with(
            '[data-page-agent-id="page-agent-challenge-0"]'
        )
        self.assertEqual(result.reason, "challenge_fast_path")
        self.assertEqual(result.executed, 1)

    @patch("core.page_agent.requests.Session")
    def test_model_request_direct_ignores_environment_proxy(self, session_factory):
        """默认直连应关闭 requests 环境代理，并且不传 proxies 参数。"""
        session = session_factory.return_value
        response = session.post.return_value

        result = _post_model_request(
            {"network_route": "direct", "timeout": 12},
            url="http://model.test/v1/chat/completions",
            headers={"Authorization": "Bearer test"},
            payload={"model": "test"},
        )

        self.assertIs(result, response)
        self.assertFalse(session.trust_env)
        self.assertNotIn("proxies", session.post.call_args.kwargs)
        session.close.assert_called_once()

    @patch("config.proxy.pick_proxy", return_value="http://user:secret@127.0.0.1:7890")
    @patch("core.page_agent.requests.Session")
    def test_model_request_uses_proxy_pool(self, session_factory, pick_proxy):
        """代理池出口应把同一代理同时用于 HTTP 与 HTTPS 模型请求。"""
        session = session_factory.return_value

        _post_model_request(
            {"network_route": "proxy_pool", "timeout": 12},
            url="https://model.test/v1/chat/completions",
            headers={},
            payload={"model": "test"},
        )

        pick_proxy.assert_called_once_with()
        self.assertEqual(
            session.post.call_args.kwargs["proxies"],
            {
                "http": "http://user:secret@127.0.0.1:7890",
                "https": "http://user:secret@127.0.0.1:7890",
            },
        )
        session.close.assert_called_once()

    @patch("config.proxy.pick_proxy", return_value="")
    @patch("core.page_agent.requests.Session")
    def test_model_request_rejects_empty_proxy_pool(self, session_factory, pick_proxy):
        """选择代理池但未配置代理时应返回明确原因。"""
        session = session_factory.return_value

        with self.assertRaisesRegex(PageAgentConfigError, "代理池为空"):
            _post_model_request(
                {"network_route": "proxy_pool", "timeout": 12},
                url="https://model.test/v1/chat/completions",
                headers={},
                payload={"model": "test"},
            )

        pick_proxy.assert_called_once_with()
        session.post.assert_not_called()
        session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
