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

## 再生水班次水量登记（/api/water）

面向五套再生水处理系统的班次进出水、回流、损耗登记。核心规则：

- **按设施+班次登记**：每个设施每天 3 个班次（含跨日三班延伸至次日零点），重复登记返回 409，差异补录走修订接口。
- **单位与抄表时刻校验**：表计单位（m³/L）在设施表计档案中固定，提交单位不一致返回 422，绝不静默换算；L 按 1000:1 折算为 m³ 计算。抄表时刻必须落在班次窗口内。
- **时间连续性**：校验班次序号连续（`sequence_gap`）、与上一班次的交接空档（`time_gap`）以及计数器读数与相邻班次的衔接（`continuity_gap`），跨日 seq3→次日 seq1 正确识别。
- **不可覆盖的修订版本**：每次补录生成新版本快照（`ww_shift_versions` + `ww_reading_versions`），历史行不可变，未补录表计原样继承；`GET /shifts/{id}/diff?from_version=&to_version=` 给前后读数与循环率/损耗差异摘要；支持版本回滚（指针回退，历史版本仍可查看）。
- **部分缺测与区间**：接班/交班读数可单侧为 null，逻辑相邻班次可锚定推断并打标（`carried_start`/`carried_end`）；无法闭合的缺测使体积/循环率/损耗变为区间（`low/high/bounded`，无界侧为 null），不输出虚假精确值。
- **异常读数待复核**：负值、计数器倒退、时刻越界的读数进入 `pending` 状态（`GET /reviews/pending`），由具备 `water.review` 权限者确认（`resolved`，如换表归零）或驳回（`rejected`，按缺测处理），系统不做静默修正。
- **批次边界与幂等**：`POST /batches/import` 以 `batch_key` + 请求体指纹去重，重复导入重放同一结果（`replayed=true`），同批次号不同内容返回 409；汇总接口严格限定在批次内班次，批次外班次不会混入。
- **权限与审计**：`water.read` / `water.write` / `water.review` 三个权限点，越权访问返回 403 且写入 `outcome=denied` 审计事件；所有登记、补录、回滚、复核、批次导入均留痕（`audit_events` 与 `ww_shift_events`）。

验证见 `tests/test_water_shifts.py`，覆盖部分缺测锚定与无界区间、跨日班次、补录差异、版本回滚、异常复核、批次幂等与边界汇总、权限拒绝审计。

