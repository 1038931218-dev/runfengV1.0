# D2 市场宽度与指数分析 — 设计与数据依赖核验

- 阶段：D2（市场宽度与指数分析）
- 开发基线：`b2307b515a7d1730a05c27d6ccdc6b38647dd535`
- 分支：`dev/d-analysis-engine`
- 父阶段：D1 两轮独立验收已通过
- 设计依据：`reports/phase3/d-analysis-design.md` 第 6 节（D0-04 样本级统计，已验收）
- 生成时间：2026-11-20

---

## 一、数据依赖核验（只读，真实库未改动）

核验方式：以 `mode=ro` 只读连接 `data/invest_center.db`，前后 SHA256 均为
`c5668fdac053ee1b11e713e0c598f036997951132bda7f31ad8d847d879fb8cc`。

| 依赖项 | 实际现状 | D2 判定 |
|---|---|---|
| `index_quote` | 3 指数（上证 `000001.SH`、沪深300 `000300.SH`、深证成指 `399001.SZ`）× 10 交易日（2025-11-03 ~ 11-14），全部 akshare / history | ✅ 指数日涨跌幅可做；指数成交量单位**未核实** → 量能类指标标记 `volume_unit_unverified`，不套用个股 ×100 |
| `daily_quote` | 仅 3 只（000001 / 300750 / 600519）× 10 交易日，全部 akshare / none / daily / history | ⚠️ 只能做**样本级**统计，全部字段 `sample_*` 前缀，禁止冒充全市场 |
| `stock_info` | **0 行**（无交易所/上市日期/行业字段数据） | ⛔ 上市时间限制、北交所覆盖 → `unavailable/blocked`，声明无法判定，不臆断 |
| 停牌 / 交易日历 | 无停牌字段；`is_trading_day()` 仅工作日过滤，无精确交易日历 | ⚠️ 当日无行情只标注 `missing_quote_symbols`，不臆断为停牌或漏采 |
| 成交额 | akshare 为真实值（`amount_available=True`）；sina/tencent 为占位 | ✅ 复用 `amount_available()` / `sum_real_amount()`；占位不进 `sample_amount_total` |

### 阻塞与限制清单（必须随结果输出，不得省略）

1. **全市场覆盖率阻塞**：`stock_info` 为空、无全市场股票基数 → 不计算 `market_coverage_ratio`，
   返回 `unavailable(reason=universe_base_unavailable)`，不以 3 只样本臆算覆盖率。
2. **北交所覆盖阻塞**：适配器明确不支持北交所（8/4 段位），且无交易所字段 → 结果声明
   `beijing_exchange_coverage=not_supported`，不纳入、不伪造。
3. **上市时间限制阻塞**：缺 `list_date` → 无法判定上市未满窗口 → 声明 `listing_date_unavailable`。
4. **停牌缺失**：无停牌字段与交易日历 → 只列出当日无行情标的，不判定停牌原因。
5. **指数成交量单位未核实**：指数量能类标记 `volume_unit_unverified`，不参与跨源量能比较。
6. **样本用途限制**：`sample_*` 仅描述当前样本，不得作为全市场趋势信号或买卖依据。

> 结论：D2 **不阻塞**——样本级统计 + 明确的阻塞/限制声明即为 D0 规定的合规形态。
> 严禁把 3 只样本包装成全市场真实宽度。

---

## 二、架构与 API 设计

新增 `analysis/market.py`（D0 第 6 节指定文件名），纯函数、不查库、不联网，
调用方注入记录（dict / 对象），与 D1 同纪律。

### 2.1 结果契约

复用 D1 的状态常量与 `IndicatorValue`（ok / insufficient_data / rejected / unavailable），
新增 D2 专用 `MarketAnalysisResult`（含强制口径字段与阻塞声明），定义于
`analysis/market_contracts.py`（不污染 D1 `contracts.py`）。

```
MarketAnalysisResult
├─ data_as_of              # 数据截止交易日
├─ trade_date              # 本次统计交易日
├─ index_results           # {index_code: {指标名: IndicatorValue}}
├─ values                  # {sample_* 指标名: IndicatorValue}
├─ # —— 强制口径字段（缺一不可）——
├─ sample_size             # 当日实际纳入统计标的数
├─ universe_definition     # 样本定义
├─ coverage_note           # 覆盖说明（非全市场、不可外推）
├─ # —— 缺失披露 ——
├─ excluded_symbols        # [{symbol, reason}] 被排除标的
├─ missing_quote_symbols   # [symbol] 当日无行情标的
├─ # —— 阻塞/限制声明 ——
├─ market_coverage_ratio   # 恒 unavailable
├─ beijing_exchange_coverage = "not_supported"
├─ listing_date_limitation = "unavailable"
├─ index_volume_unit = "unverified"
├─ usage_limitation        # 用途限制文本
├─ notes / warnings
```

### 2.2 核心函数

| 函数 | 职责 | 关键约束 |
|---|---|---|
| `index_pct_change(index_records, *, index_code, trade_date)` | 单指数相邻交易日 close 涨跌幅 | 公式同 D1 5.2；复权口径 = none（指数无复权）；量能不在此函数 |
| `sample_breadth(stock_records, *, trade_date, adjust_type)` | 样本级涨跌平家数 + 涨跌比例 + 趋势状态 | 全部 `sample_*` 前缀；逐标的日涨跌幅；不含当日无行情标的 |
| `sample_volume_total(stock_records, *, trade_date)` | 样本成交量合计（归一为股） | 复用 D1 `normalize_volume` / `from_raw_volume`；源单位 akhare 手×100 |
| `sample_amount_total(stock_records, *, trade_date)` | 样本成交额合计 | 仅全部 `amount_available=True` 才算；含占位则 `unavailable` + 列出排除来源 |
| `analyze_market(*, index_records, stock_records, trade_date, ...)` | 编排入口，组装 `MarketAnalysisResult` | 强制口径字段、缺失披露、阻塞声明随结果输出 |

### 2.3 逐标的择源与复权

- 来源混用：按 D0 4A.1 逐标的、逐日择源（复用 D1 `_source_candidate` 精神），
  整行来自单一来源，禁止跨源拼 OHLCV；
- 每个 `sample_*` 结果绑定 `adjust_type`，混入不同口径的记录在编排层拒绝进入同一次统计；
- 缺失日：当日无行情标的列入 `missing_quote_symbols`，不计入 `sample_size`。

---

## 三、成交额契约（任务书重点 4）

- 复用 `adapters.base.amount_available()` 与 `adapters.base.sum_real_amount()`；
- 判定优先级与 B2/B3 一致：对象属性 → 映射字段 → source 兜底（sina/tencent 为占位源）；
- `sample_amount_total`：**仅当所有纳入样本的 `amount_available=True`** 才计算并求和；
  任一来源为占位 → 返回 `unavailable(reason=amount_placeholder_excluded)`，
  在 warnings 列出被排除来源；`amount_available=False` 不导致整条行情被拒绝（仍参与价格/量能统计）。

---

## 四、测试矩阵（任务书重点 6）

| 类别 | 覆盖 |
|---|---|
| 指数涨跌幅 | 正常相邻日、单日 insufficient、空数据、缺当日、异常价格、来源异常 |
| 样本宽度 | 涨/跌/平计数、比例、趋势状态、空数据、样本不完整 |
| 成交额契约 | akshare 真实值求和；sina/tencent 占位 → unavailable + 排除来源；False 不拒绝行情 |
| 样本量能 | 归一为股（手×100）、幂等、C 态拒绝 |
| 覆盖率纪律 | stock_info 空 → market_coverage_ratio unavailable |
| 阻塞声明 | 北交所 not_supported、上市时间 unavailable、指数单位 unverified、用途限制 |
| 复权/来源 | 混入不同 adjust_type 拒绝进入同次统计；逐标的择源可追溯 |

---

## 四·补：R1 独立审查修复（算法版本 v1 → v2）

ChatGPT 第一轮独立审查发现 2 项 P1 + 1 项 P2，均已修复。`ALGORITHM_VERSION_D2` 递增为 `d2-market-v2`。

| 编号 | 问题 | 修复口径 |
|---|---|---|
| P1-01 交易日连续性 | 指数/个股各自取「自身最近一条历史记录」当前收，可跨过缺失交易日算多日涨跌幅 | 新增 `observable_trading_dates()`（优先指数行情日期、退回股票日期）建立本次分析的**可观察交易日序列**，编排层统一注入指数与个股；前收必须取该序列中严格前一交易日，且**必须验证该标的当日有合格行情**，缺失/无法验证 → unavailable（`previous_trading_day_missing` / `previous_trading_day_unverifiable`），**禁止取更早日期代替**。明确声明该序列非交易所法定交易日历（`trading_calendar_basis`）。血缘新增 `observable_trading_dates` / `prev_td_missing_symbols` 字段。 |
| P1-02 成交量归一失败仍 OK | `sample_volume_total` 对归一异常仅记 warning，仍把部分合计标为 OK | 任一纳入样本记录归一失败 → `sample_volume_total` 整体 unavailable（`volume_normalization_failed`），`params.failures` 披露失败标的/来源/原因，不把部分合计当完整样本。 |
| P2-01 成交额静默漏算 | `sum_real_amount()` 静默跳过缺失/None/非法 amount，部分合计仍标 OK | 复用 `amount_available()` 判定可用性后，**统计前逐条验证**每条真实成交额记录的 amount 存在、有限、非负；任一条无法确认 → `sample_amount_total` unavailable（`amount_invalid_in_sample`），披露失败标的/来源/原因；全部通过仍走 `sum_real_amount()` 求和，不绕过 B3 契约。 |

新增原因码：`previous_trading_day_missing`、`previous_trading_day_unverifiable`、`volume_normalization_failed`、`amount_invalid_in_sample`。
新增测试（13 项）：指数/个股中间交易日缺失不回退、前一交易日存在正常计算、无可观察前日 unverifiable、编排层日历血缘、`volume_total` 归一失败不可用/全正常可用、成交额缺字段/None/非法/负数/正常混合/全有效复用契约。D2 测试 28 → 41 全通过。

---

## 四·补 2：R2 独立复核修复 — P1-01 残留（算法版本 v2 → v3）

ChatGPT 第二轮复核发现 P1-01 残留缺陷：`observable_trading_dates()` 原实现「指数有合格日期就直接返回、忽略股票日期」，与 `TRADING_CALENDAR_BASIS` 声明的「指数优先、合并股票」不一致；当指数缺某交易日而股票有该日数据时，全局序列遗漏该日，个股前收仍可能跨日计算。已修复，`ALGORITHM_VERSION_D2` 递增为 `d2-market-v3`。

| 项 | 修复口径 |
|---|---|
| 日期并集 | `observable_trading_dates()` 改为纳入本次输入中**指数与股票全部合格行情日期的并集**（去重、升序），不再「指数非空即忽略股票」；指数优先仅作数据来源说明，不剔除股票已有日期。 |
| 来源披露 | 新增 `trading_calendar_breakdown()` 返回 `index_dates`/`stock_dates`/`merged_dates`/`index_only`/`stock_only`/`index_first`；血缘新增 `trading_calendar_source` 字段，`to_dict()` 输出，编排层 notes 追加序列来源说明。 |
| 共用序列 | `analyze_market` 计算一次并集 `cal` 同时注入 `index_pct_change()` 与 `sample_breadth()`，两者判定一致。 |
| 契约一致 | 同步更新 `TRADING_CALENDAR_BASIS` 文本、函数 docstring、注释、设计报告：明确「并集、非交易所法定交易日历」；前一交易日缺目标标的合格行情仍 unavailable，禁止取更早日期。 |

新增测试（8 项）：并集含股票独有 d4、来源差异披露、股票 d5 用 d4 当前收、指数缺 d4 不用 d3、指数目标日缺失但股票有则全局保留、日期一致无回归、空指数时股票建序列、to_dict 来源披露与非法定日历声明。D2 测试 41 → 49 全通过。

---

## 五、开发纪律

- 只改 D2 涉及文件（`analysis/market*.py`、`analysis/__init__.py` 导出、`tests/test_analysis_market.py`、本报告）；
- 不改 D0 主设计文档、不碰 D1/其它模块逻辑、不碰真实库、不碰 main、不启动 D3–D5/E；
- 全程 Decimal + ROUND_HALF_UP（对齐 D1 精度），状态语义与 D1 一致。
