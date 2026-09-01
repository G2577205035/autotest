# 从 PyCharm 构建并部署到局域网

> 本文是 Windows/PyCharm 开发环境到 Linux 局域网部署的操作入口。完整的生产检查、备份、恢复和回滚要求以 [`deployment-backup-rollback-runbook.md`](deployment-backup-rollback-runbook.md) 为准。

## 1. 本地准备

在 PyCharm 项目根目录确认：

```powershell
..\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q
```

不得把 `config/config.local.yml`、`instance/`、`.env.production`、运行数据或任何真实凭据放入交付包。部署路径变化时先核对 [`../PROJECT_STRUCTURE.md`](../PROJECT_STRUCTURE.md) 中的部署层级。

## 2. 构建镜像

构建机可联网时，从项目根目录执行：

```powershell
docker build --pull -f deploy/docker/Dockerfile -t liema-auto:<不可变版本> .
```

需要离线构建时，先在受控联网环境准备并校验依赖：

```powershell
..\.venv\Scripts\python.exe scripts\prepare_offline_bundle.py --output vendor
docker build --network=none -f deploy/docker/Dockerfile.offline -t liema-auto:<不可变版本> .
```

在具备 `sha256sum` 的 Linux、WSL 或 Git Bash 环境中执行 `(cd vendor && sha256sum -c SHA256SUMS)`，确认清单内每个文件都通过后再构建。交付镜像归档、源码包或离线依赖包时，应另外生成 SHA-256，并在目标服务器核验后再加载。基础镜像、应用镜像和依赖包都应使用固定版本，不能用可漂移的 `latest` 代替验收版本。

## 3. 准备目标服务器

目标服务器至少需要：

- Docker Engine 与 Compose 插件；
- 平台专用 MySQL 数据库/账号；
- 平台专用 MinIO bucket 或严格隔离 prefix；
- 已限制网络访问且启用认证的 Redis；
- 稳定保存、不会随升级变化的 `LIEMA_MASTER_KEY`；
- 不与现有服务冲突的 Web 绑定地址和端口。

业务服务器与 GPU 服务器默认同机。只有用户明确指定独立 GPU 服务器时，才配置第二个目标；平台不得修改业务原始数据库、现有容器、驱动、CUDA 或共享 Redis 配置。

## 4. 配置生产变量

在服务器部署目录复制模板：

```bash
cp deploy/env.production.example .env.production
chmod 600 .env.production
```

通过服务器侧受限环境文件、Docker Secret 或企业密钥系统填写真实值。不要把填充后的内容输出到聊天、公开日志或代码仓库。受控局域网 HTTP 使用 `LIEMA_SESSION_COOKIE_SECURE=false`；入口切换为 HTTPS 后必须改为 `true`。

## 5. 启动与验收

复用外部 MySQL、MinIO 和 Redis 时，从项目根目录执行：

```bash
docker compose --env-file .env.production -f deploy/compose.external.yml config --quiet
docker compose --env-file .env.production -f deploy/compose.external.yml up -d --no-build worker
docker compose --env-file .env.production -f deploy/compose.external.yml up -d --no-build web
docker compose --env-file .env.production -f deploy/compose.external.yml ps
curl --fail --silent http://127.0.0.1:8080/health
```

验收至少确认：MySQL、MinIO、Redis 均可用，执行模式为 `external`，Worker 心跳在线，匿名业务接口返回 401，登录后项目隔离、任务领取和报告下载正常。不要擅自占用本机或服务器已有的 8080 端口；端口由部署变量和运维入口统一安排。

## 6. 升级与回滚

升级前先备份平台专用 MySQL、MinIO 和持久卷，确认所有任务队列状态，再以新的不可变镜像版本依次更新 Worker、Web。回滚时恢复上一稳定镜像；如果数据结构发生不兼容变化，必须按已验证备份恢复方案处理，绝不能直接覆盖业务原始数据。

具体命令、停机窗口、RPO/RTO、备份核验和回滚触发条件见 [`deployment-backup-rollback-runbook.md`](deployment-backup-rollback-runbook.md)。
