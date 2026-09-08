# -*- coding: utf-8 -*-
"""llm_params reasoning_value mapping tests (A7④)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.llm_params import reasoning_value


class TestReasoningValue(unittest.TestCase):
    def test_off_maps_none(self):
        self.assertEqual(reasoning_value("off"), "none")

    def test_passthrough_valid(self):
        for v in ("none", "low", "medium", "high"):
            self.assertEqual(reasoning_value(v), v)

    def test_case_insensitive(self):
        self.assertEqual(reasoning_value("OFF"), "none")
        self.assertEqual(reasoning_value("Low"), "low")

    def test_unknown_falls_back_none(self):
        self.assertEqual(reasoning_value("deep"), "none")
        self.assertEqual(reasoning_value(""), "none")
        self.assertEqual(reasoning_value(None), "none")
        self.assertEqual(reasoning_value(123), "none")


if __name__ == "__main__":
    unittest.main()
