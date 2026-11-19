# D 阶段设计报告：分析引擎（D0 只读盘点与方案设计）

- 阶段：D0（只盘点 + 只设计，不改业务代码、不实现 D1–D5）
- 基线：`dev/b3-collector` @ `9e9ef97d25bbe06ac967d47c43ea5abb10c68bf3`（B3 已验收提交）
- 开发分支：`dev/d-analysis-engine`（从上述基线新建，未合并 main）
- 盘点时间：2026-11-19 (Asia/Shanghai)
- 仓库：https://github.com/1038931218-dev/runfengV1.0
- 状态：**设计稿，等待 ChatGPT 审查；审查通过前不实现 D1–D5**

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
  symbol/code         标的或指数代码
  calculation_time    ISO 北京时间，精确到秒
  data_end_date       实际参与计算的最新交易日（YYYY-MM-DD）
  source/source_policy 使用来源与来源选择策略
  adjust_type         复权口径（指数标 N/A）
  period              周期（daily）
  input_row_count     实际参与计算的行数
  quality_status      ok | insufficient_data | rejected | unavailable
  algorithm_version   如 "d1-indicators-v1"
  values              {指标名: 数值 或 状态对象}
  notes/warnings      数据缺口、单位换算、来源去重等说明
```

可复算保证：同一输入 + 同一参数 + 同一 `algorithm_version` ⇒ 结果逐字段一致；
计算时间字段不参与数值比较（用响应内附带的规则版本而非当前时间影响结果）。

---

## 5. D1 技术指标引擎设计

### 5.1 输入与清洗
- 输入：`code, start_date, end_date, adjust_type, source_policy`（接口名可微调，语义不变）。
- 读取受控 db_path 的 `daily_quote`，过滤 `period`、`adjust_type`、`data_type=history`；
- **来源策略 `source_policy`**（默认 `single_source`）：同一 (code, trade_date) 若有多来源，
  按优先级 `akshare > sina > tencent` 仅取一条参与单次计算，禁止同日双源重复计入；
- **成交量单位归一**：进入指标前按 source 把 volume 统一为「股」——akshare ×100（手→股），
  sina/tencent 保持（已是股）。换算记录写入 `notes`，算法版本包含该规则。
- 输入校验：缺失值、重复交易日、非正价格、OHLC 逻辑非法 → 拒绝或标记 `rejected`；
  交易日在序列中不连续时**不静默拼接**，在 notes 标注缺失区间（精确交易日历暂缺，见 1.3-E）。

### 5.2 指标公式与参数

| 指标 | 公式 / 参数 | 最小样本 | 精度 |
|---|---|---|---|
| MA5/10/20/60 | 简单移动平均 `MA_n(t)=Σclose[t-n+1..t]/n` | n 个有效交易日 | 保留 4 位小数（round half up） |
| MACD | EMA12、EMA26；`DIF=EMA12-EMA26`；`DEA=EMA(DIF,9)`；`MACD柱=2*(DIF-DEA)`（通达信口径）；EMA 递归 `EMA_t=α*x_t+(1-α)*EMA_{t-1}, α=2/(n+1)`，首值用首个 close | 需 ≥26+9-1=34 个（稳健建议 ≥35） | 4 位 |
| RSI | RSI14，**Wilder 平滑**（α=1/14）：首次 AVG Gain/Loss=14 期简单均值，其后 `AVG=prev*(13)+curr/14`；`RSI=100-100/(1+RS)`，RS=AVG Gain/AVG Loss | 需 ≥15 个 close（14 涨跌 + 首值） | 2 位 |
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

---

## 6. D2 市场宽度与指数分析设计

- 指数日涨跌幅：基于 `index_quote` 单指数相邻交易日 close，公式同 5.2。
- 市场宽度：基于**当前 daily_quote 全部股票样本**（现状仅 3 只）。
  - 上涨/下跌/平盘家数：按日涨跌幅 >0 / <0 / =0 计数；
  - 比例：上涨家数/样本数、下跌家数/样本数；
  - 成交量统计：按 source 归一为股后求和；
  - 成交额统计：**仅当所有样本 `amount_available=True` 才计算**，否则返回 `unavailable`
    并说明被排除的来源；
  - 趋势状态：基于样本内上涨家数占比的阈值判定（阈值参数化），标注为「样本趋势」。
- **强制口径标注**：每次输出携带 `sample_size`、`coverage_note`（如"仅 3 只样本，非全市场"）、
  `data_end_date`、`universe_definition`。
- **处理方式说明**：停牌（无当日行情）不计入当日样本并注明；上市未满窗口
  （缺 list_date，stock_info 为空）无法判定 → 在缺口报告声明；北交所（8/4 段位）
  现有适配器 `expected_cn_prefix` 明确不支持 → **不纳入**并在报告声明；数据源混用按
  5.1 来源策略逐标的择一。
- **严禁**把 3 只样本的统计包装成全市场真实统计；覆盖率不足时报告限制，不伪造。

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

## 9. D5 账户风险约束接口设计

- 纯计算、不连券商、不读真实账户；输入全部为参数：账户总资产、现金、持仓、
  单股最大仓位、单笔最大风险、计划买入价、止损价、交易单位、手续费估算。
- **全部金额用 Decimal**（禁止浮点做资金核心计算），输入先以字符串转 Decimal，
  除法用 `ROUND_HALF_UP`，输出金额量化到分（0.01）。
- 默认约束：`account_cap=10000`；单股 ≤ 总资产 40%；单笔计划亏损 ≤ 总资产 2%；
  A 股按 100 股整数倍；佣金最低 5 元/笔（可改）；禁止杠杆/融资融券。
- 输出：计划投入金额、预计手续费、计划最大亏损、风险占总资产比例、是否违反约束、
  违反条款、建议最大可买股数。
- 违反任一约束 → 返回 `status="rejected"` 并列出违反条款，**不得通过降低风险提示等级放行**。
- 用模拟账户数据测试边界（现金不足、仓位超限、风险超限、非 100 整数倍、最低佣金）。

---

## 10. 测试策略（各子阶段）

| 子阶段 | 测试重点 |
|---|---|
| D0 | 本报告审查通过（无业务代码） |
| D1 | 每指标公式用手工可复算样例；数据不足→insufficient_data；缺失/重复/非法输入拒绝；成交量单位归一（akshare×100）；多来源去重；复算一致性 |
| D2 | 3 只样本统计口径正确；amount 占位排除→unavailable；样本/非全市场标注；停牌/缺口处理 |
| D3 | 阻塞报告存在；无分类数据时接口不产出板块结论 |
| D4 | 0 只允许；评分可解释可复算；上限 5；每字段齐全 |
| D5 | Decimal 精度（无浮点误差）；各约束边界；rejected 不放行；最低佣金；100 股整数倍 |

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

## 12. 待 ChatGPT 审查确认的关键决策点

1. **成交量单位归一**：是否认可「akshare ×100 归一为股、进入指标前统一」及算法版本固化？
2. **来源优先级**：默认 `akshare > sina > tencent` 是否认可？是否需改为可配置？
3. **量比定义**：采用「当日量 / 前 5 日均量（不含当日）」是否认可？
4. **MACD 口径**：采用通达信 `MACD柱=2*(DIF-DEA)` 是否认可？
5. **波动率**：首版非年化（20 日对数收益样本标准差）是否认可？是否直接纳入 √252 年化？
6. **D2 样本统计**：仅 3 只且无全市场覆盖时，是否允许输出「样本级」统计（带强制标注），
   还是 D2 也一并标记数据不足、只交付接口？
7. **D3 阻塞**：是否同意 `blocked_by_data_dependency` 定性及仅交付接口+缺口报告？

---

本文件为 D0 设计交付，**不代表验收通过**。等待 ChatGPT 审查后再决定是否进入 D1。
