# D3-1 前置第二阶段｜数据模型与历史时点规则设计

- 阶段：设计文档（只设计，不实现业务代码、不改数据库结构）
- 采集基线：`2389e2da09e6a0cc231bca823fcf5f261d4c41b2`
- 本轮修订基线：`9e71380d8191d7a22870c332717747e7db54e40b`（D3-1 前置第二阶段第五轮定点修正）
- 说明：以下为建议模型，字段来自本轮与外部接口实测证据；**落地前须先解除分类与指数源阻塞**（见 readiness-checklist）。
- 本轮修订（P1-03/P2-02）：拆分 `category_valid_to`（目录节点终止日期）与 `effective_to`（个股归属结束日期），禁止用前者填后者；`classification_version` 无依据标 unknown；SCD2 生效日期语义未确认，保持设计阻塞。

---

## A. 行业分类表（`industry_classification`）

| 字段 | 类型建议 | 说明 | 来源依据 |
|---|---|---|---|
| `security_code` | CHAR(6) | 6 位代码，保留前导零；北交所存 920 新段 | 5580 集合逐只验证；旧代码段映射失败 |
| `classification_standard` | VARCHAR | 标准标识；有明确编码证据时方填（如 change 接口返回 `008002`）；profile"所属行业"无标准编码/版本/生效日期 → 标 `unknown` | change 接口"分类标准编码"（有编码）；profile"所属行业"（无编码，标 unknown） |
| `classification_version` | VARCHAR | 分类标准版本/生效批次。**当前无明确版本依据 → 标 `unknown`**；巨潮目录"终止日期"是目录节点属性，不等于标准版本，不得据此填充 | 无官方版本字段证据；终止日期≠版本（见下 `category_valid_to`） |
| `industry_code` | VARCHAR | 行业编码（如巨潮 Z01010101） | 巨潮目录"类目编码"、change"行业编码" |
| `industry_name` | VARCHAR | 行业名称 | 同上 |
| `industry_level` | SMALLINT | 层级 0-4 | 巨潮目录"分级"；change 门类/大类/中类/次类 |
| `category_valid_to` | DATE | **分类目录节点的终止日期**（目录/类目自身属性，如巨潮目录"终止日期"字段）——仅描述该目录节点何时停用，**不是任何个股的行业归属结束日期** | 巨潮目录"终止日期"字段 |
| `effective_from` | DATE | 个股行业归属生效起始。**change"变更日期"是否等于生效日期语义未确认 → 保持设计阻塞，不得直接宣称已证实** | change"变更日期"（语义 unknown，见 C.1 / D.2） |
| `effective_to` | DATE | **个股行业归属有效结束日期**（SCD2 关闭旧行），来自该股"下一次归属变更"边界；**禁止用 `category_valid_to`（目录终止日期）直接填充** | 个股变更历史（语义待确认）；非目录属性 |
| `source` | VARCHAR | 来源接口标识 | `cninfo_change` / `cninfo_profile` |
| `fetched_at` | TIMESTAMP | 采集时间 | 本轮 request_time |
| `data_status` | VARCHAR | `ok`/`stale`/`unknown` | 时效判定（当前 unknown） |

**日期语义拆分纪律（P2-02）**：
- `category_valid_to` = **目录节点**终止日期（类目自身何时停用），属分类标准目录元数据。
- `effective_to` = **个股**行业归属有效结束日期（该股票在该行业归属的结束），属个股事实。
- 两者物理与语义均不同，**不得用目录终止日期填充个股有效结束日期**；当前未取得"目录停用→个股归属批量迁移"的证据，故不得互相推导。

**主键建议**：`(security_code, classification_standard, industry_level, effective_from)` —— 同一股票同一标准同一层级同一生效期唯一。

**约束**：`security_code` 必须属于 universe 基准表（5580）；北交所 920 段；禁止空 `classification_standard`（profile 来源填 `unknown`，非留空）；`classification_version` 未取得依据时填 `unknown`。

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
- 采用 **SCD Type-2**：每次分类变更插入新行，`effective_from` 取个股归属生效起始，`effective_to=下次生效起始-1`。**但 change 接口"变更日期"是否等于"生效日期"语义仍未确认（承接 D3-0 unknown）→ SCD2 生效日期语义保持设计阻塞，不得把"变更日期"直接宣称为已证实的生效日期**（见阻塞项 D.2）。
- `effective_to` 必须来自该股自身下一次归属边界；**严禁用 `category_valid_to`（目录节点终止日期）填充个股 `effective_to`**——后者是目录元数据，与个股归属无关。
- `classification_version` 当前无明确版本依据 → 填 `unknown`；目录"终止日期"是 `category_valid_to`，不作为版本依据。
- 巨潮 change 接口提供"变更日期 + 分类标准编码 + 四级行业"，字段可支撑结构；但变更日期语义未确认且时效止于 2022，需补齐后续变更。
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

1. 分类标准冻结（当前 008002 仅候选，时效/当前覆盖未证实）；profile"所属行业"无编码 → `classification_standard=unknown`，不作标准认定。
2. 分类变更日期语义确认（change"变更日期"是否=生效日期，承接 D3-0 unknown）——SCD2 `effective_from/effective_to` 生效语义未定前，模型保持设计阻塞。
3. `classification_version` 版本依据确认（当前无依据 → unknown；不得以目录终止日期充当版本）。
4. `category_valid_to`（目录节点终止日期）与 `effective_to`（个股归属结束日期）语义已拆分；禁止用前者填充后者。
5. 北交所旧→新代码映射（历史数据引用旧段会失败）。
6. 行业指数源质量与单位确认（中证节假日异常、单位 unknown）。
7. 交易日历来源（识别节假日记录，当前 unavailable）。
8. 分类多标准冲突统计与去重规则（当前 unknown）。

> 本设计仅为草案；在上述依赖解除前，不进入 D3-1 业务代码开发。
