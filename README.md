# 烈马自动化测试平台

> 新会话或接手开发时，请先阅读 [`PROJECT_PLAN.md`](PROJECT_PLAN.md)，再阅读 [`PROJECT_STRUCTURE.md`](PROJECT_STRUCTURE.md)。前者维护统一规划与进度，后者说明目录职责、启动入口和层级变更规则。

该项目用于批量执行蓝鲨平台的文件上传、解析、翻译、中文断言、摘要校验、AI 接口检查、原文/译文导出、服务器性能采集、服务器压测和企业测试报告生成。命令行与 Web 页面共用同一套 AutomationService 主流程。

## 交付与部署文档

- [`docs/product-requirements-document.md`](docs/product-requirements-document.md)：产品定位、功能规划、当前实现、技术栈、未完成范围和后续路线。
- [`docs/delivery-acceptance.md`](docs/delivery-acceptance.md)：交付范围、角色权限、功能验收、安全边界、已知限制和验收记录模板。
- [`docs/pycharm-lan-deployment-guide.md`](docs/pycharm-lan-deployment-guide.md)：从 Windows/PyCharm 构建 Linux 镜像、制作无敏感信息的交付包，并部署到一套新局域网环境的逐步操作手册。
- [`docs/deployment-backup-rollback-runbook.md`](docs/deployment-backup-rollback-runbook.md)：正式部署前置信息、Redis 认证、MySQL/MinIO/持久卷备份、升级、恢复与回滚步骤。
- [`docs/disaster-recovery-rehearsal-20260824.md`](docs/disaster-recovery-rehearsal-20260824.md)：2026-08-24 平台专用数据隔离恢复、MinIO 完整性校验和镜像回滚实操记录。

`deploy/compose.yml` 是 Web、Worker、Redis 和持久卷的本地/自包含部署基线。正式服务器复用已有 MySQL、MinIO 和 Redis 时使用 `deploy/compose.external.yml` 与脱敏模板 `deploy/env.production.example`，该配置只创建平台 Web、Worker 和独立命名的持久卷，不定义第二套 Redis。上线前仍需配置访问控制、生产级 Redis 认证、外部密钥注入，并完成一次备份恢复和升级回滚演练。

构建服务器无法可靠访问软件源时，可先在受控联网环境运行 `python scripts/prepare_offline_bundle.py --output vendor`，核验 `vendor/SHA256SUMS` 后把依赖包送入部署环境，再使用 `DOCKER_BUILDKIT=0 docker build --network=none -f deploy/docker/Dockerfile.offline -t <不可变镜像版本> .` 构建。`vendor/` 默认不提交 Git，构建前仍需确保官方 `python:3.12-slim` 基础镜像已可信加载。

## 本地启动页面

建议使用 Python 3.11 或更高版本：

~~~powershell
python -m pip install -e .
Copy-Item config/config.example.yml config/config.local.yml
python web_main.py
~~~

启动窗口会显示：

~~~text
烈马自动化测试平台：http://127.0.0.1:8080
首次打开需创建平台管理员；后续使用账号密码登录。
~~~

浏览器打开 http://127.0.0.1:8080。首次启动时，页面会要求创建首位平台管理员和初始项目；系统不会生成默认账号或默认明文密码。后续登录使用 HttpOnly 会话 Cookie，写操作同时校验 CSRF 令牌。

直接运行 python web_main.py 时，程序会在 instance/local_web_secrets.json 中生成并复用本机开发用主密钥，用于加密模型 Key 和排队任务 SSH 凭据。正式部署应由密钥管理系统提供：

~~~powershell
$env:LIEMA_ENV = "production"
$env:LIEMA_MASTER_KEY = "用于加密模型 Key 的高强度主密钥"
$env:LIEMA_UPLOAD_MAX_FILES = "20000"
$env:LIEMA_UPLOAD_LIMIT_GB = "20"
uvicorn auto_test.web:app --host 0.0.0.0 --port 8080 --workers 1
~~~

启用外部队列后，业务自动化、接口场景和企业报告由独立 Worker 执行；Web 暂时仍保持 `--workers 1`，因为服务器 SSH 长连接与压测会话尚未迁出 Web 进程。

### Redis 与独立 Worker

Redis 不保存任务最终状态，只负责跨进程唤醒；任务、日志和结果仍写入 SQLite/MySQL。Redis 暂时不可用时，Worker 会继续按间隔轮询数据库，因此不会因为唤醒信号丢失而丢任务。

PyCharm 本地单进程默认无需 Redis，使用：

~~~yaml
task_queue:
  backend: local
  execution_mode: embedded
~~~

需要让耗时任务离开 Web 进程时，先安装依赖并启动 Redis，然后把本机配置改为：

~~~yaml
task_queue:
  backend: redis
  execution_mode: external
  redis_url: redis://127.0.0.1:6379/0
  namespace: liema
  connect_timeout: 1
  interface_concurrency: 5
~~~

如果运行环境分别提供 `SERVICE_REDIS_IP`、`SERVICE_REDIS_PORT`、`SERVICE_REDIS_PASSWORD`，平台会直接读取这些变量，密码不会拼入 URL、YAML 或日志。也可以使用应用专属的 `LIEMA_REDIS_HOST`、`LIEMA_REDIS_PORT`、`LIEMA_REDIS_USERNAME`、`LIEMA_REDIS_PASSWORD` 覆盖。启用认证的 Redis 6+ 若关闭了 `default` 用户，还必须同时提供对应 ACL 用户名。服务端拒绝凭据时平台会明确报告 Redis 不可用，不会静默退回匿名连接。

分别启动页面与 Worker：

~~~powershell
python web_main.py
python -m auto_test.worker
~~~

也可以在设置好 `LIEMA_MASTER_KEY` 后使用 `docker compose -f deploy/compose.yml up --build -d` 启动 Web、Worker 和 Redis。生产环境应通过环境变量或密钥管理系统提供 Redis/MySQL/MinIO 凭据，不要将密码写入 Compose 文件。

启动后访问 `/health`：`task_queue.available=true` 表示 Redis 可用，外部模式下 `task_queue.worker_available=true` 表示 Worker 最近 20 秒内仍有心跳。升级前先备份正式 MySQL、MinIO 和持久卷，再给镜像设置不可变版本号，例如 `LIEMA_IMAGE=liema-auto:2026.08.20`，依次更新 Redis、Worker 和 Web 并核验健康状态。需要回滚时恢复上一镜像版本并执行 `docker compose -f deploy/compose.yml up -d --no-build`；如果升级包含不可逆数据迁移，应同时按备份方案回滚专用平台数据库，绝不能操作业务原始数据库。

Redis 主要改善长任务与 Web 进程争用、跨实例唤醒和任务调度延迟；它不会自动消除远程 MySQL/MinIO 的网络延迟。首页已改为只加载当前视图需要的数据，模型、接口、服务器和权限模块在进入对应页面后再请求。

除 `/health`、页面静态资源及初始化/登录接口外，`/api`、`/docs` 和 OpenAPI 文档均要求登录。受控局域网通过 HTTP 直连时设置 `LIEMA_SESSION_COOKIE_SECURE=false` 并限制允许访问的网段；如果入口切换为 HTTPS，则改为 `true`。无论哪种方式，生产环境都必须通过密钥管理系统提供 `LIEMA_MASTER_KEY`。

## 页面能力

- 运行总览：最近任务、成功率、当前阶段和真实执行结果。
- 发起测试：直接填写本次业务账号，选择服务器资产或手动授权 SSH，上传文件夹并配置案件、语种和分析能力；密码不会进入任务记录。
- 实时监控：增量日志、阶段进度、CPU/GPU/内存实时曲线。
- 服务器压测：独立执行 CPU/GPU/监控型压测，采集 CPU、GPU、内存和温度曲线，并导出压测报告。
- 报告中心：固定六章报告模板、段落自定义、规则/模型结论、DOCX/PDF 下载。
- 模型配置：页面配置名称、Base URL、模型名和 Key；Key 加密后保存，接口不会返回明文。
- 接口中心：导入 JSON、YAML、OpenAPI 或 cURL，形成不可变版本；发布后热更新协议、主机、端口、方法和 path。
- 用户与权限：管理平台用户、项目空间、项目成员角色和审计记录；内置平台管理员、项目管理员、测试执行者和只读成员边界。

接口中心不会执行粘贴内容中的脚本。请求字段、认证语义和业务响应判定仍由代码中的逻辑接口约束，避免“自动适配”变成任意代码执行。

## 配置边界

`config/config.local.yml` 只保存本机环境参数和非敏感运行默认值，并已被 Git 忽略；`config/config.example.yml` 是可提交的脱敏模板。业务账号、可选业务管理员授权和 SSH 密码均由页面按本次任务提交，不再进入 YAML。

页面可选择“服务器资产”复用由 `LIEMA_MASTER_KEY` 加密保存的 SSH 凭据，也可提交一次性密码。业务账号、一次性 SSH 和可选管理员密码使用独立任务凭据表加密保存，执行器领取后立即删除；任务参数、列表、日志和历史记录均不返回明文。账号不存在且需要自动创建时，必须在本次任务中同时填写业务管理员账号和密码。

`defaults.translate_name` 配置 Web、自定义账号和 API 任务的默认翻译语种；任务未显式提交语种时使用该值，最终仍为空则在入队前拒绝请求。

目标业务 API 默认为内网直连，不继承 PyCharm 或系统的 HTTP 代理，避免内网请求被转发到本机代理后超时。只有目标明确要求经过系统代理时，才在 `api.use_system_proxy` 中显式启用。

安装后的运行数据目录默认为启动命令时的当前目录，也可以通过 `LIEMA_HOME` 指定固定目录。

模型配置不再把 Key 写入 yml。模型信息保存在 data/platform.db，Key 使用 LIEMA_MASTER_KEY 派生密钥加密。页面列表只返回是否已配置 Key。

## 本地目录布局

源码采用标准 `src` 包布局：

完整的根目录、部署目录、脚本目录、运行数据目录和源码分层说明统一维护在 [`PROJECT_STRUCTURE.md`](PROJECT_STRUCTURE.md)。只要新增、删除、移动或重命名目录/模块，就必须在同一次改动中更新该文件。

- src/auto_test/common/：配置、日志、路径和通用函数。
- src/auto_test/core/：任务调度和自动化服务。
- src/auto_test/integrations/：HTTP API 与 SSH 集成。
- src/auto_test/pipeline/：上传、解析、验证、导出等测试阶段。
- src/auto_test/monitoring/：性能监控、服务器压测和日志分析。
- src/auto_test/reporting/：图表、汇总报告和企业报告。
- src/auto_test/platform/：Web API、模型配置、数据库仓储接口和产物存储接口。
- src/auto_test/static/：Web 前端静态资源。

运行数据按职责分开保存：

- data/：SQLite 持久化数据，包括 tasks.db 和 platform.db。
- runtime/uploads/：页面上传的临时输入文件。
- artifacts/runs/：按时间戳保存的自动化测试日志、图表和报告。
- artifacts/server_stress/：独立服务器压测产物。
- logs/web/：Web 服务自身的标准输出和错误日志。
- instance/：仅供本地开发使用的密钥文件；生产环境必须改用环境变量或密钥管理系统。

这些目录都不会提交到 Git。程序启动时会自动迁移旧版 logs/ 下的数据库、上传文件、测试产物、压测产物及本地密钥；迁移不会覆盖已存在的目标文件。

## 企业化存储建议

本地版本默认使用 SQLite 与本地文件，适合单机部署和量产测试现场快速落地。平台已提供MySQL、MinIO适配器及dry-run迁移/回滚工具；切换前应先在测试环境核验连接、数据量、报告下载和对象数量。企业上线时建议按数据类型拆分：

- MySQL：保存用户、角色、任务状态、模型配置元数据、接口版本、报告任务和审计日志。若部署环境已有 MySQL，可通过迁移脚本复用现有实例，新建独立库或独立 schema，避免污染业务库。
- MinIO：保存上传输入快照、运行日志归档、图表、DOCX/PDF、压测报告和大文件产物。数据库只保存对象 key、hash、大小和版本。
- SQLite：保留为本地开发和单机应急模式，不建议承担多人协作和长期归档。

## 报告可靠性

报告生成采用独立持久化任务：

- 运行数据先生成不可变 report_snapshot.json。
- 六章模板按版本保存，历史报告使用提交时的模板版本。
- 相同运行、模板和参数具备幂等键，重复提交不会生成重复任务。
- 失败自动重试三次；服务重启会恢复未完成任务。
- 同时生成 DOCX 与 PDF，并记录各自产物的 SHA-256。
- 模型不可用时，综合结论自动降级为规则结论，不阻断报告。

默认章节：

1. 测试背景与概述
2. 测试环境
3. 测试方法与工具
4. 核心测试用例
5. 测试结果汇总统计
6. 综合测试结论

## 命令行运行

~~~powershell
python main.py
~~~

命令行不再读取 YAML 账号。交互运行会安全提示输入本次业务账号和密码；非交互运行可使用 `LIEMA_RUN_USERNAME`、`LIEMA_RUN_PASSWORD`、`LIEMA_RUN_HOST`、`LIEMA_RUN_UPLOAD_PATH` 等一次性环境变量。

## 主要 API

`/health` 保持匿名可用，供部署探针检查；其余业务 API 使用登录会话、项目上下文与角色权限控制。前端通过 `X-Project-ID` 选择当前项目，并在写操作中提交 `X-CSRF-Token`。新任务、报告和压测记录会写入项目归属，列表与详情按当前项目隔离；历史无项目标记的数据仅在初始项目向平台管理员兼容展示，避免新项目总览混入旧记录。

- GET /api/auth/status：查询初始化或登录状态。
- POST /api/auth/setup：仅在平台没有用户时创建首位管理员和初始项目。
- POST /api/auth/login、POST /api/auth/logout：建立或销毁登录会话。
- GET/POST/PATCH /api/identity/users：平台用户管理。
- GET/POST/PATCH /api/identity/projects：项目空间管理。
- GET/PUT /api/identity/projects/{project_id}/members：项目成员角色管理。
- GET /api/identity/audit-events：按 `page` / `page_size` 分页查询安全与操作审计记录。

- POST /api/uploads：上传页面测试文件。
- POST /api/run：创建持久化测试任务。
- GET /api/runs：查询任务历史。
- GET /api/runs/{run_id}：读取状态、阶段和进度。
- GET /api/runs/{run_id}/logs：增量读取日志。
- GET /api/runs/{run_id}/metrics：增量读取性能采样。
- GET/POST /api/model-profiles：模型配置。
- GET/PUT /api/report-template：六章报告模板。
- POST /api/runs/{run_id}/reports：创建报告任务。
- GET /api/reports/{job_id}/download/{docx|pdf}：下载报告。
- POST /api/interface-specs/import：导入并版本化接口定义。
- GET/POST /api/stress-jobs：查询或创建服务器压测任务。
- POST /api/stress-jobs/{job_id}/stop：取消排队任务或安全停止运行中的压测。
- POST /api/server-capabilities/probe：只读探测服务器兼容性，并返回可执行、降级或阻断结论。
- GET /api/stress-jobs/{job_id}/metrics：读取压测性能采样。
- GET /api/stress-jobs/{job_id}/download：下载压测报告。

业务自动化与压测任务的密码均不会写入任务参数或查询接口。需要密码的排队任务使用
`LIEMA_MASTER_KEY` 加密保存，服务重启后仍可继续调度；任务领取、取消或结束时会清理对应密文。

## 项目入口

- `main.py`：兼容的命令行入口，实际调用 `auto_test.pipeline.runner`。
- `web_main.py`：兼容的 Web 入口，实际加载 `auto_test.web`。
- `deploy/`：Compose、生产环境变量模板、在线/离线镜像与 GPU burn 镜像定义。
- `PROJECT_STRUCTURE.md`：接手导航、目录职责和层级变更维护规则。
- 安装项目后也可以使用 `auto-test` 和 `auto-test-web` 命令。
