"""Local collection only. Never starts the legacy upload worker."""
import json
import os
import threading
from datetime import datetime
from mx_runtime import ROOT, CST
from mx_collector import db, collect_loop

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
            x["source"] = x.get("room_name") or x.get("source") or "MX"
            days.setdefault(day, []).append(x)
        folder = ROOT / "exports"
        folder.mkdir(exist_ok=True)
        for day, items in days.items():
            target = folder / (day + ".json")
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps({"items":items,"reports":[]},ensure_ascii=False),encoding="utf-8")
            os.replace(tmp,target)
        print("Local export complete:", sum(map(len,days.values())), "items", flush=True)
    finally:
        conn.close()

def main():
    stop = threading.Event()
    def loop():
        while not stop.wait(60):
            try:
                export_snapshot()
            except Exception as error:
                print("Export failed:",type(error).__name__,flush=True)
    worker = threading.Thread(target=loop,daemon=True)
    conn = db()
    print("LOCAL MODE: no cloud upload, no AI calls. Export every 60 seconds.")
    print("Login manually in the browser. Keep this window open.")
    worker.start()
    try:
        collect_loop(conn)
    finally:
        stop.set()
        worker.join()
        conn.close()
        export_snapshot()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped. Local records retained.")
