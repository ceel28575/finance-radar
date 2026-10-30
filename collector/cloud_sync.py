"""Send exported records to the owner's private Site; retain failed work locally."""
import hashlib
import json
import sqlite3
from pathlib import Path

class SyncError(ValueError):
    """Safe user-facing message, never includes credentials or response bodies."""


SITE_URL = 'https://finance-radar-jenson.sunlitbison257.chatgpt.site'


def read_config(root):
    path = root / 'cloud-config.json'
    if not path.exists():
        return None
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    if config.get('site_url', '').rstrip('/') != SITE_URL:
        raise SyncError('同步网址与本项目不一致，请从网页重新下载配置')
    if not isinstance(config.get('site_token'), str) or len(config['site_token']) < 20:
        raise SyncError('同步配置缺少凭据，请从网页重新下载')
    return config


def fingerprint(item):
    item = {k: v for k, v in item.items() if k != 'collected_at'}
    return hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def sync_exports(root, stop=None, post=None):
    config = read_config(root)
    if not config:
        return None
    if post is None:
        import requests
        post = requests.post
    target = hashlib.sha256((SITE_URL + config['site_token']).encode()).hexdigest()
    conn = sqlite3.connect(root / 'mx_cloud.db', timeout=15)
    conn.execute('CREATE TABLE IF NOT EXISTS synced(target TEXT,id TEXT,hash TEXT,PRIMARY KEY(target,id))')
    uploaded = 0
    try:
        pending = []
        pending_reports = []
        latest_ai_status = None
        latest_collector_status = None
        for file in sorted((root / 'exports').glob('????-??-??.json')):
            snapshot = json.loads(file.read_text(encoding='utf-8'))
            candidate_status = snapshot.get('ai_status')
            if isinstance(candidate_status, dict):
                current_stamp = str((latest_ai_status or {}).get('last_run_at') or '')
                candidate_stamp = str(candidate_status.get('last_run_at') or '')
                if latest_ai_status is None or candidate_stamp >= current_stamp:
                    latest_ai_status = candidate_status
            candidate_collector = snapshot.get('collector_status')
            if isinstance(candidate_collector, dict):
                current_stamp = str((latest_collector_status or {}).get('updated_at') or '')
                candidate_stamp = str(candidate_collector.get('updated_at') or '')
                if latest_collector_status is None or candidate_stamp >= current_stamp:
                    latest_collector_status = candidate_collector
            for item in snapshot.get('items', []):
                digest = fingerprint(item)
                old = conn.execute('SELECT hash FROM synced WHERE target=? AND id=?', (target,item['id'])).fetchone()
                if not old or old[0] != digest:
                    pending.append((item,digest))
            for report in snapshot.get('reports', []):
                report_id = 'report:' + str(report.get('date', '')) + '|' + str(report.get('generated_at', ''))
                if report_id == 'report:|':
                    continue
                digest = fingerprint(report)
                old = conn.execute('SELECT hash FROM synced WHERE target=? AND id=?', (target,report_id)).fetchone()
                if not old or old[0] != digest:
                    pending_reports.append((report_id,report,digest))

        pending_status = None
        if latest_ai_status is not None:
            status_id = 'status:ai_pipeline'
            status_digest = fingerprint(latest_ai_status)
            old = conn.execute(
                'SELECT hash FROM synced WHERE target=? AND id=?', (target, status_id)
            ).fetchone()
            if not old or old[0] != status_digest:
                pending_status = (status_id, latest_ai_status, status_digest)
        pending_collector_status = None
        if latest_collector_status is not None:
            status_id = 'status:collector_scan'
            status_digest = fingerprint(latest_collector_status)
            old = conn.execute(
                'SELECT hash FROM synced WHERE target=? AND id=?', (target, status_id)
            ).fetchone()
            if not old or old[0] != status_digest:
                pending_collector_status = (status_id, latest_collector_status, status_digest)

        batches, batch, size = [], [], 0
        for record in pending:
            n = len(json.dumps(record[0], ensure_ascii=False).encode())
            if n > 4_000_000:
                raise SyncError('单条记录过大；仍保留在本机')
            if batch and (len(batch) >= 100 or size+n > 4_000_000):
                batches.append(batch);batch=[];size=0
            batch.append(record);size+=n
        if batch:
            batches.append(batch)
        if pending_status is not None:
            status_id, ai_status, digest = pending_status
            if stop and stop.is_set():
                return uploaded
            response = post(
                SITE_URL + '/api/radar/ingest',
                headers={'OAI-Sites-Authorization':'Bearer '+config['site_token']},
                json={'items': [], 'reports': [], 'ai_status': ai_status, 'client':'collector'},
                timeout=(10,30), allow_redirects=False,
            )
            if response.status_code in (401,403) or 300 <= response.status_code < 400:
                raise SyncError('同步授权已失效，请登录网页重新下载同步配置')
            response.raise_for_status()
            result = response.json()
            if result.get('ok') is not True or result.get('ai_status_saved') is not True:
                raise SyncError('AI 运行状态未获服务确认，保留本机等待重试')
            conn.execute(
                'INSERT INTO synced(target,id,hash) VALUES(?,?,?) '
                'ON CONFLICT(target,id) DO UPDATE SET hash=excluded.hash',
                (target, status_id, digest),
            )
            conn.commit()
            uploaded += 1

        if not batches and not pending_reports and pending_status is None and pending_collector_status is None:
            batches=[[]]  # heartbeat, even when there is no new message, report or status
        for batch in batches:
            if stop and stop.is_set():
                break
            response=post(SITE_URL+'/api/radar/ingest',headers={'OAI-Sites-Authorization':'Bearer '+config['site_token']},json={'items':[r[0] for r in batch],'reports':[],'client':'collector'},timeout=(10,30),allow_redirects=False)
            if response.status_code in (401,403) or 300<=response.status_code<400:
                raise SyncError('同步授权已失效，请登录网页重新下载同步配置')
            response.raise_for_status()
            result=response.json()
            if result.get('ok') is not True or not isinstance(result.get('accepted_ids'),list):
                raise SyncError('服务未确认接收，保留本机等待重试')
            if not all(isinstance(value, str) for value in result['accepted_ids']):
                raise SyncError('服务确认格式错误，保留本机等待重试')
            expected = {item['id'] for item, digest in batch}
            accepted=set(result['accepted_ids']) & expected
            for item,digest in batch:
                if item['id'] in accepted:
                    conn.execute('INSERT INTO synced(target,id,hash) VALUES(?,?,?) ON CONFLICT(target,id) DO UPDATE SET hash=excluded.hash',(target,item['id'],digest))
                    uploaded+=1
            conn.commit()
            if accepted != expected:
                raise SyncError('部分记录未接收，请检查来源过滤与数据格式')
        if pending_collector_status is not None:
            status_id, collector_status, digest = pending_collector_status
            if stop and stop.is_set():
                return uploaded
            response = post(
                SITE_URL + '/api/radar/ingest',
                headers={'OAI-Sites-Authorization':'Bearer '+config['site_token']},
                json={'items': [], 'reports': [], 'collector_status': collector_status, 'client':'collector'},
                timeout=(10,30), allow_redirects=False,
            )
            if response.status_code in (401,403) or 300 <= response.status_code < 400:
                raise SyncError('同步授权已失效，请登录网页重新下载同步配置')
            response.raise_for_status()
            result = response.json()
            if result.get('ok') is not True or result.get('collector_status_saved') is not True:
                raise SyncError('采集进度状态未获服务确认，保留本机等待重试')
            conn.execute(
                'INSERT INTO synced(target,id,hash) VALUES(?,?,?) '
                'ON CONFLICT(target,id) DO UPDATE SET hash=excluded.hash',
                (target, status_id, digest),
            )
            conn.commit()
            uploaded += 1

        # Reports are uploaded separately because the site accepts at most 20 per request.
        for offset in range(0, len(pending_reports), 20):
            report_batch = pending_reports[offset:offset + 20]
            if stop and stop.is_set():
                break
            response = post(
                SITE_URL + '/api/radar/ingest',
                headers={'OAI-Sites-Authorization':'Bearer '+config['site_token']},
                json={'items': [], 'reports': [r[1] for r in report_batch], 'client':'collector'},
                timeout=(10,30), allow_redirects=False,
            )
            if response.status_code in (401,403) or 300 <= response.status_code < 400:
                raise SyncError('同步授权已失效，请登录网页重新下载同步配置')
            response.raise_for_status()
            result = response.json()
            if result.get('ok') is not True or result.get('reports') != len(report_batch):
                raise SyncError('报告未获服务确认，保留本机等待重试')
            for report_id, report, digest in report_batch:
                conn.execute(
                    'INSERT INTO synced(target,id,hash) VALUES(?,?,?) '
                    'ON CONFLICT(target,id) DO UPDATE SET hash=excluded.hash',
                    (target, report_id, digest),
                )
            conn.commit()
            uploaded += len(report_batch)

        print('Cloud sync complete:',uploaded,'new/updated items/reports/status',flush=True)
        return uploaded
    finally:
        conn.close()
