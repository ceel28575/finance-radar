import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector"))

from mx_collector import apply_room_identity


class MXRoomIdentityTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE room_identity (identity_key TEXT PRIMARY KEY, room_key TEXT NOT NULL, "
            "room_name TEXT NOT NULL, stable_id TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )

    def tearDown(self):
        self.conn.close()

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
