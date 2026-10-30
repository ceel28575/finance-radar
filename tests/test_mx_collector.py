import sqlite3
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector"))

from mx_collector import apply_room_identity, load_history, _oldest_message_key, parse_time_for_archive


class FakeLocator:
    def __init__(self, page):
        self.page = page
    def count(self):
        return len(self.page.pages[self.page.index])


class FakeButton:
    def __init__(self, page):
        self.page = page
        self.first = self
    def count(self):
        return 1 if self.page.index < len(self.page.pages) - 1 else 0
    def click(self, timeout=5000):
        self.page.index += 1


class FakePage:
    def __init__(self, pages):
        self.pages = pages
        self.index = 0
    def get_by_text(self, text, exact=True):
        return FakeButton(self)
    def locator(self, selector):
        return FakeLocator(self)
    def wait_for_timeout(self, milliseconds):
        return None


class MXRoomIdentityTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE room_identity (identity_key TEXT PRIMARY KEY, room_key TEXT NOT NULL, "
            "room_name TEXT NOT NULL, stable_id TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )

    def tearDown(self):
        self.conn.close()

    def test_history_backfill_resumes_from_saved_cursor_and_finishes(self):
        self.conn.execute(
            "CREATE TABLE rooms (room_key TEXT PRIMARY KEY,name TEXT,loaded_all INTEGER,"
            "is_dup INTEGER,updated_at TEXT,history_cursor TEXT)"
        )
        room = {"room_key": "重复房间#1", "name": "重复房间", "is_dup": True}
        self.conn.execute(
            "INSERT INTO rooms(room_key,name,loaded_all,is_dup,history_cursor) VALUES(?,?,?,?,?)",
            (room["room_key"], room["name"], 0, 1, "cursor"),
        )
        pages = [
            [{"source_key": "new", "published_at": "2026-10-30T10:00:00+00:00"}],
            [
                {"source_key": "cursor", "published_at": "2026-10-30T09:00:00+00:00"},
                {"source_key": "new", "published_at": "2026-10-30T10:00:00+00:00"},
            ],
            [
                {"source_key": "oldest", "published_at": "2026-10-30T08:00:00+00:00"},
                {"source_key": "cursor", "published_at": "2026-10-30T09:00:00+00:00"},
                {"source_key": "new", "published_at": "2026-10-30T10:00:00+00:00"},
            ],
        ]
        page = FakePage(pages)
        with patch("mx_collector.extract_messages", side_effect=lambda p, *args: p.pages[p.index]), patch(
            "mx_collector.enqueue"
        ):
            complete = load_history(page, False, self.conn, room, [0], "cursor")
        self.assertTrue(complete)
        self.assertEqual(page.index, 2)
        saved = self.conn.execute(
            "SELECT loaded_all,history_cursor FROM rooms WHERE room_key=?",
            (room["room_key"],),
        ).fetchone()
        self.assertEqual(saved, (1, "oldest"))

    def test_loaded_room_does_not_repeat_full_history_backfill(self):
        self.conn.execute(
            "CREATE TABLE rooms (room_key TEXT PRIMARY KEY,name TEXT,loaded_all INTEGER,"
            "is_dup INTEGER,updated_at TEXT,history_cursor TEXT)"
        )
        room = {"room_key": "room", "name": "room", "is_dup": False}
        page = FakePage([[{"source_key": "latest", "published_at": "2026-10-30T10:00:00+00:00"}],
                         [{"source_key": "older", "published_at": "2026-10-30T09:00:00+00:00"}]])
        with patch("mx_collector.extract_messages", side_effect=lambda p, *args: p.pages[p.index]), patch(
            "mx_collector.enqueue"
        ) as enqueue_mock:
            self.assertTrue(load_history(page, True, self.conn, room, [0], "older"))
        self.assertEqual(page.index, 0)
        enqueue_mock.assert_called_once()

    def test_unrecognized_timestamp_is_archived_with_collection_time_fallback(self):
        fallback = "2026-10-30T08:30:00+00:00"
        stamp, rule = parse_time_for_archive("昨天上午九点半", fallback)
        self.assertEqual(stamp, fallback)
        self.assertEqual(rule, "collected_at_fallback")

    def test_explicit_timestamp_is_not_replaced_by_collection_time(self):
        fallback = "2026-10-30T08:30:00+00:00"
        stamp, rule = parse_time_for_archive("2026-10-30T09:30:00+08:00", fallback)
        self.assertEqual(stamp, "2026-10-30T01:30:00+00:00")
        self.assertEqual(rule, "explicit_iso")

    def test_oldest_message_cursor_uses_earliest_timestamp(self):
        items = [
            {"source_key": "new", "published_at": "2026-10-30T10:00:00+00:00"},
            {"source_key": "old", "published_at": "2026-10-30T08:00:00+00:00"},
        ]
        self.assertEqual(_oldest_message_key(items), "old")

    def test_duplicate_room_keys_survive_dom_reordering(self):
        first_scan = [
            {"name": "到期待上新", "room_key": "到期待上新#1", "dom_index": 1, "stable_id": "data-room-id:101"},
            {"name": "到期待上新", "room_key": "到期待上新#2", "dom_index": 2, "stable_id": "data-room-id:202"},
        ]
        apply_room_identity(self.conn, first_scan)
        expected = {row["stable_id"]: row["room_key"] for row in first_scan}
        second_scan = [
            {"name": "到期待上新", "room_key": "到期待上新#1", "dom_index": 1, "stable_id": "data-room-id:202"},
            {"name": "到期待上新", "room_key": "到期待上新#2", "dom_index": 2, "stable_id": "data-room-id:101"},
        ]
        apply_room_identity(self.conn, second_scan)
        actual = {row["stable_id"]: row["room_key"] for row in second_scan}
        self.assertEqual(actual, expected)

    def test_first_identity_adopts_existing_legacy_key(self):
        room = {"name": "重复房间", "room_key": "重复房间#3", "dom_index": 3, "stable_id": "data-id:abc"}
        apply_room_identity(self.conn, [room])
        self.assertEqual(room["room_key"], "重复房间#3")
        saved = self.conn.execute("SELECT room_key FROM room_identity").fetchone()[0]
        self.assertEqual(saved, "重复房间#3")

    def test_room_rename_keeps_the_existing_identity_key(self):
        first = {"name": "旧房间名", "room_key": "旧房间名#1", "dom_index": 1, "stable_id": "data-room-id:303"}
        apply_room_identity(self.conn, [first])
        renamed = {"name": "新房间名", "room_key": "新房间名#1", "dom_index": 1, "stable_id": "data-room-id:303"}
        apply_room_identity(self.conn, [renamed])
        self.assertEqual(renamed["room_key"], "旧房间名#1")
        saved_name = self.conn.execute("SELECT room_name FROM room_identity").fetchone()[0]
        self.assertEqual(saved_name, "新房间名")

    def test_same_stable_id_on_different_rows_is_not_merged(self):
        rooms = [
            {"name": "甲房间", "room_key": "甲房间", "dom_index": 1, "stable_id": "data-id:shared"},
            {"name": "乙房间", "room_key": "乙房间", "dom_index": 2, "stable_id": "data-id:shared"},
        ]
        apply_room_identity(self.conn, rooms)
        self.assertEqual([x["room_key"] for x in rooms], ["甲房间", "乙房间"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM room_identity").fetchone()[0], 0)

    def test_ambiguous_duplicate_dom_ids_keep_dom_fallback(self):
        rooms = [
            {"name": "重复房间", "room_key": "重复房间#1", "dom_index": 1, "stable_id": "data-id:same"},
            {"name": "重复房间", "room_key": "重复房间#2", "dom_index": 2, "stable_id": "data-id:same"},
        ]
        apply_room_identity(self.conn, rooms)
        self.assertEqual([x["room_key"] for x in rooms], ["重复房间#1", "重复房间#2"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM room_identity").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
