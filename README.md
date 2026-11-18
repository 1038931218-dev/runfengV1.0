# 润锋投资数据中心 V1.0

A 股数据采集、存储、校验与研究报告生成系统。**只做数据采集、研究分析和报告生成，不连接券商、不下单、不自动交易。**

> 开发状态：**第二阶段（AKShare + BaoStock 历史行情接入）已完成**。已真实采集 6 个标的共 60 行历史日线入库并通过测试；技术指标、报告生成等后续阶段按任务书推进。

## 数据源可用性（本沙箱实测）

| 数据源 | 接口 | 状态 |
|---|---|---|
| AKShare | `stock_zh_a_hist` / `stock_zh_index_daily_em` | ✅ 真实采集成功（走标准 HTTP） |
| BaoStock | `query_history_k_data_plus` | ❌ 私有端口 10030/10031 被沙箱封锁，login 失败（10002007）；代码已实现，环境放行即可用 |

关键修复：`adapters/base.py` 设置全局 `socket.setdefaulttimeout(12)`，兜底第三方库内部无超时请求导致的挂起。


## 环境要求

- Python 3.11+
- SQLite（Python 内置，无需单独安装）

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置密钥（可选，Tushare 阶段才需要）
cp .env.example .env
# 编辑 .env 填入 TUSHARE_TOKEN（绝不提交 .env）

# 3. 初始化数据库
python -c "from storage.db import init_db; print(init_db())"

# 4. 运行测试
python -m pytest tests/ -v
```

## 目录结构

| 目录 | 职责 |
|---|---|
| `config/` | YAML 非敏感配置 + `.env` 环境变量加载 |
| `adapters/` | 数据源适配器（AKShare / BaoStock / Tushare / 巨潮） |
| `storage/` | SQLite 建表、连接、仓储 CRUD |
| `quality/` | 数据质量校验（后续阶段） |
| `analysis/` | 技术指标与分析（后续阶段） |
| `report/` | 中文报告生成（后续阶段） |
| `scheduler/` | 定时任务（后续阶段） |
| `utils/` | 日志、时区、通知工具 |
| `tests/` | 单元测试 + 数据源测试 |
| `data/` | SQLite 数据库与报告输出（已 gitignore） |

## 配置与密钥

- 非敏感配置（`config/config.yaml`）：路径、调度时间、风控阈值、数据源开关。
- 密钥（`.env`）：`TUSHARE_TOKEN` 等。**`.env` 已加入 `.gitignore`，严禁提交 Git。**
- 仓库提供 `.env.example` 作为模板。

## 时区

全系统强制使用北京时间 `Asia/Shanghai`，不依赖操作系统时区（`utils/timezone.py`）。

## 数据表

| 表 | 说明 |
|---|---|
| `daily_quote` | 行情主表，含来源 / 行情时间 / 采集时间 / 数据时效字段，按 (代码,日期,复权,来源) 去重 |
| `announcement` | 公告数据 |
| `stock_info` | 股票基础信息 |
| `index_quote` | 指数行情 |
| `data_quality_log` | 数据质量异常记录 |
| `fetch_log` | 接口调用成功 / 失败记录 |

## 风控红线（贯穿全系统）

- 账户总资金上限 10,000 元，不得假设追加资金。
- 单笔交易计划风险原则上 50～100 元。
- 不融资、不借款、不满仓；只输出研究计划，不承诺收益。

## 当前阶段的已知限制

当前沙箱环境中，Python 数据源 SDK 直连外网 socket 被网络策略拦截（curl 可达但 Python 取数超时），因此**后续数据源实盘采集测试可能受限**；相关阶段会如实记录测试结果，不以缓存冒充实时数据，不编造结论。
