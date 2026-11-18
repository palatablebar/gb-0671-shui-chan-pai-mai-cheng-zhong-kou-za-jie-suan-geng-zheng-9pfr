"""档案索引服务层测试：完整性缺口、迁移策略、幂等、按时开放、审计隔离、原件保全。"""
import sqlite3

import pytest

from app.errors import BusinessError
from app.modules.registry import service, store as rs
from app.modules.registry.legacy import canonical_seal, sha256_blob

NOW = '2026-11-18T00:00:00Z'


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / 'registry.sqlite')
    monkeypatch.setenv('REGISTRY_NOW', NOW)
    return path


@pytest.fixture
def blobs(tmp_path):
    """与 scripts/make_legacy.py 同构的五批旧封存，独立构造避免依赖脚本副作用。"""
    import io
    import zipfile
    from app.engine import evaluate
    state = evaluate({'dataset': 'legacy'})
    dirty = evaluate({'dataset': 'dirty'})
    source = __import__('app.config', fromlist=['ROOT']).ROOT / 'sample_data' / 'legacy.csv'
    dirty_csv = __import__('app.config', fromlist=['ROOT']).ROOT / 'sample_data' / 'dirty.csv'
    src_bytes, dirty_bytes = source.read_bytes(), dirty_csv.read_bytes()
    params, dirty_params = state['input_snapshot']['context'], dirty['input_snapshot']['context']

    from decimal import Decimal, ROUND_HALF_UP
    def legacy_tickets(pairs):
        out = []
        for p in pairs:
            billable = (Decimal(p['net_kg']) * (1 - Decimal(p['rate']))).quantize(Decimal('0.01'))
            amount = (billable * Decimal(p['price'])).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            out.append({'ticket': p['ticket'], 'version': p['version'], 'amount': str(amount),
                        'paid': p['paid'], 'due': str(amount - Decimal(p['paid'])), 'lines': p['lines']})
        return out

    tickets, qrows = legacy_tickets(state['pairs']), [
        {'line': q['line'], 'ticket': q.get('raw', {}).get('ticket', ''), 'code': q['code'], 'detail': q['detail']}
        for q in state['quarantine']]
    # 删除整票 00001 的全部版本，制造“复算有、封存无”的真实缺票
    cut_tickets = [t for t in tickets if t['ticket'] != '00001']
    dtickets, dqrows = legacy_tickets(dirty['pairs']), [
        {'line': q['line'], 'ticket': q.get('raw', {}).get('ticket', ''), 'code': q['code'], 'detail': q['detail']}
        for q in dirty['quarantine']]

    def pack(name, tk, qr, src, pm, seal_override=None, with_src=True, with_params=True, broken_q=False):
        if broken_q:
            qr = [dict(q, code='', detail='') for q in qr]
        manifest = {'batch_id': name, 'sealed_at': '2023-11-18T08:30:00+08:00',
                    'rule_version': '2023.09.01-legacy',
                    'seal': seal_override or canonical_seal(tk)}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('manifest.json', __import__('json').dumps(manifest, ensure_ascii=False))
            zf.writestr('evidence/tickets.json', __import__('json').dumps(tk, ensure_ascii=False))
            zf.writestr('evidence/quarantine.json', __import__('json').dumps(qr, ensure_ascii=False))
            if with_src:
                zf.writestr('source.csv', src)
            if with_params:
                zf.writestr('params.json', __import__('json').dumps(pm, ensure_ascii=False))
        return buf.getvalue()

    return {
        'A': pack('legacy-batch-2023A', dtickets, dqrows, dirty_bytes, dirty_params),
        'B': pack('legacy-batch-2023B', tickets, qrows, src_bytes, params, seal_override='0' * 64),
        'C': pack('legacy-batch-2023C', tickets, qrows, src_bytes, params, with_src=False, with_params=False),
        'D': pack('legacy-batch-2023D', cut_tickets, qrows, src_bytes, params),
        'E': pack('legacy-batch-2023E', dtickets, dqrows, dirty_bytes, dirty_params, broken_q=True),
    }


def _ingest_all(db, blobs):
    out = {}
    for key, blob in blobs.items():
        entry, created = service.ingest_archive(db, blob, f'legacy-batch-2023{key}.zip')
        assert created
        out[key] = entry
    return out


def test_complete_archive_has_no_gaps_and_is_auditable(db, blobs):
    entry, created = service.ingest_archive(db, blobs['A'], 'a.zip')
    assert created is True
    assert entry['gaps'] == []
    assert entry['evidence']['seal_ok'] is True
    assert rs.is_auditable(entry) is True


def test_seal_tamper_gap(db, blobs):
    entry, _ = service.ingest_archive(db, blobs['B'], 'b.zip')
    codes = {g['code'] for g in entry['gaps']}
    assert 'SEAL_MISMATCH' in codes
    assert rs.is_auditable(entry) is False


def test_missing_params_gap_and_no_recompute(db, blobs):
    entry, _ = service.ingest_archive(db, blobs['C'], 'c.zip')
    codes = {g['code'] for g in entry['gaps']}
    assert 'PARAMS_MISSING' in codes
    assert entry['recomputed']['available'] is False
    assert rs.is_auditable(entry) is False


def test_missing_ticket_evidence_gap(db, blobs):
    entry, _ = service.ingest_archive(db, blobs['D'], 'd.zip')
    codes = {g['code'] for g in entry['gaps']}
    assert 'TICKET_EVIDENCE_MISSING' in codes
    # 复算生效票里有 00001，但封存票证据里整票缺失
    detail = ' '.join(g['detail'] for g in entry['gaps'] if g['code'] == 'TICKET_EVIDENCE_MISSING')
    assert '00001' in detail
    assert entry['evidence']['tickets_archived'] == entry['evidence']['tickets_recomputed'] - 1


def test_quarantine_reason_missing_gap(db, blobs):
    entry, _ = service.ingest_archive(db, blobs['E'], 'e.zip')
    codes = {g['code'] for g in entry['gaps']}
    assert 'QUARANTINE_REASON_MISSING' in codes
    # 原始隔离行仍逐行保留在索引里，原因码被清空的事实不被掩盖
    assert len(entry['quarantine']) > 0


def test_damaged_bundle_gap(db, blobs):
    entry, _ = service.ingest_archive(db, b'not-a-zip', 'broken.zip')
    assert {g['code'] for g in entry['gaps']} == {'BUNDLE_DAMAGED'}
    assert rs.is_auditable(entry) is False


def test_idempotent_same_blob_single_index(db, blobs):
    first, c1 = service.ingest_archive(db, blobs['A'], 'a.zip')
    second, c2 = service.ingest_archive(db, blobs['A'], 'a.zip')
    assert c1 is True and c2 is False
    assert first['batch_id'] == second['batch_id']
    assert len(rs.list_entries(db)) == 1


def test_same_batch_different_content_rejected(db, blobs):
    service.ingest_archive(db, blobs['A'], 'a.zip')
    with pytest.raises(BusinessError) as exc:
        service.ingest_archive(db, blobs['A'] + b'tampered', 'a.zip')
    assert exc.value.code == 'ARCHIVE_BATCH_CONFLICT'
    assert exc.value.status == 409
    assert len(rs.list_entries(db)) == 1  # 冲突不产生第二条索引


def test_same_blob_under_alias_is_idempotent(db, blobs):
    # 批号取自封存清单内部，换文件名重复迁入仍是同一批，不产生第二条索引
    first, c1 = service.ingest_archive(db, blobs['A'], 'a.zip')
    second, c2 = service.ingest_archive(db, blobs['A'], 'other-name.zip')
    assert c1 is True and c2 is False
    assert first['batch_id'] == second['batch_id']
    assert len(rs.list_entries(db)) == 1


def test_history_amount_never_rewritten_by_recompute(db, blobs):
    entry, _ = service.ingest_archive(db, blobs['A'], 'a.zip')
    # 旧公式与现行规则至少存在一张票漂移；漂移只登记，历史金额保持封存原值
    assert entry['drift'], '样例应能展示金额漂移'
    drifted = entry['drift'][0]
    archived = next(t for t in entry['historical']['tickets'] if t['ticket'] == drifted['ticket'])
    assert str(archived['amount']) == drifted['historical_amount']
    assert archived['amount'] != drifted['recomputed_amount']
    assert entry['policy'].startswith('保留原封存')


def test_vault_bytes_immutable(db, blobs):
    entry, _ = service.ingest_archive(db, blobs['A'], 'a.zip')
    assert rs.get_blob(db, entry['batch_id']) == blobs['A']
    assert sha256_blob(rs.get_blob(db, entry['batch_id'])) == entry['digest']


def test_timed_open_gates_search(db, blobs):
    entries = _ingest_all(db, blobs)
    service.publish_archive(db, entries['A']['batch_id'], open_at='2026-12-31T00:00:00Z')
    assert service.search(db) == []  # 未到开放时间，入口不可见
    import os
    os.environ['REGISTRY_NOW'] = '2027-01-02T00:00:00Z'
    rows = service.search(db)
    assert [e['batch_id'] for e in rows] == [entries['A']['batch_id']]


def test_incomplete_batches_never_enter_audit(db, blobs):
    entries = _ingest_all(db, blobs)
    for e in entries.values():
        service.publish_archive(db, e['batch_id'])
    import os
    os.environ['REGISTRY_NOW'] = '2027-01-02T00:00:00Z'
    all_rows = service.search(db)
    audit_rows = service.search(db, scope='audit')
    assert len(all_rows) == 5
    assert [e['batch_id'] for e in audit_rows] == [entries['A']['batch_id']]
    for e in all_rows:
        if e['batch_id'] != entries['A']['batch_id']:
            assert e['auditable'] is False


def test_ticket_search_finds_archived_evidence(db, blobs):
    entries = _ingest_all(db, blobs)
    for e in entries.values():
        service.publish_archive(db, e['batch_id'])
    import os
    os.environ['REGISTRY_NOW'] = '2027-01-02T00:00:00Z'
    ticket = entries['B']['historical']['tickets'][0]['ticket']
    hits = service.search(db, ticket=ticket)
    assert entries['B']['batch_id'] in [e['batch_id'] for e in hits]
    audit_hits = service.search(db, ticket=ticket, scope='audit')
    assert entries['B']['batch_id'] not in [e['batch_id'] for e in audit_hits]


def test_republish_conflict_keeps_single_version(db, blobs):
    entry, _ = service.ingest_archive(db, blobs['A'], 'a.zip')
    service.publish_archive(db, entry['batch_id'])
    with pytest.raises(BusinessError) as exc:
        service.publish_archive(db, entry['batch_id'])
    assert exc.value.code == 'ARCHIVE_ALREADY_PUBLISHED'
    assert rs.get_entry(db, entry['batch_id'])['state'] == '可审计'


def test_publish_failure_rolls_back_keeping_old_state(db, blobs):
    entry, _ = service.ingest_archive(db, blobs['A'], 'a.zip')
    # 用第二个连接占住写锁，迫使发布事务失败，验证整笔回滚、旧版索引保留。
    locker = rs.connect(db)
    locker.execute('BEGIN IMMEDIATE')
    import app.modules.registry.store as mod
    original = mod.sqlite3.connect
    def fast_connect(path, *a, **kw):
        kw['timeout'] = 1
        return original(path, *a, **kw)
    mod.sqlite3.connect = fast_connect
    try:
        with pytest.raises(sqlite3.OperationalError):
            rs.publish(db, entry['batch_id'], '2026-11-18T00:00:00Z', NOW)
    finally:
        mod.sqlite3.connect = original
        locker.rollback()
        locker.close()
    saved = rs.get_entry(db, entry['batch_id'])
    assert saved['state'] == '待发布'  # 发布失败，旧版（待发布）索引保留
    assert saved['open_at'] is None
    # 锁释放后同一批次仍可正常发布
    published = service.publish_archive(db, entry['batch_id'])
    assert published['state'] == '可审计'


def test_bad_open_time_rejected(db, blobs):
    entry, _ = service.ingest_archive(db, blobs['A'], 'a.zip')
    with pytest.raises(BusinessError) as exc:
        service.publish_archive(db, entry['batch_id'], open_at='not-a-time')
    assert exc.value.code == 'BAD_TIME'


def test_quarantine_reasons_preserved_verbatim(db, blobs):
    entry, _ = service.ingest_archive(db, blobs['A'], 'a.zip')
    archived = entry['quarantine']
    assert len(archived) == 8
    assert all(q['code'] and q['detail'] and q['line'] for q in archived)


def test_gap_codes_unique_with_merged_detail(db, blobs):
    # E 批既在档案自查、又在复算对照中发现隔离缺原因：原因码只出现一次，细节合并
    entry, _ = service.ingest_archive(db, blobs['E'], 'e.zip')
    codes = [g['code'] for g in entry['gaps']]
    assert len(codes) == len(set(codes))
    gap = next(g for g in entry['gaps'] if g['code'] == 'QUARANTINE_REASON_MISSING')
    assert gap['detail']
