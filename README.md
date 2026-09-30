# 城市生态运营服务

这是一个面向城市湿地保护团队的 Python 后端服务。项目提供本地 HTTP 接口、SQLite 持久化、身份与角色管理、审计记录、任务编排和可扩展的生态数据处理边界，便于在单机环境中保存运营状态并复核业务决定。

## 运行环境

- Python 3.11 或更高版本
- SQLite 3（使用 Python 标准库）

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据文件位于 `data/compute-operations.db`，可以复制 `.env.example` 后调整本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康接口为 `GET /api/system/health`。所有状态变化都写入 SQLite，并由应用内事务保证关联记录的一致性。

## 测试

```bash
python -m pytest
```

测试覆盖参数校验、身份权限、事务边界、任务状态、失败恢复、审计写入和现有生态计算接口。

## 编译检查

```bash
python -m compileall -q app tests
```

## 本地验收

```bash
python -m app.cli check-db
python -m app.cli smoke
```

`check-db` 检查 SQLite 完整性和外键设置，`smoke` 在进程内调用健康接口并验证基础路由。项目不依赖外部数据库、消息队列或网络服务。

## 再生水班次台账

`app/water/` 为五套再生水处理系统提供按设施、按班次的进出水/回流/损耗台账：

- **原始读数登记**（`POST /api/water/shifts`）：校验读数单位与设施登记单位一致、班次时长与名义班次（默认 8 小时，±3 小时容差）匹配；按设施名义班次间隔检查与前后班次的时间连续性，跨日班次（如 22:00–06:00）正常支持。
- **可追溯的区间计算**（`app/water/calculations.py`，公式版本 `water-balance-v1`）：依据 `进水 = 出水 + 回流 + 损耗` 计算循环率（回流/进水）与损耗。仪表部分缺测时不做点估计，而是在物理可行多面体（各项非负、`出水+回流 ≤ 进水`、`进水 ≤ 设施班次能力`）上求极值，给出损耗区间与循环率区间，并记录所用假设与公式版本。
- **不可覆盖的修订版本**：补录（`POST /api/water/shifts/{id}/amend`）、复核纠正与回滚（`/rollback`）都只向 `water_shift_versions` 追加版本行，历史读数、计算结果、读数摘要（digest）永久保留；接口返回前后读数差异、损耗与循环率差异摘要（`GET /api/water/shifts/{id}/diff`）。回滚是"以新版本恢复旧读数"，不删除任何版本。
- **异常待复核**：负读数、出水/回流大于进水、超能力等违反物理约束的读数进入 `anomaly` 状态，原始值原样保留、绝不静默修正；须由复核角色确认（`anomaly_reviewed`）或纠正（追加 `correction` 版本）。待复核期间禁止直接补录。
- **批次边界**：批次（`water_batches`）是跨设施汇总的唯一边界，`GET /api/water/batches/{code}/summary` 只合并相同 `batch_code` 的当前版本，并列出尚未登记的在用设施；关账（`/close`）冻结历史修改，且存在待复核异常时拒绝关账。
- **重复导入幂等**：`POST /api/water/imports` 以 `import_key` + 请求内容摘要去重，重复导入重放首次结果，不产生重复数据；同一键不同内容返回冲突。
- **权限与审计**：`water.read` / `water.write` / `water.review` 三个权限分别授予系统角色 `water-operator`（登记、补录、导入）、`water-reviewer`（复核、回滚、关账）与只读的 `auditor`；所有登记、修订、复核、回滚、关账和权限拒绝都写入 `audit_events`。

验证（部分缺测区间、跨日班次连续性、不可覆盖版本与回滚、重复导入、异常复核、权限审计）：

```bash
python -m pytest tests/test_water_ledger.py -v
```

