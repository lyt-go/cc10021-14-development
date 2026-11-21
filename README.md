# 数据质量监控与元数据治理平台

面向数据质量标准定义、自动巡检、异常告警、元数据采集与数据目录构建的数据质量治理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 质量规则定义 | `dq_rule_def` | 质量规则 | 规则编号、规则名称、目标表名 |
| 巡检任务管理 | `dq_check_run` | 巡检任务 | 任务编号、关联规则集、数据范围 |
| 质量事件管理 | `dq_incident` | 质量事件 | 事件编号、关联规则、触发时间 |
| 元数据采集器 | `metadata_crawler` | 采集器配置 | 采集器编号、数据源类型、连接地址 |
| 数据表目录 | `table_catalog` | 表目录 | 表编号、表名称、所属库名 |
| 字段画像分析 | `column_profile` | 字段画像 | 画像编号、关联表名、字段名称 |
| 血缘图谱 | `lineage_graph` | 血缘关系 | 关系编号、上游表名、下游表名 |
| 指标波动监控 | `metric_monitor` | 监控指标 | 指标编号、目标表名、监控字段 |
| 质量评分卡 | `dq_scorecard` | 评分记录 | 记录编号、目标表名、完整性得分 |
| 数据分类标签 | `tag_classify` | 分类标签 | 标签编号、标签名称、分类维度 |
| 规则模板库 | `dq_template` | 规则模板 | 模板编号、模板名称、规则类型 |
| 模板参数覆盖 | `dq_param_override` | 参数覆盖 | 覆盖编号、模板族、模板版本、业务域、覆盖参数 |
| 目标表归属 | `dq_table_ownership` | 表归属 | 归属编号、目标表名、业务域、绑定字段 |
| 编译会话 | `dq_compile_session` | 编译会话 | 会话编号、编译批次、编译来源、状态 |
| 派生规则复核 | `dq_derived_rule` | 派生规则 | 派生编号、目标表名、模板版本、覆盖版本、编译来源 |
| 异常模式识别 | `anomaly_pattern` | 异常模式 | 模式编号、模式名称、检测方法 |
| 质量报告生成 | `dq_report` | 质量报告 | 报告编号、报告周期、覆盖表数 |
| 元数据变更对比 | `metadata_diff` | 变更记录 | 变更编号、变更对象、变更类型 |
| 告警通知通道 | `dq_alert_channel` | 告警配置 | 配置编号、关联规则集、通知方式 |
| 质量基线管理 | `dq_baseline` | 基线定义 | 基线编号、目标表名、基线指标 |
| 主数据实体注册 | `mdm_entity` | 主数据实体 | 实体编号、实体名称、业务域 |
| 质量趋势看板 | `dq_trend` | 趋势记录 | 记录编号、目标表名、日期 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

### 派生规则治理（模板参数归属与版本）

`backend/app/services/derived_rule.py` 在模板实例化之上补全参数生产来源与版本治理：

- **参数校验**：`POST /api/dq_derived_rule/validate-params` 按模板 `参数规格` 校验必填、类型（identifier/number）与 SQL 占位符一致性；规则参数禁止经绑定通道传入。
- **三种生产来源**：`本域绑定`（table/column 等标识参数）、`本域覆盖`（`dq_param_override`，带覆盖版本）、`继承共用默认`（带模板版本），每个参数逐项标注并随派生规则留存可回查。
- **越权防护**：`dq_table_ownership` 登记的表只能由归属域绑定，共享模板组直接改归属表、跨域绑定、未登记字段一律拦截（HTTP 403）。
- **中断/恢复**：`interrupt=true` 编译停在参数解析后、SQL 渲染前，会话固化 `编译来源`；`POST /api/dq_compile_session/{id}/resume` 恢复时重算来源并校验与固化值一致。
- **复核与权限范围**：编译结果回写 `dq_derived_rule`（待复核），并在 `dq_rule_def` 登记实际绑定表/字段与版本；`GET /api/dq_derived_rule/scope/list?team=` 让共享模板组只看继承口径、本域团队只看本域归属，复核动作同样按范围鉴权。
- **共用字段调整对比**：`POST /api/dq_template/shared-change/recompile` 仅共享模板组可调共用默认值并发布新版本；对各域最新派生规则重编译，逐口径返回调整前后的可运行 SQL、派生版本与 SQL 是否变化（本域覆盖口径 SQL 不变但版本仍升级待复核）。

领域：数据工程
子方向：数仓与数据湖
