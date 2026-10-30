import sqlite3
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'collector'))
from datetime import datetime
from zoneinfo import ZoneInfo

from ai_pipeline import (
    _save_pool_once, _get_pool, _trade_action_signals,
    _action_based_candidates, _merge_action_candidates, worker_delay,
    _ollama_runtime_status, next_run_delay,
)

CST = ZoneInfo("Asia/Shanghai")


class AIPipelineTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE ai_daily_pool(pool_date TEXT PRIMARY KEY,payload TEXT NOT NULL,created_at TEXT NOT NULL)")

    def tearDown(self):
        self.conn.close()

    def test_daily_pool_is_frozen_after_first_write(self):
        first = [{"name": "甲公司", "direction": "bull", "evidence_ids": ["a"]}]
        second = [{"name": "乙公司", "direction": "bear", "evidence_ids": ["b"]}]
        saved = _save_pool_once(self.conn, "2026-10-30", first, "2026-10-29T15:10:00+08:00")
        again = _save_pool_once(self.conn, "2026-10-30", second, "2026-10-29T15:40:00+08:00")
        self.assertEqual(saved["stocks"], first)
        self.assertEqual(again["stocks"], first)
        self.assertEqual(_get_pool(self.conn, "2026-10-30")["stocks"], first)

    def test_empty_daily_pool_is_frozen_too(self):
        first = [{"name": "甲公司", "direction": "bull", "evidence_ids": ["a"]}]
        _save_pool_once(self.conn, "2026-10-30", [], "2026-10-29T15:10:00+08:00")
        unchanged = _save_pool_once(self.conn, "2026-10-30", first, "2026-10-30T10:00:00+08:00")
        self.assertEqual(unchanged["stocks"], [])

    def test_trade_action_keywords_are_exposed_with_negation_context(self):
        signals = _trade_action_signals(
            "甲公司低吸，乙公司高抛，丙公司不建议买，丁公司暂不卖，戊公司高抛低吸"
        )
        compact = [(x["direction"], x["phrase"], x["negated"]) for x in signals]
        self.assertIn(("bull", "低吸", False), compact)
        self.assertIn(("bear", "高抛", False), compact)
        self.assertIn(("bull", "买", True), compact)
        self.assertIn(("bear", "卖", True), compact)
        self.assertIn(("bull", "低吸", False), compact)
        self.assertIn(("bear", "高抛", False), compact)

    def test_action_candidates_map_buy_and_sell_to_the_named_company(self):
        rows = [
            {
                "source": {
                    "id": "msg-1",
                    "published_at": "2026-10-30T09:31:00+08:00",
                    "content": "沪电股份低吸；生益电子高抛",
                },
                "local_analysis": {
                    "companies": ["沪电股份", "生益电子"],
                    "relevance": 5,
                    "summary": "PCB相关操作观点",
                },
            }
        ]
        candidates = _action_based_candidates(rows)
        self.assertEqual({x["name"]: x["direction"] for x in candidates},
                         {"沪电股份": "bull", "生益电子": "bear"})
        self.assertTrue(all(x["evidence_ids"] == ["msg-1"] for x in candidates))

    def test_action_candidates_ignore_negation_and_mixed_strategy(self):
        rows = [
            {
                "source": {
                    "id": "msg-2",
                    "published_at": "2026-10-30T09:32:00+08:00",
                    "content": "甲公司不建议买；乙公司暂不卖；丙公司高抛低吸",
                },
                "local_analysis": {
                    "companies": ["甲公司", "乙公司", "丙公司"],
                    "relevance": 5,
                },
            }
        ]
        self.assertEqual(_action_based_candidates(rows), [])

    def test_latest_explicit_action_wins_and_overrides_model_direction(self):
        rows = [
            {
                "source": {"id": "old", "published_at": "2026-10-30T09:00:00+08:00", "content": "甲公司低吸"},
                "local_analysis": {"companies": ["甲公司"], "relevance": 4},
            },
            {
                "source": {"id": "new", "published_at": "2026-10-30T10:00:00+08:00", "content": "甲公司高抛"},
                "local_analysis": {"companies": ["甲公司"], "relevance": 4},
            },
        ]
        candidates = _action_based_candidates(rows)
        merged = _merge_action_candidates(
            [{"name": "甲公司", "direction": "bull", "reason": "模型判断"}],
            candidates,
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["direction"], "bear")
        self.assertEqual(merged[0]["evidence_ids"], ["new"])

    def test_backlog_is_processed_quickly_and_errors_retry(self):
        self.assertEqual(worker_delay({"pending_remaining": 80}), 5)
        self.assertEqual(worker_delay({"pending_remaining": 0}, failed=True), 120)
        self.assertEqual(worker_delay({"pending_remaining": 0},
                                      now=datetime(2026, 10, 30, 15, 30, tzinfo=CST)), 1800)

    def test_ollama_gpu_vram_is_reported_from_runtime(self):
        class Response:
            def raise_for_status(self):
                pass
            def json(self):
                return {"models": [{"name": "qwen3.5:4b", "size_vram": 2147483648}]}
        with patch("ai_pipeline.requests.get", return_value=Response()):
            status = _ollama_runtime_status()
        self.assertEqual(status["ollama_gpu_status"], "gpu")
        self.assertEqual(status["ollama_vram_bytes"], 2147483648)

    def test_ollama_cpu_and_not_loaded_states_are_distinguished(self):
        class Response:
            def __init__(self, models):
                self.models = models
            def raise_for_status(self):
                pass
            def json(self):
                return {"models": self.models}
        with patch("ai_pipeline.requests.get", return_value=Response([{"name": "qwen3.5:4b", "size_vram": 0}])):
            self.assertEqual(_ollama_runtime_status()["ollama_gpu_status"], "cpu")
        with patch("ai_pipeline.requests.get", return_value=Response([])):
            self.assertEqual(_ollama_runtime_status()["ollama_gpu_status"], "not_loaded")

    def test_market_session_runs_every_minute(self):
        now = datetime(2026, 10, 30, 10, 0, tzinfo=CST)
        self.assertEqual(next_run_delay(now), 60)

    def test_lunch_break_waits_until_one_pm(self):
        now = datetime(2026, 10, 30, 11, 45, tzinfo=CST)
        self.assertEqual(next_run_delay(now), 4500)

    def test_after_close_runs_every_thirty_minutes(self):
        now = datetime(2026, 10, 30, 15, 30, tzinfo=CST)
        self.assertEqual(next_run_delay(now), 1800)


if __name__ == "__main__":
    unittest.main()
