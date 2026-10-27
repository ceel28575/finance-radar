# -*- coding: utf-8 -*-
"""Shared HTTP diagnostics. Never log response bodies, credentials or redirects."""
import requests


def response_state(response):
    status = response.status_code
    try:
        body = response.json()
        if not isinstance(body, dict):
            body = {}
    except (ValueError, TypeError):
        body = {}
    error = str(body.get("error", ""))
    if status == 402:
        return {"code": "quota_blocked", "message": "后端额度不足（HTTP 402）；本地采集继续，15分钟后再试上传。", "retry_after": 900, "http_status": status}, body
    if "This app is not yet available" in error:
        return {"code": "app_unavailable", "message": "应用尚不可用；需先核对Base44部署与访问设置。本地数据保留，5分钟后再试。", "retry_after": 300, "http_status": status}, body
    if status in (401, 403):
        return {"code": "auth_failed", "message": "后端鉴权未通过；请在本机核对COLLECTOR_TOKEN，不要把口令发到聊天。", "retry_after": 300, "http_status": status}, body
    if status == 429:
        try:
            wait = max(30, min(3600, int(getattr(response, "headers", {}).get("Retry-After", 60))))
        except (ValueError, TypeError):
            wait = 60
        return {"code": "rate_limited", "message": "后端限流，已暂停重试以等待恢复；本地数据保留。", "retry_after": wait, "http_status": status}, body
    if 300 <= status < 400:
        return {"code": "redirect_blocked", "message": "接口返回跳转；未向跳转地址转发口令，请核对应用地址。", "retry_after": 300, "http_status": status}, body
    if status == 503 and "COLLECTOR_TOKEN" in error:
        return {"code": "missing_config", "message": "后端尚未配置COLLECTOR_TOKEN；本地数据保留。", "retry_after": 300, "http_status": status}, body
    if status == 200:
        return {"code": "ok", "message": "接口已响应。", "retry_after": 0, "http_status": status}, body
    return {"code": "http_error", "message": "接口HTTP %s；尚未确认接入，本地数据保留。" % status, "retry_after": 60, "http_status": status}, body


def safe_post(url, token, payload, timeout=(10, 45)):
    try:
        response = requests.post(url, json=payload,
                                 headers={"Content-Type": "application/json", "x-collector-token": token},
                                 timeout=timeout, allow_redirects=False)
        return response_state(response)
    except requests.RequestException:
        return {"code": "network_error", "message": "网络连接失败或超时；本地采集继续，稍后重试。", "retry_after": 60, "http_status": None}, {}


def check_backend(config, token):
    # Empty items cannot insert data. Older app versions return 400 after successful auth.
    result, body = safe_post(config["ingest_url"], token, {"mode": "health", "items": []}, timeout=(5, 15))
    if result["code"] == "ok" and body.get("status") == "ready" and body.get("read_only") is True:
        result = dict(result, code="ready", message="后端只读检查通过；实际入库仍以逐条确认为准。")
    elif result["http_status"] == 400 and body.get("error") == "items 为空":
        result = dict(result, code="ready", message="旧版后端已响应且鉴权通过；实际入库仍以逐条确认为准。", retry_after=0)
    elif result["code"] == "ok":
        result = dict(result, code="unconfirmed", message="接口有响应，但不符合只读检查协议；未宣称接入成功。", retry_after=60)
    return result