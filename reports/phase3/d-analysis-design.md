# D 阶段设计报告：分析引擎（D0 只读盘点与方案设计）

- 阶段：D0（只盘点 + 只设计，不改业务代码、不实现 D1–D5）
- 基线：`dev/b3-collector` @ `9e9ef97d25bbe06ac967d47c43ea5abb10c68bf3`（B3 已验收提交）
- 开发分支：`dev/d-analysis-engine`（从上述基线新建，未合并 main）
- 盘点时间：2026-11-19 (Asia/Shanghai)
- 第二轮修订：2026-11-19 (Asia/Shanghai)，针对 D0 第一轮审查意见逐项修订
- 仓库：https://github.com/1038931218-dev/runfengV1.0
- 状态：**第二轮设计修订稿，等待 ChatGPT 第二轮独立复核；D1 尚未启动，审查通过前不实现 D1–D5**

---

## 0A. 第二轮审查修订对照表（本次修订）

仅修改本设计报告，未改业务代码、数据库、真实行情数据，未开发 D1–D5。

| 审查项 | 修订落点 | 处理摘要 |
|---|---|---|
| D0-01 来源优先级 | 新增 **4A 来源选择策略**；5.1、12 | 默认改为 `sina_primary`（新浪主、腾讯备，与 B3 一致）；AKShare 仅显式配置启用；逐交易日择一、不跨源拼 OHLC；价格偏差超阈值标记异常不盲目择优 |
| D0-02 成交量单位 | 4A.2、4（血缘）、5.1、6、10 | 标准单位「股」；akshare/腾讯 ×100、sina ×1；记录原始/归一单位；幂等归一函数只执行一次；三来源+重复归一测试；指数单位单独核实前不套用个股规则 |
| D0-03 分析血缘 | 4（统一结果契约） | `AnalysisResult` 增加 `sources_used`、`source_row_counts`、`source_by_trade_date`、`volume_unit_original`、`volume_unit_normalized`；混用来源逐交易日可追溯 |
| D0-04 D2 样本级统计 | 6 | 字段改 `sample_*` 命名；强制 `sample_size/universe_definition/coverage_note`；缺当日行情披露；样本统计不得当全市场信号；无可靠覆盖率则不计算覆盖率 |
| D0-05 D5 风险计算 | 9 | 补齐 9 条细则：买卖价校验、100 股向下取整、双向费用、费率和费率版本/日期、滑点、三类 rejected、Decimal、零股例外 |
| 六、指标边界 | 5.2、5.4 | RSI 除零规则、EMA/MACD 初始化与首个有效位置、非交易日≠数据缺口、不复权价格跳变影响 |

> 特别提示（D0-01 后果披露）：真实库现有 30 行日线全部为 `akshare`。默认 `sina_primary`
> 策略下这些行**不参与**指标计算；如需用真实库 akshare 数据做 D1，必须显式配置
> `source_policy=akshare_only` 并接受其成交量单位（手→股）与成交额可用的口径。见 4A.1。

---

## 0. 执行前置核验（已完成）

| 核验项 | 结果 |
|---|---|
| 工作区干净 | 通过（git status 空） |
| 当前分支（切分支前） | `dev/b3-collector` |
| HEAD 完整 SHA | `9e9ef97d25bbe06ac967d47c43ea5abb10c68bf3` |
| B3 提交在历史中 | 通过（HEAD 即 B3 提交） |
| main / review/round5 | `88d1145` / `020959c`，未触碰 |
| 新分支 | `dev/d-analysis-engine`，HEAD 仍为 B3 |
| 真实库只读打开 | 全程 `mode=ro`，未写入、未改结构 |

---

## 1. 只读盘点结果（现状，不含设计意见）

### 1.1 数据库表结构与唯一键

库版本 `user_version=2`，共 6 张业务表：`daily_quote`、`index_quote`、`fetch_log`、
`stock_info`、`announcement`、`data_quality_log`。

| 表 | 主键 | 唯一键（UNIQUE） | 与 D 阶段相关字段 |
|---|---|---|---|
| `daily_quote` | `id` 自增 | `(code, trade_date, adjust_type, period, source)` | code, trade_date, OHLC, volume, amount, adjust_type, period, source, quote_time, collected_at, data_type |
| `index_quote` | `id` 自增 | `(index_code, trade_date, source)` | index_code, trade_date, OHLC, volume, amount, source, quote_time, collected_at, data_type（**无** adjust_type/period） |
| `stock_info` | `code` | — | code, name, exchange, industry, list_date, source, collected_at |
| `fetch_log` | `id` | — | source, task, status, rows, elapsed_ms, started_at, finished_at |

索引：`idx_dq_code_date`、`idx_dq_date`、`idx_dq_period`、`idx_ann_code`、`idx_ann_date`。

### 1.2 真实库数据覆盖（只读统计）

| 表 | 行数 | 标的数 | 日期范围 | 来源 | 复权 | 周期 | data_type |
|---|---|---|---|---|---|---|---|
| `daily_quote` | 30 | 3（000001 / 300750 / 600519） | 2025-11-03 ~ 2025-11-14 | 全部 akshare | 全部 none | 全部 daily | 全部 history |
| `index_quote` | 30 | 3（000300.SH / 000001.SH / 399001.SZ） | 2025-11-03 ~ 2025-11-14 | 全部 akshare | — | — | 全部 history |
| `stock_info` | **0** | 0 | — | — | — | — | — |
| `announcement` | **0** | 0 | — | — | — | — | — |
| `fetch_log` | 19 | — | 2026-11-18（采集日志时间） | — | — | — | — |

**每只标的仅 10 个交易日数据**（2025-11-03 至 11-14）。数据为约一年前历史日线，
非最新交易日。B3 真实端到端采集写入的是**临时库**（证据 JSON 标注 `<temporary, auto-cleaned>`），
**未写入真实库**。

### 1.3 关键数据语义发现（务必审查）

**发现 A — 成交量单位跨来源不一致（高危）**
- 新浪适配器：`volume` 单位 = **股**（`sina_adapter.py` 直接取源字段）。
- 腾讯适配器：源单位 = **手**，乘 `VOLUME_UNIT_MULTIPLIER=100` 归一为 **股**。
- akshare 适配器：`volume=float(row.成交量)` **直接透传，未归一**。交叉验证坐实其为**手**：
  真实库 000001 某日 `close=11.43, volume=952326, amount=1084422503.85`，
  `amount/close/volume≈99.62≈100` ⇒ akshare volume 为「手」。
- 结论：同一 `daily_quote` 表中，**akshare 行 volume 是手，sina/tencent 行是股**，
  两者量级差 100 倍。任何量能/均量/量比指标若跨来源混算将产生 100 倍错误。

**发现 B — 成交额可用性**
- `sina`/`tencent` 无成交额字段，`amount=0.0` 为占位，`amount_available=False`。
- `akshare`（真实库现有数据）`amount` 为真实成交额（非零，量级亿元），按 source 推断可用。
- 契约函数 `amount_available(quote)`（base.py）：对象属性 → dict/Row 显式字段 → source 兜底
  （`AMOUNT_PLACEHOLDER_SOURCES={"sina","tencent"}`）。
- 统计入口 `sum_real_amount(quotes)` 排除占位值。数据库表**无** `amount_available` 列，
  历史行靠 source 推断。

**发现 C — 来源重复**
- 唯一键含 `source`，同一标的同一交易日可同时存在新浪行与腾讯行（B3 定性为「跨来源唯一/来源留存」）。
- 指标计算前必须按来源策略去重，否则同日双源数据被重复计入。

**发现 D — 复权口径**
- 全部现有数据 `adjust_type=none`（不复权）。B2 适配器对 qfq/hfq 明确抛 `ParameterError` 拒绝。
- 指数表无 adjust_type/period 字段。

**发现 E — 交易日判断**
- `utils/timezone.is_trading_day()` 仅做周一至周五工作日过滤，**不含法定节假日表**，
  不能作为精确交易日历使用。

### 1.4 质量门禁契约（quality/gate.py）

`check_quality(quotes, start, end, *, kind="stock") -> QualityReport`，返回
`{ok, reasons, rows, duplicates, duplicate_dates}`，**不抛异常**。校验项：非空、必填字段、
日期在区间内、OHLC 有限且为正且逻辑自洽（high≥max, low≤min）、volume 非负、
`data_type==history`、成交额契约字段可判定、批内无重复交易日。B3 已加 `_quality_of()`
将检查函数自身异常归一为 `ok=False`。

**门禁当前局限（供 D 阶段补强，不在 D0 修改）**：
- 不校验 volume 单位一致性；
- 不校验同一 (code, trade_date) 是否混入多来源；
- 不校验跨批次交易日的连续性/缺失；
- `kind` 仅用于文案，个股与指数同一套字段校验。

### 1.5 标准行情模型（adapters/base.py）

`StandardQuote` / `StandardIndexQuote` dataclass，字段见 base.py；两者均带
`amount_available: bool=True` 血缘字段。`DATA_HISTORY/REALTIME/DELAYED`、
`ADJ_NONE/QFQ/HFQ` 常量已定义。仓储层 `DailyQuoteRepo.query()` 默认 `connect()` 连
**默认真实库**（无 db_path 参数），D 阶段读取需显式注入受控路径以隔离。

### 1.6 现有模块 / 测试 / 配置 / 日志 / 报告

- **分析类模块：全部为空壳**。`analysis/`、`scheduler/`、`report/`（含 `templates/`）
  仅 `__init__.py`，无任何指标、市场统计、板块、候选股、风控实现。
- 全局 grep（MACD/RSI/均线/波动率/板块/行业/候选/止损/仓位等）在业务 .py 中**零命中**。
- 测试框架：pytest，markers = `network`、`integration`；`conftest.py` 提供 `tmp_db` fixture
  （临时库）。现有测试覆盖 B1–B3，非网络基线 174 passed / 8 deselected。
- 配置：`config/config.yaml`（非敏感）+ `.env`（密钥）；`analysis.max_candidates=5`、
  `risk.account_cap_cny=10000`、`risk.single_trade_risk_min/max=50/100`。
- 日志：`utils/logger.py`，控制台+文件双输出，北京时间，logger.propagate=False。
- 报告目录：`reports/phase2/`、`reports/b2_evidence/`、`reports/b3_evidence/`；本阶段新建
  `reports/phase3/`。
- 依赖：pandas 3.0、numpy 2.3、pytest 9.0、PyYAML、python-dotenv、pytz、requests、APScheduler。
  **资金风险计算将用 Decimal（标准库），不依赖浮点。**

---

## 2. 数据能力边界总表（D1–D5 可实施性判定）

| 模块 | 数据依赖 | 现状判定 | 结论 |
|---|---|---|---|
| **D1 技术指标** | 单标的连续日线、复权/周期明确 | 每标的仅 10 天；MA5/MA10、量比短窗可做，MA20/MA60、MACD(26,9)、RSI14 数据不足 | **可做，但大量指标返回 `insufficient_data`**；需先解决成交量单位与多来源去重 |
| **D2 市场宽度/指数** | 足够股票样本覆盖 + 指数日线 | 仅 3 只股票，`stock_info` 为空（无交易所/上市日期/行业），无全市场覆盖、无停牌信息 | **只能做「样本级」统计且必须明确标注非全市场**；不得包装成全市场真实统计 |
| **D3 行业/板块** | 可靠行业分类数据 | `stock_info.industry` 列存在但全空，无板块分类、无可靠映射来源 | **`blocked_by_data_dependency`**：只输出接口设计 + 数据缺口报告，不实现、不编造 |
| **D4 候选筛选** | D1 指标 + 数据质量 + 名称 | 名称可从 daily_quote.name 取；但 D1 多数指标数据不足、样本仅 3 只 | **可搭框架，初版大概率输出 0 只**；严禁凑数；依赖 D1 数据充足性 |
| **D5 风险约束** | 纯参数计算，不依赖行情库 | 仅需输入模拟账户参数 | **可完整实现**，用 Decimal 精确计算，模拟账户测试 |

---

## 3. 总体架构与分层（待审查后实现）

沿用项目既有分层风格，新增模块全部落在 `analysis/` 与 `report/`：

```
analysis/
  models.py          # 分析结果数据结构（AnalysisResult / IndicatorSet / ...）
  provenance.py      # 统一血缘与时间字段构造（calculation_time/algorithm_version 等）
  source_policy.py   # 来源选择 + 同标的同日去重（默认每标的每日仅选一个来源）
  data_loader.py     # 受控 db_path 读取 + 输入校验 + 连续性检查（不跨缺失日拼接）
  indicators.py      # D1：MA/MACD/RSI/量能/涨跌幅/波动率（确定性）
  market.py          # D2：指数涨跌 + 样本宽度统计（明确标注样本口径）
  sector.py          # D3：接口 + 阻塞报告（无可靠分类不实现计算）
  candidates.py      # D4：可解释规则筛选，最多 5 只
  risk.py            # D5：Decimal 风险约束计算
quality/
  gate.py            # 复用 B3 门禁；D 阶段新增分析输入校验函数（审查后再改）
report/
  templates/         # 分析结果渲染（本阶段不实现报告生成）
```

依赖方向：`data_loader → source_policy → indicators/market/candidates`；`risk` 独立；
所有模块输出统一 `AnalysisResult`。控制器/调用方组装，计算层不依赖 HTTP/DB 连接细节。

---

## 4. 统一结果契约（所有分析输出必带）

```
AnalysisResult / IndicatorSet 公共字段：
  symbol/code           标的或指数代码
  calculation_time      ISO 北京时间，精确到秒
  data_end_date         实际参与计算的最新交易日（YYYY-MM-DD）
  source_policy         来源选择策略名，如 "sina_primary" / "akshare_only"（D0-01）
  sources_used          本次计算实际用到的来源列表，如 ["sina","tencent"]（D0-03）
  source_row_counts     各来源贡献行数，如 {"sina": 8, "tencent": 2}（D0-03）
  source_by_trade_date  逐交易日来源映射，如 {"2026-11-03":"sina","2026-11-04":"tencent"}（D0-03）
  volume_unit_original  逐来源原始单位，如 {"sina":"股","tencent":"手","akshare":"手"}（D0-02）
  volume_unit_normalized 归一后标准单位，恒为 "股"（D0-02）
  adjust_type           复权口径（指数标 N/A）
  period                周期（daily）
  input_row_count       实际参与计算的行数（去重、归一后）
  quality_status        ok | insufficient_data | rejected | unavailable
  source_divergence     同日跨源价格偏差超阈值的日期与偏差值（D0-01）；无则空
  algorithm_version     如 "d1-indicators-v1"
  values                {指标名: 数值 或 状态对象}
  notes/warnings        数据缺口、单位换算、来源去重、跳变等说明
```

**来源可追溯性（D0-03 强制）**：一次计算窗口内即便混用多来源，`sources_used` 给出来源全集、
`source_row_counts` 给出各来源行数、`source_by_trade_date` 给出**每一个交易日**实际取值来源；
不得以单一 `source` 字段概括整个窗口。每个交易日的 OHLCV 整行来自同一来源，禁止跨来源拼接。

可复算保证：同一输入 + 同一参数 + 同一 `algorithm_version` ⇒ 结果逐字段一致；
计算时间字段不参与数值比较（用响应内附带的规则版本而非当前时间影响结果）。

---

## 4A. 来源选择策略与成交量单位契约（D0-01 / D0-02）

### 4A.1 来源选择策略（对齐 B3）

**默认策略 `sina_primary`：新浪主源、腾讯备源，与 B3 采集器完全一致。**

| 策略名 | 含义 | 是否默认 |
|---|---|---|
| `sina_primary` | 逐交易日：新浪优先，新浪当日缺失/不合格则用腾讯；逐标的独立择源 | **是** |
| `akshare_only` | 仅用 AKShare（用于分析真实库既有 akshare 数据） | 否，须显式指定 |
| `tencent_only` | 仅用腾讯 | 否，须显式指定 |

规则（D0-01）：
1. **默认不覆盖 B3 策略**：AKShare 仅作为可配置来源，绝不默认启用；未显式指定时，
   AKShare 行不参与计算，即使真实库现有数据全是 akshare（后果见文首提示）。
2. **选择维度**：对每个 (标的, 交易日) 的候选行，依次考察
   ①数据质量（B3 门禁 `ok`）、②日期（落在窗口内且为实际交易日）、
   ③复权口径（`adjust_type` 与请求一致，不一致者排除）、
   ④成交量单位（已归一，见 4A.2）——四项均合格才进入择优。
3. **禁止拼接**：单个交易日的 OHLCV 整行必须来自同一来源，
   **不允许**把新浪的 close 与腾讯的 volume 等跨来源字段拼成一条行情。
   窗口内不同交易日可使用不同来源，但逐日来源写入 `source_by_trade_date`。
4. **价格偏差守护**：同一 (标的, 交易日) 若存在多来源，比较其收盘价：
   `deviation=|close_a - close_b| / max(close_a, close_b)`。
   超过可配置阈值（`source_price_divergence_threshold`，默认 0.01 即 1%）时，
   **标记 `source_divergence` 异常并不盲目择优**——默认放弃该交易日（记为不可用并在
   notes 说明），不静默选“看起来更合理”的来源；阈值可在配置中调整并记录到血缘。
5. 择源结果（选中的来源、被排除来源、偏差情况）全部写入第 4 节血缘字段。

### 4A.2 成交量标准单位契约（D0-02）

**标准单位统一为「股」。归一规则表（写入数据契约，作为算法版本一部分固化）：**

| 来源 | 原始单位 | 归一到「股」 | 说明 |
|---|---|---|---|
| `sina` | 股 | ×1（不转换） | 源字段即股 |
| `tencent` | 手 | ×100 | `VOLUME_UNIT_MULTIPLIER=100`，与 B3 一致 |
| `akshare` | 手 | ×100 | 交叉验证 `amount/close/volume≈99.62≈100` |

规则：
1. **写入标准数据契约**：`analysis/units.py`（设计层面）定义
   `VOLUME_UNIT_ORIGINAL_BY_SOURCE = {"sina":"股","tencent":"手","akshare":"手"}` 与
   归一函数 `normalize_volume(volume, source) -> Decimal|float`，标准单位常量 `STANDARD_VOLUME_UNIT="股"`。
2. **记录双重单位**：每条结果保留 `volume_unit_original`（逐来源原始单位）与
   `volume_unit_normalized`（恒 `"股"`），见第 4 节血缘字段。
3. **只归一一次（幂等防护）**：归一在数据加载层 `data_loader` 入口执行且仅执行一次；
   归一化后的行打标 `volume_normalized=True`（内存标记），指标层只消费归一后值。
   归一函数内二次校验：若入参行已带 `volume_normalized=True` 或已存在 `volume_std` 字段，
   **直接返回不重复乘 100**，从根本上防止重复归一导致 100 倍放大。
4. **测试覆盖（D1 阶段实现）**：三个来源各自归一正确性（akshare 手→股、tencent 手→股、
   sina 股不变）+ 重复归一防护（同一行归一两次结果不变）。
5. **指数成交量单独核实（重要）**：`index_quote` 的 volume 单位**不与个股共用**上述规则；
   指数成交量单位在 D2 实现前需单独核实（不同指数数据源字段口径可能不同）。
   核实完成前，D2 的指数量能类指标标记 `volume_unit_unverified`，不得套用个股 ×100 规则，
   也不得参与跨来源量能比较。

---

## 5. D1 技术指标引擎设计

### 5.1 输入与清洗
- 输入：`code, start_date, end_date, adjust_type, source_policy`（接口名可微调，语义不变）。
- 读取受控 db_path 的 `daily_quote`，过滤 `period`、`adjust_type`、`data_type=history`；
- **来源策略 `source_policy`**：默认 `sina_primary`（新浪主、腾讯备，与 B3 一致），
  AKShare 仅显式配置启用；逐交易日择一、禁止同日双源重复计入、禁止跨源拼 OHLC、
  价格偏差超阈值标记异常——完整规则见 **4A.1**；
- **成交量单位归一**：进入指标前在数据加载层按 4A.2 一次性归一到「股」
  （akshare ×100、tencent ×100、sina ×1），带幂等防护防重复乘 100，
  原始/归一单位写入血缘，换算记录写入 `notes`，算法版本包含该规则。
- 输入校验：缺失值、重复交易日、非正价格、OHLC 逻辑非法 → 拒绝或标记 `rejected`；
  交易日缺口与价格跳变的处理见 **5.4**。

### 5.2 指标公式与参数

| 指标 | 公式 / 参数 | 最小样本 | 精度 |
|---|---|---|---|
| MA5/10/20/60 | 简单移动平均 `MA_n(t)=Σclose[t-n+1..t]/n` | n 个有效交易日 | 保留 4 位小数（round half up） |
| MACD | EMA12、EMA26；`DIF=EMA12-EMA26`；`DEA=EMA(DIF,9)`；`MACD柱=2*(DIF-DEA)`（通达信口径）；**初始化**：EMA12/EMA26 首个值取第 1 个 close，`EMA_t=α*x_t+(1-α)*EMA_{t-1}, α=2/(n+1)`；DEA 首个值取第 1 个 DIF。**首个有效输出位置**：DIF 自第 1 根可算但处于预热期；完整有效 MACD 需 EMA26 收敛，标注 `warmup` 至第 26 根、稳定输出自第 34 根起（见 5.4） | 完整有效需 ≥34 个（预热期输出带 `warmup` 标记） | 4 位 |
| RSI | RSI14，**Wilder 平滑**（α=1/14）：首次 AVG Gain/Loss=14 期简单均值，其后 `AVG=prev*(13)+curr/14`；`RSI=100-100/(1+RS)`，RS=AVG Gain/AVG Loss；**首个输出**在第 15 根 close（第 1 根无前值）。**除零规则见 5.4** | 需 ≥15 个 close（14 涨跌 + 首值） | 2 位 |
| VOL_MA5/10 | 成交量（已归一为股）的简单均线 | n 个 | 整数股 |
| 量比 | 定义待审查确认，建议：`量比=当日成交量 / VOL_MA5(不含当日,前5日均量)`；窗口 5，输出明确窗口标签 | ≥6 个 | 4 位 |
| 日涨跌幅 | `(close_t - close_{t-1})/close_{t-1} * 100%` | 2 个 | 4 位（%） |
| 区间涨跌幅 | `(close_end - close_start)/close_start * 100%` | 2 个 | 4 位（%） |
| 波动率 | 首版**非年化**：N=20 日收盘价对数收益的样本标准差（ddof=1）；明确标注 `annualized=false`。年化口径（×√252）留待审查后决定 | ≥21 个 | 6 位 |

### 5.3 数据不足 / 异常策略
- 样本不足 → 该指标值返回 `{"status": "insufficient_data", "required": n, "actual": m}`，
  **不填 0、不用残缺窗口硬算**；其它满足的指标正常输出。
- 输入含非法值 → `quality_status=rejected`，附原因，不输出任何指标。
- 指标绑定 `adjust_type` 与 `period`；跨复权口径的数据不混入同一次计算。

### 5.4 指标边界与特殊情形（审查补充六）

**5.4.1 RSI14 除零规则**：设 `avg_gain`、`avg_loss` 为 Wilder 平滑后的平均涨/跌幅。
- `avg_loss == 0 且 avg_gain > 0` → `RSI=100`（单边上涨）；
- `avg_gain == 0 且 avg_loss > 0` → `RSI=0`（单边下跌）；
- `avg_gain == 0 且 avg_loss == 0`（窗口内价格完全无变动）→ 定义 `RSI=50`（中性），
  **不发生除零**，并在 notes 标注 `flat_window`；
- 其余按 `RSI=100-100/(1+avg_gain/avg_loss)` 正常计算。

**5.4.2 EMA/MACD 初始化与首个有效输出**：
- 初始化：EMA12/EMA26 首值 = 第 1 个 close；DEA 首值 = 第 1 个 DIF（=0 附近）。
- 预热标记：第 1~25 根 DIF/DEA 处于 EMA 预热期，输出值附带 `warmup=true`，
  便于调用方识别其仅供参考；第 26 根起 EMA26 完成一个完整周期，第 34 根（26+9-1）
  起 DEA 也完成 9 周期收敛，标记为完整有效输出。
- 样本 <26 根时 MACD 返回 `insufficient_data`（required=34），不输出预热期值以免误用。

**5.4.3 非交易日 vs 数据缺口（不得混淆）**：
- 本项目**无精确交易日历**（见 1.3-E，`is_trading_day()` 仅工作日过滤）。
  因此只能标注「交易日之间的日期缺口」，**不得据此擅自判定为“漏采”或“数据缺陷”**——
  缺口可能来自周末、法定节假日或停牌。
- 实现：检测相邻两点日期差，若为非工作日跨度则在 notes 标注 `date_gap`（仅描述事实）；
  **不触发** `quality_status=rejected`，不臆断采集中断。
- 只有引入权威交易日历后，才能把非交易日缺口与真实漏采区分，届时再升级判定逻辑（D0 不做）。

**5.4.4 不复权价格跳变的影响**：
- 现有数据均为 `adjust_type=none`。除权除息日 close 会出现非交易性跳变，
  会被 MA、涨跌幅、RSI、波动率误读为剧烈波动。
- 处理：计算日涨跌幅时若 `|pct_change|` 超过可配置阈值（默认 10%，接近涨跌停幅度，
  参数化），在结果 marks 中标注 `possible_ex_rights_jump`（疑似除权除息跳变），
  并在 notes 提示“不复权数据，跳变可能来自除权除息而非真实涨跌”。
- **不自动前复权、不修改原始 close**（保持可追溯）；跳变日照常参与计算但带标记，
  是否剔除由调用方按策略决定；后续接入复权数据后另行处理。

---

## 6. D2 市场宽度与指数分析设计（D0-04 样本级统计）

- 指数日涨跌幅：基于 `index_quote` 单指数相邻交易日 close，公式同 5.2；
  指数成交量单位未核实前，量能类指标标记 `volume_unit_unverified`（见 4A.2 第 5 条）。
- **样本级统计（禁止冒充全市场）**：基于当前 `daily_quote` 股票样本（现状仅 3 只），
  **所有字段以 `sample_` 前缀命名**，明确只是样本描述：
  - `sample_advancers` / `sample_decliners` / `sample_unchanged`：按日涨跌幅 >0 / <0 / =0 计数；
  - `sample_advance_ratio` / `sample_decline_ratio`：上涨/下跌家数 ÷ `sample_size`；
  - `sample_volume_total`：按 4A.2 归一为股后求和；
  - `sample_amount_total`：**仅当所有样本 `amount_available=True` 才计算**，
    否则返回 `unavailable` 并列出被排除的来源（akshare 真实值可用；sina/tencent 占位排除）。
- **强制口径字段**（每次输出必带，缺一不可）：
  - `sample_size`：当日实际纳入统计的标的数；
  - `universe_definition`：样本定义，如「daily_quote 中去重后的全部股票」；
  - `coverage_note`：覆盖说明，如「仅 3 只样本，非全市场，不可外推」。
- **缺失披露**：当日无行情的标的（停牌/无数据）**不计入样本**，并在
  `excluded_symbols` / `missing_quote_symbols` 明确列出被排除标的与原因。
- **覆盖率纪律**：没有可靠的全市场股票基数（`stock_info` 为空、无全市场覆盖）时，
  **不计算 `market_coverage_ratio` 等全市场覆盖率指标**，返回 `unavailable` 并说明原因；
  不以 3 只样本臆算覆盖率。
- **用途限制（写入报告）**：`sample_*` 统计仅用于描述当前样本，**不得**作为全市场趋势信号，
  **不得**直接作为买卖依据；样本趋势状态字段命名 `sample_trend_state`，并附同样限制说明。
- **其它处理**：上市未满窗口（缺 list_date）无法判定 → 在缺口报告声明；北交所（8/4 段位）
  适配器明确不支持 → 不纳入并声明；来源混用按 4A.1 逐标的择源、逐日可追溯。
- **严禁**把样本统计包装成全市场真实统计；覆盖率不足时报告限制，不伪造、不硬凑。

---

## 7. D3 行业与板块分析设计（阻塞）

- 盘点结论：`stock_info.industry` 存在但全空；无板块分类、无可靠映射来源；
  `stock_info` 表 0 行。
- 处置：**D3 标记 `blocked_by_data_dependency`**，本阶段只交付：
  1) 接口签名草案（输入：可靠行业分类表 + 日线；输出：板块涨跌/量额/强弱排名/涨跌家数/持续性）；
  2) 数据缺口报告（需要引入何种可靠分类数据源及字段要求）；
  3) 明确不通过股票名称猜行业、不硬编码不可靠映射表、不编造板块归属。
- 该阻塞不阻塞 D1/D2/D4/D5 的有效开发。

---

## 8. D4 候选股票筛选设计

- 规则透明、可解释、可复算，无黑箱、无 AI 直接算指标。
- 初版维度：趋势（MA5/10/20 相对关系）、动能（MACD/RSI）、量能（量 vs 均量）、
  波动（波动率/异常波动）、数据质量（完整度/来源可信度/最新交易日）。
- 每只候选输出：代码/名称/数据截止日/数据源/复权口径/指标明细/入选理由/风险提示/
  触发条件/失效条件/评分构成及权重/不满足原因。
- 数量上限取 `analysis.max_candidates=5`；**无符合条件时输出 0 只，严禁凑数**。
- 数据源自 D1 结果；样本不足股票直接不满足，不会因缺数据被误评。
- 定性声明：仅研究候选，非买入指令，不自动交易，不承诺收益。

---

## 9. D5 账户风险约束接口设计（D0-05 完整细则）

### 9.1 定位与输入
- 纯计算、不连券商、不读真实账户；输入全部为参数：账户总资产、现金、持仓、
  单股最大仓位上限、单笔最大风险、计划买入价、止损价、交易单位、费率参数、滑点参数。

### 9.2 计算纪律
- **全程 Decimal（D0-05-8）**：所有资金核心计算禁止 float 参与；输入以字符串转 `Decimal`，
  除法用 `ROUND_HALF_UP`，输出金额量化到分（0.01）。测试须验证无浮点误差（如 0.1+0.2）。

### 9.3 校验与拒绝规则（逐条对应审查 D0-05）

| 编号 | 规则 | 实现 |
|---|---|---|
| 1 | 买入价 > 止损价 | 否则 `status="rejected"`，违反条款 `invalid_price_order` |
| 2 | 最大可买股数按 100 股整数倍**向下取整** | `max_buy_shares = floor(capacity / 100) * 100`，不得四舍五入 |
| 3 | 最大亏损含买入+卖出两边费用 | `max_loss = (买入价-止损价)*股数 + buy_fee + sell_fee + 税费` |
| 4 | 佣金最低 5 元/笔，**买卖两边分别计算** | `buy_fee=max(买入金额*commission_rate, min_commission)`；`sell_fee=max(卖出金额*commission_rate, min_commission)`，各算各的 |
| 5 | 费率可配置 + 版本与日期 | 印花税率、过户费率、佣金费率均为参数；输出记录 `fee_version` 与 `fee_calculation_date` |
| 6 | 滑点作为参数并明确是否计入风险 | `slippage` 参数化；`slippage_in_risk` 布尔明确是否计入；默认计入买入价上浮、卖出价下浮，结果中标注 |
| 7 | 现金不足 / 单票>40% / 单笔风险>2% | 三类均返回 `rejected`，分别记 `insufficient_cash` / `position_limit_breached` / `risk_limit_breached` |
| 9 | 零股例外规则 | **买入**必须 100 股整数倍，买入零股**不放行**（`rejected`）；仅**卖出**允许零股（持仓可能为零股）一次性卖出，规则单独说明，不得借卖出例外放行买入 |

> 第 8 条（全程 Decimal）见 9.2。

### 9.4 默认约束
- `account_cap=10000`（用户设定上限）、单股 ≤ 总资产 40%、单笔计划亏损 ≤ 总资产 2%、
  A 股 100 股整数倍、佣金最低 5 元/笔（可改）、禁止杠杆与融资融券、印花税/过户费/佣金费率可配置。

### 9.5 输出字段
计划投入金额、预计手续费（买入+卖出分项）、计划最大亏损（含全部费用）、
风险占总资产比例、是否违反约束、违反条款列表、`fee_version`、`fee_calculation_date`、
滑点计入说明、建议最大可买股数（100 整数倍向下取整）。

### 9.6 拒绝纪律与测试
- 违反任一约束 → `status="rejected"` 并列出全部违反条款，**不得通过降低风险提示等级放行**。
- 用模拟账户数据测试边界：买卖价倒置、现金不足、仓位超限、单笔风险超限、
  非 100 整数倍买入、买入零股、最低佣金买卖两边、滑点计入/不计入、Decimal 精度。

---

## 10. 测试策略（各子阶段）

| 子阶段 | 测试重点 |
|---|---|
| D0 | 本报告审查通过（无业务代码） |
| D1 | 每指标公式手工可复算样例；数据不足→insufficient_data；缺失/重复/非法输入拒绝；**成交量三来源归一正确性（akshare/tencent ×100、sina ×1）+ 重复归一防护（幂等，不重复乘100）**（D0-02）；来源默认 sina_primary、akshare 非默认；同日价格偏差超阈值标记异常；不跨源拼 OHLC；**逐交易日来源血缘字段**（D0-03）；RSI 除零（全平→50）、EMA/MACD 初始化与预热标记、跳变标注、日期缺口≠漏采；复算一致性 |
| D2 | `sample_*` 字段命名正确、不出现全市场命名；强制 sample_size/universe_definition/coverage_note；amount 占位排除→unavailable；缺当日行情标的披露；无可靠覆盖率→unavailable；指数单位未核实标记；样本统计用途限制说明 |
| D3 | 阻塞报告存在；无分类数据时接口不产出板块结论 |
| D4 | 0 只允许；评分可解释可复算；上限 5；每字段齐全 |
| D5 | Decimal 无浮点误差；买卖价倒置 rejected；100 股向下取整；最大亏损含双向费用；佣金买卖两边各最低 5 元；费率版本/日期记录；滑点计入开关；三类约束 rejected；买入零股不放行；卖出零股例外 |

- 全程 `tmp_db` 临时库 + 注入 db_path，不读写真实库；真实库 SHA256 保持不变。
- 每子阶段：独立提交 + 推送 GitHub + 保留测试输出 + 等待审查。

---

## 11. 不在本阶段实现（明确边界）

- 定时调度 / 自动采集编排（scheduler 保持空壳）；
- 券商连接、账户读取、自动下单、自动交易；
- 报告自动生成与渲染（report/templates 保持空壳）；
- AI 大模型计算指标或编造数值（本阶段纯确定性 Python）；
- 复权数据接入（qfq/hfq 仍被 B2 拒绝）；
- 精确交易日历（法定节假日）接入；
- D3 板块实际计算（数据阻塞）；
- 任何真实库写入或结构变更。

---

## 12. 决策点状态（第二轮修订后）

**已按第一轮审查意见定稿（本轮修订）**：
1. 来源优先级 → 默认 `sina_primary`（新浪主、腾讯备），AKShare 可配置非默认（D0-01）。
2. 成交量单位 → 标准「股」，akshare/tencent ×100、sina ×1，幂等归一（D0-02）。
3. 分析血缘 → 5 个追溯字段、逐交易日来源映射（D0-03）。
4. D2 样本统计 → `sample_*` 命名 + 强制口径字段 + 不冒充全市场（D0-04）。
5. D5 风险计算 → 9 条细则全部纳入（D0-05）。
6. 指标边界 → RSI 除零、EMA/MACD 初始化、日期缺口、不复权跳变（审查第六项）。

**仍待 ChatGPT 第二轮复核确认的开放项**：
1. **量比定义**：采用「当日量 / 前 5 日均量（不含当日）」是否认可？
2. **MACD 口径**：采用通达信 `MACD柱=2*(DIF-DEA)` 是否认可？
3. **波动率**：首版非年化（20 日对数收益样本标准差）是否认可？是否直接纳入 √252 年化？
4. **来源偏差阈值**：默认 1% 是否合适？超阈值「放弃该交易日」是否认可（D0-01 第 4 条）？
5. **D3 阻塞**：是否同意 `blocked_by_data_dependency` 定性及仅交付接口+缺口报告？

---

本文件为 D0 第二轮设计修订交付，**不代表验收通过**。
**D1 尚未启动**；本轮仅修改本设计报告，未改任何业务代码/数据库/真实数据。
等待 ChatGPT 第二轮独立复核后再决定是否进入 D1。
