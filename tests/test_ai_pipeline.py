import json
import sqlite3
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'collector'))
from datetime import datetime
from zoneinfo import ZoneInfo

from ai_pipeline import (
    _save_pool_once, _get_pool, _trade_action_signals, _clean_company_name, _normalize_stocks,
    _action_based_candidates, _merge_action_candidates, worker_delay,
    _collector_round_complete, _ollama_runtime_status, next_run_delay,
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

    def test_company_names_strip_tickers_and_exchange_suffixes(self):
        self.assertEqual(_clean_company_name("沪电股份（002463.SZ）"), "沪电股份")
        self.assertEqual(_clean_company_name("002463.SZ 沪电股份"), "沪电股份")
        self.assertEqual(_clean_company_name("沪电股份002463"), "沪电股份")
        stocks = _normalize_stocks([{
            "name": "沪电股份（002463.SZ）", "direction": "bull",
            "reason": "原文支持", "evidence_ids": ["source-1"],
        }], {"source-1"})
        self.assertEqual(stocks[0]["name"], "沪电股份")

    def test_model_stock_duplicates_merge_evidence_and_conflicts_are_excluded(self):
        rows = [
            {"name": "甲公司", "direction": "bull", "score": 60, "reason": "a", "evidence_ids": ["a"]},
            {"name": "甲公司", "direction": "bull", "score": 75, "reason": "b", "evidence_ids": ["b"]},
            {"name": "乙公司", "direction": "bull", "score": 80, "reason": "c", "evidence_ids": ["c"]},
            {"name": "乙公司", "direction": "bear", "score": 80, "reason": "d", "evidence_ids": ["d"]},
        ]
        normalized = _normalize_stocks(rows, {"a", "b", "c", "d"})
        self.assertEqual([row["name"] for row in normalized], ["甲公司"])
        self.assertEqual(normalized[0]["score"], 75)
        self.assertEqual(normalized[0]["evidence_ids"], ["a", "b"])

    def test_action_candidates_match_cleaned_company_name(self):
        rows = [{
            "source": {
                "id": "ticker-name", "published_at": "2026-10-30T10:00:00+08:00",
                "content": "沪电股份（002463.SZ）低吸",
            },
            "local_analysis": {
                "companies": ["沪电股份（002463.SZ）"], "relevance": 4,
            },
        }]
        candidates = _action_based_candidates(rows)
        self.assertEqual(candidates[0]["name"], "沪电股份")

    def test_market_flow_and_non_action_phrases_are_not_trade_signals(self):
        signals = _trade_action_signals(
            "甲公司买盘增加；乙公司买入价上涨；丙公司买不到；丁公司低吸；"
            "戊公司卖盘增加；己公司卖不出去；庚公司高抛"
        )
        active = [(x["direction"], x["phrase"]) for x in signals if not x["negated"]]
        self.assertEqual(active, [("bull", "低吸"), ("bear", "高抛")])

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

    def test_explicit_action_candidates_are_not_crowded_out_by_model_rows(self):
        model_rows = [
            {"name": "模型公司%d" % i, "direction": "bull", "score": 50}
            for i in range(40)
        ]
        action_rows = [
            {"name": "明确动作公司%d" % i, "direction": "bull", "score": 80, "evidence_ids": ["e%d" % i]}
            for i in range(25)
        ]
        merged = _merge_action_candidates(model_rows, action_rows)
        names = {row["name"] for row in merged}
        self.assertEqual(len(merged), 40)
        self.assertTrue(all(row["name"] in names for row in action_rows))
        self.assertEqual(sum(row["name"].startswith("模型公司") for row in merged), 15)

    def test_backlog_is_processed_quickly_and_errors_retry(self):
        self.assertEqual(worker_delay({"pending_remaining": 80}), 5)
        self.assertEqual(worker_delay({"pending_remaining": 0}, failed=True), 120)
        self.assertEqual(worker_delay({"pending_remaining": 0},
                                      now=datetime(2026, 10, 30, 15, 30, tzinfo=CST)), 1800)

    def test_next_day_pool_waits_for_fresh_complete_room_scan(self):
        self.conn.execute("CREATE TABLE runtime_status(scope TEXT PRIMARY KEY,value TEXT NOT NULL)")
        now = datetime(2026, 10, 30, 15, 10, tzinfo=CST)
        complete = {
            "status": "collecting", "stage": "round_complete",
            "updated_at": "2026-10-30T15:09:30+08:00",
            "rooms_total": 135, "rooms_done": 135,
            "backlog": 0, "rooms_unmapped": 0, "last_error": "",
        }
        self.conn.execute(
            "INSERT INTO runtime_status(scope,value) VALUES(?,?)",
            ("collector", json.dumps(complete)),
        )
        self.assertTrue(_collector_round_complete(self.conn, "2026-10-30", now))
        incomplete = dict(complete, rooms_done=120, backlog=15)
        self.conn.execute(
            "UPDATE runtime_status SET value=? WHERE scope='collector'",
            (json.dumps(incomplete),),
        )
        self.assertFalse(_collector_round_complete(self.conn, "2026-10-30", now))
        stale = dict(complete, updated_at="2026-10-29T15:09:30+08:00")
        self.conn.execute(
            "UPDATE runtime_status SET value=? WHERE scope='collector'",
            (json.dumps(stale),),
        )
        self.assertFalse(_collector_round_complete(self.conn, "2026-10-30", now))
        errored = dict(complete, last_error="MX room click failed")
        self.conn.execute(
            "UPDATE runtime_status SET value=? WHERE scope='collector'",
            (json.dumps(errored),),
        )
        self.assertFalse(_collector_round_complete(self.conn, "2026-10-30", now))
        self.assertEqual(worker_delay({"retry_soon": True}), 5)

    def test_missing_collector_status_does_not_claim_scan_complete(self):
        self.assertFalse(_collector_round_complete(
            self.conn, "2026-10-30", datetime(2026, 10, 30, 15, 10, tzinfo=CST)
        ))

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
