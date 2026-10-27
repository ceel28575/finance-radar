"""Send exported records to the owner's private Site; retain failed work locally."""
import hashlib
import json
import sqlite3
from pathlib import Path

SITE_URL = 'https://finance-radar-jenson.sunlitbison257.chatgpt.site'


def read_config(root):
    path = root / 'cloud-config.json'
    if not path.exists():
        return None
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    if config.get('site_url', '').rstrip('/') != SITE_URL:
        raise ValueError('同步网址与本项目不一致，请从网页重新下载配置')
    if not isinstance(config.get('site_token'), str) or len(config['site_token']) < 20:
        raise ValueError('同步配置缺少凭据，请从网页重新下载')
    return config


def fingerprint(item):
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
        for file in sorted((root / 'exports').glob('????-??-??.json')):
            for item in json.loads(file.read_text(encoding='utf-8'))['items']:
                digest = fingerprint(item)
                old = conn.execute('SELECT hash FROM synced WHERE target=? AND id=?', (target,item['id'])).fetchone()
                if not old or old[0] != digest:
                    pending.append((item,digest))
        batches, batch, size = [], [], 0
        for record in pending:
            n = len(json.dumps(record[0], ensure_ascii=False).encode())
            if n > 4_000_000:
                raise ValueError('单条记录过大；仍保留在本机')
            if batch and (len(batch) >= 100 or size+n > 4_000_000):
                batches.append(batch);batch=[];size=0
            batch.append(record);size+=n
        if batch:
            batches.append(batch)
        if not batches:
            batches=[[]]  # heartbeat, even when there is no new message
        for batch in batches:
            if stop and stop.is_set():
                break
            response=post(SITE_URL+'/api/radar/ingest',headers={'OAI-Sites-Authorization':'Bearer '+config['site_token']},json={'items':[r[0] for r in batch],'reports':[],'client':'collector'},timeout=(10,30),allow_redirects=False)
            if response.status_code in (401,403) or 300<=response.status_code<400:
                raise ValueError('同步授权已失效，请登录网页重新下载同步配置')
            response.raise_for_status()
            result=response.json()
            if result.get('ok') is not True or not isinstance(result.get('accepted_ids'),list):
                raise ValueError('服务未确认接收，保留本机等待重试')
            accepted=set(result['accepted_ids'])
            for item,digest in batch:
                if item['id'] in accepted:
                    conn.execute('INSERT INTO synced(target,id,hash) VALUES(?,?,?) ON CONFLICT(target,id) DO UPDATE SET hash=excluded.hash',(target,item['id'],digest))
                    uploaded+=1
            conn.commit()
            if len(accepted) < len(batch):
                raise ValueError('部分记录未接收，请检查来源过滤与数据格式')
        print('Cloud sync complete:',uploaded,'new/updated items',flush=True)
        return uploaded
    finally:
        conn.close()
