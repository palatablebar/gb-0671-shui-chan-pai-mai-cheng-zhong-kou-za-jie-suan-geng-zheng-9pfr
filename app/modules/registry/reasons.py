# 档案索引缺口原因码：只描述“证据为什么不完整”，不改变任何历史金额。
GAP_REASONS = {
    'SEAL_MISMATCH': '封存哈希与重算结果不一致，原封存可能被改动',
    'SEAL_MISSING': '缺少封存哈希，无法验证原封存完整性',
    'TICKET_EVIDENCE_MISSING': '部分票缺少逐票封存证据',
    'PARAMS_MISSING': '缺少计价参数快照，无法按现行规则复算旁证',
    'QUARANTINE_REASON_MISSING': '隔离行缺少隔离原因记录',
    'BUNDLE_DAMAGED': '封存包结构损坏或清单无法读取',
}

# 迁移策略：旧批一律保留原封存、另建索引；重算只作只读旁证，绝不改写历史金额。
POLICY_PRESERVE = '保留原封存·另建索引'
POLICY_RECOMPUTE = '现行规则重算旁证（不改写原金额）'

# 批次在索引中的生命周期：待发布 →（发布）→ 可审计 / 证据不完整·暂缓
STATE_INGESTED = '待发布'
STATE_AUDITABLE = '可审计'
STATE_HELD = '证据不完整·暂缓'
