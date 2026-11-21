# D3-0 补充核验 — 外部行业分类数据源能力

- 阶段：D3-0 补充核验（**只核验外部数据源能力，不修改业务代码，不写入真实数据库**）
- 分支：`dev/d-analysis-engine`
- 基线 / HEAD：`6fd9a26533a327e79db0cb13fc24b914a6e0adc3` → 补充证据提交后递增
- 采集时间：2026-11-21 08:01 ~ 08:05（北京时间）
- 原始核验报告：`reports/phase3/d3-0-sector-design.md`（本报告仅作补充与交叉引用，不覆盖其原始记录）
- 脱敏证据：`reports/phase3/d3-0-evidence/source_probe_raw.json`、`sql_evidence.json`
- 核验方法：**实际接口调用**（非文档推断），每接口单独超时 25~30s，异常分类，响应脱敏后落盘

---

## 一、核验纪律

| 项 | 值 |
|---|---|
| 真实库连接 | `mode=ro` 只读，全程未写入 |
| 真实库 SHA256（前/后） | `c5668fdac053ee1b11e713e0c598f036997951132bda7f31ad8d847d879fb8cc`（**未变**） |
| AKShare 版本 | 1.18.22（实际 `import akshare` 可用） |
| requests / pandas | 2.32.5 / 3.0.0 |
| 证据脱敏 | 仅保留公开接口 URL、字段结构、前 3~8 条样例；无密钥/Token/Bearer（已 grep 确认） |
| 未触碰 | D0/D1/D2 代码与文档、`.env`、业务代码、真实数据库、`main` |

> 方法声明：所有结论来自本轮**实际调用**并返回的数据；接口文档仅用于理解参数，不作为可用性证明。失败接口均标注了具体的失败类别与错误原文。

---

## 二、逐源核验结果（实际调用）

### 2.1 AKShare — 行业分类/板块接口

| 接口 | 返回状态 | 关键字段/样例 | 代码→行业映射 | 行业层级 | 生效日期 | 历史分类快照 | 板指日线 |
|---|---|---|---|---|---|---|---|
| `stock_industry_change_cninfo` | ✅ ok (4~7 行/股) | 证券代码、新证券简称、行业门类/大类/中类/次类、行业编码、**分类标准**、**变更日期** | ✅ | ✅ 四级门类 | ✅ 变更日期 + 支持 start/end 区间 | ✅ **可查历史变更** | — |
| `stock_industry_category_cninfo` | ✅ ok (294 行) | 类目编码、类目名称、父类编码、**分级(0~4)**、终止日期 | ❌ 仅类目表 | ✅ 5 级 | ⚠️ 仅终止日期 NaT | ❌ 无个股快照 | — |
| `index_stock_cons_csindex` | ✅ ok (300 行) | **日期**、指数代码、成分券代码、成分券名称、交易所 | ⚠️ 指数成分非行业 | ❌ | ⚠️ 返回快照日 | ❌ | — |
| `stock_board_industry_name_em` | ❌ 网络不可达 | `RemoteDisconnected: Remote end closed connection without response` | — | — | — | — | — |
| `stock_board_industry_cons_em` | ❌ 网络不可达 | 同上（`symbol=银行`） | — | — | — | — | — |
| `stock_board_industry_hist_em` | ❌ 网络不可达（重试 2 次均失败） | 同上（板指日线） | — | — | — | — | — |
| `stock_industry_clf_hist_sw` | ❌ 网络不可达 | `SSLError ... www.swsresearch.com ... SwClass2021/StockClassifyUse_stock.xls` | — | — | — | — | — |

**巨潮行业变更接口实际数据样例**（000001 平安银行，脱敏）：

| 分类标准 | 行业门类 | 行业中类 | 变更日期 |
|---|---|---|---|
| 巨潮行业分类标准 | 金融 | 综合性银行 | 2019-05-08 |
| 中证行业分类标准(旧) | 金融地产 | 商业银行 | 2020-03-16 |
| 申银万国行业分类标准 | 银行 | 股份制银行Ⅲ | 2021-07-30 |
| 中证行业分类标准 | 金融 | 综合性银行 | 2021-12-17 |

**日期区间查询实测**（`start_date/end_date` 参数有效，证明历史时点可复核）：

- 查 `000001` 2019 年度 → 返回 1 条：巨潮/金融/2019-05-08
- 查 `000001` 2021 年度 → 返回 2 条：申银万国/银行/2021-07-30、中证/金融/2021-12-17

### 2.2 新浪数据接口

| 接口 | 返回状态 | 关键字段/样例 | 判定 |
|---|---|---|---|
| `newSinaHy.php`（行业分类列表） | ✅ HTTP 200 | `new_blhy,玻璃行业,19,...`；`new_cbzz,船舶制造,8,...` | ✅ 当前行业板块清单，无历史生效日期 |
| `Market_Center.getHQNodeData`（hangye 成分） | ✅ HTTP 200 | `symbol, code, name, trade, ...`（如 `920403 康农种业`） | ✅ 当前板块成分，无行业层级/无历史 |
| `stock_sector_spot(indicator=新浪行业)` | ✅ ok (49 行) | 板块、股票代码、股票名称、公司家数、涨跌幅、总成交额 | ✅ 当前板块→个股映射，无历史日期 |
| `hq.sinajs.cn/list=sh000001` | ✅ HTTP 200 | 上证指数行情（对照用） | 行情接口，非分类 |

### 2.3 腾讯数据接口

| 接口 | 返回状态 | 关键字段/样例 | 判定 |
|---|---|---|---|
| `qt.gtimg.cn/q=sh000001` | ✅ HTTP 200 | `v_sh000001="1~上证指数~000001~..."`（对照用） | 行情接口，**非行业分类** |
| `proxy.finance.qq.com/.../getRank?board_type=hy` | ❌ HTTP 400 | 空响应体 | 接口路径/参数不被接受，无法确认行业分类能力（**不能判定为不可用，只能判定本次试探失败**） |

> 腾讯公开接口未发现可稳定获取「股票代码→行业名称 + 层级 + 历史生效日期」的证据。

### 2.4 中证指数 / 其他公开源

| 接口 | 返回状态 | 关键字段/样例 | 判定 |
|---|---|---|---|
| `index_stock_cons_csindex(symbol=000300)` | ✅ ok (300 行) | 日期=2026-11-20、成分券代码、成分券名称、交易所 | ✅ 宽基指数成分，非行业分类；含快照日期但无历史序列 |
| `csindex.com.cn/.../index-basic-info` | ❌ HTTP 404 | 空 | 官网接口路径失效 |

### 2.5 申万 / 中信候选分类体系

| 接口 | 返回状态 | 关键字段/样例 | 代码→行业映射 | 层级 | 生效日期 | 历史快照 |
|---|---|---|---|---|---|---|
| `sw_index_second_info` | ✅ ok (131 行) | 行业代码(801016.SI)、行业名称(种植业)、**上级行业**、成份个数、市盈率等 | ⚠️ 行业维度非个股 | ✅ 有上级行业 | ❌ 无 | ❌ |
| `sw_index_third_info` | ✅ ok (336 行) | 行业代码(850111.SI)、行业名称(种子)、上级行业 | ⚠️ 行业维度非个股 | ✅ 三级 | ❌ 无 | ❌ |
| `sw_index_first_info` | ❌ 接口失效 | `AttributeError: 'NoneType' object has no attribute 'find_all'` | — | — | — | — |
| `sw_index_third_cons` | ❌ 接口失效 | `ValueError: No tables found` | — | — | — | — |
| `stock_industry_clf_hist_sw` | ❌ 网络不可达 | SSLError（swsresearch.com xls 下载失败） | — | — | — | — |
| **中信行业分类** | ❌ 未发现可用接口 | AKShare 无 `citic`/中信行业分类函数；无公开稳定接口证据 | — | — | — | — |

---

## 三、失败原因分类汇总（严格区分）

| 类别 | 涉及接口 | 证据原文 |
|---|---|---|
| 网络不可达 / 无响应 | 东财 `stock_board_industry_*_em`（3 个）、申万 `stock_industry_clf_hist_sw` | `RemoteDisconnected: Remote end closed connection without response`；`SSLError ... swsresearch.com` |
| 接口不存在 / 已失效 | `sw_index_daily`（AttributeError 无此函数）、`sw_index_first_info`（NoneType.find_all）、`sw_index_third_cons`（No tables found）、`index_stock_info`（No tables found） | 见各接口 error_detail |
| 需要授权或 Token | 无（本次未命中明确的 403/token 拒绝） | — |
| 接口限流 | 无明确 429 证据；东财失败表现为断连，归类网络不可达 | — |
| 返回数据不含所需字段 | 新浪行情/腾讯行情（仅行情无分类）、腾讯行业排行 400 空响应 | `missing_required_fields` / `HTTP 400` |
| 无法确认历史分类 | 申万二级/三级（无日期）、新浪板块（无日期）、中证成分（单日快照）、巨潮类目表（仅终止日期） | 见各接口 `has_date=false` |

> 说明：东财接口在本沙箱环境连续断连，判定为"本环境网络不可达"，**不能据此断言接口本身在公网不可用**——换网络环境可能恢复，属于"可重试（更换出口/时段）"。

---

## 四、补充 SQL 证据（与真实库结构一致，可复核）

以下 SQL 已在 `mode=ro` 下实际执行，结果见 `d3-0-evidence/sql_evidence.json`：

```sql
-- 1. 全部表名（7 张）
SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;
-- 实测: announcement, daily_quote, data_quality_log, fetch_log, index_quote, sqlite_sequence, stock_info

-- 2. 每张表记录数量
SELECT 'stock_info' t, COUNT(*) n FROM stock_info;           -- 0
SELECT 'daily_quote' t, COUNT(*) n FROM daily_quote;         -- 30
SELECT 'index_quote' t, COUNT(*) n FROM index_quote;         -- 30
SELECT 'announcement' t, COUNT(*) n FROM announcement;        -- 0
SELECT 'data_quality_log' t, COUNT(*) n FROM data_quality_log;-- 0
SELECT 'fetch_log' t, COUNT(*) n FROM fetch_log;             -- 19

-- 3. stock_info 各字段非空数量（全部为 0，表为空）
SELECT COUNT(*) FROM stock_info WHERE industry IS NOT NULL AND trim(industry)<>'';   -- 0
SELECT COUNT(*) FROM stock_info WHERE exchange IS NOT NULL AND trim(exchange)<>'';   -- 0
SELECT COUNT(*) FROM stock_info WHERE list_date IS NOT NULL AND trim(list_date)<>''; -- 0

-- 4. daily_quote 标的与日期覆盖（3 只 × 10 交易日）
SELECT code, name, COUNT(*), COUNT(DISTINCT trade_date), MIN(trade_date), MAX(trade_date), GROUP_CONCAT(DISTINCT source)
FROM daily_quote GROUP BY code, name ORDER BY code;
-- 000001 平安银行 / 300750 宁德时代 / 600519 贵州茅台；均 10 日 2025-11-03~11-14，source=akshare

-- 5. index_quote 指数代码与日期覆盖（3 指数 × 10 交易日）
SELECT index_code, index_name, COUNT(*), COUNT(DISTINCT trade_date), MIN(trade_date), MAX(trade_date), GROUP_CONCAT(DISTINCT source)
FROM index_quote GROUP BY index_code, index_name ORDER BY index_code;
-- 000001.SH 上证指数 / 000300.SH 沪深300 / 399001.SZ 深证成指；均 10 日 2025-11-03~11-14，source=akshare

-- 6. 行业/板块/分类相关字段检索（全库仅 1 处命中）
-- 逐表 PRAGMA table_info 检索 industry/sector/concept/board/行业/板块/分类
-- 命中: stock_info.industry（唯一）
SELECT COUNT(*) FROM announcement WHERE lower(title) LIKE '%行业%' OR lower(title) LIKE '%板块%' OR lower(title) LIKE '%分类%';      -- 0
SELECT COUNT(*) FROM data_quality_log WHERE lower(detail) LIKE '%行业%' OR lower(detail) LIKE '%板块%' OR lower(detail) LIKE '%分类%'; -- 0
```

结论：本地库结构与原报告一致，无任何行业/板块/分类数据表，关键字段全空。

---

## 五、最终结论 A–E

### A. 是否存在可用的可靠行业分类源？

**存在（外部），但本地库未接入。** 实际调用证明三类可靠源可用：
1. **巨潮 cninfo**（`stock_industry_change_cninfo`）——证券代码→行业门类/大类/中类/次类，多套权威标准（巨潮、中证、申银万国、恒生、证监会），权威且可复核；
2. **申万**（`sw_index_second_info` 131 行 / `sw_index_third_info` 336 行）——行业代码+名称+上级行业，层级清晰（当前快照）；
3. **新浪**（`stock_sector_spot` / `newSinaHy.php`）——板块→个股映射（当前快照）。

> 这修正了原 D3-0 报告"无可靠分类来源"的绝对表述：应表述为"**本地库无、但外部存在可实际调用的可靠分类源**"。东财系在本环境不可达，不影响上述可用源的存在性。

### B. 是否存在可用的历史分类数据源？

**存在，唯一确认可用的是巨潮 `stock_industry_change_cninfo`。** 它提供带 `变更日期` 的行业归属变动记录，且 `start_date/end_date` 区间查询实测有效（2019 年度、2021 年度分别返回对应变更）。这是可复核的历史分类数据源。
但需注意：数据为**变动事件序列**（最新变更多停留在 2021 年），要还原"某历史交易日某股所属行业"需按"不晚于该日的最近一次变更"回溯；分类标准多套并存，须固定单一标准（如统一取申万或中证）后才可交叉比较。申万/新浪/中证均**无历史分类日期字段**，不支持历史时点分类。

### C. 是否存在可用的板块指数行情源？

**本环境未取得可用源。** 东财 `stock_board_industry_hist_em`（板块指数日线）连续两次 `RemoteDisconnected`，申万指数日线函数不存在。中证成分接口提供的是宽基指数成分而非行业指数日线。因此"板块指数日线"在本轮核验中**不可确认可用**（属网络不可达/接口缺失，换环境可重试）。

### D. 是否满足解除 D3 阻塞的条件？

**仍不满足，但阻塞性质发生变化。** 解除阻塞需要"可靠分类数据已进入可控数据管线且可复核"。当前状态：
- 外部源可用（A/B 已证），但**本地库 `stock_info` 0 行、无任何分类表、未采集入库、未持久化历史快照**；
- 历史时点分类所需的多标准统一、变更事件回溯逻辑尚未设计定稿；
- 板块指数日线源未确认（C）。

因此 **D3 维持 `blocked_by_data_dependency`**，不进入 D3-1 实现。但阻塞从"数据源不存在"更新为"**数据源存在但未接入/未入库/未固化**"——这是本轮最重要的口径修正。

### E. 如果不能解除，具体缺少哪些数据？

1. **分类数据入库**：将巨潮/申万/新浪的股票→行业映射采集并持久化到本地受控表（含 `code`、`sector_name`、`sector_level`、`classification_standard`、`source`、`collected_at`）；
2. **历史分类固化**：将巨潮行业变更记录按 `变更日期` 存为历史快照表，明确"不晚于目标日的最近一次变更"回溯规则，并**固定单一分类标准**；
3. **板块指数日线**：确认并接入可用的行业/板块指数日线源（需更换网络出口重试东财或另选源），并核实指数量纲；
4. **全市场基数**：补 `stock_info` 全市场记录（交易所/上市日期/行业），支撑覆盖率与板块内家数统计；
5. **可复核证据链**：入库数据的采集时间、来源、字段契约需随结果输出，确保历史时点分类可追溯。

---

## 六、合规与本阶段边界

- 本轮**仅新增** `reports/phase3/d3-0-source-verification.md` 与 `reports/phase3/d3-0-evidence/*`，**未修改业务代码**；
- 未修改原 `d3-0-sector-design.md` 的核验记录（本报告仅交叉引用并声明口径修正）；
- 未写入真实数据库（SHA256 前后一致 `c5668fd...fb8cc`），未改 `.env`；
- 未开发 D3-1，未启动 D4/D5/E，未合并 `main`；
- 全部接口为公开数据源，未使用任何需授权的私有 Token，响应已脱敏。

## 七、状态

提交并推送本报告与证据后**停止**，等待 ChatGPT 独立复核与下一步授权。**本轮口径修正（外部存在可靠分类源）请审查方重点确认**；不自行宣布 D3 阻塞解除，不进入 D3-1。
