# 档案索引（统一检索）设计说明

把近三年散存的结算封存迁入统一检索。复核员可以按原票号找到旧封存证据，同时历史金额不会被任何重算改写。

## 1. 迁移策略：保留原封存，另建索引

旧批可能缺参数或含已隔离行。结论是**不按现行规则重算后覆盖迁入**，而是：

1. 原始封存 zip 以 BLOB 原样入库（`registry.vault`），系统不提供删除/覆写接口，原始档案与隔离原因不可误删；
2. 另建一条索引记录，保存批号、封存哈希、逐票证据指针、隔离原因、缺口清单和复算旁证；
3. 若封存内含源字节与参数快照，则用现行规则**只读复算**一次，结果只作旁证，与历史金额的差异登记在 `drift`，绝不回写历史。

这样“重算”与“历史”是两条证据链：历史金额以封存为准，复算只回答“按今天的规则会是多少”。

## 2. 封存证据完整性与缺口

索引对每份封存做四类核验，缺口用原因码登记，多条可并存：

| 原因码 | 含义 |
|---|---|
| `SEAL_MISMATCH` | manifest 哈希与逐票证据的规范化哈希不一致，封存可能被改动 |
| `SEAL_MISSING` | manifest 缺封存哈希，无法验证完整性 |
| `TICKET_EVIDENCE_MISSING` | 复算覆盖到的票在封存中缺逐票证据，或票证据字段不全 |
| `PARAMS_MISSING` | 缺源字节或参数快照，无法按现行规则复算，仅保留原封存 |
| `QUARANTINE_REASON_MISSING` | 已隔离行缺原因码/原因说明 |
| `BUNDLE_DAMAGED` | 封存包结构损坏或清单无法读取 |

封存哈希公式与 `app/modules/archive/rules.py` 一致（对票证据 `sort_keys=True` 后 SHA-256），跨年代可比对。

## 3. 生命周期与“按时开放、缺口不进审计”

```
待发布 ──发布──┬─ 无缺口且哈希一致 → 可审计（auditable=1）
              └─ 存在任一缺口     → 证据不完整·暂缓（auditable=0）
```

- 发布时带 `open_at`：入口在该时刻之后才出现在检索结果，未到点任何视野都查不到；
- `GET /api/registry/search?scope=all`：全部已开放批次（含暂缓），用于复核缺口；
- `GET /api/registry/audit` 与 `scope=audit`：只返回 `auditable=1` 的批次，**证据不完整的批次永不进入可审计结果**；
- 重复发布返回 409，不生成新版本；发布在单事务内完成，失败整笔回滚，旧版索引保留。

## 4. 幂等与保全不变量

| 要求 | 实现 |
|---|---|
| 同批重复迁入不生成多条索引 | 以封存清单内 `batch_id` 为主键，同内容返回已有记录 `created=false` |
| 同批不同内容拒绝覆盖 | `ARCHIVE_BATCH_CONFLICT` 409，防篡改件替换原封存 |
| 发布失败保留旧版 | 单事务 UPDATE，异常回滚；另有行锁回滚测试 |
| 原始档案不误删 | vault BLOB 只增不改，无删除 API；下载返回入库原件字节 |
| 隔离原因不误删 | 隔离行逐字进索引，原因缺失本身登记为缺口而非丢弃该行 |

## 5. API 一览

| 方法与路径 | 作用 |
|---|---|
| `GET /registry` | 索引页面：批次、票对应关系、封存校验、缺口、隔离原因 |
| `POST /api/registry/ingest` | 迁入 base64 封存 zip，返回索引条目与 `created` |
| `GET /api/registry/entries` | 全部批次（含未发布）与当前索引时钟 |
| `GET /api/registry/entries/{batch_id}` | 单批完整索引（含逐票索引、漂移、缺口） |
| `GET /api/registry/entries/{batch_id}/raw` | 下载原始封存 zip（字节与入库时一致） |
| `POST /api/registry/entries/{batch_id}/publish` | 发布，可带 `open_at` 定时开放 |
| `GET /api/registry/search?ticket=&scope=all\|audit` | 按原票号检索开放批次 |
| `GET /api/registry/audit?ticket=` | 仅可审计结果 |

时钟默认取 UTC 当前时间；复核或演示可用环境变量 `REGISTRY_NOW`（`YYYY-MM-DDTHH:MM:SSZ`）固定。

## 6. 样例与复核脚本

```sh
# 造五批旧封存到 registry_data/incoming/（独立目录，不碰 sample_data/ 与运行库）
python3 -I scripts/make_legacy.py
# 造样例并直接迁入默认索引库
REGISTRY_DB_PATH=/tmp/registry.sqlite python3 -I scripts/make_legacy.py --ingest
```

五批覆盖：A 完整可审计、B 哈希被改、C 缺参数无源、D 整票缺证据、E 隔离行缺原因。读取任何外来数据的 Python 一律加 `-I`，脚本路径作为参数传入，避免工作目录植入模块劫持。
