# -*- coding: utf-8 -*-
import os
import unittest
from unittest.mock import patch

from config import env_loader
from config import browser
from webui import config_editor


class ConfigDefaultFallbackTests(unittest.TestCase):
    def test_blank_env_value_uses_default_for_all_supported_types(self):
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        try:
            with patch.dict(os.environ, {
                "BOOL_KEY": "",
                "INT_KEY": "",
                "FLOAT_KEY": "",
                "STR_KEY": "",
                "LIST_KEY": "",
            }, clear=True):
                self.assertTrue(env_loader.env_bool("BOOL_KEY", True))
                self.assertEqual(env_loader.env_int("INT_KEY", 90), 90)
                self.assertEqual(env_loader.env_float("FLOAT_KEY", 1.5), 1.5)
                self.assertEqual(env_loader.env_str("STR_KEY", "default"), "default")
                self.assertEqual(env_loader.env_list("LIST_KEY", ["a"]), ["a"])
        finally:
            env_loader._LOADED = old_loaded

    def test_proxy_pool_blank_env_value_means_empty_list(self):
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        namespace = {"PROXY_POOL": ["socks5://127.0.0.1:7897"]}
        try:
            with patch.dict(os.environ, {"PROXY_POOL": ""}, clear=True):
                env_loader.apply_env_overrides(namespace, {"PROXY_POOL": "list_str_multiline"})
        finally:
            env_loader._LOADED = old_loaded

        self.assertEqual(namespace["PROXY_POOL"], [])

    def test_config_editor_formats_empty_list_as_literal_empty_list(self):
        self.assertEqual(config_editor._format_env_value([], "list_str_multiline"), "[]")

    def test_apply_env_overrides_does_not_let_blank_values_mask_defaults(self):
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        namespace = {"FEATURE_ENABLED": True, "BASE_URL": "https://example.test"}
        try:
            with patch.dict(os.environ, {"FEATURE_ENABLED": "", "BASE_URL": ""}, clear=True):
                env_loader.apply_env_overrides(namespace, {"FEATURE_ENABLED": "bool", "BASE_URL": "str"})
        finally:
            env_loader._LOADED = old_loaded

        self.assertTrue(namespace["FEATURE_ENABLED"])
        self.assertEqual(namespace["BASE_URL"], "https://example.test")

    def test_config_editor_parses_env_str_default_from_source(self):
        source = 'API_KEY: str = env_str("API_KEY", "fallback-key")\n'
        self.assertEqual(
            config_editor._parse_value_from_source(source, "API_KEY", "str"),
            "fallback-key",
        )

    def test_config_editor_blank_env_value_falls_back_to_source_default(self):
        self.assertEqual(
            config_editor._coerce_raw_value("", "wss://connect.browser-use.com", "str"),
            "wss://connect.browser-use.com",
        )
        self.assertTrue(config_editor._coerce_raw_value("", True, "bool"))

    def test_region_profile_switch_has_follow_proxy_semantics(self):
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}
        self.assertEqual(fields["AUTO_BROWSER_LOCALE_FROM_IP"]["label"], "跟随代理池 IP")
        self.assertIn("关闭", fields["AUTO_BROWSER_LOCALE_FROM_IP"]["help"])
        self.assertIn("自定义", fields["BROWSER_LOCALE_PROFILE"]["label"])

    def test_proxy_pool_has_registration_preflight_switch(self):
        """代理池配置应提供默认关闭的注册前连通性门禁。"""
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}
        field = fields["PROXY_CHECK_BEFORE_REGISTRATION"]
        self.assertEqual(field["type"], "bool")
        self.assertEqual(field["group"], "代理池")

        source = (config_editor._CONFIG_DIR / "proxy.py").read_text(encoding="utf-8")
        self.assertFalse(
            config_editor._parse_value_from_source(
                source,
                "PROXY_CHECK_BEFORE_REGISTRATION",
                "bool",
            )
        )

    def test_registration_has_oaics_completion_query_switch(self):
        """注册方式配置应暴露注册完成后的 OAICS 自动查询开关。"""
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}
        field = fields["OAICS_CHECK_AFTER_REGISTRATION"]
        self.assertEqual(field["type"], "bool")
        self.assertEqual(field["group"], "注册方式")
        self.assertIn("注册成功", field["help"])

        source = (config_editor._CONFIG_DIR / "register.py").read_text(encoding="utf-8")
        self.assertTrue(
            config_editor._parse_value_from_source(
                source,
                "OAICS_CHECK_AFTER_REGISTRATION",
                "bool",
            )
        )

    def test_registration_traffic_mode_has_three_choices_and_original_default(self):
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}
        field = fields["REGISTRATION_TRAFFIC_MODE"]
        self.assertEqual(field["group"], "注册方式")
        self.assertEqual(field["choices"], ["default", "stable", "throttle"])
        self.assertEqual(
            field["choice_labels"],
            {"default": "默认（保持原来的不变）", "stable": "稳定模式", "throttle": "节流模式"},
        )

        traffic_source = (config_editor._CONFIG_DIR / "traffic.py").read_text(encoding="utf-8")
        protocol_source = (config_editor._CONFIG_DIR / "openai_protocol.py").read_text(encoding="utf-8")
        self.assertEqual(
            config_editor._parse_value_from_source(traffic_source, "REGISTRATION_TRAFFIC_MODE", "str"),
            "default",
        )
        self.assertEqual(
            config_editor._parse_value_from_source(protocol_source, "PROTOCOL_PREFLIGHT_MODE", "str"),
            "full",
        )
        self.assertTrue(
            config_editor._parse_value_from_source(protocol_source, "CHATGPT_ANON_BOOTSTRAP_ENABLED", "bool")
        )
        self.assertTrue(
            config_editor._parse_value_from_source(protocol_source, "CHATGPT_AUTH_BOOTSTRAP_ENABLED", "bool")
        )

    def test_proxy_warmup_has_multidimensional_cleanliness_fields(self):
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}

        self.assertIn("PROXY_WARMUP_REPUTATION_URL", fields)
        self.assertIn("PROXY_WARMUP_ANONYMITY_URL", fields)
        self.assertEqual(fields["PROXY_WARMUP_MIN_CLEAN_SCORE"]["type"], "int")
        self.assertEqual(fields["PROXY_WARMUP_MAX_LATENCY"]["type"], "float")
        self.assertEqual(fields["PROXY_WARMUP_EXIT_SAMPLES"]["type"], "int")
        self.assertEqual(fields["PROXY_BROWSER_CHALLENGE_AUTO_ROTATE"]["type"], "bool")
        self.assertIn("信誉", fields["PROXY_WARMUP_REPUTATION_URL"]["help"])
        self.assertIn("匿名性", fields["PROXY_WARMUP_ANONYMITY_URL"]["help"])
        self.assertEqual(fields["PROXY_WARMUP_RECHECK_CLEAN_IPS"]["type"], "bool")
        self.assertIn("再检测一轮", fields["PROXY_WARMUP_RECHECK_CLEAN_IPS"]["help"])

        source = (config_editor._CONFIG_DIR / "proxy.py").read_text(encoding="utf-8")
        self.assertFalse(
            config_editor._parse_value_from_source(
                source,
                "PROXY_WARMUP_RECHECK_CLEAN_IPS",
                "bool",
            )
        )

    def test_page_agent_choices_have_chinese_labels_and_direct_default(self):
        """页面 Agent 机器值保持兼容，WebUI 下拉统一展示中文。"""
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}
        self.assertEqual(
            fields["CLOAK_AGENT_MODE"]["choice_labels"],
            {"takeover": "完全接管", "hybrid": "混合模式"},
        )
        self.assertEqual(
            fields["PAGE_AGENT_PROVIDER"]["choice_labels"],
            {"disabled": "关闭", "local": "本地 DOM Agent", "openai_compatible": "兼容模型 API"},
        )
        self.assertEqual(
            fields["PAGE_AGENT_NETWORK_ROUTE"]["choice_labels"],
            {"direct": "本机直连", "proxy_pool": "代理池出口"},
        )
        source = (config_editor._CONFIG_DIR / "page_agent.py").read_text(encoding="utf-8")
        self.assertEqual(
            config_editor._parse_value_from_source(source, "PAGE_AGENT_NETWORK_ROUTE", "str"),
            "direct",
        )

        template = (config_editor._PROJECT_ROOT / "webui" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("(f.choice_labels || {})[x] || x", template)

    def test_custom_region_profile_ignores_proxy_geo_when_switch_is_off(self):
        with patch.object(browser, "AUTO_BROWSER_LOCALE_FROM_IP", False):
            profile = browser.build_browser_environment({"country": "US", "timezone": "America/Los_Angeles"})
        self.assertEqual(profile["locale_profile"], browser.BROWSER_LOCALE_PROFILE)
        self.assertEqual(profile["timezone_iana"], browser.BROWSER_LOCALE_PROFILES[browser.BROWSER_LOCALE_PROFILE]["timezone_iana"])


if __name__ == "__main__":
    unittest.main()
