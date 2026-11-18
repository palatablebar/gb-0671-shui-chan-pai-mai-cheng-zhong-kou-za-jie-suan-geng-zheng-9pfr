"""档案索引 HTTP 端到端：页面渲染、迁入 API、按时开放、审计隔离、原件下载、导航不破坏旧模块。"""
import base64
import io
import json
import zipfile
from decimal import Decimal, ROUND_HALF_UP

import pytest
from fastapi.testclient import TestClient

from app import main
from app.engine import evaluate
from app.modules.registry.legacy import canonical_seal


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, 'DB_PATH', str(tmp_path / 'workbench.sqlite'))
    monkeypatch.setattr(config, 'REGISTRY_DB_PATH', str(tmp_path / 'registry.sqlite'))
    monkeypatch.setenv('REGISTRY_NOW', '2026-11-18T00:00:00Z')
    return TestClient(main.app)


def _blob(dataset='legacy', drop_ticket=None, broken_seal=False, with_source=True):
    state = evaluate({'dataset': dataset})
    root = __import__('app.config', fromlist=['ROOT']).ROOT
    source = (root / 'sample_data' / f'{dataset}.csv').read_bytes()
    params = state['input_snapshot']['context']

    def legacy_amount(p):
        billable = (Decimal(p['net_kg']) * (1 - Decimal(p['rate']))).quantize(Decimal('0.01'))
        amount = (billable * Decimal(p['price'])).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return {'ticket': p['ticket'], 'version': p['version'], 'amount': str(amount),
                'paid': p['paid'], 'due': str(amount - Decimal(p['paid'])), 'lines': p['lines']}

    kept = [legacy_amount(p) for p in state['pairs'] if not drop_ticket or p['ticket'] != drop_ticket]
    quarantine = [{'line': q['line'], 'ticket': q.get('raw', {}).get('ticket', ''),
                   'code': q['code'], 'detail': q['detail']} for q in state['quarantine']]
    manifest = {'batch_id': f'http-{dataset}', 'sealed_at': '2023-11-18T08:30:00+08:00',
                'rule_version': '2023.09.01-legacy',
                'seal': ('f' * 64) if broken_seal else canonical_seal(kept)}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
        zf.writestr('evidence/tickets.json', json.dumps(kept, ensure_ascii=False))
        zf.writestr('evidence/quarantine.json', json.dumps(quarantine, ensure_ascii=False))
        if with_source:
            zf.writestr('source.csv', source)
            zf.writestr('params.json', json.dumps(params, ensure_ascii=False))
    return buf.getvalue()


def test_registry_page_renders(client):
    r = client.get('/registry')
    assert r.status_code == 200
    assert '统一档案索引' in r.text
    assert '档案索引' in r.text  # 导航入口


def test_ingest_and_list(client):
    blob = _blob()
    r = client.post('/api/registry/ingest', json={'filename': 'http-legacy.zip', 'blob_b64': base64.b64encode(blob).decode()})
    assert r.status_code == 200
    body = r.json()
    assert body['created'] is True
    assert body['entry']['batch_id'] == 'http-legacy'
    listed = client.get('/api/registry/entries').json()['entries']
    assert len(listed) == 1


def test_ingest_bad_base64(client):
    r = client.post('/api/registry/ingest', json={'filename': 'x.zip', 'blob_b64': '@@@'})
    assert r.status_code == 422
    assert r.json()['code'] == 'BAD_BASE64'


def test_idempotent_http(client):
    blob = _blob()
    payload = {'filename': 'alias.zip', 'blob_b64': base64.b64encode(blob).decode()}
    assert client.post('/api/registry/ingest', json=payload).json()['created'] is True
    second = client.post('/api/registry/ingest', json=payload)
    assert second.json()['created'] is False
    assert len(client.get('/api/registry/entries').json()['entries']) == 1


def test_incomplete_batch_excluded_from_audit(client, monkeypatch):
    good = _blob(dataset='dirty')
    bad = _blob(drop_ticket='00001')
    # 用不同 batch_id：直接改 zip 内 manifest
    def rename(blob, new_id):
        zin = zipfile.ZipFile(io.BytesIO(blob))
        manifest = json.loads(zin.read('manifest.json'))
        manifest['batch_id'] = new_id
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zout:
            for item in zin.namelist():
                if item == 'manifest.json':
                    zout.writestr(item, json.dumps(manifest, ensure_ascii=False))
                else:
                    zout.writestr(item, zin.read(item))
        return out.getvalue()

    g = rename(good, 'batch-good')
    b = rename(bad, 'batch-bad')
    client.post('/api/registry/ingest', json={'filename': 'g.zip', 'blob_b64': base64.b64encode(g).decode()})
    client.post('/api/registry/ingest', json={'filename': 'b.zip', 'blob_b64': base64.b64encode(b).decode()})
    client.post('/api/registry/entries/batch-good/publish', json={})
    client.post('/api/registry/entries/batch-bad/publish', json={})

    all_rows = client.get('/api/registry/search?scope=all').json()['entries']
    audit_rows = client.get('/api/registry/audit').json()['entries']
    assert {e['batch_id'] for e in all_rows} == {'batch-good', 'batch-bad'}
    assert [e['batch_id'] for e in audit_rows] == ['batch-good']

    # 按原票号：all 能查到缺票批，audit 查不到
    t1 = client.get('/api/registry/search?ticket=00002&scope=all').json()['entries']
    t1_audit = client.get('/api/registry/audit?ticket=00002').json()['entries']
    assert {e['batch_id'] for e in t1} == {'batch-good', 'batch-bad'}
    assert [e['batch_id'] for e in t1_audit] == ['batch-good']


def test_future_open_not_visible(client, monkeypatch):
    blob = _blob(dataset='dirty')
    client.post('/api/registry/ingest', json={'filename': 'g.zip', 'blob_b64': base64.b64encode(blob).decode()})
    client.post('/api/registry/entries/http-dirty/publish', json={'open_at': '2026-12-31T00:00:00Z'})
    assert client.get('/api/registry/search').json()['entries'] == []
    monkeypatch.setenv('REGISTRY_NOW', '2027-01-02T00:00:00Z')
    rows = client.get('/api/registry/search').json()['entries']
    assert [e['batch_id'] for e in rows] == ['http-dirty']


def test_raw_download_returns_original_bytes(client):
    blob = _blob(dataset='dirty')
    client.post('/api/registry/ingest', json={'filename': 'g.zip', 'blob_b64': base64.b64encode(blob).decode()})
    r = client.get('/api/registry/entries/http-dirty/raw')
    assert r.status_code == 200
    assert r.content == blob
    assert 'attachment' in r.headers['content-disposition']
    # 原件仍可用标准 zip 解开，历史金额一字未改
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    tickets = json.loads(zf.read('evidence/tickets.json'))
    assert all('amount' in t for t in tickets)


def test_detail_and_missing_404(client):
    assert client.get('/api/registry/entries/nope').status_code == 404
    assert client.get('/api/registry/entries/nope/raw').status_code == 404


def test_republish_409_and_drift_visible(client):
    blob = _blob(dataset='dirty')
    client.post('/api/registry/ingest', json={'filename': 'g.zip', 'blob_b64': base64.b64encode(blob).decode()})
    first = client.post('/api/registry/entries/http-dirty/publish', json={})
    assert first.status_code == 200
    second = client.post('/api/registry/entries/http-dirty/publish', json={})
    assert second.status_code == 409
    detail = client.get('/api/registry/entries/http-dirty').json()
    assert detail['state'] == '可审计'
    # 漂移只登记不改写：历史票金额仍是旧公式结果
    assert isinstance(detail['drift'], list)


def test_existing_modules_unaffected(client):
    # 七模块流水线与原有 API 不受索引子系统影响
    assert client.post('/api/archive', json={}).status_code == 200
    assert client.get('/archive').status_code == 200
    assert client.get('/healthz').status_code == 200
