# 第五轮修复说明（含独立审查后补充修复，待独立验收）

- 基准提交：`88d1145968013b843b90ad79c66cbc950b75f269`（main，**未变更、未合并、未覆盖**）
- 第五轮初始审查提交：`2e2f9b91c7ca5dd92c3efc679733c8fc404e1e39`（分支 `review/round5`）
- 本补充修复位于分支：`review/round5`（在 `2e2f9b9` 之上新增独立提交）
- 状态：**代码已修改、测试已实测、已推送到 review/round5；尚未独立验收通过**
- 实际测试执行时间：**2026-11-18 18:04（Asia/Shanghai）**
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

## 二、独立审查后补充修复 4 项（本次）

| # | 问题 | 位置 | 修复要点 |
|---|---|---|---|
| P1 | BaoStock 结构化业务错误误判为 KeyError | `adapters/baostock_adapter.py::_fetch` | 区分「子进程执行层面 ok」与「业务结果字典 ok=False」：仅两层均成功才读 `result["rows"]`；业务失败按 `error_type` 从 `_EXC_CLASS_MAP` 重建异常，网络类保留 NetworkLimitError 子类，未知归 DataParseError，子进程自身异常保留分类 |
| P2 | 退避后沿用旧 timeout | `adapters/base.py::_call_with_retry` | 退避 sleep 移至循环起始，sleep 后重算 `total.remaining()` 并据此重新收敛 requests timeout；预算耗尽不再发请求、抛 TaskTimeoutError；文档明确 requests timeout 非绝对硬截止 |
| P3 | collector 日志失败保护不足 | `adapters/collector.py` | `adapter` 改显式 None 判断；采集/入库/日志分阶段捕获并带阶段标签（FETCH_ADAPTER / DB_WRITE）；失败日志写入自身异常经 `_finish_fetch_log` 保护，不静默吞掉、由 logger 输出原始异常与标的；数据库故障不与接口故障混淆；单标的失败不中断其他标的 |
| P4 | 验收材料口径 | `reports/phase2/round5-fixes.md` | 修正「未 commit/未 push」等与事实不符表述，如实记录分支、SHA、测试时间与口径，标注尚未独立验收 |

## 三、修改文件清单

第五轮初始审查提交 `2e2f9b9`（11 个）：
- 代码（5）：`utils/timeout.py`、`adapters/base.py`、`adapters/baostock_adapter.py`、`quality/cross_check.py`、`adapters/collector.py`
- 测试（2）：`tests/test_adapters.py`、`tests/test_integration.py`
- 报告（4）：`reports/phase2/round5-fixes.md`、`unit_result.txt`、`integration_result.txt`、`network_result.txt`

本次补充修复（在 `2e2f9b9` 之上）：
- 代码（3）：`adapters/baostock_adapter.py`、`adapters/base.py`、`adapters/collector.py`
- 测试（2）：`tests/test_adapters.py`、`tests/test_integration.py`
- 报告（1）：`reports/phase2/round5-fixes.md`
- 测试产物（3）：`reports/phase2/{unit,integration,network}_result.txt`

## 四、本轮测试结果（2026-11-18 18:04 实际执行）

### 4.1 代码测试（不依赖网络）

| 类别 | 命令 | 结果 |
|---|---|---|
| 编译检查 | `compileall -q` | 通过 |
| 单元测试 | `pytest -m "not network and not integration" -q` | **48 passed, 15 deselected in 5.73s** |
| 集成测试 | `pytest -m "integration" -q` | **12 passed, 51 deselected in 5.31s** |
| 代码测试小计 | 单元 + 集成 | **60 passed** |

新增测试：P1 5 项（登录/参数/网络/未知/正常 rows）、P2 2 项（退避后按最新预算收敛、预算耗尽停请求）、P3 3 项（阶段标签区分、日志写入失败非静默、显式 None 判断），本轮相关子集实测全绿。

### 4.2 网络测试（依赖外部行情接口，单独统计）

| 类别 | 命令 | 结果 |
|---|---|---|
| 网络测试 | `pytest -m "network" -v` | **2 failed, 1 skipped, 60 deselected in 12.06s** |
| 全量合计 | `pytest tests/ -q` | **2 failed, 60 passed, 1 skipped in 22.23s** |

- 2 项网络失败均为 `RemoteDisconnected('Remote end closed connection without response')`：裸 `requests` 直连东方财富接口同样在 0.10s 内被断连，确认为接口对本沙箱 IP 的限频/封禁，**与本轮代码改动无关**；未伪造为通过。
- 1 项 skip 为 BaoStock 环境限制（端口不可达）。

## 五、剩余风险（遵守「不顺手改无关功能」，未在本次修改）

1. **指数接口网络错误分类仍不准确**：`_call_isolated` 把子进程回传的 `ConnectionError` 归为 `DataParseError`（不可重试/不可 skip），与异常分类设计不一致；建议后续单列修复。本次 P1 仅修复 BaoStock 的结构化业务错误重建，未触碰 akshare 指数路径。
2. 东方财富接口限频期间，网络测试无法稳定通过；恢复后需重跑确认。
3. `run_isolated` 依赖 Linux `fork`，非 Linux 平台需改用 spawn 并验证可 pickle。

## 六、边界声明

- 代码位于 `review/round5`，main 保持 `88d1145` 未变更、未合并、未覆盖。
- 未接入 Tushare、未索取 Token；未删除/覆盖 `data/` 历史数据；未提交任何数据库/密钥文件。
- 本材料**不代表独立验收通过**，等待独立审查结论。
