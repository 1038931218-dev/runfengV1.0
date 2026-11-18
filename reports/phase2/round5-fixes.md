# 第五轮修复说明（含独立审查后补充修复，待独立验收）

- 基准提交：`88d1145968013b843b90ad79c66cbc950b75f269`（main，**未变更、未合并、未覆盖**）
- 第五轮初始审查提交：`2e2f9b91c7ca5dd92c3efc679733c8fc404e1e39`（分支 `review/round5`）
- 补充修复 P1-P4 提交：`bf2624b1b26fda9b0f39f57a53104d6dd8623a01`（分支 `review/round5`）
- 本最终补充修复（P1-P3）位于分支：`review/round5`（在 `bf2624b` 之上新增独立提交）
- 状态：**代码已修改、测试已实测；本提交待独立审查，尚未宣布最终验收通过**
- 实际测试执行时间：**2026-11-18 18:23（Asia/Shanghai）**
- 声明：本文所有测试数字均为本轮实际执行结果，未沿用历史测试报告。

## 一、第五轮原始 6 项修复（审查提交 2e2f9b9）

| # | 问题 | 位置 | 根因 |
|---|---|---|---|
| 1 | Queue 竞态/资源泄漏 | `utils/timeout.py::run_isolated` | `queue.empty()` 不可靠 + 无超时 `get()` 可能永久阻塞；queue/process 未 close 泄漏 fd；先 join 后 get 导致大结果被误判超时 |
| 2 | 确定性错误盲目重试 | `adapters/base.py::_call_with_retry` | `except Exception` 对参数/解析/登录错误也重试，浪费预算并掩盖分类 |
| 3 | 异常分类子串误匹配 | `adapters/baostock_adapter.py::classify_query_error` | `"date" in text` 误命中 `"update"`，把非参数错误判为 ParameterError |
| 4 | backoff_base 未校验 | `utils/timeout.py::TimeoutConfig` | 退避基数可为 0/负/超大，导致 sleep 异常或退避爆炸 |
| 5 | 第一数据源缺失未校验 | `quality/cross_check.py::cross_check` | 仅校验 quotes_b；quotes_a 为 None 抛 TypeError，空列表产出误导结果 |
| 6 | collector 异常漏记 | `adapters/collector.py::collect_and_store` | 仅捕获 AdapterError，解析/类型等 ValueError 冒泡中断整批且 fetch_log 漏记 |

## 二、前次补充修复 4 项（提交 bf2624b）

| # | 问题 | 位置 | 修复要点 |
|---|---|---|---|
| P1 | BaoStock 结构化业务错误误判为 KeyError | `adapters/baostock_adapter.py::_fetch` | 区分「子进程执行层面 ok」与「业务结果字典 ok=False」：仅两层均成功才读 `result["rows"]`；业务失败按 `error_type` 从 `_EXC_CLASS_MAP` 重建异常 |
| P2 | 退避后沿用旧 timeout | `adapters/base.py::_call_with_retry` | 退避 sleep 移至循环起始，sleep 后重算 `total.remaining()` 并据此重新收敛 requests timeout |
| P3 | collector 日志失败保护不足 | `adapters/collector.py` | 采集/入库/日志分阶段捕获并带阶段标签；失败日志写入自身异常经 `_finish_fetch_log` 保护 |
| P4 | 验收材料口径 | `reports/phase2/round5-fixes.md` | 修正与事实不符表述，如实记录分支、SHA、测试时间与口径 |

## 三、本最终补充修复 3 项（本次，在 bf2624b 之上）

| # | 问题 | 位置 | 修复要点 |
|---|---|---|---|
| P1 | `_call_isolated` 把子进程所有非超时异常统一包装成 `DataParseError`，网络错误（ConnectionError/Timeout/ConnectionRefusedError）丢失网络分类 | `adapters/base.py` | 新增 `_ISOLATED_NETWORK_EXC` **异常类型名白名单** + `_rebuild_isolated_error()`：**仅依据子进程回传的类型名**（`run_isolated` 回传 `"类型名: 消息"`）精确重建网络异常分类；主进程硬超时 `TASK_TIMEOUT` 保留 `TaskTimeoutError`；未知异常归 `DataParseError`；**绝不扫描消息文本**中的 `network` 字样，杜绝把真实解析错误误判为网络异常 |
| P2 | `FetchLogRepo.start` 位于 try 保护外，单个标的日志创建失败会中断整批 | `adapters/collector.py` | 新增 `_start_fetch_log()`：start 失败时记录「标的 + 阶段 FETCH_LOG_START + 原始异常」到 `errors`/`log_errors`，返回 `None`（**不伪造 log_id**）；调用方跳过该标的并继续其余标的 |
| P3 | 成功入库后 `_finish_fetch_log` 返回 `False` 未被检查，统计看不到日志写入失败 | `adapters/collector.py` | 新增独立 `stats["log_errors"]`；`_finish_fetch_log` 增加可选 `log_errors` 列表参数，失败时追加结构化明细；成功日志写失败**不影响** `stocks/indices` 真实入库行数统计、**不伪装成采集失败**；失败日志写失败同样计入 `log_errors` |

## 四、修改文件清单

第五轮初始审查提交 `2e2f9b9`（11 个）：代码 5 + 测试 2 + 报告 4（详见 `git show 2e2f9b9 --stat`）。
补充修复 P1-P4 提交 `bf2624b`（9 个）：代码 3 + 测试 2 + 报告 1 + 测试产物 3。
本次最终补充（在 `bf2624b` 之上）：
- 代码（2）：`adapters/base.py`、`adapters/collector.py`
- 测试（2）：`tests/test_adapters.py`、`tests/test_integration.py`
- 报告（1）：`reports/phase2/round5-fixes.md`
- 测试产物（3）：`reports/phase2/{unit,integration,network}_result.txt`

## 五、本轮测试结果（2026-11-18 18:23 实际执行）

### 5.1 代码测试（不依赖网络）

| 类别 | 命令 | 结果 |
|---|---|---|
| 编译检查 | `compileall -q` | 通过 |
| 单元测试 | `pytest -m "not network and not integration" -q` | **55 passed, 18 deselected in 5.71s** |
| 集成测试 | `pytest -m "integration" -q` | **15 passed, 58 deselected in 7.38s** |
| 代码测试小计 | 单元 + 集成 | **70 passed** |

本轮新增测试共 10 项，实测全绿：
- P1（7 项，`test_adapters.py`）：ConnectionError→网络、Timeout→网络、ConnectionRefusedError→网络、ValueError(消息含 network)→DataParseError、未知错误→DataParseError、硬超时→TaskTimeoutError、正常结果原样返回。
- P2/P3（3 项，`test_integration.py`）：start 失败跳过并继续、成功日志写失败照常计入行数且记入 log_errors 不伪装、失败日志写失败记入 log_errors。

### 5.2 网络测试（依赖外部行情接口，单独统计）

| 类别 | 命令 | 结果 |
|---|---|---|
| 网络测试 | `pytest -m "network" -v` | **2 failed, 1 skipped, 70 deselected in 12.07s** |
| 全量合计 | `pytest tests/ -q` | **2 failed, 70 passed, 1 skipped in 24.26s** |

- 2 项网络失败均为 `RemoteDisconnected('Remote end closed connection without response')`：裸 `requests` 直连东方财富接口同样在约 0.1s 内被断连，确认为接口对本沙箱 IP 的限频/封禁，**与本轮代码改动无关**；未伪造为通过。
- P1 修复后，指数接口的网络错误已从错误的 `DataParseError` 正确重建为 `NetworkLimitError`（`ConnectionError`），分类符合设计；测试仍失败的根因是外部网络不可达，而非分类错误。
- 1 项 skip 为 BaoStock 环境限制（按设计对 `NetworkLimitError` 如实跳过）。

## 六、剩余风险（遵守「不顺手改无关功能」，未在本次修改）

1. 东方财富接口限频期间，网络测试无法稳定通过；恢复后需重跑确认。
2. `run_isolated` 依赖 Linux `fork`，非 Linux 平台需改用 spawn 并验证可 pickle。
3. `data/` 目录历史数据未触碰、未覆盖、未提交；真实数据库未改动。

## 七、边界声明

- 代码位于 `review/round5`，main 保持 `88d1145` 未变更、未合并、未覆盖。
- 未接入 Tushare、未索取 Token；未删除/覆盖 `data/` 历史数据；未提交任何数据库/密钥/账户文件。
- 本材料**不代表独立验收通过**，等待独立审查结论。
