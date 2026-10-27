# -*- coding: utf-8 -*-
"""Startup diagnostics with in-memory credential redaction."""
from datetime import datetime
from pathlib import Path
import tempfile
import traceback

_secrets = []
_stage = "bootstrap"
_log_path = None


def remember_secret(value):
    if value and value not in _secrets:
        _secrets.append(value)


def redact(value):
    text = str(value)
    for secret in sorted(_secrets, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return text


def configure(root):
    global _log_path
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    for folder in (Path(root) / "logs", Path(tempfile.gettempdir()) / "MX-startup-logs"):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            candidate = folder / ("startup-" + stamp + ".log")
            candidate.write_text("MX startup diagnostics\n", encoding="utf-8")
            _log_path = candidate
            return candidate
        except OSError:
            continue
    _log_path = None
    return None


def record(text):
    if _log_path:
        try:
            with _log_path.open("a", encoding="utf-8") as file:
                file.write(redact(text) + "\n")
        except OSError:
            pass


def mark_stage(stage):
    global _stage
    _stage = stage
    record("STAGE: " + stage)


def report_error(error, fatal=True):
    record("%s: %s\n%s" % ("FATAL" if fatal else "WARNING", _stage, traceback.format_exc()))
    print(redact("启动阶段：%s；错误：%s；%s" % (_stage, type(error).__name__, error)), flush=True)
    if _log_path:
        print("错误已记录：" + str(_log_path), flush=True)


def run(entry, root):
    configure(root)
    mark_stage("bootstrap")
    try:
        entry()
        record("EXIT: 0")
        return 0
    except KeyboardInterrupt:
        record("EXIT: interrupted")
        print("已停止；已保存的本地数据保留。", flush=True)
        return 130
    except SystemExit as error:
        if error.code in (None, 0):
            return 0
        report_error(error)
        return error.code if isinstance(error.code, int) else 1
    except Exception as error:
        report_error(error)
        return 1
