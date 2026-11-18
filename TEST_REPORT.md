# 润锋投资数据中心 V1.0 — 测试报告

> 测试环境：Ubuntu 24.04 / Python 3.11.1 / SQLite
> 时区：Asia/Shanghai（北京时间）
> 最新执行时间：2026-11-18

---

## 一、第一阶段测试报告（环境 / 结构 / 数据库）

### 1.1 测试范围
- 配置加载与环境变量分离（`config/settings.py`）
- 时区工具强制北京时间（`utils/timezone.py`）
- SQLite 建表、幂等初始化（`storage/db.py`、`storage/models.py`）
- 仓储层 CRUD、唯一约束去重、多来源保留（`storage/repository.py`）

### 1.2 测试用例与结果

| 测试文件 | 用例 | 结果 |
|---|---|---|
| `tests/test_db.py` | 建表完整性（6 张表） | PASS |
| `tests/test_db.py` | 行情表字段完整性（含数据血缘字段） | PASS |
| `tests/test_db.py` | 重复初始化幂等性 | PASS |
| `tests/test_crud.py` | 行情写入与查询（采集时间自动填充） | PASS |
| `tests/test_crud.py` | 唯一约束去重（重复写入被忽略） | PASS |
| `tests/test_crud.py` | 不同来源同日数据分别保留 | PASS |
| `tests/test_crud.py` | 公告数据去重 | PASS |
| `tests/test_timezone.py` | 当前时区为 Asia/Shanghai（UTC+8） | PASS |
| `tests/test_timezone.py` | 时间格式化含 +0800 | PASS |
| `tests/test_timezone.py` | UTC 转北京时间（0 点→8 点） | PASS |
| `tests/test_timezone.py` | naive 时间按 UTC 处理 | PASS |
| `tests/test_timezone.py` | 交易日工作日判断 | PASS |

### 1.3 阶段测试结果
- **12 / 12 通过，0 失败。**
- 端到端验证：实际建库 → 写入带血缘字段行情 → 查询 → 清理测试数据，全部成功。
- 修复记录：`test_insert_and_query` 初次因未提交事务失败，补充 `commit()` 后通过（明确"传入 conn 由调用方管理事务"的语义）。

---

## 二、第二阶段测试报告（AKShare + BaoStock 历史行情接入）

### 2.1 真实采集结果（非模拟数据）
- 采集区间：2025-11-03 ~ 2025-11-14
- 入库：**个股 30 行 + 指数 30 行 = 60 行真实数据，0 错误**
- 标的：贵州茅台(600519)、宁德时代(300750)、平安银行(000001)、沪深300、上证指数、深证成指
- 接口：`stock_zh_a_hist`（个股）、`stock_zh_index_daily_em`（指数）

### 2.2 阶段测试结果
- **25 项：24 通过，1 跳过，0 失败。**
- 覆盖：标准化、完整性、去重、复权口径、交叉验证、单位换算、入库校验、网络连通性。
- 跳过项：BaoStock 网络测试（沙箱私有端口 10030 被封，`pytest.skip` 如实记录，非代码缺陷）。

### 2.3 交叉验证要点
- AKShare 两次采集差异为 0（数据可复现）。
- 前复权 vs 不复权价格差异生效（茅台 51.98 元），口径区分正确。
- 多源交叉验证（AKShare vs BaoStock）**未执行**：BaoStock 端口不可达，如实标注，未伪造结果。

---

## 三、审查修复后测试结果（最新）

按测试分层独立执行：

| 类别 | 命令 | 结果 |
|---|---|---|
| 单元测试 | `pytest -m "not network and not integration"` | 30 passed |
| 数据库集成测试 | `pytest -m integration` | 6 passed |
| 网络测试 | `pytest -m network` | 2 passed, 1 skipped |
| 全量 | `pytest tests/ -q` | **38 passed, 1 skipped, 0 failed** |

修复要点：测试隔离（移除真实库依赖）、显式 connect/read 超时+进程隔离硬超时、BaoStock 异常分类、交叉验证重复检测、daily_quote 增加 period 字段与安全迁移、测试重命名。详见 `reports/phase2/acceptance-audit.md`。

## 四、累计测试结果
- 修复后全量：**38 / 39 通过，1 跳过（环境限制），0 失败。**
- 全新环境（无预置数据库）非网络测试：**35 passed**，独立运行成功。

## 五、已知限制
1. 当前沙箱 BaoStock 私有端口（10030/10031）被封锁，无法真实采集；异常分类已就位，仅此项按规则 skip。
2. 超时机制：可控调用用显式 `(connect,read)`；不可中断调用用进程隔离硬超时；socket 默认超时仅兜底；重试有限（0~5）。
3. 历史行情均为 `data_type=history`，未涉及实时行情，不拿历史缓存冒充实时。
