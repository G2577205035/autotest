# 烈马自动化测试平台：项目结构与接手指引

> 最后更新：2026-09-10
> 适用项目：`auto-test` / Python 包 `auto_test`  
> 维护要求：任何新增、删除、移动或重命名目录、源码分层、启动入口、部署文件的改动，都必须在同一次改动中更新本文件。

## 1. 接手顺序

1. 阅读 `PROJECT_PLAN.md`，确认当前进度、安全边界和仅保留的下一步。
2. 阅读本文件，了解目录职责和代码调用层级。
3. 按运行目标阅读 `README.md`：PyCharm/Web、CLI、Worker 或容器部署。
4. 只在任务相关时阅读 `docs/` 专题并进入对应源码目录，避免无目的扫描整个项目。

日常开发默认从项目根目录执行命令。用户通过 PyCharm 启动 `web_main.py`，不要由自动化工具擅自占用本机 8080 端口。

## 2. 根目录规则

根目录只保留项目入口、项目治理文件和一级职责目录。新的临时脚本、截图、日报或 Docker 变体不得直接堆在根目录，应分别进入 `scripts/`、`docs/assets/`、`docs/` 或 `deploy/`。

### 2.1 根目录文件

| 路径 | 职责 | 是否应留在根目录 |
| --- | --- | --- |
| `.dockerignore` | 控制 Docker 构建上下文，必须位于构建上下文根目录 | 是 |
| `.gitignore` | 排除本机配置、运行数据、缓存和生成物 | 是 |
| `AGENTS.md` | 对后续开发者/代理生效的工作约束 | 是 |
| `PROJECT_PLAN.md` | 唯一的规划、进度、当前工作和测试基线入口 | 是 |
| `PROJECT_STRUCTURE.md` | 本文件，维护目录地图与层级变更规则 | 是 |
| `README.md` | 安装、启动、配置边界、功能和 API 使用说明 | 是 |
| `pyproject.toml` | Python 包、依赖、命令行入口和构建元数据 | 是 |
| `main.py` | 兼容 CLI 启动器，转发到 `auto_test.pipeline.runner` | 是 |
| `web_main.py` | PyCharm 兼容 Web 启动器，转发到 `auto_test.web` | 是 |

### 2.2 一级目录

| 路径 | 类型 | 职责 |
| --- | --- | --- |
| `src/` | 源码 | 标准 src-layout Python 包，业务代码只能在这里扩展 |
| `tests/` | 测试 | 单元、集成、权限、安全、部署资产和静态页面回归 |
| `config/` | 配置 | 脱敏模板与本机配置；`config/config.local.yml` 不提交 |
| `deploy/` | 部署 | Compose、生产变量模板、在线/离线镜像及 GPU burn 定义 |
| `scripts/` | 工具 | 离线依赖准备、受控部署辅助和文档生成工具 |
| `docs/` | 文档 | 架构、兼容、部署、验收、日报和历史资料 |
| `data/` | 运行数据 | SQLite 数据库；MySQL 模式下仍可作为开发/应急目录 |
| `runtime/` | 运行数据 | 浏览器上传暂存和运行期工作区 |
| `artifacts/` | 运行产物 | 任务日志归档、图表、DOCX/PDF 与压测产物 |
| `logs/` | 运行日志 | Web 等进程日志，不保存源码 |
| `instance/` | 本机密钥 | 仅本地开发的密钥文件；生产必须外部注入 |
| `backups/` | 备份 | 本地迁移/恢复工具产生的备份，不得混入源码 |
| `deliverables/` | 生成物 | 方案文档及发布生成物；部署归档同步核验后可清理，本机只保留轻量文档、校验和清理记录 |
| `output/` | 生成物 | 临时导出结果，保留用于兼容既有工具 |
| `tmp/` | 临时文件 | 可重建的临时工作文件 |
| `vendor/` | 离线依赖 | 按需恢复的 Linux wheel、字体和 EvalScope 构建依赖；默认不提交，发布后可清空本地副本 |
| `.qa/` | 质量检查与隔离运行时 | DOCX/PDF 渲染等 QA 产物，以及本机正在使用的 EvalScope 环境；不能作为普通缓存整体删除 |

`__pycache__/`、`.pytest_cache/` 和 `src/auto_test.egg-info/` 都是可重新生成的缓存或安装元数据，不属于项目结构；看到它们时不要在其中维护源码。

## 3. 源码层级

`src/auto_test/` 是唯一正式源码包：

```text
src/auto_test/
├── common/         配置、日志、运行路径和通用函数
├── core/           业务自动化服务、任务管理和调度
├── evaluation/     模型评测领域、持久化、隔离后端和结果映射
├── integrations/   目标 HTTP API、SSH 等外部系统适配
├── pipeline/       上传、解析、翻译、断言、导出等流水线阶段
├── monitoring/     主机指标、CPU/GPU 压测和能力探测
├── reporting/      图表、企业报告和性能报告生成
├── platform/       Web 平台仓储、权限、接口资产、队列和对象存储
├── static/         原生 HTML/CSS/JavaScript 页面资源
├── web.py          FastAPI 应用与 Web API 组合入口
└── worker.py       外部 Worker 入口
```

- `src/auto_test/common/` 不依赖具体业务流程，提供所有模块可复用的基础能力。
- `src/auto_test/core/` 负责用例级编排，不直接承载页面表现。
- `src/auto_test/evaluation/` 负责模型测试集/运行领域、SQLite/MySQL 持久化契约、EvalScope 隔离子进程、Mock 后端、停止语义和稳定结果映射；不得在主服务环境直接导入 EvalScope。
- `src/auto_test/evaluation/value_matching.py` 为新版翻译规则提供日期、带币种/单位的精确数量及批次等价匹配，保护片段仍逐字检查；`tests/test_model_evaluation_acceptance.py` 覆盖真实验收暴露的误判、采样、响应中断、恢复及可选 Redis 密码边界。
- `src/auto_test/evaluation/resources.py` 在 Worker 内对用户绑定的已保存服务器资产执行只读 CPU/内存/GPU 采样，按实测性能阶段时间窗关联；未绑定、凭据不可用或缺失指标均保留不可用原因，不填零。
- `src/auto_test/evaluation/full.py` 固化全部真实专项的测试集版本、完整用例与执行参数；`evaluation/backends/full.py` 串行执行组合任务并保留停止、失败和阶段证据，不引入另一套队列表。
- `src/auto_test/reporting/model_evaluation_full.py` 生成全量覆盖、规则、裁判、性能、资源和来源表，供 HTML/DOCX/PDF 共用，并导出带校验清单的证据 ZIP；`tests/test_model_evaluation_full.py` 覆盖组合执行、缺测、完整性及项目边界。
- `src/auto_test/integrations/` 隔离外部系统差异，不把公司服务器或凭据写死。
- `src/auto_test/pipeline/` 组织既有业务自动化阶段，必须保留兼容能力。
- `src/auto_test/monitoring/` 负责在线监控、预检和压力测试，监控与满载压测结论分离。
- `src/auto_test/reporting/` 只消费稳定结果/快照生成阅读型产物。
- `src/auto_test/reporting/model_assessment.py` 定义模型报告的环境、方法、判定依据和内置 AI 分析提示词；复用已启用报告模型，按证据指纹缓存、校验引用并保留分析溯源。GET 下载只读取分析缓存，显式 POST 生成分析；执行快照 `test_environment.json` 及按运行缓存的 `model_evaluation_analysis.json` 放在运行产物目录；测试位于 `tests/test_model_evaluation_report.py` 和 `tests/test_model_evaluation_mvp.py`。
- `src/auto_test/platform/` 提供任务、存储、权限、接口中心和队列等平台基础设施。
- `src/auto_test/static/` 与 `src/auto_test/web.py` 共同构成当前 Web 层。
- `src/auto_test/worker.py` 领取持久化任务；数据库是最终事实源，Redis 只做唤醒与短期心跳。

推荐依赖方向是“入口/Web → core、pipeline、monitoring、reporting → integrations/platform/common”。新增代码应放入职责最接近的模块，不能重新在根目录建立平铺业务模块。

## 4. 部署层级

所有容器构建定义统一放在 `deploy/docker/`，GPU burn 变体继续下沉一层，避免 Dockerfile 散落在根目录。

```text
deploy/
├── compose.yml                    本地/自包含 Web + Worker + Redis 基线
├── compose.external.yml           复用外部 MySQL、MinIO、Redis 的部署定义
├── env.production.example         不含真实地址和凭据的生产变量模板
└── docker/
    ├── Dockerfile                 在线依赖镜像
    ├── Dockerfile.offline         使用 vendor/ 的完全离线镜像
    ├── Dockerfile.evalscope-offline 在平台镜像上添加隔离 EvalScope 的离线镜像
    └── gpu-burn/
        ├── Dockerfile.runtime     封装已构建 gpu-burn 二进制
        ├── Dockerfile.universal   CUDA 11.8 通用架构构建
        └── Dockerfile.blackwell   CUDA 12.8 Blackwell 原生构建
```

从项目根目录执行：

```powershell
docker compose -f deploy/compose.yml up --build -d
docker build -f deploy/docker/Dockerfile.offline -t <不可变镜像版本> .
```

Compose 的构建上下文仍是项目根目录；移动部署文件时必须同步检查 `build.context`、Dockerfile 路径、部署文档和 `tests/test_deployment_assets.py`。

完整模型评测镜像先构建平台基础镜像，再使用 `Dockerfile.evalscope-offline`，通过 `LIEMA_BASE_IMAGE` 构建参数指定已验证的基础镜像。离线依赖位于被忽略的 `vendor/evalscope/`，包含固定依赖清单、wheel、来源/许可证元数据、SHA-256 清单和 NLTK 离线资源；EvalScope 安装到镜像内独立 `/opt/evalscope/`，主服务 Python 环境保持隔离。发布生成物保存在 `deliverables/`，服务器上的版本目录与回滚备份均属于运行环境，不放回源码目录。

`deploy/env.production.example` 跟随当前已发布镜像版本更新，真实地址和凭据保持空白。换机部署统一从 `docs/pycharm-lan-deployment-guide.md` 开始；现场额外的 `compose.site.yml` 与完整非敏感配置只保存在部署机器的受控位置，不能写入通用模板。交付包根目录提供 `DEPLOYMENT_GUIDE.md`，内容与该手册一致；文档修订后同步更新源码包、ZIP 内外校验清单，应用镜像 ID 保持原发布记录。

本机不长期保留已用完的部署归档与解包依赖。2026-09-09 已在确认服务器交付 ZIP 哈希正确后清理本地大文件和发布临时文件，保留发布清单与验收证据；`vendor/` 当前为空。再次离线部署或构建前，从发布记录登记的服务器版本目录取回并校验对应归档。清理范围不包含开发虚拟环境、`.qa/` 内被本机配置引用的 EvalScope 运行时、密钥或运行数据。

## 5. 工具、文档与测试

- `scripts/prepare_offline_bundle.py`：准备并校验离线依赖包。
- `scripts/model_evaluation_poc.py`：使用本机假 OpenAI 服务重复验证隔离 EvalScope 的标准评测、WMT24++ 与性能压测入口。
- `scripts/upload_production_env.py`：在内存中组装生产变量并通过 SFTP 创建受限环境文件。
- `scripts/documentation/`：一次性或可复用的文档生成工具；当前 `scripts/documentation/build_future_plan.py` 输出到 `deliverables/`。
- `docs/assets/`：文档引用的图片等静态附件。
- `docs/archive/`：已被当前文档替代、但仍需追溯的历史资料。
- `tests/bootstrap.py`：测试环境导入与公共引导。
- `tests/test_*.py`：按功能域组织的自动化回归。
- `tests/test_project_structure.py`：防止根目录重新堆放未说明文件，并检查源码/部署层级是否已登记在本指引。
- `tests/test_workspace_pagination.py`：项目范围历史分页、稳定排序/末页/历史权限及 JavaScript 页码和过期响应行为回归；不依赖正式数据库。
- `docs/ui-layout-pagination-audit-20260909.md`：全站 24 个页面/子页签及附加弹窗的对齐、分页和多分辨率验收清单。
- `docs/model-real-acceptance-20260909.md`：用户指定模型的首轮真实验收，记录接口结果、翻译规则误判、实际遗漏与尚未执行的范围；对应轻量证据位于 `deliverables/qwen-real-acceptance-20260909/`，不属于部署归档。
- `docs/model-real-acceptance-final-20260909.md`：评分修正后的真实业务、独立裁判、并发/容量、恢复及资源补充验收；完整轻量证据在 `deliverables/qwen-real-acceptance-final-20260909/`。`deliverables/liema-auto-2026.09.09.7/` 仅保存本轮发布校验清单和清理记录，大型镜像/依赖包保留在服务器。

列表分页 UI 继续统一维护在 `src/auto_test/static/app.js`、`index.html` 和 `styles.css`；通用历史分页由既有 `platform/store.py` 提供，SQLite/MySQL 共用查询流程，`platform/api.py` 负责当前项目和历史数据权限。未增加新的源码层级或启动入口。

全局外观由 `static/theme.js` 在样式加载前恢复浏览器主题选择，`static/theme.css` 提供浅色语义颜色、登录页适配和主题按钮。`styles.css` 中的主题变量保留原有深色值作为回退，所有组件（包括高优先级状态规则）使用同一套颜色边界；图表由 `app.js` 在主题变化时重绘。`tests/test_theme.py` 覆盖首屏偏好、切换/刷新/跨标签页同步、存储不可用回退、颜色令牌完整性和浅色状态对比度。

- `docs/model-full-acceptance-20260909.md`：全量按钮、21 组指标、真实组合执行与报告验收；报告和轻量证据在 `deliverables/qwen-full-acceptance-20260909/`，最新发布清单在 `deliverables/liema-auto-2026.09.09.10/`。

## 6. 运行数据流

```text
浏览器上传
  → runtime/uploads/
  → core / pipeline 执行
  → data/ 或 MySQL 保存任务事实
  → artifacts/ 或 MinIO 保存日志、图表和报告
  → logs/ 保存服务日志
```

不要把 `data/`、`runtime/`、`artifacts/`、`logs/`、`instance/`、`vendor/` 或 QA/导出目录当成源码。迁移、恢复或清理这些目录前，必须先确认目标环境和数据归属，且不得修改业务原始数据库。

## 7. 层级变更维护清单

每次代码改动涉及层级变化时，在结束前逐项完成：

1. 更新本文件中的根目录表、源码树或部署树；不存在“稍后再补文档”的例外。
2. 更新 `README.md` 中的启动命令、路径和入口；部署变化还要更新 `docs/deployment-backup-rollback-runbook.md`。
3. 更新受路径影响的脚本、配置和测试，至少运行 `tests/test_project_structure.py` 以及相关功能测试。
4. 按 `AGENTS.md` 要求更新 `PROJECT_PLAN.md` 的日期、当前工作、能力进度和测试数量。
5. 新文件优先放入已有职责目录；确需新增一级目录时，先在本文件说明边界，避免与现有目录重叠。

部署环境模板和两套 Compose 增加可选 `LIEMA_MODEL_CA_BUNDLE`，通过现有 instance 卷保存经核验的 PEM；不增加服务、卷或凭据。换机方法见部署手册第 10 节。

测试集详情继续位于既有 `static/app.js`、`index.html`、`styles.css`；复用 `platform/api.py` 的版本读取接口，以 `page/page_size` 和 `evaluation/persistence.py` 的有序 LIMIT/OFFSET 分页，仓储契约同步 `platform/contracts.py`。回归复用 `tests/test_model_evaluation_advanced.py`、`tests/test_workspace_pagination.py`，没有新增源码层级或启动入口。

全站指定页跳转与每页条数复用上述静态文件；普通目录、审计、模型报告章节和详情抽屉共享分页控件与滚动区域，未新增源码层级。回归仍在 `tests/test_workspace_pagination.py`、`tests/test_static_ui.py`。`deploy/env.production.example` 与换机手册同步 `2026.09.10.3`；本机 `deliverables/liema-auto-2026.09.10.3/` 仅保留清单和验收证据，大包保留服务器。
