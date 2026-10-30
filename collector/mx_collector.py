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
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import sync_playwright
from mx_runtime import ROOT, CST, load_runtime
from mx_transport import safe_post
from mx_ocr import find_tesseract, ocr_image_bytes

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
    conn.execute("CREATE TABLE IF NOT EXISTS room_identity (identity_key TEXT PRIMARY KEY, room_key TEXT NOT NULL, room_name TEXT NOT NULL, stable_id TEXT NOT NULL, updated_at TEXT NOT NULL)")
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
        previous = conn.execute("SELECT payload FROM outbox WHERE source_key=?", (item["source_key"],)).fetchone()
        if previous:
            old = json.loads(previous[0])
            # Collection timestamps alone must not resend unchanged records.
            old.pop("collected_at", None)
            current = {k: v for k, v in item.items() if k != "collected_at"}
            if old != current:
                conn.execute("UPDATE outbox SET payload=?, status='pending', attempts=0, sent_at=NULL, fail_reason=NULL WHERE source_key=?",
                             (json.dumps(item, ensure_ascii=False), item["source_key"]))
            continue
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


def discover_rooms(page, conn=None):
    rooms = []
    # Snapshot primitive values only; room DOM nodes may be replaced after each click.
    snapshot = page.locator(SEL_ROOM).evaluate_all(
        """(elements, selectors) => elements.map((element, index) => {
            const name = element.querySelector(selectors.name);
            const latest = element.querySelector(selectors.time);
            const attrs = ['data-room-id', 'data-id', 'data-chat-id', 'data-conversation-id', 'href'];
            let stable_id = null;
            for (const attr of attrs) {
                const value = element.getAttribute(attr) || (element.querySelector('a[href]') && attr === 'href' ? element.querySelector('a[href]').getAttribute('href') : null);
                if (value && value.trim()) { stable_id = attr + ':' + value.trim(); break; }
            }
            return {name: name ? (name.innerText || name.textContent || '').trim() : '',
                    latest_raw: latest ? latest.getAttribute('datetime') : null,
                    stable_id,
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
        rooms.append({
            "name": name, "latest_ms": latest_ms,
            "is_dup": False, "room_key": name, "dom_index": entry["dom_index"],
            "stable_id": str(entry.get("stable_id") or "")[:180],
        })
    counts = {}
    for room in rooms:
        counts[room["name"]] = counts.get(room["name"], 0) + 1
    for room in rooms:
        if counts[room["name"]] > 1:
            room["is_dup"] = True
            # Preserve the existing local room-key format to avoid duplicating
            # previously collected messages after this upgrade.
            room["room_key"] = "%s#%d" % (room["name"], room["dom_index"])
    if counts.get("到期待上新", 0):
        print("本轮发现“到期待上新”%d 个；重名房间按 DOM 序号临时区分，稳定映射仍待核实。" % counts["到期待上新"])
    if conn is not None:
        apply_room_identity(conn, rooms)
    rooms.sort(key=lambda room: -(room["latest_ms"] or 0))
    return rooms


def apply_room_identity(conn, rooms):
    """Persist duplicate-room identity across MX list reordering when DOM exposes an ID."""
    stable_counts = {}
    for room in rooms:
        stable_id = str(room.get("stable_id") or "").strip()
        if stable_id:
            pair = (room["name"], stable_id)
            stable_counts[pair] = stable_counts.get(pair, 0) + 1

    for room in rooms:
        stable_id = str(room.get("stable_id") or "").strip()
        # An ID repeated by multiple rows is not a unique identity; keep the
        # verified DOM-position fallback rather than merging two conversations.
        if not stable_id or stable_counts.get((room["name"], stable_id)) != 1:
            continue
        identity_key = hashlib.sha256(
            (room["name"] + "\\0" + stable_id).encode("utf-8")
        ).hexdigest()
        previous = conn.execute(
            "SELECT room_key FROM room_identity WHERE identity_key=?",
            (identity_key,),
        ).fetchone()
        if previous:
            room["room_key"] = previous[0]
            conn.execute(
                "UPDATE room_identity SET updated_at=? WHERE identity_key=?",
                (now_iso(), identity_key),
            )
        else:
            # First observation adopts the legacy key, so the existing SQLite
            # history/outbox is not duplicated by this upgrade.
            conn.execute(
                "INSERT INTO room_identity(identity_key,room_key,room_name,stable_id,updated_at) VALUES(?,?,?,?,?)",
                (identity_key, room["room_key"], room["name"], stable_id, now_iso()),
            )
    conn.commit()


def click_room(page, room):
    # Unique names are resolved by exact text. Duplicate names use a stable DOM
    # attribute when present, otherwise the captured row index plus a name/time
    # recheck. This avoids silently dropping duplicate rooms while refusing a
    # click if the room list changed underneath the snapshot.
    if room.get("is_dup"):
        rows = page.locator(SEL_ROOM)
        chosen_index = None
        stable_id = str(room.get("stable_id") or "")
        if stable_id:
            matches = rows.evaluate_all(
                """(elements, selectors) => elements.map((element, index) => {
                    const attrs = ['data-room-id', 'data-id', 'data-chat-id', 'data-conversation-id', 'href'];
                    let stable_id = null;
                    for (const attr of attrs) {
                        const value = element.getAttribute(attr) || (element.querySelector('a[href]') && attr === 'href' ? element.querySelector('a[href]').getAttribute('href') : null);
                        if (value && value.trim()) { stable_id = attr + ':' + value.trim(); break; }
                    }
                    const name = element.querySelector(selectors.name);
                    return {index, stable_id, name: name ? (name.innerText || name.textContent || '').trim() : ''};
                })""",
                {"name": SEL_ROOM_NAME},
            )
            indexes = [
                row["index"] for row in matches
                if row.get("stable_id") == stable_id and row.get("name") == room["name"]
            ]
            if len(indexes) == 1:
                chosen_index = indexes[0]
        if chosen_index is None:
            try:
                chosen_index = int(room.get("dom_index", 0)) - 1
            except (TypeError, ValueError):
                chosen_index = -1
        if chosen_index < 0 or chosen_index >= rows.count():
            raise ValueError("duplicate room row index is no longer available")
        row = rows.nth(chosen_index)
        title = row.locator(SEL_ROOM_NAME)
        if title.count() != 1 or title.inner_text().strip() != room["name"].strip():
            raise ValueError("duplicate room row changed before click")
        if room.get("latest_ms") is not None:
            time_node = row.locator(SEL_ROOM_TIME)
            if time_node.count() == 1:
                raw = time_node.get_attribute("datetime")
                try:
                    current_latest = int(raw) if raw else None
                except (TypeError, ValueError):
                    current_latest = None
                if current_latest is not None and current_latest != room["latest_ms"]:
                    raise ValueError("duplicate room latest timestamp changed before click")
        target = title
    else:
        titles = page.locator(SEL_ROOM_NAME)
        names = titles.all_inner_texts()
        matches = [i for i, name in enumerate(names) if name.strip() == room["name"].strip()]
        if len(matches) != 1:
            print("房间定位失败：标题总数=%d，精确匹配数=%d。" % (len(names), len(matches)), flush=True)
            raise ValueError("room title is missing or not unique")
        target = titles.nth(matches[0])
        if target.inner_text().strip() != room["name"].strip():
            raise ValueError("room list changed before click")
    target.scroll_into_view_if_needed(timeout=10000)
    target.click(timeout=10000)


def _ocr_image_from_page(page, src, executable):
    parsed = urlparse(src)
    page_host = (urlparse(page.url).hostname or "").lower()
    image_host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not page_host:
        return ""
    # Only fetch images from MX's own host family; do not send browser cookies
    # to arbitrary hosts embedded in message content.
    if image_host != page_host and not image_host.endswith(".hhhuu.com"):
        return ""
    try:
        response = page.request.get(src, timeout=8000)
        if not response.ok or not response.headers.get("content-type", "").lower().startswith("image/"):
            return ""
        return ocr_image_bytes(response.body(), executable=executable)
    except Exception:
        return ""


def extract_messages(page, room, conn=None, ocr_budget=None):
    items = []
    ocr_executable = find_tesseract()
    if ocr_budget is None:
        ocr_budget = [8]
    for row in page.query_selector_all(SEL_MSG):
        text = "\n".join(element.inner_text().strip() for element in row.query_selector_all(SEL_TEXT)).strip()
        media = []
        image_sources = []
        for element in row.query_selector_all(SEL_IMAGE):
            src = element.evaluate("e => e.currentSrc || e.getAttribute('src') || ''")
            if src:
                full_src = urljoin(page.url, src)
                image_sources.append(full_src)
                media.append({"type": "image", "src": full_src})
        for element in row.query_selector_all(".bubble video, .bubble audio, .bubble a[href]"):
            tag = element.evaluate("e => e.tagName.toLowerCase()")
            src = element.evaluate("e => e.currentSrc || e.src || e.href || ''")
            if src:
                media.append({"type": {"video": "video", "audio": "audio", "a": "link"}[tag], "src": urljoin(page.url, src)})
        if not text and not media:
            continue

        raw_time = sender = ""
        meta = row.query_selector(SEL_META)
        if meta:
            time_element = meta.query_selector("time")
            if time_element:
                raw_time = (time_element.get_attribute("datetime") or time_element.inner_text() or "").strip()
            sender = (meta.inner_text() or "").split("\n")[0].strip()
        published_at, rule = parse_time(raw_time)

        # Keep the same source key before/after OCR; OCR text is content, not identity.
        identity_content = text or "[媒体消息无正文，待解析]"
        fingerprint = identity_content if not media else identity_content + json.dumps(media, ensure_ascii=False, sort_keys=True)
        key = "%s|%s|%s" % (
            room["room_key"],
            published_at or ("raw:" + (raw_time or "null")),
            hashlib.md5(fingerprint.encode("utf-8")).hexdigest(),
        )

        previous_content = None
        previous_parse_status = ""
        if conn is not None:
            previous = conn.execute("SELECT payload FROM outbox WHERE source_key=?", (key,)).fetchone()
            if previous:
                try:
                    previous_payload = json.loads(previous[0])
                    previous_parse_status = str(previous_payload.get("parse_status") or "")
                    if previous_parse_status == "ocr_done":
                        previous_content = str(previous_payload.get("content") or "")
                except (TypeError, ValueError):
                    pass

        ocr_texts = []
        if previous_content is not None:
            content = previous_content
        else:
            if ocr_executable and ocr_budget and ocr_budget[0] > 0:
                for image_src in image_sources:
                    if ocr_budget[0] <= 0 or len(ocr_texts) >= 3:
                        break
                    ocr_budget[0] -= 1
                    recognized = _ocr_image_from_page(page, image_src, ocr_executable)
                    if recognized:
                        ocr_texts.append(recognized)
            if ocr_texts:
                content = (text + "\n" if text else "") + "[图片文字 OCR]\n" + "\n".join(ocr_texts)
            else:
                # Pure image messages remain in the local outbox even when OCR is unavailable.
                content = identity_content

        types = {item["type"] for item in media}
        if "image" in types:
            parse_status = "ocr_done" if previous_content is not None or ocr_texts else "ocr_pending"
        else:
            parse_status = "link_pending" if types == {"link"} else ("attachment_pending" if media else "text_ok")
        items.append({
            "item_type": "chat", "source_key": key, "source": "MX", "room": room["name"],
            "room_key": room["room_key"], "author": sender[:80], "title": content[:60],
            "content": content, "published_at": published_at, "raw_time": raw_time, "time_rule": rule,
            "collected_at": now_iso(), "parse_status": parse_status, "media": media,
        })
    return items

def load_history(page, loaded_all, conn, room, ocr_budget=None):
    # 每次入房间都补查，不能因过去追平而忽略断线期间的消息。
    # 每页先持久化，下一页失败也不丢已经读取的正文。
    if ocr_budget is None:
        ocr_budget = [24]
    enqueue(conn, extract_messages(page, room, conn, ocr_budget))
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
                enqueue(conn, extract_messages(page, room, conn, ocr_budget))
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
                rooms = discover_rooms(page, conn)
                total = len(rooms)
                unmapped = 0
                duplicates = sum(1 for room in rooms if room["is_dup"])
                if not rooms:
                    last_error = "未读到房间列表，请人工核对登录状态。"
                save_status(conn, "collector", {"status": "collecting", "stage": "scanning", "rooms_total": total,
                            "rooms_done": 0, "backlog": total, "rooms_unmapped": unmapped, "rooms_duplicate": duplicates})
                for room in rooms:
                    progress = conn.execute("SELECT loaded_all FROM rooms WHERE room_key=?", (room["room_key"],)).fetchone()
                    loaded_all = bool(progress[0]) if progress else False
                    try:
                        click_room(page, room)
                    except Exception as error:
                        last_error = "房间切换失败（%s），保留采集断点。" % type(error).__name__
                        unmapped += 1
                        consecutive_failures += 1
                        print(last_error)
                        save_status(conn, "collector", {"status": "collecting", "stage": "room_switch_failed",
                                    "rooms_total": total, "rooms_done": done, "backlog": total - history_done,
                                    "rooms_unmapped": unmapped, "rooms_duplicate": duplicates, "last_error": last_error})
                        if consecutive_failures >= 3:
                            print("连续3次切换失败，暂停本轮，60秒后自动重试；请检查MX登录状态。", flush=True)
                            break
                        continue
                    consecutive_failures = 0
                    page.wait_for_timeout(1200)
                    ocr_budget = [24]  # Bound local OCR work per room scan; completed images are cached in SQLite.
                    loaded_all = load_history(page, loaded_all, conn, room, ocr_budget)
                    enqueue(conn, extract_messages(page, room, conn, ocr_budget))
                    conn.execute("INSERT OR REPLACE INTO rooms (room_key, name, loaded_all, is_dup, updated_at) VALUES (?,?,?,?,?)",
                                 (room["room_key"], room["name"], int(loaded_all), int(room["is_dup"]), now_iso()))
                    conn.commit()
                    done += 1
                    history_done += int(loaded_all)
                    backlog += int(not loaded_all)
                    save_status(conn, "collector", {"status": "collecting", "stage": "scanning", "rooms_total": total,
                                "rooms_done": done, "backlog": total - history_done,
                                "rooms_unmapped": unmapped, "rooms_duplicate": duplicates, "last_error": last_error})
                    pending = conn.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0]
                    print("采集进度：本轮 %d/%d 房间；本地待上传 %d 条；当前房间历史%s。" %
                          (done, total, pending, "已追平" if loaded_all else "待回补"))
            except Exception as error:
                # 不输出请求头、口令、完整响应正文或浏览器会话。
                last_error = "本轮异常：%s；已入队数据保留。" % type(error).__name__
                print(last_error)
            save_status(conn, "collector", {"status": "collecting" if done else "failed", "stage": "round_complete",
                        "rooms_total": total, "rooms_done": done, "backlog": total - history_done,
                        "rooms_unmapped": unmapped, "rooms_duplicate": duplicates, "last_error": last_error})
            time.sleep(SCAN_INTERVAL)


def main():
    check_config()
    print("采集器版本：2026-10-28-recovery（断线重试与记录更新版）")
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
