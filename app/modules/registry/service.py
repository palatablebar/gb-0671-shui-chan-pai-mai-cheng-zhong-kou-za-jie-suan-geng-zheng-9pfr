"""档案索引服务层：解析迁入、按时发布、按票检索。

时钟可通过环境变量 REGISTRY_NOW（UTC，YYYY-MM-DDTHH:MM:SSZ）固定，便于复核与测试“按时开放”。
"""
import os
from datetime import datetime, timezone

from app.errors import BusinessError
from . import store as rstore
from .legacy import build_index_entry

TIME_FMT = '%Y-%m-%dT%H:%M:%SZ'


def now_iso():
    fixed = os.environ.get('REGISTRY_NOW')
    if fixed:
        return _parse_iso(fixed)
    return datetime.now(timezone.utc).strftime(TIME_FMT)


def _parse_iso(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(timezone.utc).strftime(TIME_FMT)
    except (ValueError, TypeError):
        raise BusinessError('BAD_TIME', '时间须为带时区的 ISO8601，如 2026-12-01T00:00:00Z')


def ingest_archive(path, blob: bytes, filename: str):
    if not blob:
        raise BusinessError('EMPTY_ARCHIVE', '封存文件为空')
    entry = build_index_entry(blob, filename, now_iso())
    saved, created = rstore.ingest(path, entry, blob)
    return saved, created


def publish_archive(path, batch_id: str, open_at=None):
    open_iso = _parse_iso(open_at) if open_at else now_iso()
    return rstore.publish(path, batch_id, open_iso, now_iso())


def search(path, ticket=None, scope='all'):
    """scope=audit 只返回可审计结果，证据不完整的批次不会出现。"""
    if scope not in ('all', 'audit'):
        raise BusinessError('BAD_SCOPE', "scope 只允许 'all' 或 'audit'")
    return rstore.open_entries(path, now_iso(), ticket=ticket, auditable_only=(scope == 'audit'))
