import sqlite3
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from ai_pipeline import _save_pool_once, _get_pool, next_run_delay

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
