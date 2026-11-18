#!/usr/bin/env python3
"""造三份“近三年散存”的旧封存样例并落到独立目录 registry_data/incoming/。

用法：
  python3 -I scripts/make_legacy.py                 # 只生成 zip
  python3 -I scripts/make_legacy.py --ingest        # 生成并直接迁入索引库

设计：旧封存是独立年代的产物，本脚本用独立公式写历史金额；
索引侧再用现行规则只读复算作旁证，两者不应互相改写。
所有输出只写到 registry_data/，不触碰 sample_data/ 与任何运行库。
"""
import argparse
import hashlib
import io
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import REGISTRY_DB_PATH  # noqa: E402
from app.engine import evaluate  # noqa: E402
from app.modules.registry.legacy import canonical_seal  # noqa: E402
from app.modules.registry import service  # noqa: E402

OUT = ROOT / 'registry_data' / 'incoming'
LEGACY_RULE = '2023.09.01-legacy'


def seal(rows):
    return canonical_seal(rows)


def make_zip(name, tickets, quarantine, source_bytes, params, manifest_seal=None,
             include_source=True, include_params=True, broken_q=False):
    manifest = {
        'batch_id': name,
        'sealed_at': '2023-11-18T08:30:00+08:00',
        'rule_version': LEGACY_RULE,
        'seal': manifest_seal or seal(tickets),
    }
    if broken_q:
        quarantine = [dict(q, code='', detail='') for q in quarantine]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr('evidence/tickets.json', json.dumps(tickets, ensure_ascii=False, indent=2))
        zf.writestr('evidence/quarantine.json', json.dumps(quarantine, ensure_ascii=False, indent=2))
        if include_source:
            zf.writestr('source.csv', source_bytes)
        if include_params:
            zf.writestr('params.json', json.dumps(params, ensure_ascii=False, indent=2))
    return buf.getvalue()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ingest', action='store_true')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    state = evaluate({'dataset': 'legacy'})
    source_bytes = (ROOT / 'sample_data' / 'legacy.csv').read_bytes()
    params = state['input_snapshot']['context']
    # E 批需要真实隔离原因，使用含 8 条隔离行的现场脏批作为旧封存源。
    dirty_state = evaluate({'dataset': 'dirty'})
    dirty_bytes = (ROOT / 'sample_data' / 'dirty.csv').read_bytes()
    dirty_params = dirty_state['input_snapshot']['context']

    # 用独立“旧公式”重写一遍金额：四舍五入到 0.01，避免与现行 0.001kg 量化完全一致，
    # 从而能展示“历史金额保留、复算旁证存在漂移但不回写”的复核场景。
    def legacy_amount(pair):
        from decimal import Decimal, ROUND_HALF_UP
        billable = (Decimal(pair['net_kg']) * (1 - Decimal(pair['rate']))).quantize(Decimal('0.01'))
        amount = (billable * Decimal(pair['price'])).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        paid = Decimal(pair['paid'])
        return {
            'ticket': pair['ticket'], 'version': pair['version'],
            'amount': str(amount), 'paid': pair['paid'], 'due': str(amount - paid),
            'lines': pair['lines'],
        }

    tickets = [legacy_amount(p) for p in state['pairs']]
    quarantine = [
        {'line': q['line'], 'ticket': q.get('raw', {}).get('ticket', ''),
         'code': q['code'], 'detail': q['detail']}
        for q in state['quarantine']
    ]
    dirty_tickets = [legacy_amount(p) for p in dirty_state['pairs']]
    dirty_quarantine = [
        {'line': q['line'], 'ticket': q.get('raw', {}).get('ticket', ''),
         'code': q['code'], 'detail': q['detail']}
        for q in dirty_state['quarantine']
    ]

    batches = []
    # A 完整封存：哈希自洽、源+参数齐全、隔离原因齐全（用含隔离行的脏批覆盖隔离证据链）。
    batches.append(('legacy-batch-2023A',
                    make_zip('legacy-batch-2023A', dirty_tickets, dirty_quarantine, dirty_bytes, dirty_params)))
    # B 封存被改动：manifest 哈希与票证据不符（历史金额原样保留）。
    batches.append(('legacy-batch-2023B', make_zip('legacy-batch-2023B', tickets, quarantine, source_bytes, params,
                                                   manifest_seal='0' * 64)))
    # C 缺参数与源快照：无法复算旁证，仅保留原封存。
    batches.append(('legacy-batch-2023C', make_zip('legacy-batch-2023C', tickets, quarantine, source_bytes, params,
                                                   include_source=False, include_params=False)))
    # D 缺票证据：整票 00001 的封存证据被抽走，现行复算仍能算出该票。
    cut = [t for t in tickets if t['ticket'] != '00001']
    batches.append(('legacy-batch-2023D', make_zip('legacy-batch-2023D', cut, quarantine, source_bytes, params)))
    # E 隔离行缺原因：隔离原因码与说明被清空（源仍含隔离行，复算侧可对照发现）。
    batches.append(('legacy-batch-2023E',
                    make_zip('legacy-batch-2023E', dirty_tickets, dirty_quarantine, dirty_bytes, dirty_params,
                             broken_q=True)))

    written = []
    for name, blob in batches:
        path = OUT / f'{name}.zip'
        path.write_bytes(blob)
        written.append((name, path, len(blob)))

    print(f'now={datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")} out={OUT}')
    for name, path, size in written:
        print(f'  {name}.zip  {size} bytes  sha256={hashlib.sha256(path.read_bytes()).hexdigest()[:16]}')

    if args.ingest:
        for name, path, _ in written:
            entry, created = service.ingest_archive(REGISTRY_DB_PATH, path.read_bytes(), path.name)
            gap_codes = ','.join(sorted({g['code'] for g in entry['gaps']})) or '无缺口'
            print(f'  ingest {name}: created={created} gaps=[{gap_codes}] drift={len(entry["drift"])}票')


if __name__ == '__main__':
    main()
