# 东港水产拍卖结算

给水产市场结算员处理双次称重、扣杂计价、结算更正与应付复核。

七个模块：称重采信、货品换算、毛皮配对、扣杂竞价、更正裁决、应付对账、结算归档。每个导航页均可重跑同一批数据，隔离行保留编号、原值、行号和原因码。

```sh
docker compose up --build -d
docker compose exec -T app pytest -q
```

浏览器访问 http://localhost:18240 。默认载入现场脏批，包含53条逻辑输入、重复表头、缺列、空值、重复冲突、缺皮重、毛重小于皮重等问题。切换“核验清批”或“旧编码批”后重跑。进入“更正裁决”核对票00001：原应付1168.50CNY，更正后1285.35CNY，已付1168.50CNY，仅待付116.85CNY。

API：各模块 `POST /api/<导航英文名>`；完整运行 `POST /api/runs`，JSON参数可用 `dataset` 或 `csv_text`。查询历史 `GET /api/runs`，复核 `POST /api/runs/{run_id}/review`，传入当前 `revision`。封存下载只有 `GET /api/export/{run_id}` 一个入口。

同输入和规则摘要生成相同批号，不新增重复记录；复核版本改变后旧版本提交返回409。SQLite保存在命名卷。可使用新卷名称重建演示数据，原卷保留。

定义见 `docs/DATA-DICTIONARY.md`，模块与测试映射见 `docs/DEPTH.md`。源码在 `app/`，业务样例在 `sample_data/`，测试在 `tests/`。不连接实际秤台和支付渠道。

采信页支持上传或粘贴CSV。完整源字节与参数随批次持久化，跨页通过run_id读取同一结果。`POST /api/runs/{run_id}/rerun` 读取该批不可变输入快照重新计算，规则未变时批号不变。切换另一示例则创建或复用另一批次。

## 统一档案索引（第八区，`/registry`）

把近三年散存的旧封存迁入统一检索，设计细节见 `docs/REGISTRY.md`。核心取舍：**保留原封存、另建索引**——现行规则重算只作只读旁证，历史金额与隔离原因永不改写；原始封存 zip 原样入库且无删除接口。

- 迁入 `POST /api/registry/ingest`（base64 zip），同批重复迁入幂等不新增索引，同批不同内容返回409；
- 发布 `POST /api/registry/entries/{batch_id}/publish`，可带 `open_at` 定时开放；发布单事务，失败回滚并保留旧版；
- 检索 `GET /api/registry/search?ticket=&scope=all|audit`，审计视野 `GET /api/registry/audit` 只返回证据完整批次，封存哈希不符、缺票证据、缺参数、隔离缺原因的批次一律暂缓、不进可审计结果；
- 原件 `GET /api/registry/entries/{batch_id}/raw` 下载字节与入库时一致。

```sh
python3 -I scripts/make_legacy.py --ingest   # 造五批旧封存样例并迁入（完整/哈希被改/缺参数/缺票/隔离缺原因）
```

复核“按时开放”可用环境变量 `REGISTRY_NOW=2026-11-18T00:00:00Z` 固定索引时钟。索引库由 `REGISTRY_DB_PATH` 指定，默认 `/data/registry.sqlite`，与结算库分离。
