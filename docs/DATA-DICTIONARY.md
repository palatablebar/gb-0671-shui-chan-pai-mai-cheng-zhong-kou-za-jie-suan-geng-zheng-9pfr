# 数据定义

票号与原始编号均为字符串，保留前导零。称重支持 kg、公斤与 t，统一为 kg，单价为 CNY/kg。
毛重与皮重按票号和版本配对，品类、单价、扣杂率与更正目标必须一致。计价重量=(毛重−皮重)×(1−扣杂率)，先量化到0.001kg，再乘单价，金额四舍五入到0.01CNY。
每票第一版不填写 replaces。后续版本只能替代当前生效版，分支或缺基版整票阻断结算。已支付额为各版本中最大累计已付值，待付=最终应付−累计已付；负待付表示待收回。
同编号同原值仅计一次；同编号不同原值全部隔离。空文件、全空行和仅表头分别报错。原始输入计数包括空行及重复表头，对账会分别解释。
归档封存保存逐版证据、隔离原因和运行摘要，非支付执行系统。数据使用虚构编号，不连接实际支付渠道。

## 旧封存包与档案索引

散存旧封存为 zip，固定条目：`manifest.json`（batch_id、sealed_at、rule_version、seal）、`evidence/tickets.json`（逐票 ticket/version/amount/due/paid/lines）、`evidence/quarantine.json`（line/ticket/code/detail），可选 `source.csv` 与 `params.json`（缺失即登记 PARAMS_MISSING，只保留原封存不复算）。封存 seal 为票证据按 `sort_keys=True` 序列化后的 SHA-256，与现行归档公式一致。

索引条目额外字段：`gaps`（证据缺口原因码列表）、`evidence`（manifest/evidence/recomputed 三种哈希与计数）、`drift`（历史金额与现行复算旁证的差异，只登记不改写）、`ticket_index`（票号→封存金额/复算金额/隔离行）、`auditable`（无缺口且哈希一致才为1）。索引库独立于结算库，由 `REGISTRY_DB_PATH` 指定。
