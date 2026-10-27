# -*- coding: utf-8 -*-
"""Windows 本机配置与隐密输入；不读取环境变量口令，不写入口令文件。"""
import getpass
import json
import sys
import warnings
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from mx_diagnostics import remember_secret

ROOT = Path(__file__).resolve().parent
CST = ZoneInfo("Asia/Shanghai")  # Windows 的时区数据库由 requirements.txt 中的 tzdata 提供


def load_settings():
    filename = ROOT / "config.json"
    if not filename.exists():
        filename = ROOT / "config.example.json"
    with filename.open(encoding="utf-8-sig") as f:
        config = json.load(f)
    paths = {
        "ingest_url": "/api/functions/ingestData",
        "heartbeat_url": "/api/functions/collectorHeartbeat",
        "summary_url": "/api/functions/generateSummary",
    }
    for key, path in paths.items():
        url = urlparse(config.get(key, ""))
        if (url.scheme != "https" or not url.hostname or url.path != path
                or url.username or url.password or url.query or url.fragment):
            raise SystemExit("配置无效：%s 必须是 HTTPS 完整函数地址，路径为 %s。" % (key, path))
    origins = {urlparse(config[key]).netloc for key in paths}
    if len(origins) != 1:
        raise SystemExit("三个函数地址必须属于同一应用域名。")
    return config


def read_token():
    if "--token-stdin" in sys.argv:
        token = sys.stdin.readline().rstrip("\r\n")  # 启动器通过匿名管道传入，不经过命令行或环境变量
    else:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                token = getpass.getpass("请输入 COLLECTOR_TOKEN（不回显、不保存）：")
        except (getpass.GetPassWarning, EOFError):
            raise SystemExit("当前终端不支持安全输入，已停止；请双击 start.bat，在本机控制台运行。")
    if not token or len(token) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in token):
        raise SystemExit("口令不能为空，且应使用可打印 ASCII 字符；未保存任何口令。")
    remember_secret(token)
    return token


def load_runtime():
    return load_settings(), read_token()