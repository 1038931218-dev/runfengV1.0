# 第五轮修复说明（待独立审查，未提交）

- 基准 HEAD：`88d1145968013b843b90ad79c66cbc950b75f269`
- 时间：2026-11-18（Asia/Shanghai）
- 状态：**代码已修改、测试已实测，未 commit、未 push**
- 声明：本文所有测试数字均为本轮实际执行结果，未沿用历史测试报告。

## 一、修复的 6 个问题（均对照 HEAD 确认为尚未修复）

| # | 问题 | 位置 | 根因 |
|---|---|---|---|
| 1 | Queue 竞态/资源泄漏 | `utils/timeout.py::run_isolated` | `queue.empty()` 不可靠 + 无超时 `get()` 可能永久阻塞；queue/process 未 close 泄漏 fd；先 join 后 get 导致大结果被误判超时 |
| 2 | 确定性错误盲目重试 | `adapters/base.py::_call_with_retry` | `except Exception` 对参数/解析/登录错误也重试，浪费预算并掩盖分类 |
| 3 | 异常分类子串误匹配 | `adapters/baostock_adapter.py::classify_query_error` | `"date" in text` 误命中 `"update"`，把非参数错误判为 ParameterError |
| 4 | backoff_base 未校验 | `utils/timeout.py::TimeoutConfig` | 退避基数可为 0/负/超大，导致 sleep 异常或退避爆炸 |
| 5 | 第一数据源缺失未校验 | `quality/cross_check.py::cross_check` | 仅校验 quotes_b；quotes_a 为 None 抛 TypeError，空列表产出误导结果 |
| 6 | collector 异常漏记 | `adapters/collector.py::collect_and_store` | 仅捕获 AdapterError，解析/类型等 ValueError 冒泡中断整批且 fetch_log 漏记 |

## 二、修改文件清单（10 个）

代码（6）：`utils/timeout.py`、`adapters/base.py`、`adapters/baostock_adapter.py`、`quality/cross_check.py`、`adapters/collector.py`、`tests/test_adapters.py` + `tests/test_integration.py`（测试 2 个）
测试产物（3）：`reports/phase2/{unit,integration,network}_result.txt`
本文（1）：`reports/phase2/round5-fixes.md`

## 三、关键代码差异

1. **run_isolated**：改为「先 `queue.get(timeout=)` 读结果（同时作为硬超时并清空管道让大结果 feeder 写完）→ 再 `proc.join(2s)` → 存活则 terminate/kill」；`finally` 中 `queue.close()/join_thread()`、`proc.close()`。新增大结果取回（20 万元素）与 30 次调用 fd 不增长验证。
2. **_call_with_retry**：新增 `NON_RETRYABLE_ERRORS = (ParameterError, DataParseError, LoginError)`，命中即记录 `last_error` 并原样抛出；其余异常（含 requests 网络类）保持有限重试。
3. **classify_query_error**：改用词边界正则 `\bparam...\b|\bdates?\b|参数` 与网络词边界，消除 `update→date` 误命中。
4. **TimeoutConfig**：`__post_init__` 增加 `0 < backoff_base <= 10` 校验。
5. **cross_check**：新增对称的 `if not quotes_a` 分支，返回 `executed=False` 并标注「第一数据源无有效数据」。
6. **collect_and_store**：新增可注入 `adapter` 参数（缺省 AKShareAdapter）；两处 `except AdapterError` 改为 `except Exception` 兜底，非 AdapterError 加 `UNEXPECTED:` 前缀写入 `stats.errors` 与 `fetch_log`，保证单个标的失败不中断整批。

## 四、本轮测试结果（实际执行）

| 类别 | 命令 | 结果 |
|---|---|---|
| 单元测试 | `pytest -m "not network and not integration" -q` | **41 passed** |
| 集成测试 | `pytest -m integration -q` | **9 passed** |
| 网络测试 | `pytest -m network -v` | 2 failed, 1 skipped |
| 全量 | `pytest tests/ -q` | 50 passed, 2 failed, 1 skipped |

- 修复前基线：非网络 42 passed；网络 2 failed（与本轮后相同）。
- 新增测试 8 个（问题1:2、问题2:2、问题3:1、问题4:1、问题5:1、问题6:1）。
- 网络 2 项失败经裸 `requests` 直连复现：`RemoteDisconnected('Remote end closed connection without response')`，0.1s 内断连，为东方财富接口对本沙箱 IP 的限频/封禁，**修改前即存在，与本轮改动无关**；未伪造为通过。

## 五、剩余风险（未改，遵守「不顺手改无关功能」）

1. **指数接口网络错误分类不准确**：`_call_isolated` 把子进程回传的 `ConnectionError` 一律归为 `DataParseError`（不可重试/不可 skip），与既有异常分类设计不一致。建议下一轮单列修复：按错误类名重建 `NetworkLimitError` 子类。
2. 东方财富接口限频期间，网络测试无法稳定通过；恢复后需重跑确认。
3. `run_isolated` 依赖 Linux `fork`，非 Linux 平台需改用 spawn 并验证可 pickle。

## 六、边界声明

- 未 commit、未 push；未进入第三阶段；未接入 Tushare、未索取 Token。
- 未删除/覆盖 `data/` 历史数据（仍 30+30 行），未提交任何数据库/密钥文件。
