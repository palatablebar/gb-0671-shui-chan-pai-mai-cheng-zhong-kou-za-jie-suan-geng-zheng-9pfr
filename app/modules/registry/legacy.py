"""旧封存档案的只读解析与完整性校验。

策略（对应迁移取舍）：
- 旧批一律“保留原封存、另建索引”，本模块从不改写封存内的历史金额；
- 按现行规则的重算只作为只读旁证，差异记入 drift，不回写历史；
- 证据缺口只描述证据本身的问题（缺参数、缺票证据、哈希不符等）。
"""
import base64
import hashlib
import io
import json
import zipfile
from decimal import Decimal

from app.coerce import money
from app.engine import evaluate
from app.errors import BusinessError
from .reasons import GAP_REASONS, POLICY_PRESERVE, POLICY_RECOMPUTE

TICKET_FIELDS = ('ticket', 'version', 'amount', 'due', 'paid')


def add_gap(entry, code, detail):
    """同类缺口只登记一次，但保留各自细节，便于复核员一次看全。"""
    for gap in entry['gaps']:
        if gap['code'] == code:
            if detail not in gap['detail']:
                gap['detail'] = f"{gap['detail']}；{detail}"
            return
    entry['gaps'].append({'code': code, 'detail': detail})


def sha256_blob(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def canonical_seal(rows) -> str:
    # 与 app/modules/archive/rules.py 的封存公式保持一致，保证跨年代可比对。
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _sum(rows, key):
    return money(sum((Decimal(str(r[key])) for r in rows), Decimal(0)))


def _load_zip(blob):
    """返回 (manifest, tickets, quarantine, source_bytes, params)。任何结构问题抛 BusinessError。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
        names = set(zf.namelist())
        for required in ('manifest.json', 'evidence/tickets.json', 'evidence/quarantine.json'):
            if required not in names:
                raise BusinessError('BUNDLE_DAMAGED', f'封存包缺少 {required}')
        manifest = json.loads(zf.read('manifest.json').decode('utf-8'))
        tickets = json.loads(zf.read('evidence/tickets.json').decode('utf-8'))
        quarantine = json.loads(zf.read('evidence/quarantine.json').decode('utf-8'))
        if not isinstance(manifest, dict) or not isinstance(tickets, list) or not isinstance(quarantine, list):
            raise BusinessError('BUNDLE_DAMAGED', '封存清单结构不是预期类型')
        source = zf.read('source.csv') if 'source.csv' in names else None
        params = json.loads(zf.read('params.json').decode('utf-8')) if 'params.json' in names else None
        return manifest, tickets, quarantine, source, params
    except BusinessError:
        raise
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessError('BUNDLE_DAMAGED', f'封存包损坏：{exc.__class__.__name__}')


def _recompute(source, params):
    """用现行规则只读复算，返回 engine 终态；失败返回 None。"""
    if source is None or params is None:
        return None
    try:
        return evaluate(
            {'dataset': 'legacy'},
            snapshot={'bytes_b64': base64.b64encode(source).decode(), 'context': params},
        )
    except BusinessError:
        return None


def build_index_entry(blob: bytes, filename: str, now_iso: str) -> dict:
    """把一份旧封存 zip 解析成索引条目。历史金额原样保留，缺口逐条登记。"""
    digest = sha256_blob(blob)
    entry = {
        'batch_id': '',
        'filename': filename,
        'digest': digest,
        'policy': POLICY_PRESERVE,
        'recompute_policy': POLICY_RECOMPUTE,
        'ingested_at': now_iso,
        'sealed_at': None,
        'legacy_rule_version': None,
        'gaps': [],
        'evidence': {},
        'historical': {'tickets': [], 'total_amount': None, 'total_due': None, 'total_paid': None},
        'recomputed': {'available': False, 'rule_version': None, 'total_amount': None},
        'drift': [],
        'ticket_index': {},
        'quarantine': [],
    }

    try:
        manifest, tickets, quarantine, source, params = _load_zip(blob)
    except BusinessError as exc:
        entry['batch_id'] = filename.rsplit('.', 1)[0]
        entry['gaps'].append({'code': exc.code, 'detail': GAP_REASONS.get(exc.code, exc.message)})
        entry['evidence'] = {'seal_ok': False}
        return entry

    entry['batch_id'] = str(manifest.get('batch_id') or filename.rsplit('.', 1)[0])
    entry['sealed_at'] = manifest.get('sealed_at')
    entry['legacy_rule_version'] = manifest.get('rule_version')
    # 隔离原因逐字保留，只作展示与核验，绝不删除或改写。
    entry['quarantine'] = quarantine
    entry['historical']['tickets'] = tickets
    entry['historical']['total_amount'] = _sum(tickets, 'amount') if tickets else None
    entry['historical']['total_due'] = _sum(tickets, 'due') if tickets else None
    entry['historical']['total_paid'] = _sum(tickets, 'paid') if tickets else None

    # 1) 封存哈希校验：清单哈希 == 票证据自身规范化哈希，证明原封存未被改动。
    evidence_seal = canonical_seal(tickets)
    manifest_seal = manifest.get('seal')
    seal_ok = bool(manifest_seal) and manifest_seal == evidence_seal
    if not manifest_seal:
        add_gap(entry, 'SEAL_MISSING', GAP_REASONS['SEAL_MISSING'])
    elif not seal_ok:
        add_gap(entry, 'SEAL_MISMATCH', GAP_REASONS['SEAL_MISMATCH'])

    # 2) 逐票证据字段完整性。
    malformed = [t.get('ticket', '?') for t in tickets if any(f not in t for f in TICKET_FIELDS)]
    if malformed:
        add_gap(entry, 'TICKET_EVIDENCE_MISSING',
                f"{GAP_REASONS['TICKET_EVIDENCE_MISSING']}：票 {','.join(map(str, malformed))}")

    # 3) 隔离原因完整性（先查档案自身记载）。
    reasonless = [q.get('line', '?') for q in quarantine if not q.get('code') or not q.get('detail')]
    if reasonless:
        add_gap(entry, 'QUARANTINE_REASON_MISSING',
                f"{GAP_REASONS['QUARANTINE_REASON_MISSING']}：行 {','.join(map(str, reasonless))}")

    # 4) 现行规则只读复算旁证。
    state = _recompute(source, params)
    if state is None:
        if source is None or params is None:
            add_gap(entry, 'PARAMS_MISSING', GAP_REASONS['PARAMS_MISSING'])
        else:
            add_gap(entry, 'RECOMPUTE_FAILED', '源数据无法按现行规则复算，仅保留原封存')
        recomputed_final, recomputed_quarantine, recomputed_seal = [], [], None
    else:
        recomputed_final = state['final']
        recomputed_quarantine = state['quarantine']
        recomputed_seal = canonical_seal(recomputed_final)
        entry['recomputed'] = {
            'available': True,
            'rule_version': state['manifest']['rule_version'],
            'total_amount': _sum(recomputed_final, 'amount'),
        }
        # 复算覆盖票 vs 封存票：复算出现而封存缺证据的票即缺口。
        archived_ids = {t['ticket'] for t in tickets if 'ticket' in t}
        missing = sorted({r['ticket'] for r in recomputed_final if r['ticket'] not in archived_ids})
        if missing:
            add_gap(entry, 'TICKET_EVIDENCE_MISSING',
                    f"{GAP_REASONS['TICKET_EVIDENCE_MISSING']}：票 {','.join(missing)}")
        # 复算认定的隔离行若在档案中没有原因记录，同样是缺口。
        archived_q_lines = {q.get('line') for q in quarantine}
        q_missing = sorted({q['line'] for q in recomputed_quarantine if q.get('line') not in archived_q_lines})
        if q_missing:
            add_gap(entry, 'QUARANTINE_REASON_MISSING',
                    f"{GAP_REASONS['QUARANTINE_REASON_MISSING']}：行 {','.join(map(str, q_missing))}")

    # 5) 历史金额 vs 现行复算旁证：只登记差异，绝不回写历史。
    recomputed_by_ticket = {r['ticket']: r for r in recomputed_final}
    for t in tickets:
        ticket = t.get('ticket')
        rec = recomputed_by_ticket.get(ticket)
        entry['ticket_index'][ticket] = {
            'evidence_present': all(f in t for f in TICKET_FIELDS),
            'historical_amount': t.get('amount'),
            'recomputed_amount': rec['amount'] if rec else None,
            'quarantined_lines': [q.get('line') for q in quarantine if q.get('ticket') == ticket],
        }
        if rec and 'amount' in t and str(rec['amount']) != str(t['amount']):
            entry['drift'].append({
                'ticket': ticket,
                'historical_amount': str(t['amount']),
                'recomputed_amount': rec['amount'],
                'delta': money(Decimal(rec['amount']) - Decimal(str(t['amount']))),
            })

    entry['evidence'] = {
        'seal_ok': seal_ok,
        'manifest_seal': manifest_seal,
        'evidence_seal': evidence_seal,
        'recomputed_seal': recomputed_seal,
        'tickets_archived': len(tickets),
        'tickets_recomputed': len(recomputed_final),
        'quarantine_archived': len(quarantine),
        'quarantine_recomputed': len(recomputed_quarantine),
    }
    return entry
