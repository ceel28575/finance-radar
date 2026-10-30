# -*- coding: utf-8 -*-
"""Local Qwen preprocessing + Volcengine Doubao market analysis.

Secrets are read from the process environment or hidden console input only.
No API key is written to disk or included in logs.
"""
import getpass
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

CST = ZoneInfo("Asia/Shanghai")
OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
QWEN_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:4b")
ARK_URL = "https://ark.cn-beijing.volces.com/api/v3/responses"
ARK_MODEL = os.environ.get("ARK_MODEL", "doubao-seed-evolving")
MAX_QWEN_ITEMS = 6
MAX_DAILY_CONTEXT = 40
MAX_ACTION_SCAN = 3000
PROMPT_VERSION = "trade-actions-v3"
API_KEY_ERROR = ""


class AIError(RuntimeError):
    """Safe, user-facing AI error; never contains credentials or raw request data."""


def now_bj():
    return datetime.now(CST)


def iso_now():
    return now_bj().isoformat(timespec="seconds")


def ensure_ai_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS ai_item_analysis (
        source_key TEXT PRIMARY KEY,
        day TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        source_json TEXT NOT NULL,
        analysis_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ai_item_analysis_day ON ai_item_analysis(day, updated_at);
    CREATE TABLE IF NOT EXISTS ai_reports (
        report_id TEXT PRIMARY KEY,
        date TEXT NOT NULL,
        generated_at TEXT NOT NULL,
        payload TEXT NOT NULL,
        fingerprint TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ai_reports_day ON ai_reports(date, generated_at);
    CREATE TABLE IF NOT EXISTS ai_daily_pool (
        pool_date TEXT PRIMARY KEY,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS ai_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """)
    conn.commit()


def load_api_key():
    global API_KEY_ERROR
    API_KEY_ERROR = ""
    key = os.environ.get("ARK_API_KEY", "").strip()
    if not key:
        try:
            key = getpass.getpass("请输入豆包 API Key（隐藏输入，仅保存在本次运行内存）：").strip()
        except (EOFError, KeyboardInterrupt):
            return ""
    if not key:
        return ""
    # Validate the exact key through the exact transport used by production calls.
    # This catches terminal paste/input issues before the long-running worker starts.
    try:
        _verify_doubao_key(key)
    except AIError as error:
        API_KEY_ERROR = str(error)[:240]
        print("豆包 API Key 启动验证失败：%s" % error, flush=True)
        return ""
    print("豆包 API Key 启动验证成功；后续研判复用同一认证方式。", flush=True)
    return key


def _state_get(conn, key):
    row = conn.execute("SELECT value FROM ai_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _state_set(conn, key, value):
    conn.execute(
        "INSERT INTO ai_state(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value),
    )
    conn.commit()


def _status(conn, status, message, last_run=None, last_report=None, details=None):
    payload = {
        "status": status,
        "provider": "本地千问 + 云端豆包",
        "qwen_model": QWEN_MODEL,
        "doubao_model": ARK_MODEL,
        "message": str(message)[:240],
        "last_run_at": last_run or iso_now(),
        "last_report_at": last_report,
    }
    if isinstance(details, dict):
        payload.update(details)
    _state_set(conn, "status", payload)
    return payload


def _ollama_runtime_status():
    try:
        response = requests.get(OLLAMA_BASE + "/api/ps", timeout=(1, 3))
        response.raise_for_status()
        models = response.json().get("models", [])
        model = next(
            (row for row in models if str(row.get("name") or row.get("model") or "").split(":")[0] == QWEN_MODEL.split(":")[0]),
            None,
        )
        if model is None:
            return {"ollama_gpu_status": "not_loaded", "ollama_vram_bytes": 0}
        vram = max(0, int(model.get("size_vram", 0) or 0))
        return {
            "ollama_gpu_status": "gpu" if vram > 0 else "cpu",
            "ollama_vram_bytes": vram,
        }
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        return {"ollama_gpu_status": "unavailable", "ollama_vram_bytes": 0}


def _blocked(item):
    text = " ".join(str(item.get(k, "")) for k in ("source", "room", "author", "content"))
    return bool(re.search(r"奇幻团课|qmxp2077", text, re.I))


def _day_of(stamp):
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone(CST).date().isoformat()
    except (TypeError, ValueError):
        return ""


def _trade_action_signals(text):
    """Expose explicit buy/low-entry and high-sell/sell language to both models."""
    compact = re.sub(r"\s+", "", str(text or ""))
    signals = []
    patterns = (
        ("bull", re.compile(r"低吸|买入|买进|买")),
        ("bear", re.compile(r"高抛|卖出|卖")),
    )
    negation = re.compile(
        r"(?:不|别|不要|暂不|暂时不|并不|并非|不是|无需|不宜|不建议|没必要|"
        r"没有必要|不能|禁止|未到|尚未).{0,3}$"
    )
    for direction, pattern in patterns:
        for match in pattern.finditer(compact):
            start, end = match.span()
            phrase = match.group(0)
            around = compact[max(0, start - 2):min(len(compact), end + 2)]
            prefix = compact[max(0, start - 10):start]
            suffix = compact[end:end + 10]
            # Market-flow, price/volume and inability phrases are not explicit
            # recommendations to buy or sell the named security.
            if "买卖" in around or compact[end:end + 1] == "方":
                continue
            if direction == "bull" and re.match(
                r"(?:盘|方|价|成本|金额|量|额|资金|订单|需求|意愿|"
                r"不(?:到|起|动|出|了|进)|难以|无法)", suffix
            ):
                continue
            if direction == "bear" and re.match(
                r"(?:盘|方|价|成本|金额|量|额|资金|订单|需求|意愿|"
                r"不(?:到|起|动|出|了)|难以|无法)", suffix
            ):
                continue
            negated = bool(negation.search(prefix)) or bool(
                re.match(r"(?:条件未到|时机未到|还不能|暂缓)", suffix)
            )
            signals.append({
                "direction": direction,
                "phrase": phrase,
                "negated": negated,
                "context": compact[max(0, start - 16):min(len(compact), end + 16)],
            })
    return signals[:24]


def _fingerprint(item):
    stable = {k: v for k, v in item.items() if k not in ("collected_at",)}
    stable["_analysis_prompt_version"] = PROMPT_VERSION
    raw = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _extract_json(text):
    text = re.sub(r"<think>.*?</think>", "", str(text), flags=re.I | re.S).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise AIError("本地模型没有返回可解析的 JSON；本轮原文保留，等待下轮重试。")
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        raise AIError("本地模型返回格式不完整；本轮原文保留，等待下轮重试。")
    if not isinstance(value, dict):
        raise AIError("本地模型返回格式不正确；本轮原文保留。")
    return value


def _source_view(source_key, item):
    media = item.get("media") if isinstance(item.get("media"), list) else []
    return {
        "id": source_key,
        "room": str(item.get("room") or item.get("source") or "MX")[:100],
        "author": str(item.get("author") or "")[:80],
        "published_at": str(item.get("published_at") or "")[:40],
        "content": str(item.get("content") or "")[:900],
        "trade_action_signals": _trade_action_signals(item.get("content")),
        "media_types": sorted({str(x.get("type")) for x in media if isinstance(x, dict) and x.get("type")}),
        "image_ocr_pending": bool(item.get("parse_status") == "ocr_pending"),
    }


def _load_today_sources(conn, today):
    rows = conn.execute(
        "SELECT source_key,payload FROM outbox ORDER BY created_at DESC LIMIT 3000"
    ).fetchall()
    result = []
    for source_key, raw in rows:
        try:
            item = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if _blocked(item) or _day_of(item.get("published_at")) != today:
            continue
        content = str(item.get("content") or "").strip()
        if not content or content.startswith("[媒体消息无正文"):
            continue
        result.append((source_key, item))
    result.sort(key=lambda pair: str(pair[1].get("published_at") or ""))
    return result


def _call_qwen(items):
    request_items = [
        {
            "id": source_key,
            "room": str(item.get("room") or item.get("source") or "MX")[:80],
            "author": str(item.get("author") or "")[:60],
            "published_at": str(item.get("published_at") or "")[:40],
            "content": str(item.get("content") or "")[:1400],
            "trade_action_signals": _trade_action_signals(item.get("content")),
            "has_images": any(
                isinstance(media, dict) and media.get("type") == "image"
                for media in (item.get("media") or [])
            ),
        }
        for source_key, item in items
    ]
    system = (
        "你是财经资讯预处理器。只做原文归类、去重线索和事实提取，不给投资建议。"
        "不得补造公司、数字、客户、订单或结论。股票和公司尽量写中文全称，不输出六位股票代码。"
        "严格返回 JSON 对象，不要 Markdown，不要解释。"
        '格式：{"items":[{"id":"原样ID","relevance":0,"category":"行业/公司/宏观/市场/无关",'
        '"companies":[],"stance":"bullish|bearish|neutral|mixed",'
        '"summary":"一句话摘要","facts":[],"catalysts":[],"risks":[]}] }。'
        "每条输入必须返回且 id 必须逐字一致。relevance 为 0 到 5；无关信息为 0。"
        "交易方向按原文操作意图判断，不要求出现“看多/看空”字样：针对具体标的出现“买/买入/低吸”标 bullish；出现“高抛/卖/卖出”标 bearish。\n"
        "识别否定表达（如不要买、不建议买、不卖、暂不卖）时不要按关键词机械归类；“高抛低吸”等同句双向策略标 mixed，除非上下文能明确对应到具体标的的单一操作。\n"
        "“买方/卖方/买卖双方”等非操作建议不作为交易信号；公司名称与动作必须能从原文对应。证据不足用 neutral。"
    )
    body = {
        "model": QWEN_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps({"items": request_items}, ensure_ascii=False)},
        ],
        "think": False,
        "format": "json",
        "stream": False,
        "keep_alive": "10m",
        "options": {"temperature": 0.1, "num_ctx": 8192, "num_predict": 1800},
    }
    try:
        response = requests.post(OLLAMA_BASE + "/api/chat", json=body, timeout=(4, 150))
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException:
        raise AIError("本地千问连接失败；请确认 Ollama 正在运行且 qwen3.5:4b 已安装。")
    except ValueError:
        raise AIError("本地千问返回了无效响应；原始资讯仍保留。")
    message = payload.get("message") if isinstance(payload, dict) else None
    answer = message.get("content", "") if isinstance(message, dict) else ""
    if not str(answer).strip():
        raise AIError("本地千问没有生成正文；已改用 /api/chat + think=false，仍需检查 Ollama 模型状态。")
    parsed = _extract_json(answer)
    returned = parsed.get("items") if isinstance(parsed.get("items"), list) else []
    by_id = {
        str(row.get("id")): row for row in returned
        if isinstance(row, dict) and row.get("id") is not None
    }
    normalized = {}
    for source_key, item in items:
        row = by_id.get(source_key)
        if not row:
            row = {
                "relevance": 1,
                "category": "待复核",
                "companies": [],
                "stance": "neutral",
                "summary": str(item.get("content") or "")[:360],
                "facts": [],
                "catalysts": [],
                "risks": [],
                "local_parse_warning": True,
            }
        def text_list(value, limit=8):
            if not isinstance(value, list):
                return []
            return [str(x).strip()[:240] for x in value[:limit] if str(x).strip()]
        try:
            relevance = max(0, min(5, int(row.get("relevance", 1))))
        except (TypeError, ValueError):
            relevance = 1
        stance = row.get("stance")
        if stance not in ("bullish", "bearish", "neutral", "mixed"):
            stance = "neutral"
        normalized[source_key] = {
            "id": source_key,
            "relevance": relevance,
            "category": str(row.get("category") or "待复核")[:40],
            "companies": text_list(row.get("companies")),
            "stance": stance,
            "summary": str(row.get("summary") or "")[:500],
            "facts": text_list(row.get("facts")),
            "catalysts": text_list(row.get("catalysts")),
            "risks": text_list(row.get("risks")),
            "local_parse_warning": bool(row.get("local_parse_warning", False)),
        }
    return normalized


def _curl_quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "") + '"'


def _curl_post_json(url, api_key, payload, timeout=120):
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            temp_path = handle.name
        body_path = temp_path.replace("\\", "/")
        config = "\n".join([
            "url = " + _curl_quote(url),
            'request = "POST"',
            "header = " + _curl_quote("Authorization: Bearer " + api_key),
            'header = "Content-Type: application/json"',
            "data-binary = " + _curl_quote("@" + body_path),
            "max-time = " + str(int(timeout)),
            "ipv4",
            "silent",
            "show-error",
            "write-out = " + _curl_quote("\\n__HTTP_STATUS__:%{http_code}\\n"),
            "",
        ])
        try:
            proc = subprocess.run(
                ["curl.exe", "--config", "-"],
                input=config,
                text=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout + 10,
                check=False,
            )
        except FileNotFoundError:
            try:
                response = requests.post(
                    url,
                    headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                    json=payload,
                    timeout=(10, timeout),
                )
                response.raise_for_status()
                return response.json()
            except requests.RequestException:
                raise AIError("豆包 HTTPS 请求失败；请检查网络后重试。")
        except subprocess.TimeoutExpired:
            raise AIError("豆包请求超时；本地采集继续，稍后自动重试。")
        marker = "__HTTP_STATUS__:"
        index = proc.stdout.rfind(marker)
        if index < 0:
            raise AIError("豆包连接未返回 HTTP 状态；本地数据已保留。")
        body_text = proc.stdout[:index].strip()
        status_text = proc.stdout[index + len(marker):].strip().splitlines()[0]
        try:
            status_code = int(status_text)
        except ValueError:
            raise AIError("豆包返回状态无法识别；本地数据已保留。")
        if status_code < 200 or status_code >= 300:
            error_code = ""
            try:
                error_payload = json.loads(body_text)
                error_code = str((error_payload.get("error") or {}).get("code") or error_payload.get("code") or "")
            except (ValueError, AttributeError, TypeError):
                pass
            suffix = "，错误码 " + error_code if error_code else ""
            raise AIError("豆包 API 返回 HTTP %d%s；请核对模型接入点与账户额度。" % (status_code, suffix))
        try:
            return json.loads(body_text)
        except ValueError:
            raise AIError("豆包返回内容不是有效 JSON；原始资讯仍保留。")
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _doubao_text(response):
    if isinstance(response.get("output_text"), str) and response["output_text"].strip():
        return response["output_text"].strip()
    chunks = []
    for part in response.get("output", []) if isinstance(response.get("output"), list) else []:
        if not isinstance(part, dict) or part.get("type") != "message":
            continue
        for item in part.get("content", []) if isinstance(part.get("content"), list) else []:
            if isinstance(item, dict) and item.get("type") in ("output_text", "text"):
                value = item.get("text")
                if isinstance(value, str) and value.strip():
                    chunks.append(value.strip())
    if not chunks:
        raise AIError("豆包接口已响应，但没有返回可用的分析正文。")
    return "\n".join(chunks)


def _verify_doubao_key(api_key):
    """Preflight the exact endpoint, model, auth header, and curl transport once."""
    payload = {
        "model": ARK_MODEL,
        "input": [{
            "role": "user",
            "content": [{"type": "input_text", "text": "仅回复 OK"}],
        }],
        "max_output_tokens": 128,
    }
    response = _curl_post_json(ARK_URL, api_key, payload, timeout=45)
    # A 2xx without usable model output is not a successful preflight.
    _doubao_text(response)


def _clean_company_name(value):
    name = str(value or "").strip()
    name = re.sub(
        r"[（(]?\s*(?<!\d)\d{6}(?:\.(?:SZ|SH|BJ))?(?!\d)\s*[）)]?",
        "",
        name,
        flags=re.I,
    )
    name = re.sub(r"\s+", " ", name).strip(" -_()（）[]【】")
    return name


def _normalize_stocks(value, allowed_ids):
    result = []
    if not isinstance(value, list):
        return result
    for row in value[:40]:
        if not isinstance(row, dict):
            continue
        name = _clean_company_name(row.get("name"))
        if not name or len(name) > 100:
            continue
        direction = row.get("direction")
        if direction in ("看多", "偏多"):
            direction = "bull"
        elif direction in ("看空", "偏空"):
            direction = "bear"
        if direction not in ("bull", "bear"):
            continue
        reason = str(row.get("reason") or "").strip()[:2000]
        if not reason:
            continue
        evidence = row.get("evidence_ids") if isinstance(row.get("evidence_ids"), list) else []
        evidence = [str(x) for x in evidence if str(x) in allowed_ids][:12]
        if not evidence:
            continue
        try:
            score = max(0, min(100, int(float(row.get("score", 50)))))
        except (TypeError, ValueError):
            score = 50
        result.append({
            "name": name,
            "direction": direction,
            "score": score,
            "sector": str(row.get("sector") or "待归类")[:80],
            "reason": reason,
            "catalyst": str(row.get("catalyst") or "")[:1000],
            "risk": str(row.get("risk") or "")[:1000],
            "invalidation": str(row.get("invalidation") or "")[:1000],
            "change": str(row.get("change") or "")[:1000],
            "evidence_ids": evidence,
        })
    by_name = {}
    conflicts = set()
    for row in result:
        name = row["name"]
        previous = by_name.get(name)
        if previous is None:
            by_name[name] = row
        elif previous["direction"] != row["direction"]:
            conflicts.add(name)
        else:
            previous["score"] = max(previous["score"], row["score"])
            previous["evidence_ids"] = list(dict.fromkeys(
                previous["evidence_ids"] + row["evidence_ids"]
            ))[:12]
    return [row for name, row in by_name.items() if name not in conflicts]


def _call_doubao(api_key, today, generated_at, analyses, previous_report, action_candidates=None):
    action_candidates = action_candidates or []
    allowed_ids = {str(row["source"]["id"]) for row in analyses}
    allowed_ids.update(
        str(evidence_id)
        for candidate in action_candidates
        for evidence_id in candidate.get("evidence_ids", [])
    )
    system = (
        "你是A股财经研究分析师。只能根据提供的原始资讯和本地千问提取结果进行研判。"
        "本地千问结果只是线索，必须回看原文；不能把未经证实的传闻写成事实。"
        "不允许虚构股价、订单、客户、产能、业绩数字或公告。证据不足时明确写证据不足。"
        "指数看多概率是主观情景判断，不是统计胜率。股票名称只写公司中文名称，不写六位代码。"
        "股票池按作者对具体标的的操作意图归类，不要求原文出现“看多/看空”：出现“买/买入/低吸”归看多 bull；出现“高抛/卖/卖出”归看空 bear。"
        "必须把动作和对应公司匹配；否定表达（不要买、不建议买、不卖、暂不卖等）不能按关键词机械归类；“高抛低吸”这类同句双向策略若无法拆分到具体标的操作则不入单边池。"
        "“买方/卖方/买卖双方”等非交易建议不算信号。若同一标的多条消息方向冲突，优先采用最新明确操作，并在 change 中说明。"
        "只输出一个严格 JSON 对象，不要 Markdown 或额外文字。"
        '结构：{"title":"...","summary":"...","index_reason":"...","bull_probability":0到100或null,'
        '"risk":"...","stocks":[{"name":"公司名称","direction":"bull或bear","score":0到100,'
        '"sector":"行业","reason":"有来源依据的逻辑","catalyst":"催化因素","risk":"风险",'
        '"invalidation":"观点失效条件","change":"相对前轮变化","evidence_ids":["输入中的原文ID"]}]}。'
        "股票池最多 20 只；每只必须至少引用一个输入 evidence_id；证据不足就不入池。"
        "只要原文明确出现对应标的的有效买/低吸或高抛/卖操作，就应纳入对应方向，不得因为没有出现“看多/看空”四个字而漏掉。"
        "同时给出看多和看空候选，不足的一侧可以为空。"
    )
    compact = {
        "date": today,
        "generated_at": generated_at,
        "previous_report": previous_report or {},
        "items": analyses,
        "explicit_action_candidates": action_candidates[:40],
    }
    body = {
        "model": ARK_MODEL,
        "input": [{
            "role": "user",
            "content": [{
                "type": "input_text",
                "text": system + "\n\n本轮数据：\n" + json.dumps(compact, ensure_ascii=False),
            }],
        }],
        "max_output_tokens": 4200,
    }
    response = _curl_post_json(ARK_URL, api_key, body, timeout=120)
    text = _doubao_text(response)
    parsed = _extract_json(text)
    probability = parsed.get("bull_probability")
    if probability is not None:
        try:
            probability = max(0, min(100, int(float(probability))))
        except (TypeError, ValueError):
            probability = None
    return {
        "date": today,
        "generated_at": generated_at,
        "title": str(parsed.get("title") or "A股盘面研判")[:120],
        "summary": str(parsed.get("summary") or "本轮分析未形成明确主线。")[:5000],
        "index_reason": str(parsed.get("index_reason") or "")[:3000],
        "bull_probability": probability,
        "risk": str(parsed.get("risk") or "")[:3000],
        "stocks": _normalize_stocks(parsed.get("stocks"), allowed_ids),
        "models": {"local": QWEN_MODEL, "cloud": ARK_MODEL},
    }


def _latest_report(conn, day):
    row = conn.execute(
        "SELECT payload FROM ai_reports WHERE date=? ORDER BY generated_at DESC LIMIT 1", (day,)
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except ValueError:
        return None


def _get_pool(conn, day):
    row = conn.execute("SELECT payload FROM ai_daily_pool WHERE pool_date=?", (day,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except ValueError:
        return None


def _save_report(conn, report):
    report_id = report["date"] + "|" + report["generated_at"]
    raw = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
    fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO ai_reports(report_id,date,generated_at,payload,fingerprint) VALUES(?,?,?,?,?) "
        "ON CONFLICT(report_id) DO UPDATE SET payload=excluded.payload,fingerprint=excluded.fingerprint",
        (report_id, report["date"], report["generated_at"], raw, fingerprint),
    )
    conn.commit()


def _save_pool_once(conn, day, stocks, generated_at):
    existing = _get_pool(conn, day)
    if existing is not None:
        # Even an empty pool is a daily snapshot; never change it intraday.
        return existing
    payload = {"stocks": stocks, "generated_at": generated_at}
    conn.execute(
        "INSERT OR IGNORE INTO ai_daily_pool(pool_date,payload,created_at) VALUES(?,?,?)",
        (day, json.dumps(payload, ensure_ascii=False), generated_at),
    )
    conn.commit()
    return _get_pool(conn, day) or payload


def _next_business_day(day):
    value = datetime.fromisoformat(day).date() + timedelta(days=1)
    while value.weekday() >= 5:
        value += timedelta(days=1)
    return value.isoformat()


def _is_after_close(now):
    return now.weekday() < 5 and (now.hour, now.minute) >= (15, 10)


def _load_analyses(conn, day, limit=MAX_DAILY_CONTEXT, include_irrelevant=False):
    rows = conn.execute(
        "SELECT source_key,source_json,analysis_json,fingerprint FROM ai_item_analysis "
        "WHERE day=? ORDER BY updated_at DESC LIMIT ?",
        (day, max(1, int(limit))),
    ).fetchall()
    result = []
    for source_key, source_json, analysis_json, fingerprint in reversed(rows):
        try:
            source = json.loads(source_json)
            analysis = json.loads(analysis_json)
        except ValueError:
            continue
        if not include_irrelevant and int(analysis.get("relevance", 0) or 0) <= 0:
            continue
        result.append({"source": source, "local_analysis": analysis})
    return result


def _action_based_candidates(analyses):
    """Build evidence-backed candidates when a company and explicit action share a clause."""
    grouped = {}
    for row in analyses:
        source = row.get("source") or {}
        local = row.get("local_analysis") or {}
        content = str(source.get("content") or "")
        companies = local.get("companies") if isinstance(local.get("companies"), list) else []
        companies = list(dict.fromkeys(
            _clean_company_name(name) for name in companies
            if isinstance(name, str) and 2 <= len(_clean_company_name(name)) <= 40
        ))
        if not content or not companies:
            continue
        clauses = [part.strip() for part in re.split(r"[，,；;。！？!?\n]+", content) if part.strip()]
        for clause in clauses:
            signals = [signal for signal in _trade_action_signals(clause) if not signal["negated"]]
            directions = {signal["direction"] for signal in signals}
            if len(directions) != 1:
                continue
            matched = list(dict.fromkeys(name for name in companies if name in clause))
            if len(matched) != 1:
                continue
            name = matched[0]
            direction = next(iter(directions))
            evidence_id = str(source.get("id") or "")
            if not evidence_id:
                continue
            phrases = list(dict.fromkeys(signal["phrase"] for signal in signals))
            phrase_text = "、".join(phrases)
            published_at = str(source.get("published_at") or "")
            try:
                relevance = max(0, min(5, int(float(local.get("relevance", 0) or 0))))
            except (TypeError, ValueError):
                relevance = 0
            candidate = {
                "name": name,
                "direction": direction,
                "score": max(60, min(85, 60 + 5 * relevance)),
                "sector": "待归类",
                "reason": "原文明确出现操作词“%s”，按交易意图规则归入%s。原文片段：%s" % (
                    phrase_text, "看多" if direction == "bull" else "看空", clause[:260]
                ),
                "catalyst": str(local.get("summary") or "")[:500],
                "risk": "这是原文操作意图归类，不代表独立核实的投资结论；请结合其他来源与风险判断。",
                "invalidation": "出现更新的相反操作信号或原有逻辑失效时重新评估。",
                "change": "按原文交易动作识别：" + phrase_text,
                "evidence_ids": [evidence_id],
                "_signal_at": published_at,
            }
            grouped.setdefault(name, []).append(candidate)

    result = []
    for name, candidates in grouped.items():
        latest_at = max(candidate["_signal_at"] for candidate in candidates)
        latest = [candidate for candidate in candidates if candidate["_signal_at"] == latest_at]
        directions = {candidate["direction"] for candidate in latest}
        if len(directions) != 1:
            continue
        chosen = dict(latest[-1])
        chosen["evidence_ids"] = list(dict.fromkeys(
            evidence for candidate in latest for evidence in candidate["evidence_ids"]
        ))[:12]
        chosen.pop("_signal_at", None)
        result.append(chosen)
    return sorted(result, key=lambda item: (-item["score"], item["name"]))


def _merge_action_candidates(model_stocks, action_candidates):
    """Keep every evidence-backed explicit action ahead of model-only candidates."""
    actions = [
        dict(row) for row in (action_candidates or [])
        if isinstance(row, dict) and row.get("name")
    ]
    action_names = {row["name"] for row in actions}
    model_only = [
        dict(row) for row in (model_stocks or [])
        if isinstance(row, dict) and row.get("name") not in action_names
    ]
    return (actions + model_only)[:40]


def worker_delay(result=None, failed=False, now=None):
    if failed:
        return 120
    if (result or {}).get("retry_soon") or int((result or {}).get("pending_remaining", 0) or 0) > 0:
        return 5
    return next_run_delay(now)


def _collector_round_complete(conn, today, now=None):
    try:
        row = conn.execute(
            "SELECT value FROM runtime_status WHERE scope='collector'"
        ).fetchone()
    except sqlite3.Error:
        return False
    if not row:
        return False
    try:
        status = json.loads(row[0])
        completed_at = datetime.fromisoformat(
            str(status.get("updated_at") or "").replace("Z", "+00:00")
        ).astimezone(CST)
    except (TypeError, ValueError):
        return False
    now = now or now_bj()
    age_seconds = (now - completed_at).total_seconds()
    # Never use yesterday's completed scan to freeze today's pool. The collector
    # normally scans every minute, so a three-minute freshness window tolerates
    # scheduling jitter without accepting stale or future-dated status.
    if (
        status.get("stage") != "round_complete"
        or status.get("status") != "collecting"
        or completed_at.date().isoformat() != today
        or age_seconds < -30
        or age_seconds > 180
        or status.get("last_error")
    ):
        return False
    total = int(status.get("rooms_total", 0) or 0)
    done = int(status.get("rooms_done", 0) or 0)
    backlog = int(status.get("backlog", 0) or 0)
    unmapped = int(status.get("rooms_unmapped", 0) or 0)
    return total > 0 and done >= total and backlog == 0 and unmapped == 0


def run_ai_cycle(root, api_key):
    root = Path(root)
    conn = sqlite3.connect(str(root / "mx_outbox.db"), timeout=30)
    try:
        ensure_ai_schema(conn)
        now = now_bj()
        today = now.date().isoformat()
        generated_at = now.isoformat(timespec="seconds")
        sources = _load_today_sources(conn, today)
        pending = []
        for source_key, item in sources:
            digest = _fingerprint(item)
            old = conn.execute(
                "SELECT fingerprint FROM ai_item_analysis WHERE source_key=?", (source_key,)
            ).fetchone()
            if not old or old[0] != digest:
                pending.append((source_key, item, digest))

        if pending:
            batch = pending[:MAX_QWEN_ITEMS]
            qwen_input = [(key, item) for key, item, _ in batch]
            qwen_results = _call_qwen(qwen_input)
            for source_key, item, digest in batch:
                source = _source_view(source_key, item)
                analysis = qwen_results[source_key]
                conn.execute(
                    "INSERT INTO ai_item_analysis(source_key,day,fingerprint,source_json,analysis_json,updated_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET day=excluded.day,"
                    "fingerprint=excluded.fingerprint,source_json=excluded.source_json,"
                    "analysis_json=excluded.analysis_json,updated_at=excluded.updated_at",
                    (
                        source_key, today, digest,
                        json.dumps(source, ensure_ascii=False),
                        json.dumps(analysis, ensure_ascii=False),
                        generated_at,
                    ),
                )
            conn.commit()
            print("本地千问完成：%d 条新增/变更资讯。" % len(batch), flush=True)

        runtime_status = _ollama_runtime_status()
        pending_remaining = max(0, len(pending) - (len(batch) if pending else 0))
        if pending_remaining:
            _status(
                conn, "processing",
                "本地千问正在分批处理当日资讯，当前队列尚余 %d 条；处理完成后再生成完整股票池。" % pending_remaining,
                generated_at,
                details=runtime_status,
            )
            return {
                "status": "processing",
                "analysed": len(sources) - pending_remaining,
                "report": False,
                "pending_remaining": pending_remaining,
            }

        all_analyses = _load_analyses(
            conn, today, limit=MAX_ACTION_SCAN, include_irrelevant=True
        )
        relevant_analyses = [
            row for row in all_analyses
            if int((row.get("local_analysis") or {}).get("relevance", 0) or 0) > 0
        ]
        analyses = relevant_analyses[-MAX_DAILY_CONTEXT:]
        action_candidates = _action_based_candidates(all_analyses)
        after_close = _is_after_close(now)
        next_day = _next_business_day(today)
        next_pool = _get_pool(conn, next_day)
        need_daily_pool = after_close and next_pool is None
        if need_daily_pool and not _collector_round_complete(conn, today, now):
            _status(
                conn, "processing",
                "等待 MX 完成当日全部房间扫描和历史回补；完成后再生成并冻结次日股票池。",
                generated_at,
                details=runtime_status,
            )
            return {
                "status": "processing",
                "analysed": len(all_analyses),
                "report": False,
                "pending_remaining": 0,
                "retry_soon": True,
            }

        if not analyses and not action_candidates:
            empty_digest = "empty:" + PROMPT_VERSION
            already_reported_empty = _state_get(conn, "report_digest:" + today) == empty_digest
            if after_close and not already_reported_empty:
                if _get_pool(conn, today) is None:
                    _save_pool_once(conn, today, [], generated_at)
                if next_pool is None:
                    _save_pool_once(conn, next_day, [], generated_at)
                empty_report = {
                    "date": today,
                    "generated_at": generated_at,
                    "title": "今日未识别到明确买卖操作",
                    "summary": "已完成当日可分析文字资讯处理，但没有找到可与具体公司对应的有效买入、低吸、高抛或卖出信号。",
                    "index_reason": "",
                    "bull_probability": None,
                    "risk": "空股票池表示当前证据不足，不代表市场没有机会。",
                    "stocks": [],
                    "models": {"local": QWEN_MODEL, "cloud": ARK_MODEL},
                }
                _save_report(conn, empty_report)
                _save_report(conn, dict(
                    empty_report, date=next_day, title="次日预备股票池",
                    summary="根据 " + today + " 的完整资讯处理结果，暂未发现可与具体公司对应的明确交易动作。",
                ))
                _state_set(conn, "report_digest:" + today, empty_digest)
            _status(conn, "ready", "已完成文字资讯处理；当前没有足够证据识别明确的个股买卖动作。", generated_at, details=runtime_status)
            return {
                "status": "ready", "analysed": len(all_analyses),
                "report": after_close and not already_reported_empty, "pending_remaining": 0,
            }

        digest_source = json.dumps(
            {
                "prompt_version": PROMPT_VERSION,
                "items": [(x["source"]["id"], x["local_analysis"]) for x in analyses],
                "action_candidates": action_candidates,
            },
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
        last_digest = _state_get(conn, "report_digest:" + today)
        if digest == last_digest and not need_daily_pool:
            _status(conn, "ready", "模型连接正常；本轮没有新的有效资讯变化。", generated_at, details=runtime_status)
            return {
                "status": "ready", "analysed": len(all_analyses),
                "report": False, "pending_remaining": 0,
            }

        previous = _latest_report(conn, today) or {}
        model_report = _call_doubao(
            api_key, today, generated_at, analyses, previous, action_candidates
        )
        model_report["stocks"] = _merge_action_candidates(
            model_report.get("stocks", []), action_candidates
        )

        # Freeze only after the Qwen backlog is fully drained. Empty is a valid
        # daily snapshot and remains empty for the rest of that trading day.
        today_pool = _get_pool(conn, today)
        if today_pool is None and after_close:
            today_pool = _save_pool_once(conn, today, model_report.get("stocks", []), generated_at)
        today_stocks = (
            today_pool.get("stocks", [])
            if today_pool is not None
            else model_report.get("stocks", [])
        )
        current_report = dict(model_report, date=today, stocks=today_stocks)
        _save_report(conn, current_report)

        if need_daily_pool:
            frozen = _save_pool_once(conn, next_day, model_report["stocks"], generated_at)
            next_report = dict(
                model_report,
                date=next_day,
                title="次日预备股票池",
                summary="根据 " + today + " 全部已处理资讯生成，供下一交易日观察；盘中不随刷新改变股票池。",
                stocks=frozen.get("stocks", []),
            )
            _save_report(conn, next_report)

        _state_set(conn, "report_digest:" + today, digest)
        _status(conn, "ready", "本地千问与云端豆包均已完成本轮分析。", generated_at, generated_at, details=runtime_status)
        print(
            "豆包研判完成：看多概率 %s；当日固定股票池 %d 只；报告已排队同步。"
            % (
                str(model_report.get("bull_probability") if model_report.get("bull_probability") is not None else "暂缺"),
                len(today_stocks),
            ),
            flush=True,
        )
        return {
            "status": "ready",
            "analysed": len(all_analyses),
            "report": True,
            "pending_remaining": 0,
        }
    finally:
        conn.close()

def next_run_delay(now=None):
    now = now or now_bj()
    minute = now.hour * 60 + now.minute
    if now.weekday() < 5:
        if 9 * 60 + 30 <= minute < 11 * 60 + 30:
            return 60
        if 13 * 60 <= minute < 15 * 60:
            return 60
        if 11 * 60 + 30 <= minute < 13 * 60:
            target = now.replace(hour=13, minute=0, second=0, microsecond=0)
            return max(15, int((target - now).total_seconds()))
        if 15 * 60 <= minute < 15 * 60 + 10:
            target = now.replace(hour=15, minute=10, second=0, microsecond=0)
            return max(15, int((target - now).total_seconds()))
    return 1800


def ai_worker(stop, root, api_key):
    if not api_key:
        try:
            conn = sqlite3.connect(str(Path(root) / "mx_outbox.db"), timeout=10)
            ensure_ai_schema(conn)
            _status(
                conn,
                "degraded" if API_KEY_ERROR else "disabled",
                API_KEY_ERROR or "未提供豆包 API Key；AI 分析未启动，MX 采集与同步继续。",
                details=_ollama_runtime_status(),
            )
            conn.close()
        except sqlite3.Error:
            pass
        if API_KEY_ERROR:
            print("豆包 API Key 启动验证失败：AI 分析未启动；MX 采集与同步不受影响。", flush=True)
        else:
            print("未提供豆包 API Key：AI 分析未启动；MX 采集与同步不受影响。", flush=True)
        return
    print(
        "双模型 AI 已启动：本地 %s（Ollama /api/chat） + 云端 %s。"
        % (QWEN_MODEL, ARK_MODEL),
        flush=True,
    )
    while not stop.is_set():
        failed = False
        result = None
        try:
            result = run_ai_cycle(root, api_key)
        except AIError as error:
            failed = True
            try:
                conn = sqlite3.connect(str(Path(root) / "mx_outbox.db"), timeout=10)
                ensure_ai_schema(conn)
                _status(conn, "degraded", str(error), details=_ollama_runtime_status())
                conn.close()
            except sqlite3.Error:
                pass
            print("AI 分析未完成：%s；原始资讯保留，采集和同步继续。" % error, flush=True)
        except Exception as error:
            failed = True
            try:
                conn = sqlite3.connect(str(Path(root) / "mx_outbox.db"), timeout=10)
                ensure_ai_schema(conn)
                _status(conn, "degraded", "AI 处理异常（%s），120秒后重试。" % type(error).__name__, details=_ollama_runtime_status())
                conn.close()
            except sqlite3.Error:
                pass
            print("AI 分析暂时失败（%s）；原始资讯保留，稍后重试。" % type(error).__name__, flush=True)
        delay = worker_delay(result, failed=failed)
        if result and result.get("pending_remaining", 0):
            print("AI 预处理积压 %d 条，5秒后继续处理。" % result["pending_remaining"], flush=True)
        if stop.wait(delay):
            break
