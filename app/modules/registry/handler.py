"""档案索引 HTTP 入口。

- 迁入支持 base64 JSON，不引入额外依赖；
- 检索分 all / audit 两个视野，证据不完整批次只能在 all 中看到、绝不进入 audit；
- 原件下载始终返回入库时的原始封存字节。
"""
import base64

from fastapi import APIRouter, Body, Request
from fastapi.responses import Response

from app import config
from app.errors import BusinessError
from app.web import render_registry
from . import service
from . import store as rstore

router = APIRouter()


def _db(request: Request):
    return config.REGISTRY_DB_PATH


@router.get('/registry')
def page(request: Request):
    return render_registry(request)


@router.post('/api/registry/ingest')
def ingest(request: Request, payload: dict = Body(...)):
    filename = str(payload.get('filename') or 'legacy.zip')
    try:
        blob = base64.b64decode(str(payload.get('blob_b64', '')), validate=True)
    except Exception:
        raise BusinessError('BAD_BASE64', '封存内容须为合法 base64')
    entry, created = service.ingest_archive(_db(request), blob, filename)
    return {'created': created, 'entry': entry}


@router.get('/api/registry/entries')
def entries(request: Request):
    return {'entries': rstore.list_entries(_db(request)), 'now': service.now_iso()}


@router.get('/api/registry/search')
def search(request: Request, ticket: str | None = None, scope: str = 'all'):
    rows = service.search(_db(request), ticket=ticket.strip() if ticket else None, scope=scope)
    return {'scope': scope, 'ticket': ticket, 'now': service.now_iso(), 'entries': rows}


@router.get('/api/registry/audit')
def audit(request: Request, ticket: str | None = None):
    rows = service.search(_db(request), ticket=ticket.strip() if ticket else None, scope='audit')
    return {'scope': 'audit', 'now': service.now_iso(), 'entries': rows}


@router.get('/api/registry/entries/{batch_id}')
def detail(request: Request, batch_id: str):
    return rstore.get_entry(_db(request), batch_id)


@router.get('/api/registry/entries/{batch_id}/raw')
def raw(request: Request, batch_id: str):
    blob = rstore.get_blob(_db(request), batch_id)
    return Response(
        blob, media_type='application/zip',
        headers={'Content-Disposition': f'attachment; filename="{batch_id}-original.zip"'},
    )


@router.post('/api/registry/entries/{batch_id}/publish')
def publish(request: Request, batch_id: str, payload: dict = Body(default={})):
    return service.publish_archive(_db(request), batch_id, payload.get('open_at'))
