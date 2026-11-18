# 第二阶段代码审查修复 — 验收审计报告

- 项目：润锋投资数据中心 V1.0
- 仓库：`1038931218-dev/runfengV1.0`（main 分支）
- 审计时间：2026-11-18（Asia/Shanghai）
- 性质：针对指挥部提出的 6 项审查问题的逐项修复与实测验证

> 本报告数据均来自本环境实际执行结果，非任务书复制。测试明细见 `reports/phase2/*_result.txt`。

---

## 一、6 项审查问题修复结论

| # | 问题 | 修复状态 | 关键改动 |
|---|---|---|---|
| 1 | 测试隔离 | ✅ 已完成 | `test_db_has_real_data` 移除真实库依赖，真实数据验收拆为 integration 测试且用临时库 |
| 2 | 网络超时机制 | ✅ 已完成 | AKShare 个股传 `(connect,read)` 显式超时；指数/不可中断调用走进程隔离硬超时；有限重试+退避；单次/总耗时记录 |
| 3 | BaoStock 异常分类 | ✅ 已完成 | 异常细分为连接超时/拒绝/登录失败/参数/解析错误；测试仅 `NetworkLimitError` 可 skip |
| 4 | 交叉验证重复检查 | ✅ 已完成 | 映射前检测重复，重复明细写入 `CrossCheckResult`，不静默覆盖 |
| 5 | 数据库 period 字段 | ✅ 已完成 | `daily_quote` 增 period（默认 daily）、唯一键含 period；v1→v2 迁移保留历史数据；指数表独立 |
| 6 | 测试命名与报告 | ✅ 已完成 | 重命名历史行情测试；三类测试独立输出；生成本报告 |

---

## 二、逐项修复详情

### 问题1 — 测试隔离
- 删除 `tests/test_adapters.py` 中依赖真实库的 `test_db_has_real_data()`。
- 新建 `tests/test_integration.py`（`@pytest.mark.integration`），全部使用 `tmp_db` 临时库 fixture，覆盖建表/字段/唯一约束/迁移/重复检查。
- 真实网络采集独立到 `tests/test_network.py`（`@pytest.mark.network`）。
- **全新环境验证**：以 `git archive` 模拟全新克隆（无 data 目录），执行 `init_db()` 后运行 `-m "not network"` → **35 passed，1.71s**，证明非网络测试无需预置真实数据库即可独立运行。

### 问题2 — 网络超时机制
检查到的 AKShare 底层实现：
- `stock_zh_a_hist` 签名含 `timeout`，底层 `requests.get(url, params, timeout=timeout)` → **可控**。
- `stock_zh_index_daily_em` 底层 `requests.get(url, params)` 无 timeout → **不可安全中断**。

据此双路径处理：
1. **可控调用**：向 `stock_zh_a_hist` 传 `timeout=(5.0, 15.0)`（connect/read 显式分离），配合 `max_retries=2` + 指数退避 `1.5^n`。
2. **不可中断调用**：`utils/timeout.py` 的 `run_isolated()` 在独立子进程执行，`total=30s` 硬超时后 `terminate/kill`。**实测**：一个 `sleep(30)` 的函数在 2s 硬超时配置下于 2.01s 被强制终止，未无限等待。
3. **兜底**：保留 `socket.setdefaulttimeout(12)`，但不作为唯一机制。
4. **耗时记录**：`ElapsedTimer` 记录单次请求耗时；采集器记录总任务耗时；均写入 `fetch_log.elapsed_ms`。
5. **禁止无限**：`TimeoutConfig.__post_init__` 拒绝 `connect/read/total<=0` 及 `max_retries>5`，实测 4 组非法配置全部被拦截。

### 问题3 — BaoStock 异常分类
在 `adapters/base.py` 建立异常层级：
- `NetworkLimitError`（可 skip）：`ConnectionTimeoutError`、`ConnectionRefusedError`、`TaskTimeoutError`
- 不可 skip（必须 fail）：`LoginError`、`ParameterError`、`DataParseError`

`adapters/baostock_adapter.py` 的 `classify_login_error()` 按错误码/消息归类：
- `10002007 网络接收错误` / `timed out` / `refused` → `ConnectionRefusedError`（环境网络限制）
- `密码/账号/password/auth` 等 → `LoginError`
- 未知错误 → 保守归 `LoginError`（绝不轻易 skip）

网络测试 `test_baostock_fetch_classified` 仅捕获 `NetworkLimitError` 才 `pytest.skip`；捕获到 `LoginError/ParameterError/DataParseError` 则 `pytest.fail` 并保留完整错误信息。本环境实测结果：`SKIPPED (环境网络限制)`，符合预期。

### 问题4 — 交叉验证重复检查
`quality/cross_check.py`：
- `detect_duplicates()` 基于 `Counter` 统计重复条数与重复日期。
- `cross_check()` 在构建日期映射**之前**检测重复；结果写入 `duplicates_in_a/b`、`duplicate_dates_a/b`、`duplicates`、`has_duplicates`。
- 存在重复时 `note` 明确告警“保留每个日期最后一条”，**不静默覆盖**。
- 新增 2 个重复数据单元测试（单源重复、双源重复）。

### 问题5 — 数据库 period 字段与迁移
- `daily_quote` 新增 `period TEXT NOT NULL DEFAULT 'daily'`，唯一键改为 `(code, trade_date, adjust_type, period, source)`。
- `schema_version` 引入，`user_version=2`。
- **v1→v2 迁移**（`storage/db.py::_migrate`）：重命名旧表→建 v2 表→`INSERT SELECT` 复制历史数据（period 填 daily）→删除旧表，**全程事务、行数校验、失败回滚**，历史数据不删除不覆盖。
- 兼容旧表补列：`fetch_log` 缺 `elapsed_ms` 时用 `ALTER TABLE ADD COLUMN` 安全补齐。
- 指数表 `index_quote` 保持独立，**未加入**复权/周期字段。

### 问题6 — 测试命名与报告
- `test_akshare_realtime_fetch` → `test_akshare_history_stock_fetch`（并新增 `test_akshare_history_index_fetch`），明确是历史行情而非实时。
- 三类测试独立执行并保存结果文件。
- 生成本验收报告。

---

## 三、实际数据库查询结果（本环境真实库 `data/invest_center.db`）

| 检查项 | 查询结果 |
|---|---|
| `user_version` | 2 |
| `daily_quote` 字段 | id, code, name, trade_date, open, high, low, close, volume, amount, adjust_type, **period**, source, quote_time, collected_at, data_type（共 16 列） |
| 行情总行数 | 30 |
| period 分布 | `daily`: 30 |
| 重复组数（按新唯一键） | **0** |
| 来源/时效分布 | `akshare/history`: 30 |
| 血缘字段为空行数 | **0** |
| 指数表字段 | id, index_code, index_name, trade_date, open, high, low, close, volume, amount, source, quote_time, collected_at, data_type（**无** adjust_type/period） |
| 指数行数 | 30 |

**字段完整性结论**：任务书要求的代码/名称/日期/开高低收/成交量/成交额/来源/行情时间/采集时间/数据时效全部齐备，且新增 period 已生效；血缘字段无空值。

**重复记录结论**：按 `(code, trade_date, adjust_type, period, source)` 分组，重复组数为 0，迁移与写入过程未产生重复。

**迁移数据保留结论**：真实库 v1→v2 迁移前后均为 30 行，样本 `600519/2025-11-03 close=1435.0`、`300750/2025-11-03 close=386.86` 保持不变。

---

## 四、测试执行结果（三类独立）

| 类别 | 命令 | 结果 |
|---|---|---|
| 单元测试 | `pytest tests/ -m "not network and not integration" -q` | **30 passed**, 9 deselected |
| 数据库集成测试 | `pytest tests/ -m "integration" -q` | **6 passed**, 33 deselected |
| 网络测试 | `pytest tests/ -m "network" -v` | **2 passed, 1 skipped**, 36 deselected |
| 全量 | `pytest tests/ -q` | **38 passed, 1 skipped** |

唯一跳过项：`test_baostock_fetch_classified`，原因 `NetworkLimitError`（沙箱私有端口 10030 封锁），属于可确认的环境网络限制，按问题3规则允许跳过。

明细文件：
- `reports/phase2/unit_result.txt`
- `reports/phase2/integration_result.txt`
- `reports/phase2/network_result.txt`

---

## 五、超时机制实测证据

| 验证项 | 方法 | 结果 |
|---|---|---|
| 显式 connect/read 超时 | `TimeoutConfig(connect=5,read=15).as_requests_timeout()` | `(5.0, 15.0)` |
| 指数接口进程隔离 | `fetch_index_daily` 真实采集 | 10 行，80~148ms，正常返回 |
| 硬超时强制终止 | `run_isolated(sleep(30), timeout=2)` | 2.01s 返回 `TASK_TIMEOUT`，子进程被终止 |
| 非法配置拦截 | `connect=0/read=None/total=-1/max_retries=99` | 4/4 全部 `ValueError` |
| 有限重试 | `max_retries` 限制 0~5 | 超限被拒 |

---

## 六、尚未解决 / 已知限制

1. BaoStock 私有端口（10030/10031）在本沙箱被封锁，无法真实采集；异常分类已就位，仅此项按规则 skip，非代码缺陷。
2. 进程隔离使用 `fork` 上下文（Linux 环境）；若迁移到 macOS/Windows 需改为 `spawn`（已预留结构，函数与结果均可 pickle）。
3. 未启动第三阶段，未索取 Tushare Token；本阶段仅修复审查问题，不涉及选股/信号。

---

## 七、复核要点建议
- 检查 `adapters/base.py` 异常层级与 `utils/timeout.py` 硬超时实现。
- 检查 `storage/db.py::_migrate` 的事务与行数校验逻辑（历史数据保护）。
- 在干净环境执行 `pytest -m "not network and not integration"`，确认无需真实数据即可通过。
