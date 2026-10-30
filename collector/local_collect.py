"""Local collection only. Never starts the legacy upload worker."""
import json
import os
import threading
from datetime import datetime
from mx_runtime import ROOT, CST
from mx_collector import db, collect_loop, save_status
from cloud_sync import sync_exports, SyncError
from instance_lock import InstanceLock
from ai_pipeline import ensure_ai_schema, load_api_key, ai_worker

def export_snapshot():
    conn = db()
    try:
        days = {}
        for key, raw in conn.execute("SELECT source_key,payload FROM outbox"):
            x = json.loads(raw)
            stamp = x.get("published_at")
            if not stamp:
                continue
            try:
                day = datetime.fromisoformat(stamp.replace("Z","+00:00")).astimezone(CST).date().isoformat()
            except (ValueError, TypeError):
                continue
            if any(t in json.dumps(x,ensure_ascii=False).lower() for t in ["奇幻团课","qmxp2077"]):
                continue
            x["id"] = key
            x["source"] = x.get("room") or x.get("room_name") or x.get("source") or "MX"
            days.setdefault(day, []).append(x)
        reports_by_day = {}
        try:
            for raw_day, raw_report in conn.execute(
                "SELECT date,payload FROM ai_reports ORDER BY generated_at"
            ):
                try:
                    reports_by_day.setdefault(raw_day, []).append(json.loads(raw_report))
                except (ValueError, TypeError):
                    continue
        except Exception:
            # AI tables may not exist during the very first collector boot.
            pass
        folder = ROOT / "exports"
        folder.mkdir(exist_ok=True)
        for day in set(days) | set(reports_by_day):
            target = folder / (day + ".json")
            tmp = target.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {"items": days.get(day, []), "reports": reports_by_day.get(day, [])},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            os.replace(tmp, target)
        print(
            "Local export complete:",
            sum(map(len, days.values())),
            "items and",
            sum(map(len, reports_by_day.values())),
            "AI reports",
            flush=True,
        )
    finally:
        conn.close()

def main():
    stop = threading.Event()
    def loop():
        while not stop.is_set():
            try:
                export_snapshot()
                uploaded = sync_exports(ROOT, stop)
                if uploaded is None:
                    print("未配置云端同步：记录仅保存在本机，请从网站下载同步配置。", flush=True)
            except SyncError as error:
                print("同步未完成：", str(error), "；数据保留，下轮重试。", flush=True)
            except Exception as error:
                print("导出或同步失败（%s）：请检查网络及本机文件，数据保留，下轮重试。" % type(error).__name__, flush=True)
            if stop.wait(60):
                break
    worker = threading.Thread(target=loop,daemon=True)
    conn = db()
    ensure_ai_schema(conn)
    print("Export every 60 seconds. Cloud sync enabled when cloud-config.json is present.")
    print("双模型 AI 会在本机千问与云端豆包之间运行；API Key 仅隐藏输入并保存在本次进程内存。")
    api_key = load_api_key()
    ai_thread = threading.Thread(target=ai_worker, args=(stop, ROOT, api_key), daemon=True, name="mx-ai")
    save_status(conn, "collector", {"status": "connected", "stage": "waiting_login", "rooms_total": 0, "rooms_done": 0})
    worker.start()
    ai_thread.start()
    try:
        collect_loop(conn)
    finally:
        stop.set()
        worker.join(timeout=5)
        ai_thread.join(timeout=5)
        save_status(conn, "collector", {"status": "stopped", "stage": "stopped"})
        conn.close()
        export_snapshot()

if __name__ == "__main__":
    try:
        with InstanceLock(ROOT / "collector.lock"):
            main()
    except KeyboardInterrupt:
        print("Stopped. Local records retained.")

