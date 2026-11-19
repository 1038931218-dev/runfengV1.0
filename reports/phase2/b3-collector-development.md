# B3 采集器接入开发报告（新浪主源 / 腾讯备源）

- 阶段：B3 采集链路开发（不含分析/选股/报告/技术指标/资金管理）
- 基线：`dev/b2-adapters` @ `5eaeaceb7ccbf42df28af88664379020ab40b993`
- 开发分支：`dev/b3-collector`
- 实际时间：2026-11-19 (Asia/Shanghai)

## 一、开发目标与边界

将 B2 已验收的新浪（主）、腾讯（备）适配器接入实际采集链路，使系统能真正完成
「行情请求 → 数据标准化 → 质量检查 → 数据库入库」闭环。严格未做：D/E 域、指标、
选股、候选股、资金管理、报告生成；未改正式库结构；未碰真实数据库；未提交任何凭证。

## 二、采集器接入说明

### 2.1 新增文件（复用优先，零重复解析）

| 文件 | 职责 |
|---|---|
| `adapters/smart_collector.py` | 采集编排层：选源 → 调 B2 适配器 → 质量门禁 → 入库 → 日志 |
| `quality/gate.py` | 数据质量门禁：结构/数值/日期/重复/时效校验 |

**不重复实现解析逻辑**：`smart_collector` 只调用 B2 已验收的 `SinaAdapter` /
`TencentAdapter`，消费其 `StandardQuote` / `StandardIndexQuote` 标准结构；
成交量归一（腾讯手→股×100）、字段映射、复权拒绝、amount 占位标记全部沿用 B2。

**默认源**：`collect_and_store()` 未显式传适配器时，`primary=SinaAdapter()`、
`backup=TencentAdapter()`。AKShare 适配能力保留且可注入（`primary=AKShareAdapter()`），
但**不是默认主源**（B1 已证实其东财后端在本环境断连）。

### 2.2 数据库隔离机制（能力与限制，审查后修订）

`collect_and_store(..., db_path=...)` 的 `db_path` 为**必填关键字参数**：
- 调用方必须显式指定写哪个库，`db_path=None` 直接抛 `ValueError`；
- 测试/证据全部传 `tmp_path` 临时库，`TemporaryDirectory` 自动清理；
- 入库前 `init_db(db_path)`、所有 `connect/transaction` 都只作用于该显式路径；
- 集成测试 `test_b3_never_falls_back_to_default_db` 通过 monkeypatch 让 `get_db_path()`
  抛错，证明只要显式传入 `db_path`，B3 全程**不会回落到默认真实库路径**。

**能力边界（准确表述，纠正此前"机制上杜绝真实库访问"的不严谨说法）**：
`db_path` 必填只能从机制上杜绝「隐式回落到默认库」，**不能**在机制上阻止调用方
**显式传入**真实库路径——若调用方传入真实库路径，`init_db/transaction` 会照常作用于该库。
因此本阶段执行以下纪律：
1. 真实数据库路径 `data/invest_center.db` **不得**传入任何 B3 调用（开发/验收/冒烟）；
2. 本阶段所有新增测试、冒烟、端到端验证一律使用临时库（tmp_path / TemporaryDirectory）；
3. 未修改真实数据库、未修改正式数据库结构；真实库 sha256 全程为
   `c5668fdac053ee1b11e713e0c598f036997951132bda7f31ad8d847d879fb8cc`；
4. **不增加路径黑名单**：跨平台路径判断（Linux/Windows 大小写、符号链接、相对/绝对路径）
   易误伤正常临时目录，且属本轮范围外改动；隔离完整性依靠「显式注入 + 临时库纪律 +
   SHA256 校验」三重保障，而非路径拦截。

## 三、主备降级流程

```
逐标的（个股/指数），P1-01 后每个源的 fetch 与质量门禁同处一层源级异常保护：
  1) 主源（新浪）fetch
     ├─ 异常/超时/网络错误        → 记 fallback_trace，转 2)
     └─ 成功 → 质量门禁 check_quality（由 _quality_of 包裹）
                 ├─ 检查函数自身抛异常 → 归一为 ok=False + [QUALITY_ERROR]，记原因，转 2)
                 ├─ 不合格（空/字段非法/价格逻辑错/重复日期/越界/非 history）
                 │     → 记 QUALITY_REJECT，转 2)
                 └─ 合格 → 采用新浪结果，结束
  2) 备源（腾讯）fetch + 同样完整的质量门禁
     ├─ 合格 → 采用腾讯结果，记「从新浪降级」轨迹
     └─ 失败/不合格/检查函数异常 → 记原因
  3) 主备均失败 → 抛 AllSourcesFailed（含两源完整原因，质量异常原因不丢）
     → stats["errors"] 逐标的记录；fetch_log 标 failed；绝不伪装成功、绝不跳过备源
  4) 采用结果 → 独立事务入库（INSERT OR IGNORE 幂等）→ 独立写 success 日志
```

关键纪律：
- 不使用任何缓存，绝不拿旧数据冒充最新数据；
- 失败的新浪请求完整留痕（`fallback_trace`），不明确标成功的绝不入库；
- 入库是独立事务，DB 故障自动 rollback，不影响其它标的；
- 成功日志独立写（P3）：日志故障记入 `log_errors`，不回滚入库、不伪装成采集失败。

## 四、数据类型与口径

- 历史日线全部 `data_type=history`（质量门禁强制校验，非 history 直接判不合格拒绝）；
- 实时与历史严格分离：本采集器不产出任何 realtime/delayed 数据；
- 复权：采集器使用 B2 适配器默认 `ADJ_NONE`；请求复权时由 B2 适配器抛 `ParameterError`
  明确拒绝（B2 已验收行为，本层不绕过）；
- 成交量：腾讯「手×100」归一为「股」，与新浪口径一致（B2 已实现）；
- amount：sina/tencent 均为占位 `amount=0.0`、`amount_available=False`；
  统计一律走 `sum_real_amount()` 排除占位，本轮端到端 `real_amount=0.0` 已验证；
- 血缘：每条入库数据保留 source / interface / quote_time / collected_at /
  adjust_type / data_type（集成测试逐字段核验）。

## 五、测试结果与完整执行命令

| 项 | 命令 | 结果 |
|---|---|---|
| 编译 | `python3.11 -m compileall -q adapters quality storage tests` | COMPILE_OK |
| B3 单测 | `pytest tests/test_smart_collector.py -q` | **24 passed**（原 20 + 审查修复新增 4） |
| B3 集成 | `pytest tests/test_smart_collector_integration.py -q` | **12 passed**（原 9 + 审查修复新增 3） |
| 全量非网络 | `pytest -m "not network" -q` | **174 passed, 8 deselected**（零回归） |
| 集成全集 | `pytest -m "integration" -q` | **27 passed** |
| B2 回归 | `pytest tests/test_new_adapters.py -q` | **68 passed**（B2 测试零回归） |
| 网络 | `pytest -m "network" -v` | **5 passed / 2 failed / 1 skipped**（本轮未改网络代码，沿用开发阶段记录） |

网络测试说明（如实记录，未伪造）：
- 新增 2 个 B3 端到端（新浪主源、腾讯降级）+ 3 个 B2 测试 = **5 passed**；
- 2 failed 为旧 AKShare 测试：东方财富接口对沙箱出口 `RemoteDisconnected` 限频
  （B1 已定位为特定源断连，非本轮代码问题）；
- 1 skipped 为 BaoStock：私有端口 10030 不可达，确认为环境限制后 skip。

B3 新增测试覆盖任务书全部要求：沪市(600519)/深市(000001,300750)股票、指数、
新浪成功、新浪失败降级腾讯、双失败记录、结构异常拦截、日期范围过滤、
重复采集幂等、数据质量不合格、来源与错误原因记录、临时库入库与事务回滚。

## 六、临时数据库集成测试证据

- `test_collect_transaction_rollback_on_db_error`：个股入库中途抛错 → 该标的试探行
  被 rollback，仅剩正常标的，其它标的不受影响；
- `test_collect_idempotent_duplicate_collection`：同标的采集两次，第二次
  `stocks=0/indices=0`，库内行数不增长（INSERT OR IGNORE 幂等）；
- 全程 `tmp_db` fixture / `TemporaryDirectory`，真实库 `data/invest_center.db`
  的 sha256 在开发前后未变。

## 七、实际数据源采集证据

| 证据文件 | 内容 |
|---|---|
| `reports/b3_evidence/b3_e2e_sina_collection.json` | 新浪主源真实采集：2 股（茅台/格力）+ 1 指数（沪深300），14+7 行入库，sources 全 sina，real_amount=0.0 |
| `reports/b3_evidence/b3_e2e_tencent_fallback.json` | 主源注入故障 → 真实腾讯备源降级：14+7 行入库，sources 全 tencent，fallback_trace 完整 |

均标注 `data_type=history`、采集时间、来源、接口，原始交易日为 2026-11-18 前 7 个交易日；
未将历史行情标记为实时。

## 八、修改文件清单

新增源码：`adapters/smart_collector.py`、`quality/gate.py`
新增测试：`tests/test_smart_collector.py`、`tests/test_smart_collector_integration.py`
修改测试：`tests/test_network.py`（追加 2 个 B3 网络测试）
报告证据：`reports/phase2/b3-collector-development.md`（本文件）、
`reports/phase2/{unit,integration,network,new_adapters}_result.txt`、
`reports/b3_evidence/*.json`

未修改：`collector.py`（旧采集器）、`repository.py`、`base.py`、各 B2 适配器、
任何数据库结构/真实数据。

## 九、已知问题与未完成事项

1. 新浪/腾讯均无成交额字段，`amount=0.0` 为占位（已通过 `amount_available` 标记 +
   `sum_real_amount` 排除，不伪装真实成交额）；如需真实成交额须另接数据源。
2. 两源仅支持不复权（qfq/hfq 由 B2 适配器明确拒绝）；如需复权须接入支持复权的源。
3. AKShare 2 个网络失败、BaoStock 1 个 skip 是 B1 已记录的外部限制，本轮未改其代码。
4. 主备降级为逐标的、同步串行；并发/异步调度不在本阶段范围（未开发 D/E）。
5. **跨数据源重复数据策略：跨来源唯一（同来源幂等）**。`daily_quote` 唯一键为
   `(code, trade_date, adjust_type, period, source)`，`index_quote` 为
   `(index_code, trade_date, source)`，均含 `source`。因此：同一来源同标的同日重复采集 →
   `INSERT OR IGNORE` 幂等不新增；**新浪与腾讯对同一标的同一交易日各自留存一行**，
   属刻意的「来源留存」策略，不是跨来源覆盖。本轮未改唯一键、未做数据库迁移。

## 十、第一轮独立审查修复记录（本轮）

审查结论「有条件通过」，针对 P1-01 / P1-02 / P2-01 三项逐项修复，均不扩大范围、不碰真实库。

### 10.1 P1-01 质量门禁异常必须触发备用源
- 根因：原 `quality_checker()` 调用在 fetch 的 `try/except` 之外，检查函数自身抛异常时会
  越过源级边界直达结束，既不降级备源，又可能被上层当成该源结果。
- 修复（`adapters/smart_collector.py`）：
  - 新增 `_quality_of()`，把质量检查函数自身异常统一归一为 `ok=False` 报告，
    reasons 带 `[QUALITY_ERROR] <异常类型>: <原因>`，绝不向外抛、绝不计为成功；
  - `fetch_with_fallback()` 将单源 `fetch` + `_quality_of` 纳入**同一 try 源级保护**，
    三类失败（fetch 异常 / 质量不合格 / 检查函数异常）统一留痕并 `continue` 到下一源；
  - 两源都失败时 `AllSourcesFailed` 消息用 ` || ` 拼接两源 trace，保留两边原因。
- 新增 4 个单测（`test_smart_collector.py`）：主源 `ok=False`→腾讯成功、主源质量检查抛
  `RuntimeError`→腾讯成功、两源质量检查都抛异常→双失败且保留两边原因、主源质量合格→
  腾讯不被调用。新增 1 个集成测试验证端到端降级成功不误记为采集失败。

### 10.2 P1-02 明确数据库隔离边界
- 已删除报告与源码注释中「`db_path` 必填即机制上杜绝真实库访问」的不严谨表述（见 2.2 节），
  明确其只能防「隐式回落」、不能防「显式传入真实库路径」。
- 新增集成测试 `test_b3_never_falls_back_to_default_db`：monkeypatch `get_db_path()` 抛错，
  验证显式传 `db_path` 时 B3 全程不解析默认真实库路径。
- 本轮未增加路径黑名单（理由见 2.2 节第 4 条），未修改真实库与正式库结构。

### 10.3 P2-01 跨数据源重复数据策略
- 新增集成测试 `test_cross_source_unique_and_same_source_idempotent`：首次新浪成功、
  二次新浪失败降级腾讯、三次新浪再成功；验证库内 `sina=1 / tencent=1` 两行共存、
  同来源第三次不新增。策略定性为**跨来源唯一（同来源幂等）**，详见第九节第 5 条。
- 未改唯一键、未做数据库迁移。

### 10.4 本轮验证结果
| 项 | 结果 |
|---|---|
| 新增测试 | 单测 4 + 集成 3 = **7 个全部通过** |
| 全量非网络 | **174 passed, 8 deselected**（修复前 167，零回归） |
| 集成全集 | **27 passed**（修复前 24） |
| B2 回归 | **68 passed**（零回归） |
| 真实库 SHA256 | `c5668fda...b8cc` 修复前后一致，未改动 |

本轮修复分支从 `dev/b3-collector` @ `c1270b33...` 分出独立提交，不合并 main、不改
`review/round5`、不开发 D/E、不提交凭证。等待 ChatGPT 独立复核，复核前不自行合并 main。
