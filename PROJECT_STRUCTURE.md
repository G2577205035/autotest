# 烈马自动化测试平台：项目结构与接手指引

> 最后更新：2026-09-01  
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
| `deliverables/` | 生成物 | 历史方案文档及其渲染/校验中间产物 |
| `output/` | 生成物 | 临时导出结果，保留用于兼容既有工具 |
| `tmp/` | 临时文件 | 可重建的临时工作文件 |
| `vendor/` | 离线依赖 | 经哈希核验的 Linux wheel 和字体包，默认不提交 |
| `.qa/` | 质量检查 | DOCX/PDF 渲染、可访问性等临时 QA 产物 |

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
- `src/auto_test/integrations/` 隔离外部系统差异，不把公司服务器或凭据写死。
- `src/auto_test/pipeline/` 组织既有业务自动化阶段，必须保留兼容能力。
- `src/auto_test/monitoring/` 负责在线监控、预检和压力测试，监控与满载压测结论分离。
- `src/auto_test/reporting/` 只消费稳定结果/快照生成阅读型产物。
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
