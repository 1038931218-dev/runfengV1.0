# D3-0 行业与板块分析 — 数据依赖只读核验与设计报告

- 阶段：D3-0（只读核验 + 设计报告；**不开发 D3-1 代码**）
- 开发基线 / 当前 HEAD：`6fd9a26533a327e79db0cb13fc24b914a6e0adc3`（已确认为 D2 正式验收版本）
- 分支：`dev/d-analysis-engine`
- 父阶段：D2 两轮独立验收已通过；三项复核问题不再重复修复
- 设计依据：`reports/phase3/d-analysis-design.md` 第 2 节、第 7 节（D3 判定 `blocked_by_data_dependency`）
- 生成时间：2026-11-21

---

## 一、核验纪律与基线

核验全程只读，未修改任何业务代码、未写入真实数据库：

| 项 | 值 | 说明 |
|---|---|---|
| 分支 | `dev/d-analysis-engine` | 与任务书一致 |
| HEAD | `6fd9a26533a327e79db0cb13fc24b914a6e0adc3` | 本地 = 远程，工作区干净 |
| 真实库 SHA256（前） | `c5668fdac053ee1b11e713e0c598f036997951132bda7f31ad8d847d879fb8cc` | 只读连接前后一致 |
| 真实库 SHA256（后） | `c5668fdac053ee1b11e713e0c598f036997951132bda7f31ad8d847d879fb8cc` | **未变** |
| 连接方式 | `sqlite3.connect('file:data/invest_center.db?mode=ro', uri=True)` | 强制只读，杜绝写入 |
| 未触碰 | D0/D1/D2 代码与文档、`.env`、真实数据库、`main` | — |

核验方式：以 `mode=ro` 打开真实库，枚举全部表与列、逐类统计行数与非空率；另 grep 代码层采集能力。下述每个数字均可按本报告所列 SQL 复核。

---

## 二、四类数据依赖核验（只读）

### 2.1 板块分类数据

| 核验项 | 实测结果 |
|---|---|
| 全库表清单（7 张） | `announcement`、`daily_quote`、`data_quality_log`、`fetch_log`、`index_quote`、`sqlite_sequence`、`stock_info` |
| 是否存在板块/行业/概念分类表 | **无**。无任何 `sector` / `industry` 分类表、无申万/中信/中证分类表、无概念板块表 |
| 含 `industry` 列的表 | 仅 `stock_info.industry`（1 处），且该表 0 行（见 2.2） |
| `daily_quote` 行业字段 | **0 个**（`pragma_table_info` 对 `industry`/`sector` 命中 0） |

**判定**：⛔ 无任何可用的板块分类数据。无板块 -> 成分股映射，无板块层级（一级/二级）定义。

### 2.2 股票行业映射

| 核验项 | 实测结果 |
|---|---|
| `stock_info` 行数 | **0 行**（`SELECT COUNT(*) FROM stock_info` = 0） |
| `industry` / `exchange` / `list_date` 非空数 | 全为空表，无一行可映射 |
| `daily_quote` 标的 | 3 只：`000001` 平安银行、`300750` 宁德时代、`600519` 贵州茅台；各 10 交易日（2025-11-03 ~ 11-14），全部 `akshare / none / daily / history` |
| 代码层行业采集实现 | 全代码库 grep `sector|industry|申万|同花顺|概念`：`storage/models.py:56` 仅 1 处 `industry TEXT` 列定义（对应 `stock_info`），**无任何采集/映射实现** |

**判定**：⛔ 3 只样本股票无任何可靠行业归属来源。按纪律严禁用股票名称（如“银行”“宁德”）猜测行业、严禁硬编码不可靠映射表。

### 2.3 板块指数行情

| 核验项 | 实测结果 |
|---|---|
| `index_quote` 指数清单 | 仅 3 个宽基：`000001.SH` 上证指数、`000300.SH` 沪深300、`399001.SZ` 深证成指 |
| 行数 / 交易日 | 每指数 10 行 / 10 个不同交易日（2025-11-03 ~ 11-14），全 `akshare / history` |
| 是否含行业/板块/主题指数 | **无**（`index_code` 无 8/9 开头行业指数；`index_name` 无“行业/板块/申万/中证”命中） |
| 数据完整性 | 三指数 `close` 无 NULL；`amount` 均非 NULL/非零，但指数量纲**未核实**，不得当真实成交额比较（沿用 D2 口径） |
| 代码层指数采集能力 | akshare/sina/tencent/baostock/collector/smart_collector 均只映射上述 3 宽基指数，**无行业/板块指数采集** |

**判定**：⛔ 无板块指数行情，无法计算板块指数涨跌、量额、持续性；宽基指数不可冒充板块指数。

### 2.4 历史分类可靠性

| 核验项 | 实测结果 |
|---|---|
| 历史分类快照表 | **无**（全库无分类时点/生效日期字段） |
| `stock_info.list_date` | 空表，无数据 |
| `announcement` 公告 | 0 行；标题无“行业/板块/分类”相关记录，无分类变更可追溯 |
| `data_quality_log` | 0 行；无历史分类质量核验记录 |

**判定**：⛔ 无分类生效日期、无历史快照、无变更公告 → **无法核验任意历史时点的行业归属**，无法做分类变更的时间序列追溯。任何“某日某股属于某板块”的历史断言都不具备可复核依据。

---

## 三、核验结论：D3 维持阻塞

四项核验（板块分类数据、股票行业映射、板块指数行情、历史分类可靠性）**全部返回无可用数据**，与 D0 设计文档第 2 节、第 7 节的判定一致：

> **D3 = `blocked_by_data_dependency`。**
> 本阶段只交付：（1）接口签名草案；（2）数据缺口报告；（3）三条禁令声明。
> **不实现任何板块计算、不编造板块归属、不以宽基指数冒充板块指数。**

该阻塞不影响 D1/D2/D4/D5 的有效开发（D5 为纯参数计算，D4 数据源自 D1 指标 + 名称，均不依赖行业分类）。

---

## 四、接口签名草案（仅设计，D3-1 待授权后实现）

以下为未来 `analysis/sector.py` 的**签名草案**，本阶段不落任何代码文件。纯度与注入纪律沿用 D1/D2：纯函数、不查库、不联网、调用方注入受控数据、全程 `Decimal + ROUND_HALF_UP`。

```
analyze_sector(
    *,
    classification,          # Sequence[SectorMembership]：可靠分类，含 code/一级行业/生效日期区间/source
    stock_records,           # Sequence：个股日线（受控库注入，非真实库直连）
    trade_date,              # str：目标交易日
    sector_index_records=(), # Sequence：板块指数日线（可选；量纲核实前不得产出量额指标）
    as_of=None,              # str|None：分类时点，默认 trade_date；需落在分类生效区间内
    divergence_threshold=Decimal("0.01"),
) -> SectorAnalysisResult
```

### 4.1 结果契约草案

- 状态复用 D1 `IndicatorValue`：`ok` / `insufficient_data` / `unavailable` / `rejected`；
- 样本不完整期间全部字段强制 `sample_sector_*` 前缀，附 `universe_definition` / `coverage_note`；
- 拟产出维度（D0 第 7 节）：板块涨跌、板块量额、强弱排名、板块内涨跌家数、持续性；
- **持续性**：需“分类在 as_of 有效 + 板块内多交易日连续行情”，当前数据不具备 → 固定 `unavailable`，不臆造。

### 4.2 前置门禁（实现时必须先过）

1. `classification` 非空且每条含 code + 行业名 + 生效区间 + source；缺生效日期 → 该条不可用；
2. `as_of` 落在生效区间内的分类才纳入；无法确认 → 该标的排除并披露；
3. 板块量额仅在“分类覆盖全部样本 + 全部 `amount_available=True` + 指数量纲已核实”时计算，否则 `unavailable`；
4. 数据不满足时返回结构完整的 `unavailable` 结果 + 阻塞清单，不返回空壳或 0 值。

---

## 五、数据缺口报告（解除阻塞所需）

### 5.1 可靠行业分类数据源（必需）

| 字段 | 要求 |
|---|---|
| `code` | 股票代码，与 `daily_quote.code` 对齐 |
| `sector_name` / `sector_level` | 行业名称与层级（一级/二级），层级口径单一且固定 |
| `effective_from` / `effective_to` | 分类生效区间，**历史分类可靠性的前提** |
| `source` / `collected_at` | 来源与采集时间，可追溯 |

- 候选权威源：申万行业分类、中信行业分类、中证行业指数成分、交易所/上市公司行业分类指引；须确认授权与获取合规。
- 严禁：股票名称关键字推断、硬编码映射表、无生效日期的分类（不可做历史核验）。

### 5.2 板块指数行情（量额/持续性所需）

- 需引入行业/主题指数日线（如申万一级行业指数、中证行业指数），字段对齐 `index_quote`；
- 必须先核实指数量纲来源（沿用 D2 未决项），否则量额类指标保持 `unavailable`。

### 5.3 历史分类快照（持续性/历史可靠性所需）

- 需带生效日期的历史分类快照表，或分类变更公告的结构化记录；
- 当前 `announcement` 0 行、`data_quality_log` 0 行，无法追溯，须补齐后方可核验历史时点归属。

---

## 六、三条禁令声明（D0 第 7 节）

1. **不通过股票名称猜行业**——名称含“银行/证券/科技”等不构成分类依据；
2. **不硬编码不可靠映射表**——任何 code->行业 的手写映射均不进入分析结果；
3. **不编造板块归属**——无可靠分类时返回 `blocked_by_data_dependency` / `unavailable`，不臆造、不凑数。

---

## 七、合规与本阶段边界

- 本轮**仅新增本报告**（`reports/phase3/d3-0-sector-design.md`），未开发 D3-1 代码；
- 未修改 D0/D1/D2 代码与文档，未修改 `.env`，未写入真实数据库（SHA256 前后一致）；
- 未启动 D3-1、D4、D5、E，未合并 `main`；
- 已按纪律完成基线核验、四类数据只读核验、接口与缺口设计。

## 八、状态

提交并推送本报告后**停止**，等待 ChatGPT 独立审查与下一步授权。**不自行宣布 D3 通过，不进入 D3-1 实现。**
