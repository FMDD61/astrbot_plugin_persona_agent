# -*- coding: utf-8 -*-
"""llm_params reasoning_value mapping tests (A7④).

2026-09-10 实测修正：网关拒绝 "none"/"off" 等值（HTTP 400）——关闭显式思考
档位的正确方式是返回 None（不发送该参数）。旧实现 off→"none" 会让每次调用
400 并被吞成空回复。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.llm_params import reasoning_value, resolve_provider_id


class TestReasoningValue(unittest.TestCase):
    def test_off_maps_none_sentinel(self):
        """off → None（不发送该键），而非字符串 "none"（网关 400）。"""
        self.assertIsNone(reasoning_value("off"))

    def test_passthrough_valid(self):
        for v in ("low", "medium", "high", "max"):
            self.assertEqual(reasoning_value(v), v)

    def test_case_insensitive(self):
        self.assertIsNone(reasoning_value("OFF"))
        self.assertEqual(reasoning_value("Low"), "low")
        self.assertEqual(reasoning_value("HIGH"), "high")

    def test_gateway_rejected_values_map_to_none(self):
        """网关明确 400 的取值（none/minimal/disabled/false）→ None。"""
        for v in ("none", "minimal", "disabled", "false", False, 0):
            self.assertIsNone(reasoning_value(v), v)

    def test_unknown_falls_back_none(self):
        self.assertIsNone(reasoning_value("deep"))
        self.assertIsNone(reasoning_value(""))
        self.assertIsNone(reasoning_value(None))
        self.assertIsNone(reasoning_value(123))


class TestResolveProviderId(unittest.TestCase):
    """S0：provider 三级回退 + **配置值存在性校验**。

    为什么需要校验：配置里写错一个 provider id，``llm_generate`` 会抛
    ``ProviderNotFoundError``，插件把它吞成空回复 → 表面"没坏"、实际全哑。
    S0 的整批缺陷都是这个形状，所以这里宁可多查一次。
    """

    def test_configured_wins(self):
        self.assertEqual(resolve_provider_id("cfg", "known", "sess", True), "cfg")

    def test_unknown_existence_still_uses_configured(self):
        """拿不到 provider_manager 时按"未知，放行"，不能因校验本身失败而停摆。"""
        self.assertEqual(resolve_provider_id("cfg", "known", "sess", None), "cfg")

    def test_bad_configured_falls_back_to_known(self):
        self.assertEqual(resolve_provider_id("typo", "known", "sess", False), "known")

    def test_bad_configured_falls_back_to_session(self):
        self.assertEqual(resolve_provider_id("typo", None, "sess", False), "sess")

    def test_bad_configured_no_fallback_returns_none(self):
        self.assertIsNone(resolve_provider_id("typo", None, None, False))

    def test_empty_configured_uses_known_then_session(self):
        self.assertEqual(resolve_provider_id("", "known", "sess", None), "known")
        self.assertEqual(resolve_provider_id("", None, "sess", None), "sess")
        self.assertIsNone(resolve_provider_id("", None, None, None))

    def test_whitespace_configured_treated_as_empty(self):
        self.assertEqual(resolve_provider_id("   ", None, "sess", None), "sess")

    def test_never_returns_empty_string(self):
        """调用方用 ``if not provider_id`` 判定 → 绝不能返回空串。"""
        for args in (("", None, None, None), ("  ", "", "", False),
                     ("typo", "", "", False)):
            self.assertFalse(resolve_provider_id(*args))


if __name__ == "__main__":
    unittest.main()
