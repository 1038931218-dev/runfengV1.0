# D1 技术指标引擎开发报告

- 阶段：D1（技术指标引擎，确定性计算，不碰真实库、不联网、不接券商）
- 基线：`dev/d-analysis-engine` @ `0b0cba9010d14e4dd39e2398acf7785e522db11d`（D0 已验收）
- 开发分支：`dev/d-analysis-engine`（从上述基线继续，未合并 main）
- 开发日期：2026-11-20 (Asia/Shanghai)
- 状态：**开发完成，等待 ChatGPT 独立审查 GitHub 实际代码；未自行宣布验收通过**

---

## 1. 核心接口与计算公式

### 1.1 数据结构（复用 D0 统一契约）
- `IndicatorValue`：单指标结果（name/params/value/status/required/actual/warmup/reason）。
- `AnalysisResult`：一次计算的完整血缘（symbol/period/adjust_type/data_as_of/source_policy/
  sources_used/source_row_counts/source_by_trade_date/volume_unit_original/
  volume_unit_normalized/input_row_count/quality_status/source_divergence/
  algorithm_version/calculation_time/values/notes/warnings）。`to_dict()` 供序列化核对。
- 算法版本：`d1-indicators-v1`（参数/口径变更须递增）。
- 全程 `Decimal` + `ROUND_HALF_UP`，精度按 D0 5.2。

### 1.2 纯计算核心（`analysis/indicators.py`，不读库、不联网）
| 指标 | 公式 / 参数 | 最小样本 | 精度 |
|---|---|---|---|
| MA5/10/20/60 | SMA，取最后 n 个 close 均值 | n | 4 位 |
| MACD | EMA12/26 首值法；DIF=EMA12−EMA26；DEA=EMA(DIF,9)；柱=2×(DIF−DEA)；通达信口径 | 完整有效 ≥34；<34 strict→insufficient(required=34) | 4 位 |
| RSI14 | Wilder 平滑（α=1/14），首值 14 期简单均值，其后 prev×13+curr /14 | 15 根 close | 2 位 |
| VOL_MA5/10 | 标准化成交量 volume_std 的简单均线 | n | 整数股 |
| 量比 | 当日 volume_std / 前 5 日 volume_std 均值（**前 5 日不含当日**） | 6 | 4 位 |
| 日涨跌幅 | (close_t/close_{t−1}−1)×100% | 2 | 4 位 |
| 区间涨跌幅 | (close_end/close_start−1)×100% | 2 | 4 位 |
| 波动率 | 最近 20 个对数收益样本标准差（ddof=1），非年化 | 21 根 close | 6 位 |

### 1.3 编排层（`analysis/engine.py`）
`calculate_indicators(records, *, symbol, adjust_type, period, source_policy,
divergence_threshold, macd_*, rsi_period, vol_ma_windows, ratio_window,
volatility_window) -> AnalysisResult`
- 接收调用方传入的标准化记录（dict / StandardQuote 对象），**不隐式查库**；
- 数据准备：标的/复权/周期一致性、来源择选（4A.1）、成交量 A/B/C 归一（4A.2）、
  日期排序去重、涨跌幅跨缺失交易日检测（任务书 3.6）。

---

## 2. 每项指标实际实现情况

| 指标 | 实现 | 数据不足行为 | 特殊情形 |
|---|---|---|---|
| MA5/10/20/60 | ✅ | insufficient_data(required=n, actual=m)，不填 0 | — |
| MACD | ✅ | strict 模式 <34 根 → DIF/DEA/HIST 全 insufficient(required=34)；keep_warmup 模式 <34 根标 warmup=true | 34 根为工程预热门槛，不宣称数学收敛；第 26–33 根状态不空白 |
| RSI14 Wilder | ✅ | <15 根 insufficient(required=15) | 全平→50 并标 `flat_window`；单边涨/跌→100/0，不除零 |
| VOL_MA5/10 | ✅ | insufficient(required=n) | 只消费 volume_std，整数股输出 |
| 量比 | ✅ | <6 根 insufficient(required=6) | 分母为 0 → unavailable(divide_by_zero_denominator)，不返回 inf/0 |
| 日涨跌幅 | ✅ | <2 根 insufficient | 前收盘价取紧邻前一日；前一交易日 unavailable → unavailable(previous_trading_day_unavailable)，不跨缺失日；跳变>10% 标 `possible_ex_rights_jump` |
| 区间涨跌幅 | ✅ | <2 根 insufficient | 起点非正 → rejected |
| 波动率 | ✅ | <21 根 insufficient(required=21) | 零/负价 → rejected(invalid_price)，不静默忽略；annualized=false |

---

## 3. 数据血缘与异常处理

- **来源择选（D0 4A.1）**：默认 `sina_primary`；双源合格先算偏差（≤阈值选新浪，>阈值放弃该日并记 `source_divergence`）；单源合格选该源并记排除原因；双源均不合格→该日 `unavailable` 不参与计算；`*_only` 策略混入他源 → `rejected(MixedSourceError)`。逐交易日来源写入 `source_by_trade_date`，跨源不拼 OHLCV。
- **成交量状态机（D0 4A.2）**：`normalize_volume()` 严格 A/B/C 分派；sina ×1、tencent/akshare ×100；B 态直接复用 volume_std 不重复×100；C 态（volume_std 字段缺失/状态不一致/非法值/source 未知）→ `InvalidInputError`；原始 volume 永不修改；JSON 往返保留三字段状态。
- **混用拒绝**：复权混用→`MixedAdjustTypeError`；周期混用→`MixedPeriodError`；同日同源重复→`InvalidInputError`；标的不符→`InvalidInputError`。
- **amount 占位**：D1 不产出任何成交额类指标；量能指标基于 volume_std，不受 amount 占位影响。
- **不填 0**：所有数据不足/不可计算状态 `value=None`，绝不填 0。

---

## 4. 手工复算对照（关键指标）

| 指标 | 固定数据集 | 手工计算 | 引擎结果 |
|---|---|---|---|
| MA5 | [1,2,3,4,5] | (1+2+3+4+5)/5 = 3 | 3.0000 ✅ |
| EMA12 递推 | [10,11] | 首值10；10+(2/13)×(11−10)=10.153846… | 10.153846… ✅ |
| RSI14（Wilder 经典教材序列） | 15 根 close | Wilder 平滑 RSI = 70.46 | 70.46 ✅ |
| 量比 | [10,10,10,10,10,20] | 20 / mean(前5日10) = 2 | 2.0000 ✅ |
| 日涨跌幅 | [90,100,110] | 110/100−1 = +10% | 10.0000 ✅ |

---

## 5. 测试命令与结果

| 项 | 命令 | 结果 |
|---|---|---|
| 编译 | `python3.11 -m compileall -q analysis adapters quality storage tests` | COMPILE_OK |
| D1 单测 | `pytest tests/test_analysis_units.py tests/test_analysis_indicators.py tests/test_analysis_engine.py -q` | **64 passed** |
| 全量非网络 | `pytest -m "not network" -q` | **238 passed, 8 deselected** |
| B2 回归 | `pytest tests/test_new_adapters.py -q` | **68 passed**（零回归） |
| B3 回归 | `pytest tests/test_smart_collector.py tests/test_smart_collector_integration.py -q` | **36 passed**（零回归） |
| 集成全集 | `pytest -m "integration" -q` | **27 passed** |

D1 测试覆盖任务书 24 项重点场景（正常/边界/异常/数据不足、A/B/C 状态机、重复归一、分母为 0、跨缺失交易日、复权/来源/周期混用、amount 占位、血缘完整、不填零），每项关键指标均有手工复算对照，非仅"能运行"。

---

## 6. 文件清单（全部新增，未改既有业务代码）

- `analysis/contracts.py` — 统一结果契约、状态常量、异常类型、Decimal 量化工具
- `analysis/units.py` — 成交量单位契约 + A/B/C 状态机
- `analysis/indicators.py` — 纯计算核心（MA/MACD/RSI/VOL_MA/量比/涨跌幅/波动率）
- `analysis/engine.py` — 数据准备 + 编排 + 血缘组装
- `analysis/__init__.py` — 更新导出
- `tests/test_analysis_units.py` — 成交量状态机测试
- `tests/test_analysis_indicators.py` — 指标核心 + 手工复算对照
- `tests/test_analysis_engine.py` — 数据校验/血缘/混用/跨缺口/amount 占位测试
- `reports/phase3/d1-indicators-development.md` — 本报告

---

## 7. 纪律核验

- 真实库 SHA256：前后均 `c5668fdac053ee1b11e713e0c598f036997951132bda7f31ad8d847d879fb8cc`（**未变**）；
- 未改 main、未合并 main；未碰 review/round5、dev/b3-collector、dev/b2-adapters；
- 未接券商/账户/自动下单；未提交任何凭证；
- 未启动 D2/D3/D4/D5 及 E 阶段。

## 8. 遗留与说明

- 无未解决的功能性问题。
- 两处"工程性说明"非缺陷：① MACD 34 根为预热门槛而非数学收敛保证（D0 已明确）；② 波动率首版非年化（D0 已明确）。
- 未改动 D0 已验收的任何计算口径与数据契约；发现任务书重点 14（涨跌幅跨缺失交易日）在纯序列层无法判定，已在编排层按 D0 4A.1 的 unavailable 语义补齐检测，未改变 D0 规则。

本报告为开发交付说明，**不代表验收通过**。等待 ChatGPT 独立审查 GitHub 实际代码、测试与提交记录。
