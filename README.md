# 供应商评分口径

季度供应商评级后端：维护评分指标、数据来源、权重版本、封账批次与申诉案件；
支持试算方案比较、正式发布固化输入与规则、四类更正版本（迟到数据、异常订单剔除、
申诉采纳、规则勘误）、逐项得分解释与按历史口径复算。纯 Python 标准库实现，零三方依赖。

## 设计要点（对应业务痛点）

- **换权重不改历史**：权重按 `scheme_code + version` 版本化，draft 可试算、published 才能发布；
  发布钉住 `weight_version_id`，事后再换新权重只产生后续口径，历史等级原样可查。
- **发布即固化**：正式发布在批次封账后进行，生成不可变输入快照（含记录集合/剔除标记与
  content_hash），并逐项钉住指标规则版本 `spec_versions`。
- **封账后只能更正、不能覆盖**：迟到数据常规通道拒收，必须经 `late_data` 更正版本补录；
  异常订单剔除只在快照层标记 excluded、原始记录保留；申诉采纳自动生成关联更正版本；
  规则勘误以指标新版本重算、输入快照不变。所有更正沿版本链追加（v1、v2、v3…）。
- **逐项可解释**：解释接口列出每个指标的公式、权重、加权贡献及每条原始记录是否达标，
  供应商可直接看到扣分来自哪张单（如 D2 迟交）。
- **历史口径可复算**：复算用版本钉住的快照 + 规则版本 + 权重重跑纯函数引擎，
  与留存 `content_hash` 比对，任何一版历史结果都可验证未被篡改。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/supplier_rating/`：评级后端。
  - `models.py`：数据来源、指标规则版本、权重版本、批次、输入快照、发布版本链、申诉。
  - `scoring.py`：纯函数评分引擎（指标计算、加权、等级、逐项解释）。
  - `service.py`：应用服务（版本管理、封账、试算比较、发布、更正、申诉、复算）。
  - `api.py`：零依赖 HTTP 接口（`python3 -m supplier_rating.api`，默认 127.0.0.1:8080）。
- `tools/check_contract.py`：契约命令行检查。
- `tools/demo_rating.py`：端到端业务演示（含全部更正场景与复算校验）。
- `tests/`：契约与评级领域回归测试。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/sources` `/metrics` `/weights` | 维护数据来源、指标（勘误即新版本）、权重草稿 |
| POST | `/weights/{wid}/publish` | 发布并冻结权重版本 |
| POST | `/batches`、`/batches/{id}/close` | 创建季度批次、封账 |
| POST | `/records` | 常规数据通道（封账后拒收，提示走更正） |
| POST | `/trials` `/compare` | 试算、多权重方案比较 |
| POST | `/publish` | 封账后正式发布（固定快照与规则版本） |
| POST | `/publications/{id}/corrections/{late-data,exclusions,errata}` | 三类更正 |
| POST | `/appeals`、`/appeals/{id}/decision` | 申诉立案、裁定（采纳自动更正） |
| GET | `/publications/{id}/versions` | 版本链 |
| GET | `/publications/{id}/explain?version=&metric=` | 逐项得分解释 |
| GET | `/publications/{id}/recompute?version=` | 历史口径复算与哈希校验 |

## 验证

```bash
python3 -m unittest discover -s tests -v     # 全部回归测试
python3 -m compileall -q src tools tests     # 编译检查
python3 tools/check_contract.py domain/contract.json
python3 tools/demo_rating.py                 # 端到端演示
```
