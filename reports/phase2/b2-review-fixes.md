# B2 独立审查修复报告（P1-01 / P1-02 / 次要清理）

> 审查基线：`dev/b2-adapters` @ `97ef0a6d9a68c464d05f8a51043e6478ff99b78c`（父 `020959c`）
> 执行时间：2026-11-19（Asia/Shanghai）
> 修复范围：仅 ChatGPT 独立审查提出的两项 P1 + 次要清理，未扩大范围、未开发 B3、未进入 D/E。
> main 保持 `88d1145`，`review/round5` 保持 `020959c`，未修改真实数据库、未提交凭证。

---

## 一、修复前后差异总览

| 项 | 修复前 | 修复后 |
|---|---|---|
| P1-01 新浪结构校验 | 仅校验顶层 list，逐行 `r.get()` 遇 None/字符串泄漏 `AttributeError` | 逐行校验 dict + 六字段齐全 + day 非空 + 五数值字段有限，畸形行统一 `DataParseError`，确定性错误只请求 1 次 |
| P1-01 腾讯结构校验 | `payload.get` / `node.get` 遇 None/list 泄漏 `AttributeError`；day 仅校验 list | 逐层校验 payload→data→symbol节点→day，逐行校验 >=6 列 list + 列值有限；非法 JSON 也归 `DataParseError`，全部不重试 |
| P1-02 成交额语义 | `amount=0.0` 占位但无标记，下游可能误用 | 标准结构加 `amount_available` 标记（sina/tencent=False，其余默认 True）+ 数据契约函数 `amount_available()`/`sum_real_amount()`，统计入口排除占位值 |
| 次要清理 import | `test_new_adapters.py` 未使用 `NetworkLimitError` | 已删除 |
| 次要清理代码映射 | 6→sh，其余一律 sz（8/4 北交所被错误映射为深市） | 6→sh、0/3→sz、8/4(北交所)及非法代码抛 `ParameterError`，绝不错误映射 |

---

## 二、P1-01：接口返回结构校验（已修复）

### 2.1 SinaAdapter

- 顶层响应非 `list` → `DataParseError`。
- 逐行校验（`_validate_rows`）：每行必须是 `dict`；`day/open/high/low/close/volume` 六字段必须存在；`day` 非空；五个数值字段必须可转 `float` 且 `math.isfinite`。
- 任何畸形行统一抛 `DataParseError`（属于 `NON_RETRYABLE_ERRORS`），**不泄漏 AttributeError/TypeError/KeyError**，且 `_call_with_retry` 不重试（测试验证 `requests.get` 仅调用 1 次）。

### 2.2 TencentAdapter

- 逐层校验（`_validate_payload`）：`payload` 为 dict → `data` 为 dict → `symbol` 节点为 dict → `day` 为 list；任一层类型错误抛 `DataParseError`。
- 逐行校验：每行必须是 `list` 且至少 6 列；`date` 非空；第 1~5 列可转 float 且有限。
- `resp.json()` 抛 `ValueError/TypeError`（非法 JSON）统一转 `DataParseError`，**不当网络错误重试**。
- 所有确定性解析错误均只请求 1 次（测试逐一验证）。

### 2.3 新增测试（16 项参数化 + 3 项独立）
- 新浪混入 `None`/字符串/空字典/缺字段/非法数值/inf → `DataParseError` 且不重试；顶层非 list 不重试。
- 腾讯 payload 为 `None`/列表/缺 data/data 类型错误；symbol 节点类型错误；day 类型错误；行元素类型错误（dict）；行 <6 列；列类型错误；非法 JSON → 均 `DataParseError` 且不重试。

---

## 三、P1-02：成交额缺失值语义（已修复，未改数据库）

### 3.1 前置检查（按要求逐项完成）
- `storage/models.py`：`amount REAL`（可空），`SCHEMA_VERSION=2`，唯一键 `(code,trade_date,adjust_type,period,source)`。
- `storage/repository.py`：`DAILY_QUOTE_COLUMNS` 含 `amount`，`bulk_insert` 用 `INSERT OR IGNORE`。
- `storage/db.py`：迁移机制 v1→v2（加 period），`_ensure_column` 支持安全补列，迁移前后快照校验。
- 全部成交额使用位置（grep `amount|成交额`）：适配器构造、入库列、迁移快照、测试数据；**当前无成交额统计/指标计算模块**（分析/报告域尚未开发，见验收报告）。

### 3.2 设计决策：建立数据契约，不改数据库
因审查纪律要求不扩大范围、不碰真实数据库，且下游统计模块尚不存在，本轮采用**内存标准结构标记 + 数据契约函数**方案，不修改数据库 schema（避免 v2→v3 迁移风险）：

1. `StandardQuote`/`StandardIndexQuote` 新增字段 `amount_available: bool = True`（默认 True 保证 akshare/baostock 等真实成交额源向后兼容）；sina/tencent 构造时显式 `amount_available=False`。
2. `adapters/base.py` 新增：
   - `AMOUNT_PLACEHOLDER_SOURCES = {"sina","tencent"}`：占位成交额来源。
   - `amount_available(quote)`：优先读标准结构布尔标记；对仅有 dict/sqlite3.Row（数据库读出）的场景退化为按 `source` 判定。
   - `sum_real_amount(quotes, field="amount")`：成交额统计统一入口，只对真实可用成交额求和，排除占位值。
3. 不把 `0.0` 当真实成交额、不编造数据：sina/tencent 仍置 0.0 占位，但标记不可用。

### 3.3 数据库兼容性与历史数据处理
- **无需迁移**：`amount_available` 仅在内存标准结构中，未加入 `DAILY_QUOTE_COLUMNS`/`INDEX_QUOTE_COLUMNS`，入库时该字段被忽略，表结构不变。
- **历史数据不受影响**：现有库历史数据来自 akshare/baostock（真实成交额），默认 `amount_available=True`；即使从数据库读出无内存标记，`amount_available()` 按 source 判定也为 True。
- **双路径判定**：内存对象走布尔标记；数据库行走 source 白名单排除，覆盖「已入库、无标记」场景。
- **占位源非零脏值也排除**：`sum_real_amount` 对占位源一律跳过，即使 amount 出现非零脏值也不会进入统计（测试验证）。
- **未直接修改真实数据库**：真实库 sha256 未变，无迁移、无 DDL。

### 3.4 新增测试（6 项）
标准结构标记、按 source 判定（dict）、`sum_real_amount` 排除占位（含非零脏值）、端到端新浪行情统计为 0。

> 注：下游成交额统计/指标计算模块（D 域）当前尚未实现，本轮提供统一契约入口 `amount_available()`/`sum_real_amount()`；待 D 域开发成交额类指标时须经此入口，不得直接对 `amount` 求和。

---

## 四、次要清理（已修复）
- 删除 `test_new_adapters.py` 未使用的 `NetworkLimitError` import。
- 沪深京代码映射明确化：`to_sina_symbol`/`to_tencent_symbol` 对 8/4 北交所代码及非法代码抛 `ParameterError`，绝不错误映射为深市 `sz`。
- 新增测试（16 项参数化）：支持代码映射、不支持代码抛错、北交所绝不映射到深市。

---

## 五、测试完整结果（本轮实测）

```
编译检查: python3.11 -m compileall -q adapters tests  -> COMPILE_OK
新增单测: pytest tests/test_new_adapters.py -q         -> 53 passed (原15 + 新增38)
全量非网络: pytest -m "not network" -q                 -> 123 passed, 6 deselected (原85 + 新增38)
集成测试: pytest -m "integration" -q                   -> 15 passed
真实数据冒烟: 新浪/腾讯各7行，amount_available=False，sum_real_amount=0.0，双源收盘价一致
```

无回归。完整原始输出见 `reports/phase2/{unit,integration,new_adapters}_result.txt`。
本轮按审查要求未重跑网络测试（上一提交网络测试为 3 passed B2 / 2 failed AKShare 限频 / 1 skipped BaoStock，状态未变）。

---

## 六、修改文件清单
- `adapters/base.py`：加 `amount_available` 字段 + 数据契约函数。
- `adapters/sina_adapter.py`：结构校验 + 代码映射规则 + 成交额标记。
- `adapters/tencent_adapter.py`：逐层结构校验 + 代码映射规则 + 成交额标记。
- `tests/test_new_adapters.py`：删无用 import + 新增 38 项测试。
- `reports/phase2/{unit,integration}_result.txt`、`reports/phase2/new_adapters_result.txt`（新）：本轮真实结果。
- `reports/phase2/b2-review-fixes.md`（本报告）。

提交 SHA 与完整 diff 见交付摘要。本提交**不代表最终验收通过**，等待 ChatGPT 对新 SHA 再次独立审查。
