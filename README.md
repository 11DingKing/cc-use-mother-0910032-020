# 供应商评分口径

季度供应商评级后端，覆盖交付、质量、响应三类数据，维护**评分指标、数据来源、
权重版本、封账批次和申诉案件**。核心目标：采购部门更换权重不会改写历史等级，
每一项扣分都可解释，任何分数变化都只能通过可追溯的更正版本发生。

## 领域约束

1. **正式发布固定输入和规则**：规则版本与权重版本发布后冻结；封账批次固定
   规则版本、权重版本和数据快照编号，封账结果永久不可变。
2. **权重更换只产生新版本**：历史批次仍绑定旧权重版本，历史等级不随新权重变化。
3. **更正只能生成新版本，绝不原地改分**：
   - 迟到数据 → 数据快照更正版（`late_data`）+ 更正批次；
   - 异常订单剔除 → 数据快照更正版（`abnormal_exclusion`），登记被剔除订单号；
   - 申诉采纳 → 数据快照更正版（`appeal_accepted`）+ 更正批次，申诉与批次互相留痕；
   - 规则勘误 → 勘误规则版本（`erratum`）+ 更正批次，数据问题不得走勘误。
   - 更正批次通过 `revises` 串联成链；被替代的原批次标记为 `corrected` 但结果原样保留。
4. **逐项得分解释**：每个指标输出原始值、归一化口径、权重、实得分、扣分和文字说明，
   并附带快照内容哈希（SHA-256）用于防篡改核验。
5. **按历史口径复算**：评分是纯函数，复算始终读取批次绑定的旧版本，输出与封账值的
   逐行比对。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/supplier_rating/`：
  - `models.py`：指标、规则/权重版本、数据快照、封账批次、申诉、试算方案；
  - `scoring.py`：确定性评分引擎（纯函数，支持比率/准时率与每百万缺陷数口径）；
  - `store.py`：仓储与应用服务，强制不可变、发布校验与更正链；
  - `api.py`：标准库实现的 JSON HTTP 接口（无第三方依赖）。
- `examples/demo.py`：封账 → 换权重 → 迟到补录 → 异常剔除 → 申诉 → 勘误 → 历史复算全链路。
- `tests/`：契约测试、领域规则测试（19 例）、HTTP 端到端测试。

## 验证

```bash
# 编译
python3 -m compileall -q src tools tests examples

# 全部测试
python3 -m unittest discover -s tests -v

# 契约摘要
python3 tools/check_contract.py domain/contract.json

# 端到端业务演示
python3 examples/demo.py

# 启动 HTTP 服务
PYTHONPATH=src python3 -m supplier_rating.api --port 8080
```

## HTTP 接口摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/metrics` | 登记指标（ratio / timeliness / cpm） |
| POST | `/rules`、`/rules/{code}/publish` | 创建/发布规则版本（指标集 + 等级分档） |
| POST | `/rules/{code}/corrections` | 规则勘误，生成已发布的新规则版本 |
| POST | `/weights`、`/weights/{code}/publish` | 创建/发布权重版本（合计须为 1） |
| POST | `/snapshots` | 录入数据来源快照（带来源系统标识，定稿后哈希冻结） |
| POST | `/snapshots/{id}/corrections` | 迟到数据 / 异常剔除 / 申诉订正 → 新快照 |
| POST | `/trials`、`/trials/compare` | 试算方案与多方案比较（不影响正式数据） |
| POST | `/batches` | 正式封账（规则、权重必须已发布，快照必须已定稿） |
| POST | `/batches/{id}/corrections` | 封账更正（四种原因，二选一地换快照或换勘误规则） |
| POST | `/appeals`、`/appeals/{id}/decision` | 申诉立案与裁决（采纳必须驱动更正批次） |
| GET | `/batches/{id}/explain?supplier=CODE` | 逐项扣分解释 + 口径与快照哈希 |
| GET | `/batches/{id}/recompute?supplier=CODE` | 按批次历史口径复算并与封账值逐行比对 |

## 设计说明

- **纯 Python 3.11 标准库**，无外部依赖；内存仓储带 `save_json()` 全量导出钩子。
- 评分计算不读取任何可变状态，同一（规则版本, 权重版本, 快照）三元组在任意时间
  复算结果完全一致，这是“历史等级不被新权重改写”的技术保证。
- 申诉只允许针对当前在效的已封账批次；申诉期间批次若已被其他更正替代，采纳时
  更正会自动挂到更正链最新批次上。
