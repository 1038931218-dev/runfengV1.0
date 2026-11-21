# D3-1 前置第二阶段｜数据模型与历史时点规则设计

- 阶段：设计文档（只设计，不实现业务代码、不改数据库结构）
- 基线：`2389e2da09e6a0cc231bca823fcf5f261d4c41b2`
- 说明：以下为建议模型，字段来自本轮与外部接口实测证据；**落地前须先解除分类与指数源阻塞**（见 readiness-checklist）。

---

## A. 行业分类表（`industry_classification`）

| 字段 | 类型建议 | 说明 | 来源依据 |
|---|---|---|---|
| `security_code` | CHAR(6) | 6 位代码，保留前导零；北交所存 920 新段 | 5580 集合逐只验证；旧代码段映射失败 |
| `classification_standard` | VARCHAR | 标准标识，如 `008002`(巨潮)/`CSRC`(证监会) | change 接口"分类标准编码"、profile"所属行业" |
| `classification_version` | VARCHAR | 版本/生效批次 | 巨潮目录"终止日期"可作版本边界 |
| `industry_code` | VARCHAR | 行业编码（如巨潮 Z01010101） | 巨潮目录"类目编码"、change"行业编码" |
| `industry_name` | VARCHAR | 行业名称 | 同上 |
| `industry_level` | SMALLINT | 层级 0-4 | 巨潮目录"分级"；change 门类/大类/中类/次类 |
| `effective_from` | DATE | 生效起始 | change"变更日期"（需确认语义） |
| `effective_to` | DATE | 生效结束（SCD2） | 巨潮目录"终止日期" |
| `source` | VARCHAR | 来源接口标识 | `cninfo_change` / `cninfo_profile` |
| `fetched_at` | TIMESTAMP | 采集时间 | 本轮 request_time |
| `data_status` | VARCHAR | `ok`/`stale`/`unknown` | 时效判定（当前 unknown） |

**主键建议**：`(security_code, classification_standard, industry_level, effective_from)` —— 同一股票同一标准同一层级同一生效期唯一。

**约束**：`security_code` 必须属于 universe 基准表（5580）；北交所 920 段；禁止空 `classification_standard`。

---

## B. 行业指数表（`industry_index_daily`）

| 字段 | 类型建议 | 说明 | 来源依据 |
|---|---|---|---|
| `index_code` | CHAR(6) | 指数代码（如 399986/000932） | `index_csindex_all` 行业类清单 |
| `index_name` | VARCHAR | 指数名称 | 同上 |
| `classification_standard` | VARCHAR | 该指数对应的分类标准 | 需业务确认（中证行业指数对应中证行业分类） |
| `industry_code` | VARCHAR | 行业编码 | 待分类标准确定后映射 |
| `trade_date` | DATE | 交易日 | 中证/新浪 date 字段 |
| `open/high/low/close` | DECIMAL | OHLC | 接口原始值 |
| `volume` | BIGINT/DECIMAL | 成交量 | 中证"成交量"列（单位 unknown） |
| `amount` | DECIMAL | 成交额 | 中证"成交金额"列（单位 unknown） |
| `amount_available` | BOOLEAN | 成交额是否真实可用 | 单位未确认→false；复用 B3 契约 |
| `source` | VARCHAR | 来源 | `csindex`/`sina`/`tx` |
| `fetched_at` | TIMESTAMP | 采集时间 | — |
| `quality_status` | VARCHAR | `ok`/`rejected`/`suspect` | 节假日重复异常→`suspect` |

**主键建议**：`(index_code, trade_date, source)`。

**质量硬规则**（来自本轮证据）：
1. 检测到"不同日期 OHLC+量+额完全相同"（如中证 2023-01-01/01-03）→ 该组记录 `quality_status=suspect`，不计入 production 数据。
2. `amount_available` 仅当单位已确认时置 true；当前中证/新浪单位 unknown → **全部 false**。
3. 非交易日记录（元旦等）需交易日历校验后方可入库；缺日历时标 `suspect`。

---

## C. 历史时点规则

### C.1 保存历史分类变更
- 采用 **SCD Type-2**：每次分类变更插入新行，`effective_from=变更日期`，`effective_to=下次变更日期-1`；变更日期语义需先确认（见阻塞项）。
- 巨潮 change 接口提供"变更日期 + 分类标准编码 + 四级行业"，可支撑；但时效止于 2022，需补齐后续变更。
- 同一股票同一标准同一天多条变更 → 需去重规则（本轮未统计冲突数，标 unknown）。

### C.2 查询某股票历史某日行业
```sql
SELECT industry_name, industry_code
FROM industry_classification
WHERE security_code = :code
  AND classification_standard = :std
  AND effective_from <= :as_of_date
  AND (effective_to IS NULL OR effective_to >= :as_of_date)
  AND data_status = 'ok';
```
- 返回 0 行 → 该日"未知分类"（unknown），不猜测、不回填。
- 返回多行 → 数据冲突，报 unavailable，不任取其一。

### C.3 退市股票处理
- 退市股票保留在 universe 历史表（`data_status=delisted`，含退市日期）；分类记录不删除，按生效区间继续可查。
- 覆盖率分母按"某时点有效股票"计算，退市股按退市日期从当期分母剔除；退市名单口径见前轮（沪 161原始/155唯一/6额外重复、深 213/213/0、北交 unknown）。

### C.4 未知分类处理
- 无分类记录或分类过期（stale）→ `data_status=unknown/stale`，**不计入有效覆盖分子**，分母保留。
- 禁止把未知分类填成 0 或"其他"行业。

### C.5 避免新分类覆盖旧分类
- 绝不 UPDATE 覆盖；只 INSERT 新生效期行 + 关闭旧行 `effective_to`。
- 采集重复运行时按主键幂等，不产生重复历史。

### C.6 避免不同标准混用
- 所有查询强制带 `classification_standard` 条件；覆盖率按单一标准分别计算，禁止跨标准合并分子。
- V1.0 主标准与辅助标准分表/分分区存储，物理隔离。

---

## D. 设计前置依赖（本模型落地前必须解除）

1. 分类标准冻结（当前 008002 仅候选，时效/当前覆盖未证实）。
2. 分类变更日期语义确认（change"变更日期"是否=生效日期，承接 D3-0 unknown）。
3. 北交所旧→新代码映射（历史数据引用旧段会失败）。
4. 行业指数源质量与单位确认（中证节假日异常、单位 unknown）。
5. 交易日历来源（识别节假日记录，当前 unavailable）。
6. 分类多标准冲突统计与去重规则（当前 unknown）。

> 本设计仅为草案；在上述依赖解除前，不进入 D3-1 业务代码开发。
