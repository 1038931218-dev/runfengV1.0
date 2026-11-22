# D3-1 第三轮任务报告：关键阻塞证据补齐与独立核验

- 项目：润锋投资数据中心 / D3-1 数据阻塞调查
- 执行方：WorkBuddy（数据调查执行方）
- 生成时间：2026-11-22（GMT+8）
- 任务性质：**只读证据补齐**，非开发、非验收。本轮自检不构成独立验收，最终验收权归润策/项目负责人。
- 交付目录：`reports/phase3/d3-1-round3/`
- Git 状态：本轮**未提交、未推送、未合并**；未修改 dev/main（详见文末）

---

## 〇、六项核心结论（开头明确回答）

| # | 问题 | 结论 | 证据路径 |
|---|---|---|---|
| 1 | 5580只基准是否可被第三方独立重建？ | **PASS** | `round3-universe-5580-original.json`（5580逐只代码/名称/交易所/板块/状态时点，全部字段标源）、`round3-universe-reconciliation.json` |
| 2 | 11指数是否已通过独立官方交易日历核验？ | **CONDITIONAL PASS** | `round3-official-trading-calendar/`、`round3-index-trading-calendar-independent.json`（用独立于新浪的日历库+官方休市公告修正，22组全0缺失；但官方**完整原始**日历文件不可得） |
| 3 | 11指数是否具备足够、连续、可追溯历史行情？ | **CONDITIONAL PASS** | `round3-index-history-coverage.json`、`round3-commercial-licensing/`（sh000807/808新浪截断可由腾讯/中证官方补齐；中证官方接口实测可用但含基日伪记录需清洗） |
| 4 | 新浪行业分类是否具备 Point-in-Time 历史版本能力？ | **UNKNOWN** | 前两轮已确认仅当前快照；本轮中证历史成分未取得可验证证据（`round3-alternative-data-sources.md`） |
| 5 | 是否取得可支持商业产品使用的合法授权？ | **BLOCKED** | `round3-commercial-licensing/licensing-report.md`（新浪免费接口协议禁商用；腾讯授权适用性未证实；官方源未签约） |
| 6 | D3-1现阶段是否可解除阻塞？ | **BLOCKED** | 综合：商业授权(BLOCKED)、Point-in-Time(UNKNOWN) 两项核心阻塞未解除 |

**总体结论：本轮证据补齐取得实质进展（任务A/B/C 由 UNKNOWN 提升为 PASS/CONDITIONAL PASS），但商业授权与历史分类 Point-in-Time 两项核心阻塞仍未解除，D3-1 不具备进入正式开发的条件。**

---

## 一、任务A：5580基准原始名单重建 —— PASS

### 完成情况
- **找回原始名单**：从项目历史原始采集记录 `full_universe_code_audit.json`（采集于 2026-11-21 08:45:19+08:00，SHA256 见 manifest）重建，非本轮新采集。该文件含全部 5 来源的**逐只完整代码列表**。
- **逐只明细**：`round3-universe-5580-original.json` 含 5580 条记录，每条含 code（6位保留前导零）、name（来自 code_name 缓存）、exchange（交易所）、board_detail（板块）、listing_date（上市日期）、status_from_name（名称前缀ST判断）、退市名单比对标记、data_asof（数据时点）。**每个字段标注来源，原始数据没有的字段不补造**（如无独立"交易状态"列，status_from_name 明确标注为名称推断）。
- **时点一致性说明**：代码集合为 2026-11-21 采集；名称/上市日期/板块细分/退市名单来自 2026-11-22 11:45 缓存（cache.json）。两者均标注，未混称为同一时点。

### 可复算集合核验（`round3-universe-reconciliation.json`）
| 核验项 | 结果 |
|---|---|
| 总数 5580 | PASS（5580） |
| 重复代码 | 0 |
| 各板块分项求和 | PASS（1700+619+2905+356=5580） |
| 三市场并集 vs 东财 | 逐只相同（交集5580，仅左0，仅右0，sets_identical=true） |
| 4815映射成功是否全属基准 | PASS（4815 ⊆ 5580） |
| 70只基准外映射代码 | 完整清单已存（`mapped_outside_universe_70`） |
| 765只未映射股票 | 完整清单已存（`universe_missing_765`） |
| 覆盖率 | 86.2903%（4815/5580，分子分母口径明确） |

**口径差异如实记录**：基准源四板块 1700/619/2905/356 与第二轮 by_board 的 1700/618/1494/1410/356/其他2 存在板块归属口径差异（两套均求和5580），具体映射原因 **UNKNOWN**，未改原始证据。

## 二、任务B：独立官方交易日历核验 —— CONDITIONAL PASS

### 关键发现与处理
1. **交易所官网不提供完整历史交易日历文件**：上交所休市安排页（`sse_closed_2026_raw.html`）仅提供年度休市公告文字，无 Excel/CSV 完整日历下载。→ 完整官方原始日历文件 **UNKNOWN**。
2. **避免同源循环验证**：本轮不用新浪日历验证新浪/腾讯数据，改用独立第三方库 `chinese_calendar 1.11.0`（基于国务院/交易所官方节假日公告，与新浪生态无依赖）+ 官方休市公告修正，构造独立日历。
3. **发现并修正库偏差**：`chinese_calendar` 把 2024-02-09（除夕）误判为交易日；经上交所官方公告（上证公告〔2023〕47号/2024-6号，`sse_closed_notice_2024_full_raw.html` 原文含"2月9日至17日休市"）及新浪日历+双源行情三重佐证，显式修正为休市日。
4. **核验结果**：11 指数 × 2 源 = 22 组，全部 missing=0 / extra=0 / duplicate=0（`round3-index-trading-calendar-independent.json`）。

### 为何是 CONDITIONAL PASS 而非 PASS
独立日历仍是"第三方库+官方公告修正"的**推导产物**，不是交易所官方发布的完整原始日历文件。独立性较第二轮显著提升，但不能等同于官方原始日历核验。

## 三、任务C：指数历史覆盖与替代数据源 —— CONDITIONAL PASS

- 11指数逐只首末/行数/缺口见 `round3-index-history-coverage.json`。
- **sh000807/808 新浪截断**：新浪分别止于 2018-12-25 / 2021-02-04，腾讯连续至 2026-11。属新浪源缺口。
- **中证官方接口实测可用**：`index-perf` 接口返回 3782 行（2011-05起）完整 OHLCV+估值字段；批量拉取 4 成功 4 超时（失败痕迹保留）。发现官方数据含 `20000101` 基日伪记录（周六，值同 20050104），须清洗。
- 完整分析见 `round3-alternative-data-sources.md`。官方历史成分/生效日期仍 UNKNOWN。

## 四、任务D：商业授权 —— BLOCKED

分项结论见 `round3-commercial-licensing/licensing-report.md`。摘要：
- **新浪当前免费接口**：用户协议 6.1/6.2 明确禁未经书面许可商用 → 明确禁止；但存在官方智研平台 APIKey 商业渠道（不同通道，需另行申请）。
- **腾讯当前接口**：适用协议未证实 → UNKNOWN。
- **官方源**：存在商业渠道但未签约 → UNKNOWN。
- **总体**：无任何来源证明本项目已获商业授权，授权为硬性阻塞。

## 五、未解决问题
1. 商业授权（硬性阻塞）：需向新浪智研、腾讯、中证正式洽询并取得书面授权。
2. 新浪行业分类 Point-in-Time 历史版本能力：UNKNOWN，影响回测有效性。
3. 完整官方交易日历原始文件：不可得（仅有年度休市公告）。
4. 中证官方历史成分、历史生效日期、复权口径：UNKNOWN。
5. 中证官方接口 4 个指数批量请求超时，未取得；早期基日伪记录需清洗规则确认。

## 六、交付与合规状态
- 本轮新增 24 份证据/分析文件 + 2 份清单文件（`round3-evidence-manifest.json`、`round3-SHA256SUMS.txt`），全部在 `reports/phase3/d3-1-round3/`。
- JSON 严格解析通过；原始响应与分析结论分目录/分文件保存；失败记录（中证超时、PDF超时、SPA空壳）均保留。
- **未修改**第一/二轮报告、ZIP、JSON、SHA256清单。
- **Git**：当前分支 `audit/d3-1-evidence-expanded`，HEAD `f31116602373039008af6fb6acd50977a69de107`；dev=`275dca7123887f2cf47a9a55e689d87bfb726f50`、main=`88d1145968013b843b90ad79c66cbc950b75f269`，均未改变。本轮**未 commit / 未 push / 未合并**。等待授权后再创建独立审查分支提交。
