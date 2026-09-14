# 烈马自动化测试平台部署、备份与回滚手册

> 文档版本：1.6
> 基线日期：2026-09-11
> 当前应用发布：`2026.09.11.2`，应用回退版本 `2026.09.11.1`；保留原主密钥、端口和持久卷。本次没有数据库结构变更，保留上一版本加密 SQL 快照，并新增本次切换前的环境/Compose 配置副本。完整换机步骤见 `pycharm-lan-deployment-guide.md`，版本校验见发布记录。
> 适用范围：正式服务器首次部署、版本升级、备份恢复与故障回退  
> 安全原则：只操作平台专用数据库、对象空间和持久卷；不得修改业务原始数据库，不在命令、文档或日志中写入明文凭据。

## 1. 当前部署边界

2026-09-11 已按用户授权发布 `2026.09.11.2`（静态 `20260911.rec4`），同步 Web、UI Worker 与录制镜像。切换前任务、录制及 SSH 会话均为空；回退配置位于 `/home/liema/rollback/2026.09.11.2`。窗口布局对新录制生效，旧套件保留原视口。具体范围与验收见 `ui-server-deployment.md`。

2026-09-11 已发布 UI 录制与回放，新增 `deploy/compose.ui.yml`、受限代理与带 Docker CLI 的 UI Worker；具体步骤及实测记录见 [UI 服务器部署手册](ui-server-deployment.md)。旧 Web/普通 Worker 的数据卷、主密钥与外部依赖保留；平台库加密快照已完成隔离恢复，新版本初始化后 45 张原表记录数一致。SSO/Vault 和独立 Server Worker 保持未启用。

以下为 2026-09-10 的发布前历史说明：新增 `server_worker.py`、`ui_worker.py` 两个可选独立入口，以及 `deploy/Dockerfile.ui-runner` / `deploy/ui-runner-launcher.cjs` 测试镜像定义。现有 Compose、现场变量和服务拓扑未切换。UI 镜像尚未构建；SSO/Vault、UI Runner 与外部 Server Worker 的配置、恢复和外部待验范围见 [本地开发功能说明](development-features-20260910.md)。新增表仅属于平台库，后续发布前须覆盖平台库与对象存储备份/恢复；本轮不执行这些部署操作。

**新机器首次部署或迁移旧平台，先按 [全新机器部署与换机手册](pycharm-lan-deployment-guide.md) 执行。** 该手册提供当前完整交付包的逐步命令、环境变量说明和切换顺序；本文件侧重备份、恢复与回滚规则。应用 ZIP 不包含 MySQL、MinIO、Redis 或其生产数据，旧平台迁移还必须单独安全转移主密钥、数据和五个应用卷。

`deploy/compose.yml` 是 Web、Worker、Redis 和持久卷的本地/自包含部署基线；复用外部 MySQL、MinIO 和 Redis 时使用 `deploy/compose.external.yml`。两者都不是未经配置即可上线的生产方案：

- Web 使用单进程 `uvicorn --workers 1`，因为 SSH 长连接和压测会话仍在 Web 进程内。
- Web 默认只绑定到 `127.0.0.1:8080`；明确采用受控局域网 HTTP 时可绑定指定内网地址，并设置 `LIEMA_SESSION_COOKIE_SECURE=false`。HTTPS 入口必须改为 `true`。
- Compose 内置 Redis 可用于自包含部署，也可通过 `deploy/compose.external.yml` 复用现场 Redis；密码是否填写由服务端配置决定。
- MySQL、MinIO 和 Redis 的真实凭据必须由环境变量、Docker Secret 或企业密钥管理系统注入。
- `LIEMA_MASTER_KEY` 必须稳定保存。丢失或变更会导致历史模型 Key、服务器凭据和未领取任务凭据无法解密。

Redis 只承载唤醒信号和短期 Worker 心跳；MySQL/SQLite 才是任务状态的最终事实源。因此 Redis 数据丢失会影响即时唤醒，但不应造成持久化任务丢失。

2026-09-09 用户确认 Redis 密码长期保持可选：有密码时填写，无密码时留空使用免密连接。平台使用唯一 `LIEMA_REDIS_NAMESPACE`，只写自己的前缀，不修改共享 Redis 配置或其他键；错误密码不会静默退回免密，不再另列强制认证改造待办。

## 2. 开始部署前需要提供的信息

以下信息齐全后才能执行正式服务器部署：

| 类别 | 必需信息 |
| --- | --- |
| 服务器 | OS/版本、SSH 地址与端口、登录用户、是否具备受控 sudo、CPU/内存/磁盘 |
| 容器 | Docker Engine 与 Compose 插件版本，或是否允许安装 |
| 网络 | 访问域名、HTTPS 证书方案、反向代理、允许访问的网段、目标业务/SSH 出站范围 |
| MySQL | 平台专用 host、port、database/schema、user、凭据注入方式、备份目录和保留策略 |
| MinIO | endpoint、HTTPS、平台专用 bucket/prefix、access key 注入方式、备份目标 |
| Redis | 独立服务或随平台部署、TLS、ACL username、password 注入方式、网络访问边界 |
| 密钥 | `LIEMA_MASTER_KEY` 的生成、保管、注入、备份和轮换责任人 |
| 运维 | 上线窗口、允许中断时长、RPO、RTO、回滚决策人和验收人 |

不要通过聊天或代码仓库发送真实密码。优先由服务器侧密钥管理、受限环境文件或 Docker Secret 提供。

## 3. 生产环境前置检查

### 3.1 系统检查

在部署服务器执行只读检查：

```bash
uname -a
df -h
free -h
docker version
docker compose version
```

要求：

- 磁盘空间可覆盖当前数据、一次完整备份、一次升级镜像和恢复临时空间。
- Docker 数据目录位于受监控、可备份的磁盘。
- 系统时间同步正常。
- 8080 不直接暴露公网；如果本机已有服务占用 8080，先调整反向代理或 Compose 映射，不强占端口。

### 3.2 存储检查

- MySQL 使用平台专用库，账号只拥有该库所需的建表、读写和索引权限。
- MinIO bucket 由运维预建，或明确批准平台首次创建；生产环境不与业务原始对象混用 prefix。
- Redis 限制在部署网络内；有密码时按现场配置认证，无密码时留空；应用凭据失败时不得退回匿名连接。
- 备份目标与生产数据不在同一故障域，至少有一份副本可离机恢复。

### 3.3 密钥检查

至少准备以下变量，真实值不写入仓库：

```text
LIEMA_ENV=production
LIEMA_SESSION_COOKIE_SECURE=false
LIEMA_MASTER_KEY=<由密钥管理系统注入>
LIEMA_DATABASE_BACKEND=mysql
LIEMA_MYSQL_HOST=<平台数据库地址>
LIEMA_MYSQL_PORT=<端口>
LIEMA_MYSQL_DATABASE=<平台专用库>
LIEMA_MYSQL_USER=<平台专用用户>
LIEMA_MYSQL_PASSWORD=<由密钥管理系统注入>
LIEMA_ARTIFACT_BACKEND=minio
LIEMA_MINIO_ENDPOINT=<对象存储地址>
LIEMA_MINIO_ACCESS_KEY=<由密钥管理系统注入>
LIEMA_MINIO_SECRET_KEY=<由密钥管理系统注入>
LIEMA_MINIO_BUCKET=<平台专用bucket>
LIEMA_MINIO_SECURE=true
LIEMA_TASK_QUEUE_BACKEND=redis
LIEMA_TASK_EXECUTION_MODE=external
LIEMA_REDIS_HOST=<Redis地址>
LIEMA_REDIS_PORT=<端口>
LIEMA_REDIS_USERNAME=<ACL用户，可选>
LIEMA_REDIS_PASSWORD=<由密钥管理系统注入>
```

如果使用平台提供的 `SERVICE_REDIS_IP`、`SERVICE_REDIS_PORT`、`SERVICE_REDIS_USERNAME`、`SERVICE_REDIS_PASSWORD`，无需把密码拼入 Redis URL。

## 4. 首次部署流程

### 4.1 构建不可变镜像

在受控构建环境使用明确版本号，不使用可漂移的 `latest`：

```bash
docker build --pull -f deploy/docker/Dockerfile -t liema-auto:2026.08.22 .
docker image inspect liema-auto:2026.08.22 --format '{{.Id}}'
```

记录镜像 ID 或仓库 digest，作为验收与回滚依据。

如果部署服务器无法可靠访问 PyPI 或 Debian 软件源，可在受控联网构建机准备离线包：

```bash
python scripts/prepare_offline_bundle.py --output vendor
cd vendor
sha256sum -c SHA256SUMS
cd ..
DOCKER_BUILDKIT=0 docker build \
  --network=none \
  -f deploy/docker/Dockerfile.offline \
  -t liema-auto:2026.08.22 .
```

离线构建仍要求先可信加载官方 `python:3.12-slim` 基础镜像；传输归档和 `vendor/` 中每个依赖都应核验 SHA-256。`.env.production`、基础镜像归档和离线传输归档必须被 `.dockerignore` 排除，不能进入构建上下文。

完整模型评测版本还需解包并核验发布包中的 `vendor/evalscope/`，其中包含 `evalscope[perf]==1.11.1` 的固定传递依赖、官方来源与许可证元数据、构建后的 wheel、SHA-256 清单及 NLTK `punkt_tab` 离线资源。先用 `Dockerfile.offline` 构建平台基础镜像，再执行：

```bash
(cd vendor/evalscope && sha256sum -c SHA256SUMS)
docker build --network=none \
  --build-arg LIEMA_BASE_IMAGE=<已验证的平台基础镜像> \
  -f deploy/docker/Dockerfile.evalscope-offline \
  -t <完整发布镜像的不可变版本> .
```

EvalScope 使用 `/opt/evalscope/bin/python` 独立虚拟环境，镜像通过 `LIEMA_EVALSCOPE_PYTHON` 指定入口，并设置离线资源环境变量。发布前必须在无外网容器内通过本地假模型的标准评测、WMT BLEU 和性能压测 PoC；该验收不调用真实模型，也不证明真实模型质量或容量。开发环境 Python 3.11 与生产隔离 Python 3.12 的兼容性分别记录，不能相互代替。新镜像的子进程脚本从已安装 Python 包解析，不依赖部署目录存在源码树。

升级应使用独立的版本构建目录，并显式保留原 Compose 项目名、生产环境文件、Web 端口和持久卷。生产环境文件只保存在原受限位置；源码包和离线依赖包不得携带该文件或开发环境配置。

### 4.2 配置生产变量

从 `deploy/env.production.example` 复制生产环境文件，例如 `cp deploy/env.production.example .env.production`；成品仅保存在部署服务器或改用等价 Secret，权限建议为 `0600`。环境文件不得提交到 Git，也不得放入交付压缩包。

正式部署有两种方式：

1. 推荐：使用 `deploy/compose.external.yml` 连接运维提供的 Redis；有密码时注入密码和可选 ACL 用户名，无密码时留空；该文件不会创建 Redis 容器。
2. 随平台部署 Redis：按现场服务端配置决定是否启用认证；客户端环境文件与服务端保持一致。

空密码使用免密连接，ACL 用户名仅在密码非空时使用；配置了错误密码时报告连接不可用，不静默降级到免密。

### 4.3 部署前配置校验

```bash
docker compose --env-file .env.production -f deploy/compose.external.yml config --quiet
docker compose --env-file .env.production -f deploy/compose.external.yml config --services
```

不要把展开后的完整 `docker compose config` 输出保存到公开日志，因为其中可能包含已注入的敏感环境变量。

### 4.4 启动顺序

1. 核验外部 Redis 网络及按现场认证配置的连接可用性。
2. 启动 Worker，确认心跳建立。
3. 启动 Web，确认数据库与产物存储后端正确。
4. 最后启用反向代理流量。

示例：

```bash
docker compose --env-file .env.production -f deploy/compose.external.yml up -d --no-build worker
docker compose --env-file .env.production -f deploy/compose.external.yml up -d --no-build web
docker compose --env-file .env.production -f deploy/compose.external.yml ps
```

`deploy/compose.external.yml` 不包含 Redis 服务，因此不会新建、停止或替换现有 Redis 容器。

### 4.5 健康验收

在服务器本机执行：

```bash
curl --fail --silent http://127.0.0.1:8080/health
```

期望至少满足：

```json
{
  "status": "ok",
  "database_backend": "mysql",
  "artifact_backend": "minio",
  "task_queue_backend": "redis",
  "task_execution_mode": "external",
  "task_queue": {
    "available": true,
    "worker_available": true
  }
}
```

随后执行 `docs/delivery-acceptance.md` 的功能、权限和报告验收。首次管理员初始化只能由指定验收人员完成，不能创建通用默认账号。

## 5. 升级前冻结与盘点

升级前必须完成：

1. 暂停新任务提交或安排业务静默窗口。
2. 确认业务自动化、接口场景、企业报告、压测和模型评测五类任务没有处于排队/运行状态。
3. 记录当前镜像 tag、镜像 digest、Compose 文件版本和 `/health` 输出。
4. 记录当前 `LIEMA_MASTER_KEY` 的 Secret 版本，只记录标识，不记录密钥值。
5. 备份平台专用 MySQL、MinIO 和 Docker 持久卷。
6. 对备份执行校验，并把备份编号写入变更单。

如果存在运行中的 SSH 会话或压测任务，应先由用户安全停止；不要直接杀死业务容器或目标服务器进程。

## 6. 备份流程

### 6.1 MySQL 平台专用库

优先使用企业数据库备份平台。使用 `mysqldump` 时，通过受限客户端配置文件或 Secret 注入密码，不在命令行写 `-p明文密码`：

```bash
LIEMA_BACKUP_ID="YYYYMMDD-HHMMSS"
LIEMA_BACKUP_ROOT="/srv/liema-backups/${LIEMA_BACKUP_ID}"
LIEMA_PLATFORM_DB="请替换为平台专用数据库名"
mkdir -p "${LIEMA_BACKUP_ROOT}/mysql"
mysqldump \
  --defaults-extra-file=/run/secrets/liema-mysql-client.cnf \
  --single-transaction \
  --routines \
  --triggers \
  --events \
  --hex-blob \
  --set-gtid-purged=OFF \
  "${LIEMA_PLATFORM_DB}" \
  | gzip -c > "${LIEMA_BACKUP_ROOT}/mysql/platform.sql.gz"
cd "${LIEMA_BACKUP_ROOT}/mysql"
sha256sum platform.sql.gz > SHA256SUMS
gzip -t platform.sql.gz
sha256sum -c SHA256SUMS
```

备份账号和恢复账号应分离。恢复只能指向新建的空白验证库或已明确批准回退的平台专用库。

### 6.2 MinIO 平台专用 bucket/prefix

优先使用 MinIO 版本控制、对象锁或企业备份。使用 `mc` 时，alias 凭据由受限配置提供：

```bash
LIEMA_BACKUP_ID="YYYYMMDD-HHMMSS"
LIEMA_BACKUP_ROOT="/srv/liema-backups/${LIEMA_BACKUP_ID}"
LIEMA_MINIO_SOURCE="请替换为受限alias/平台bucket/平台prefix"
LIEMA_MINIO_PREFIX_NAME="请替换为平台prefix名称"
mkdir -p "${LIEMA_BACKUP_ROOT}/minio/${LIEMA_MINIO_PREFIX_NAME}"
mc mirror --overwrite --preserve \
  "${LIEMA_MINIO_SOURCE}" \
  "${LIEMA_BACKUP_ROOT}/minio/${LIEMA_MINIO_PREFIX_NAME}"
find "${LIEMA_BACKUP_ROOT}/minio" -type f ! -name SHA256SUMS -print0 \
  | sort -z \
  | xargs -0 sha256sum \
  > "${LIEMA_BACKUP_ROOT}/minio/SHA256SUMS"
```

记录对象数量和总字节数，并与恢复验证结果对比。不要对业务原始 bucket 执行覆盖或删除操作。

`mc mirror` 返回成功不等于备份完整。备份前必须使用应用实际采用的存储入口建立源对象数量和总字节数基线，备份后生成逐对象或逐文件 SHA-256 清单；如果 `mc`、容器内客户端和应用侧清单不一致，应立即停止验收，保留失败证据，改用企业备份工具或能够完整枚举的受控客户端。跨主机传输必须使用 SFTP/TLS 等加密通道，且恢复时仍需逐对象回读校验。

### 6.3 Docker 持久卷

先确定实际 Compose 项目名和卷名：

```bash
docker compose ls
LIEMA_COMPOSE_PROJECT="请替换为实际Compose项目名"
docker volume ls --filter "label=com.docker.compose.project=${LIEMA_COMPOSE_PROJECT}"
```

对每个实际卷先执行 `docker volume inspect <卷名>`，确认属于本平台。静默窗口内停止 Web/Worker 后，再使用企业卷快照或受控备份工具保存以下卷：

- `liema-data`
- `liema-runtime`
- `liema-artifacts`
- `liema-instance`
- `liema-logs`
- `redis-data`

正式环境以 MySQL/MinIO 为主时，本地 data/artifacts 仍可能包含缓存、暂存和迁移状态，不能因为使用外部存储就跳过卷备份。

上述 `redis-data` 只适用于确实由本平台自包含 Compose 创建的 Redis 卷；`compose.external.yml` 只声明五个应用卷。换机恢复必须逐卷核对实际 Compose 标签及映射，不得按示例名称覆盖既有业务卷。

### 6.4 主密钥与配置

- 备份 Secret 的版本标识、恢复权限和责任人，不把 `LIEMA_MASTER_KEY` 明文打入普通备份包。
- 保存脱敏的 Compose/反向代理配置、镜像 digest、数据库名、bucket/prefix 和 Redis ACL 用户名。
- 不保存数据库密码、MinIO secret key、Redis password 或 SSH 私钥到文档与 Git。

### 6.5 现有迁移工具的适用边界

仓库提供以下 dry-run 优先的开发/迁移工具：

```powershell
python -m auto_test.platform.migration backup --output backups\manual
python -m auto_test.platform.migration sqlite-to-mysql
python -m auto_test.platform.migration sqlite-to-mysql --execute
python -m auto_test.platform.migration local-to-minio
python -m auto_test.platform.migration local-to-minio --execute
python -m auto_test.platform.migration minio-to-local --output artifacts_restored --execute
python -m auto_test.platform.migration restore-sqlite --output backups\manual --overwrite --execute
```

这些命令用于 SQLite/本地存储迁移和开发应急，不替代正式 MySQL、MinIO 和卷级灾备。正式生产恢复必须使用本节 6.1～6.4 的原生备份，并在隔离环境验证。

## 7. 升级流程

1. 完成第 5、6 节并取得备份编号。
2. 构建或拉取新的不可变镜像，记录 digest。
3. 先验证配置，再更新 Worker，最后更新 Web。
4. 不并行运行不同数据库迁移语义的多个版本。
5. 通过 `/health`、任务领取、报告下载、权限隔离和审计后再恢复流量。

示例：

```bash
LIEMA_NEW_IMAGE="liema-auto:请替换为新版本"
LIEMA_IMAGE="${LIEMA_NEW_IMAGE}" docker compose --env-file .env.production -f deploy/compose.external.yml config --quiet
LIEMA_IMAGE="${LIEMA_NEW_IMAGE}" docker compose --env-file .env.production -f deploy/compose.external.yml up -d --no-build worker
LIEMA_IMAGE="${LIEMA_NEW_IMAGE}" docker compose --env-file .env.production -f deploy/compose.external.yml up -d --no-build web
docker compose --env-file .env.production -f deploy/compose.external.yml ps
curl --fail --silent http://127.0.0.1:8080/health
```

若数据库结构由应用启动自动补充，必须先在备份副本或测试库验证兼容性。未确认迁移可逆前，不删除旧列、旧表或旧对象。

## 8. 回滚触发条件

出现以下任一情况，应停止放量并评估回滚：

- Web 或 Worker 持续无法启动，且不是短暂依赖抖动。
- `/health` 后端类型错误、Redis/Worker 持续不可用。
- 新任务无法持久化、无法领取或出现重复领取。
- 登录、CSRF、项目隔离或脱敏边界回归失败。
- DOCX/PDF 无法生成、下载或对象引用失效。
- 数据库/对象迁移校验不一致。

## 9. 应用版本回滚

如果数据结构与旧版本兼容，只回滚镜像：

```bash
LIEMA_PREVIOUS_IMAGE="liema-auto:请替换为上一稳定版本"
LIEMA_IMAGE="${LIEMA_PREVIOUS_IMAGE}" docker compose --env-file .env.production -f deploy/compose.external.yml config --quiet
LIEMA_IMAGE="${LIEMA_PREVIOUS_IMAGE}" docker compose --env-file .env.production -f deploy/compose.external.yml up -d --no-build worker
LIEMA_IMAGE="${LIEMA_PREVIOUS_IMAGE}" docker compose --env-file .env.production -f deploy/compose.external.yml up -d --no-build web
curl --fail --silent http://127.0.0.1:8080/health
```

回滚后重新执行任务领取、报告下载、登录权限和项目隔离烟雾测试。

## 10. 数据恢复与灾难回退

数据恢复属于高风险操作，必须由回滚决策人确认备份编号、目标环境和恢复点。

### 10.1 恢复前

1. 停止 Web 和 Worker，阻止新写入。
2. 再次确认目标是平台专用库、bucket/prefix 和卷。
3. 优先恢复到新的验证库、验证 bucket 或临时卷，不直接覆盖现网。
4. 校验 MySQL 压缩包、MinIO 文件清单和卷快照。
5. 确认恢复使用与备份时相同的 `LIEMA_MASTER_KEY` Secret 版本。

### 10.2 MySQL 恢复验证

示例恢复到新建空白验证库：

```bash
LIEMA_BACKUP_ID="请替换为备份编号"
LIEMA_BACKUP_ROOT="/srv/liema-backups/${LIEMA_BACKUP_ID}"
LIEMA_RESTORE_DB="请替换为空白验证数据库名"
cd "${LIEMA_BACKUP_ROOT}/mysql"
sha256sum -c SHA256SUMS
gzip -t platform.sql.gz
gunzip -c platform.sql.gz \
  | mysql --defaults-extra-file=/run/secrets/liema-mysql-client.cnf \
      "${LIEMA_RESTORE_DB}"
```

验证表数量、关键表行数、用户/项目、任务、报告元数据和审计记录。只有验证通过并获得批准后，才切换应用连接或执行现网回退。

### 10.3 MinIO 恢复验证

先恢复到新的验证 prefix：

```bash
LIEMA_BACKUP_ID="请替换为备份编号"
LIEMA_BACKUP_ROOT="/srv/liema-backups/${LIEMA_BACKUP_ID}"
LIEMA_MINIO_PREFIX_NAME="请替换为平台prefix名称"
LIEMA_MINIO_VERIFY_TARGET="请替换为受限alias/验证bucket/验证prefix"
mc mirror --overwrite --preserve \
  "${LIEMA_BACKUP_ROOT}/minio/${LIEMA_MINIO_PREFIX_NAME}" \
  "${LIEMA_MINIO_VERIFY_TARGET}"
```

核对对象数量、总字节数，并实际打开至少一份 DOCX、一份 PDF 和一个任务目录。不要使用 `mc rm --recursive --force` 清空现网对象。

### 10.4 持久卷恢复

优先恢复到新卷并通过 Compose override 挂载验证。覆盖原卷前必须：

- 停止所有使用该卷的容器。
- 通过 `docker volume inspect` 核对精确卷名和 Compose 项目标签。
- 为当前卷再做一次可恢复快照。
- 获得回滚决策人批准。

恢复后启动顺序仍为 Redis/依赖 → Worker → Web → 反向代理流量。

## 11. 恢复验收

- [ ] `/health` 返回 `status=ok`，后端类型与环境一致。
- [ ] Redis 按现场密码或免密配置连接成功，Worker 心跳在线。
- [ ] 原用户可登录，角色与项目成员关系正确。
- [ ] 历史任务、报告和审计记录可查询。
- [ ] 随机抽取的 DOCX/PDF 与对象可读取。
- [ ] 新建只读验收场景可排队、领取、完成并生成报告。
- [ ] 跨项目访问仍返回 404，敏感字段仍脱敏。
- [ ] 备份前后的关键表计数、对象数和校验记录已归档。

实际演练记录见 `docs/disaster-recovery-rehearsal-20260824.md`。该次演练同时确认：MinIO 备份必须以应用侧源清单、备份清单和恢复回读结果三方一致为完成条件。

## 12. 变更与演练记录模板

| 字段 | 记录 |
| --- | --- |
| 变更/演练编号 |  |
| 日期与窗口 |  |
| 操作环境 |  |
| 操作人/复核人 |  |
| 原镜像 digest |  |
| 新镜像 digest |  |
| 备份编号 |  |
| MySQL 校验 |  |
| MinIO 对象数/字节数 |  |
| 持久卷快照 |  |
| Master Key Secret 版本标识 |  |
| `/health` 结果 |  |
| 功能验收结果 |  |
| 是否回滚 |  |
| 遗留问题 |  |

记录只保留标识、摘要和校验结果，不记录任何明文凭据。

## 2026-09-10.3 分页界面更新

本次版本更新统一列表跳页、每页条数与滚动区域，沿用原有 Compose、平台配置和数据卷，不需要数据库结构迁移。切换前确认所有任务终态与 SSH 会话空闲，启动后复核 Web 健康、Worker 心跳、匿名访问门禁和静态版本，再按换机手册执行分页验收。用户明确暂缓新增备份；离线软件包不包含生产配置、凭据或数据，软件包不能替代数据迁移。
