# -*- coding: utf-8 -*-
# MX 网页采集器：Windows 本地浏览器手动登录，SQLite outbox，逐条确认。
# 按用户实测 DOM 编写，未进行本机 Windows / 135 房间全量联调。
# 正文 .bubble > .text-part；图片 .bubble button.image-part img。
# 135 房间中 6 个“到期待上新”为用户提供的实测证据，不是脚本已跑出的结果。
import hashlib
import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import requests
from playwright.sync_api import sync_playwright
from mx_runtime import ROOT, CST, load_runtime
from mx_transport import safe_post

INGEST_URL = HEARTBEAT_URL = TOKEN = ""
PROFILE = str(ROOT / "mx_profile")
DB_FILE = str(ROOT / "mx_outbox.db")
SCAN_INTERVAL = 60
MAX_LOAD_MORE = 50
MAX_BATCH = 200


def check_config():
    global INGEST_URL, HEARTBEAT_URL, TOKEN
    config, TOKEN = load_runtime()
    INGEST_URL = config["ingest_url"]
    HEARTBEAT_URL = config["heartbeat_url"]


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def bj_now():
    return datetime.now(CST)


RE_FULL = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2})$")
RE_MD = re.compile(r"^(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})$")
RE_HM = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_time(raw):
    if not raw:
        return None, "empty"
    text = raw.strip()
    ref = bj_now()
    # Explicit epoch / ISO timestamps preserve the source timezone and seconds.
    if re.fullmatch(r"\d{10}|\d{13}", text):
        try:
            value = int(text) / (1000 if len(text) == 13 else 1)
            return datetime.fromtimestamp(value, timezone.utc).isoformat(), "explicit_epoch"
        except (ValueError, OverflowError, OSError):
            return None, "invalid_epoch"
    if re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", text):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CST)
            return dt.astimezone(timezone.utc).isoformat(), "explicit_iso"
        except ValueError:
            return None, "invalid_iso"
    match = RE_FULL.match(text)
    if match:
        try:
            dt = datetime(*map(int, match.groups()), tzinfo=CST)
            return dt.astimezone(timezone.utc).isoformat(), "full"
        except ValueError:
            return None, "invalid_full"
    match = RE_MD.match(text)
    if match:
        month, day, hour, minute = map(int, match.groups())
        for year in (ref.year, ref.year - 1):
            try:
                dt = datetime(year, month, day, hour, minute, tzinfo=CST)
            except ValueError:
                continue
            if dt <= ref + timedelta(hours=1):
                return dt.astimezone(timezone.utc).isoformat(), "md_year%d" % year
        return None, "unresolved_md"
    match = RE_HM.match(text)
    if match:
        hour, minute = map(int, match.groups())
        try:
            dt = ref.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            return None, "invalid_hm"
        if dt > ref:
            dt -= timedelta(days=1)
        return dt.astimezone(timezone.utc).isoformat(), "hm_today_or_yesterday"
    return None, "unparsed_raw_kept"


def db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("CREATE TABLE IF NOT EXISTS outbox (source_key TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT, sent_at TEXT, fail_reason TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS rooms (room_key TEXT PRIMARY KEY, name TEXT, loaded_all INTEGER NOT NULL DEFAULT 0, is_dup INTEGER NOT NULL DEFAULT 0, updated_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS runtime_status (scope TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()
    return conn


def save_status(conn, scope, value):
    value = dict(value, updated_at=now_iso())
    conn.execute("INSERT OR REPLACE INTO runtime_status(scope,value) VALUES (?,?)", (scope, json.dumps(value, ensure_ascii=False)))
    conn.commit()


def read_status(conn, scope):
    row = conn.execute("SELECT value FROM runtime_status WHERE scope=?", (scope,)).fetchone()
    return json.loads(row[0]) if row else {}


def enqueue(conn, items):
    for item in items:
        conn.execute("INSERT OR IGNORE INTO outbox (source_key, payload, status, created_at) VALUES (?,?,'pending',?)",
                     (item["source_key"], json.dumps(item, ensure_ascii=False), now_iso()))
    conn.commit()


def flush(conn, max_batches=None):
    permanent = {"missing_source_key", "missing_content", "content_too_long"}
    batches = 0
    outcome = {"code": "idle", "message": "等待新增待上传记录。", "retry_after": 5, "http_status": None}
    while True:
        rows = conn.execute("SELECT source_key, payload FROM outbox WHERE status='pending' ORDER BY attempts,created_at LIMIT ?", (MAX_BATCH,)).fetchall()
        if not rows:
            return outcome
        outcome, body = safe_post(INGEST_URL, TOKEN, {"items": [json.loads(row[1]) for row in rows]})
        ack = body.get("results", []) if outcome["code"] == "ok" else []
        results = {item.get("source_key"): item for item in ack if isinstance(item, dict)} if isinstance(ack, list) else {}
        progressed = 0
        for key, _ in rows:
            result = results.get(key, {})
            if result.get("status") in ("accepted", "duplicate"):
                conn.execute("UPDATE outbox SET status='sent', sent_at=? WHERE source_key=?", (now_iso(), key))
                progressed += 1
            elif result.get("status") == "failed" and result.get("reason") in permanent:
                conn.execute("UPDATE outbox SET status='failed', fail_reason=?, attempts=attempts+1 WHERE source_key=?", (result["reason"], key))
                progressed += 1
            else:
                conn.execute("UPDATE outbox SET attempts=attempts+1 WHERE source_key=?", (key,))
        conn.commit()
        batches += 1
        if progressed < len(rows):
            if outcome["code"] == "ok":
                outcome = dict(outcome, code="partial_ack", message="部分记录未获得逐条确认，原文保留待重试。", retry_after=5)
            return outcome
        outcome = dict(outcome, message="本批%d条已逐条确认（含去重或明确拒绝）；本地原文保留。" % progressed)
        if max_batches is not None and batches >= max_batches:
            return outcome


def heartbeat(conn, status, total, done, backlog, unmapped, last_error=""):
    pending = conn.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0]
    failed = conn.execute("SELECT COUNT(*) FROM outbox WHERE status='failed'").fetchone()[0]
    result, body = safe_post(HEARTBEAT_URL, TOKEN, {
            "collector": "mx_web", "status": status, "rooms_total": total, "rooms_done": done,
            "backlog": backlog, "rooms_unmapped": unmapped, "outbox_pending": pending,
            "outbox_failed": failed, "last_error": last_error,
        }, timeout=(5, 15))
    if result["code"] != "ok" or body.get("status") != "ok":
        print("心跳未确认：%s" % result["message"])
    return result


def upload_worker(stop):
    # Own connection: HTTP waits never block the browser scan or share a SQLite transaction.
    conn = db()
    last_heartbeat = 0
    previous_code = None
    try:
        while not stop.is_set():
            try:
                outcome = flush(conn, max_batches=1)
                save_status(conn, "upload", outcome)
                if outcome["code"] != previous_code or outcome["code"] == "ok":
                    print("上传：%s" % outcome["message"])
                    previous_code = outcome["code"]
                # Do not spend extra calls on heartbeat while the same backend is unavailable.
                if outcome["code"] in ("ok", "idle", "partial_ack") and time.monotonic() - last_heartbeat >= 30:
                    snapshot = read_status(conn, "collector")
                    if snapshot:
                        heartbeat(conn, snapshot.get("status", "collecting"), snapshot.get("rooms_total", 0),
                                  snapshot.get("rooms_done", 0), snapshot.get("backlog", 0),
                                  snapshot.get("rooms_unmapped", 0), snapshot.get("last_error", ""))
                        last_heartbeat = time.monotonic()
                stop.wait(max(0.2, outcome["retry_after"]))
            except (sqlite3.Error, ValueError, TypeError):
                print("上传线程暂遇本地队列异常，保留数据，30秒后重试。")
                stop.wait(30)
    finally:
        conn.close()


SEL_ROOM = ".room-item"
SEL_ROOM_NAME = ".room-title-row strong"
SEL_ROOM_TIME = ".room-message-time"  # datetime 毫秒时间戳，不使用 .room-expire
SEL_MSG = "article.message-row"
SEL_META = ".message-main > .message-meta"
SEL_TEXT = ".bubble > .text-part"
SEL_IMAGE = ".bubble button.image-part img"


def discover_rooms(page):
    rooms = []
    # Snapshot primitive values only; room DOM nodes may be replaced after each click.
    snapshot = page.locator(SEL_ROOM).evaluate_all(
        """(elements, selectors) => elements.map((element, index) => {
            const name = element.querySelector(selectors.name);
            const latest = element.querySelector(selectors.time);
            return {name: name ? (name.innerText || name.textContent || '').trim() : '',
                    latest_raw: latest ? latest.getAttribute('datetime') : null,
                    dom_index: index + 1};
        })""", {"name": SEL_ROOM_NAME, "time": SEL_ROOM_TIME})
    for entry in snapshot:
        name = entry["name"]
        if not name:
            continue
        try:
            latest_ms = int(entry["latest_raw"]) if entry.get("latest_raw") else None
        except (ValueError, TypeError):
            latest_ms = None
        rooms.append({"name": name, "latest_ms": latest_ms,
                      "is_dup": False, "room_key": name, "dom_index": entry["dom_index"]})
    counts = {}
    for room in rooms:
        counts[room["name"]] = counts.get(room["name"], 0) + 1
    for room in rooms:
        if counts[room["name"]] > 1:
            room["is_dup"] = True
            room["room_key"] = "%s#%d" % (room["name"], room["dom_index"])
    if counts.get("到期待上新", 0):
        print("本轮发现“到期待上新”%d 个；重名房间按 DOM 序号临时区分，稳定映射仍待核实。" % counts["到期待上新"])
    rooms.sort(key=lambda room: -(room["latest_ms"] or 0))
    return rooms


def click_room(page, room):
    if room.get("is_dup"):
        raise ValueError("duplicate room identity is unresolved")
    # Resolve title text in Python, avoiding nested relative-locator matching.
    # Do not fall back to a guessed room index or force-click hidden elements.
    titles = page.locator(SEL_ROOM_NAME)
    names = titles.all_inner_texts()
    matches = [i for i, name in enumerate(names) if name.strip() == room["name"].strip()]
    if len(matches) != 1:
        print("房间定位失败：标题总数=%d，精确匹配数=%d。" % (len(names), len(matches)), flush=True)
        raise ValueError("room title is missing or not unique")
    target = titles.nth(matches[0])
    target.scroll_into_view_if_needed(timeout=10000)
    if target.inner_text().strip() != room["name"].strip():
        raise ValueError("room list changed before click")
    target.click(timeout=10000)


def extract_messages(page, room):
    items = []
    for row in page.query_selector_all(SEL_MSG):
        text = "\n".join(element.inner_text().strip() for element in row.query_selector_all(SEL_TEXT)).strip()
        media = []
        for element in row.query_selector_all(SEL_IMAGE):
            src = element.evaluate("e => e.currentSrc || e.getAttribute('src') || ''")
            if src:
                media.append({"type": "image", "src": urljoin(page.url, src)})
        for element in row.query_selector_all(".bubble video, .bubble audio, .bubble a[href]"):
            tag = element.evaluate("e => e.tagName.toLowerCase()")
            src = element.evaluate("e => e.currentSrc || e.src || e.href || ''")
            if src:
                media.append({"type": {"video": "video", "audio": "audio", "a": "link"}[tag], "src": urljoin(page.url, src)})
        if not text and not media:
            continue
        # 纯图片也保留；提示文字不是 OCR 结果，图片地址在 media 内。
        content = text or "[媒体消息无正文，待解析]"
        raw_time = sender = ""
        meta = row.query_selector(SEL_META)
        if meta:
            time_element = meta.query_selector("time")
            if time_element:
                raw_time = (time_element.get_attribute("datetime") or time_element.inner_text() or "").strip()
            sender = (meta.inner_text() or "").split("\n")[0].strip()
        published_at, rule = parse_time(raw_time)
        types = {item["type"] for item in media}
        parse_status = "ocr_pending" if "image" in types else ("link_pending" if types == {"link"} else ("attachment_pending" if media else "text_ok"))
        fingerprint = content if not media else content + json.dumps(media, ensure_ascii=False, sort_keys=True)
        key = "%s|%s|%s" % (room["room_key"], published_at or ("raw:" + (raw_time or "null")), hashlib.md5(fingerprint.encode("utf-8")).hexdigest())
        items.append({
            "item_type": "chat", "source_key": key, "source": "MX", "room": room["name"],
            "room_key": room["room_key"], "author": sender[:80], "title": content[:60],
            "content": content, "published_at": published_at, "raw_time": raw_time, "time_rule": rule,
            "collected_at": now_iso(), "parse_status": parse_status, "media": media,
        })
    return items


def load_history(page, loaded_all, conn, room):
    # 每次入房间都补查，不能因过去追平而忽略断线期间的消息。
    # 每页先持久化，下一页失败也不丢已经读取的正文。
    enqueue(conn, extract_messages(page, room))
    for _ in range(MAX_LOAD_MORE):
        button = page.get_by_text("加载更早", exact=True)
        if button.count() == 0:
            return True
        before = page.locator(SEL_MSG).count()
        try:
            button.first.click(timeout=5000)
        except Exception:
            return False
        for _ in range(20):
            page.wait_for_timeout(500)
            if page.locator(SEL_MSG).count() > before:
                enqueue(conn, extract_messages(page, room))
                break
        else:
            return False  # 无增长可能是网络慢，不宣称历史已追平
    return False


def collect_loop(conn):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch_persistent_context(PROFILE, headless=False)
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.goto("https://mx2025.hhhuu.com/", wait_until="domcontentloaded")
        print("请在浏览器中手动登录 MX（含验证码）；会话仅保存于本机 mx_profile。")
        try:
            page.wait_for_selector(SEL_ROOM, timeout=600000)
        except Exception:
            save_status(conn, "collector", {"status": "failed", "stage": "login_timeout", "last_error": "登录或房间列表等待超时，请人工检查浏览器。"})
            return
        print("已读到房间列表，开始采集；这不代表后端接入已完成。Ctrl+C 退出。")
        while True:
            total = done = backlog = unmapped = history_done = 0
            last_error = ""
            consecutive_failures = 0
            try:
                rooms = discover_rooms(page)
                total = len(rooms)
                unmapped = sum(1 for room in rooms if room["is_dup"])
                if not rooms:
                    last_error = "未读到房间列表，请人工核对登录状态。"
                save_status(conn, "collector", {"status": "collecting", "stage": "scanning", "rooms_total": total,
                            "rooms_done": 0, "backlog": total, "rooms_unmapped": unmapped})
                for room in rooms:
                    if room["is_dup"]:
                        backlog += 1
                        last_error = "重名房间待稳定映射，暂不入库，避免串房。"
                        continue
                    progress = conn.execute("SELECT loaded_all FROM rooms WHERE room_key=?", (room["room_key"],)).fetchone()
                    loaded_all = bool(progress[0]) if progress else False
                    try:
                        click_room(page, room)
                    except Exception as error:
                        last_error = "房间切换失败（%s），保留采集断点。" % type(error).__name__
                        consecutive_failures += 1
                        print(last_error)
                        save_status(conn, "collector", {"status": "collecting", "stage": "room_switch_failed",
                                    "rooms_total": total, "rooms_done": done, "backlog": total - history_done,
                                    "rooms_unmapped": unmapped, "last_error": last_error})
                        if consecutive_failures >= 3:
                            print("连续3次切换失败，停止本轮。请截图MX房间列表与本窗口，等待修复。", flush=True)
                            return
                        continue
                    consecutive_failures = 0
                    page.wait_for_timeout(1200)
                    loaded_all = load_history(page, loaded_all, conn, room)
                    enqueue(conn, extract_messages(page, room))
                    conn.execute("INSERT OR REPLACE INTO rooms (room_key, name, loaded_all, is_dup, updated_at) VALUES (?,?,?,?,?)",
                                 (room["room_key"], room["name"], int(loaded_all), int(room["is_dup"]), now_iso()))
                    conn.commit()
                    done += 1
                    history_done += int(loaded_all)
                    backlog += int(not loaded_all)
                    save_status(conn, "collector", {"status": "collecting", "stage": "scanning", "rooms_total": total,
                                "rooms_done": done, "backlog": total - history_done,
                                "rooms_unmapped": unmapped, "last_error": last_error})
                    pending = conn.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0]
                    print("采集进度：本轮 %d/%d 房间；本地待上传 %d 条；当前房间历史%s。" %
                          (done, total, pending, "已追平" if loaded_all else "待回补"))
            except Exception as error:
                # 不输出请求头、口令、完整响应正文或浏览器会话。
                last_error = "本轮异常：%s；已入队数据保留。" % type(error).__name__
                print(last_error)
            save_status(conn, "collector", {"status": "collecting" if done else "failed", "stage": "round_complete",
                        "rooms_total": total, "rooms_done": done, "backlog": total - history_done,
                        "rooms_unmapped": unmapped, "last_error": last_error})
            time.sleep(SCAN_INTERVAL)


def main():
    check_config()
    print("采集器版本：2026-10-23-room-fix（房间切换修复版）")
    conn = db()
    stop = threading.Event()
    save_status(conn, "collector", {"status": "connected", "stage": "waiting_login", "rooms_total": 0, "rooms_done": 0})
    worker = threading.Thread(target=upload_worker, args=(stop,), daemon=True, name="mx-upload")
    worker.start()
    print("上传已独立运行；后端超时不再阻塞浏览器采集。双击 status.bat 可查看本地计数。")
    try:
        collect_loop(conn)
    finally:
        stop.set()
        worker.join(timeout=3)
        save_status(conn, "collector", dict(read_status(conn, "collector"), stage="stopped", status="failed"))
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("采集器已停止；本地 outbox 保留。")
