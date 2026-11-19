# B2 数据适配器开发报告（新浪主源 / 腾讯备源）

> 阶段：B2 数据适配器开发（承接 B1 数据源可用性专项验证）
> 执行时间：2026-11-19（Asia/Shanghai）
> 开发基线：`review/round5` @ `020959c`（main 保持 `88d1145`，未合并、未覆盖）
> 交付方式：独立开发分支，未进入 D/E 分析报告模块

---

## 1. 分支、提交与基线

| 项 | 值 |
|---|---|
| 开发基线 | `review/round5` `020959cc6d13c754ce8b4028f81cfa72044477ce` |
| 开发分支 | `dev/b2-adapters`（本轮新建，见交付摘要中的实际 SHA） |
| main | `88d1145968013b843b90ad79c66cbc950b75f269`（未改动） |
| 真实数据库 | `data/invest_center.db` 未改动、未提交、未用于破坏性测试 |
| 敏感信息 | 无 Token/密钥/账户信息进入提交 |

---

## 2. 本阶段修改文件清单

| 文件 | 类型 | 说明 |
|---|---|---|
| `adapters/sina_adapter.py` | 新增 | 新浪行情适配器（主数据源） |
| `adapters/tencent_adapter.py` | 新增 | 腾讯行情适配器（备用数据源） |
| `tests/test_new_adapters.py` | 新增 | 15 项纯逻辑单元测试（monkeypatch，不依赖网络） |
| `tests/test_network.py` | 修改 | 追加 3 项真实采集网络测试（新浪/腾讯/双源一致性） |
| `reports/phase2/b2-adapters-development.md` | 新增 | 本报告 |
| `reports/phase2/{unit,integration,network}_result.txt` | 更新 | 本轮三组真实测试结果（带时间戳） |
| `reports/b2_evidence/*.json` | 新增 | 3 股 1 指数原始响应 + 标准结构 + 双源一致性证据 |

> 未改动任何已审查通过的既有源码（`base.py`、`akshare_adapter.py`、`baostock_adapter.py`、`collector.py`、`timeout.py` 等均保持 `020959c` 原样）；未进入 D/E 域。

---

## 3. 核心功能说明

### 3.1 设计依据（承接 B1 实测结论）

B1 专项验证已确认：东财（AKShare 后端）对本沙箱出口 IP 断连、BaoStock 私有端口不可达；新浪 `getKLineData` 与腾讯 `kline/kline` 实测 HTTP 200、连续 10/10 稳定。据此实现新浪为主源、腾讯为备源。

### 3.2 SinaAdapter（主源）

- 接口：`CN_MarketData.getKLineData`，`requests` + 显式 `(connect, read)` 超时 + 基类有限重试/退避/总 deadline。
- **日期处理**：该接口实测**忽略 start/end 参数**（传入 2025 日期仍返回最近数据），因此拉近 2000 根后在本地按 `[start, end]` 过滤；`YYYYMMDD`/`YYYY-MM-DD`/中文日期统一规范化（本轮开发中发现并修复了 8 位日期未转横线导致过滤全空的 bug）。
- 字段：每行 `day/open/high/low/close/volume`，成交量单位为**股**。
- 复权：该接口无复权参数，`adjust != none` 一律抛 `ParameterError` 明确拒绝，绝不返回未复权数据却标称复权。
- 数据血缘：`source=sina`、`interface=sina_CN_MarketData.getKLineData`、`data_type=history`、`adjust_type`、`quote_time`、`collected_at` 齐全；**amount 恒为 0.0**，因源无成交额字段，明确不编造。

### 3.3 TencentAdapter（备源）

- 接口：`web.ifzq.gtimg.cn/appstock/app/kline/kline`（**不复权日 K**），服务端支持日期过滤 + 本地兜底过滤，同样走显式超时 + 有限重试。
- **字段顺序陷阱**：`day` 行顺序为 `[date, open, close, high, low, volume]`，第 2 个是 **close 而非 high**，已按此精确映射（单元测试锁定）。
- **成交量单位**：源单位为「手」，×100 归一为「股」，与新浪口径对齐（茅台同日腾讯 29847 手 vs 新浪 2984695 股，归一后一致）。
- 复权：复权接口 `fqkline(...,qfq)` 实测被腾讯 WAF 拦截（HTTP 501），仅支持 `ADJ_NONE`，其余抛 `ParameterError`。
- 数据血缘：`source=tencent`、`data_type=history`、**amount=0.0**（源无成交额，不编造）。

### 3.4 共同约束

- 两源均为日线历史/当日收盘数据，`data_type=history`，**绝不冒充实时行情**。
- 复用既有 `BaseAdapter._call_with_retry`（标准 HTTP 可安全超时中断，无需进程隔离）。
- 异常分类：数据解析失败/空区间/非法结构/数值转换失败 → `DataParseError`；复权不支持 → `ParameterError`；网络错误保留原始 `requests.ConnectionError`（基类既定语义，重试耗尽抛原始类型）。

---

## 4. 测试命令与完整结果

```
# 编译检查
python3.11 -m compileall -q adapters tests          -> COMPILE_OK

# 新增单元测试（纯逻辑）
python3.11 -m pytest tests/test_new_adapters.py -q  -> 15 passed

# 全量非网络（单元+集成）
python3.11 -m pytest -m "not network" -q            -> 85 passed, 6 deselected

# 集成测试
python3.11 -m pytest -m "integration" -q            -> 15 passed

# 网络真实采集
python3.11 -m pytest -m "network" -v
  -> 3 passed（B2 新增：新浪/腾讯/双源一致性）
     2 failed（旧 AKShare 测试：东财 RemoteDisconnected 限频，与 B1 一致，非本轮代码问题）
     1 skipped（BaoStock：NetworkLimitError 环境限制，如实跳过）
```

代码测试合计：**85 passed**（非网络），无回归。网络失败如实记录，**未伪造通过**。完整原始输出见 `reports/phase2/{unit,integration,network}_result.txt`。

---

## 5. 真实数据验证证据

证据目录：`reports/b2_evidence/`（原始响应 + 标准结构 + 双源一致性汇总 JSON）。

| 标的 | HTTP | 最新交易日 | 收盘价 | 标准行数 | data_type |
|---|---|---|---|---|---|
| 贵州茅台 600519 | 200 | 2026-11-18 | 1442.66 | 7 | history |
| 宁德时代 300750 | 200 | 2026-11-18 | 241.01 | 7 | history |
| 格力电器 000651 | 200 | 2026-11-18 | 40.75 | 7 | history |
| 沪深300 000300.SH | 200 | 2026-11-18 | 3983.767 | 7 | history |

**双源交叉验证**：新浪 vs 腾讯对贵州茅台 7 个交易日（2026-11-10~11-18）收盘价**完全一致，最大绝对差 0.0000**。证明字段映射正确、两源可互为备份校验。

---

## 6. 已知问题与未完成事项

1. **成交额缺失**：新浪/腾讯日 K 接口均不返回成交额，`amount=0.0` 为占位值（非真实成交额，已在 `interface`/本报告标注）。若后续报告需要成交额指标，需另寻提供该字段的数据源（如 Tushare，需用户提供自有 Token）。
2. **复权数据缺失**：两源当前均无法提供复权数据（新浪无参数、腾讯 qfq 被 WAF 拦截）。需要复权口径时须接入其他源；本轮已按要求明确拒绝而非伪造。
3. **AKShare/BaoStock 仍不可用**：旧网络测试中的 AKShare 2 失败、BaoStock 1 跳过，是 B1 已记录的外部环境限制，非本轮引入；本轮未改动其代码。
4. **采集器未接入新源**：`collector.collect_and_store()` 当前仍仅支持 `source="akshare"`。本轮按"只做数据适配器开发、不扩大改动面"的边界，**未修改采集器装配**。将新浪/腾讯接入实际采集入库流程建议作为下一子任务（B3），并同步补集成测试。
5. **未进入 D/E 域**：分析/报告/调度模块本轮未触碰，符合"不提前进入 D/E"的指令。

---

## 7. 下一阶段建议

1. **B3**：将新浪/腾讯适配器接入 `collector`，支持 `source="sina"/"tencent"` 的采集入库，补集成测试（临时库，不碰真实库）。
2. **复权/成交额**：若分析域确需，评估 Tushare Pro（需用户提供 Token）或新浪另一复权接口，先做可用性验证再接入。
3. **交叉校验落地**：用 B2 的双源一致性证据，接入 `quality/cross_check` 的真实双源校验流程（C 域）。
4. 全部经 ChatGPT 审查团队独立审查通过后，方可合并 main 与部署。

> 本提交**不代表最终验收通过**，等待独立审查结论。
