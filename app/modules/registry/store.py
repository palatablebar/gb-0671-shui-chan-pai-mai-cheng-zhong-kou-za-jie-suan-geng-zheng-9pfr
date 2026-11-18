"""档案索引存储。

关键不变量：
- 原始封存 zip 以 BLOB 原样入库（vault），本模块不提供任何删除/覆写接口，原始档案与隔离原因不可误删；
- 同批重复迁入按 batch_id + 内容哈希幂等处理，不会生成第二条索引；
- 发布在单事务内完成，失败整笔回滚，旧版索引保持不变；
- 证据不完整的批次 auditable 恒为 0，任何审计查询都取不到它。
"""
import json
import sqlite3
from pathlib import Path

from app.errors import BusinessError
from .reasons import STATE_INGESTED, STATE_HELD, STATE_AUDITABLE


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute(
        """CREATE TABLE IF NOT EXISTS registry (
            batch_id TEXT PRIMARY KEY,
            digest TEXT UNIQUE NOT NULL,
            entry_json TEXT NOT NULL,
            vault BLOB NOT NULL,
            state TEXT NOT NULL DEFAULT '待发布',
            auditable INTEGER NOT NULL DEFAULT 0,
            open_at TEXT,
            published_at TEXT
        )"""
    )
    return con


def _row_to_entry(row):
    entry = json.loads(row['entry_json'])
    entry.update(state=row['state'], auditable=bool(row['auditable']),
                 open_at=row['open_at'], published_at=row['published_at'])
    return entry


def is_auditable(entry):
    # 封存证据完整（无任何缺口）且封存哈希校验通过，才允许进入可审计结果。
    return not entry['gaps'] and entry['evidence'].get('seal_ok') is True


def ingest(path, entry, blob):
    """迁入一份档案。返回 (entry, created)。

    判重以批号（封存清单内的 batch_id，与文件名无关）为准：
    - 同批同内容：幂等返回已有索引，不新增记录；
    - 同批不同内容：拒绝覆盖，防止用重算或篡改件替换原封存；
    - digest 唯一约束是最后防线，任何撞哈希的异常都显式报错而非静默新建。
    """
    batch_id = entry['batch_id']
    with connect(path) as con:
        same_id = con.execute('SELECT * FROM registry WHERE batch_id=?', (batch_id,)).fetchone()
        if same_id:
            if same_id['digest'] == entry['digest']:
                return _row_to_entry(same_id), False
            raise BusinessError('ARCHIVE_BATCH_CONFLICT', f'批次 {batch_id} 已存在且封存内容不同，禁止覆盖', 409)
        try:
            con.execute(
                'INSERT INTO registry(batch_id,digest,entry_json,vault,state,auditable) VALUES(?,?,?,?,?,0)',
                (batch_id, entry['digest'], json.dumps(entry, ensure_ascii=False),
                 sqlite3.Binary(blob), STATE_INGESTED),
            )
        except sqlite3.IntegrityError:
            raise BusinessError('ARCHIVE_DIGEST_CONFLICT', '封存内容与已入库的另一批次重复，不重复建索引', 409)
        row = con.execute('SELECT * FROM registry WHERE batch_id=?', (batch_id,)).fetchone()
    return _row_to_entry(row), True


def list_entries(path):
    with connect(path) as con:
        rows = con.execute('SELECT * FROM registry ORDER BY rowid').fetchall()
    return [_row_to_entry(r) for r in rows]


def get_entry(path, batch_id):
    with connect(path) as con:
        row = con.execute('SELECT * FROM registry WHERE batch_id=?', (batch_id,)).fetchone()
    if not row:
        raise BusinessError('NOT_FOUND', f'索引中不存在批次 {batch_id}', 404)
    return _row_to_entry(row)


def get_blob(path, batch_id):
    """取出原始封存字节。永远返回迁入时的原件，不做任何重算改写。"""
    with connect(path) as con:
        row = con.execute('SELECT vault FROM registry WHERE batch_id=?', (batch_id,)).fetchone()
    if not row:
        raise BusinessError('NOT_FOUND', f'索引中不存在批次 {batch_id}', 404)
    return bytes(row['vault'])


def publish(path, batch_id, open_at, now_iso):
    """发布批次：单事务更新。证据不完整只暂缓、不进审计；任何异常回滚，旧版索引保留。"""
    with connect(path) as con:
        row = con.execute('SELECT * FROM registry WHERE batch_id=?', (batch_id,)).fetchone()
        if not row:
            raise BusinessError('NOT_FOUND', f'索引中不存在批次 {batch_id}', 404)
        if row['state'] != STATE_INGESTED:
            raise BusinessError('ARCHIVE_ALREADY_PUBLISHED', '批次已发布，重复发布不生成新版本', 409)
        entry = json.loads(row['entry_json'])
        auditable = 1 if is_auditable(entry) else 0
        state = STATE_AUDITABLE if auditable else STATE_HELD
        cur = con.execute(
            'UPDATE registry SET state=?, auditable=?, open_at=?, published_at=? WHERE batch_id=?',
            (state, auditable, open_at, now_iso, batch_id),
        )
        if cur.rowcount != 1:  # 极端并发下更新落空，整笔回滚，旧版不动
            raise BusinessError('PUBLISH_FAILED', '发布未生效，已保留旧版索引', 409)
        saved = con.execute('SELECT * FROM registry WHERE batch_id=?', (batch_id,)).fetchone()
    return _row_to_entry(saved)


def open_entries(path, now_iso, ticket=None, auditable_only=False):
    """入口按时开放：只返回已发布且 open_at <= now 的批次；auditable_only 时证据不完整批次被排除。

    已发布批次的 state 可能是“可审计”或“证据不完整·暂缓”，因此以“非待发布”作为发布判据。
    """
    sql = "SELECT * FROM registry WHERE state!=? AND open_at IS NOT NULL AND open_at<=?"
    params = [STATE_INGESTED, now_iso]
    if auditable_only:
        sql += ' AND auditable=1'
    sql += ' ORDER BY rowid'
    with connect(path) as con:
        rows = con.execute(sql, params).fetchall()
    entries = [_row_to_entry(r) for r in rows]
    if ticket is not None:
        entries = [e for e in entries if ticket in e['ticket_index']]
    return entries
