# 全新机器部署与旧平台换机手册

> 更新日期：2026-09-10
> 适用交付：`2026.09.10.3` 完整离线包；目标为 Linux x86_64/amd64。
> 本版为所有列表、报告章节和详情抽屉统一提供指定页跳转、每页条数选择与有界滚动；规范模型测试报告，补齐测试环境、方法和判定依据，并支持选择已配置模型生成可追溯 AI 综合分析；保留全量测评按钮、全部专项指标报告和可追溯证据 ZIP，保留真实模型验收发现的采样/评分/流式/报告修正，以及全局浅色/深色主题；主题在登录页与业务顶栏切换，浏览器按当前站点保存选择，无需新增服务器配置。
> 本文从收到交付包开始。备份/恢复细节见 [部署、备份与回滚手册](deployment-backup-rollback-runbook.md)，本版本验收见 [发布记录](deployment-release-20260909.md)。

## 1. 先选定部署场景

| 场景 | 数据与主密钥 | 执行顺序 |
| --- | --- | --- |
| 新建一套独立平台 | 新平台专用库、对象空间、Redis 命名空间和主密钥；首次访问创建管理员 | 第 2～7 节 |
| 只更换应用机器，保留原 MySQL/MinIO/Redis | 原数据库、对象前缀、主密钥和 Redis 参数保持一致；搬迁五个本地卷 | 完成第 2～5 节，再按第 8 节切换；不得提前启动新 Worker |
| 平台、依赖及数据一起搬迁 | 在新依赖服务恢复平台备份，保持主密钥及对象引用兼容 | 完成第 2～5 节准备，再按第 8 节恢复和切换 |

业务服务器与 GPU 服务器默认同机，由用户在平台内选定；只有明确指定独立 GPU 地址才拆开。运行平台 Web/Worker 的机器不要求安装显卡、CUDA、GPU 容器运行时或本机 Python。完整镜像已包含隔离 EvalScope，评测通过模型接口工作。

## 2. 准备机器、网络和依赖

### 2.1 操作系统、工具与空间

- 准备 Linux x86_64 主机和受控 Docker 操作账号。本包为 `linux/amd64`，ARM64 机器需要另行构建验收。
- 安装 Docker Engine、Compose v2，以及 `tar`、`gzip`、`unzip`、`sha256sum`、`curl`。Docker 按目标发行版的 [官方安装入口](https://docs.docker.com/engine/install/) 安装；离线机器提前准备匹配系统的安装包，应用 ZIP 不包含 Docker 安装程序。
- 已有业务容器的主机先由运维确认安装/升级窗口，保留现有 Docker 数据目录及网络配置。本项目实测为 Engine 23.0.6、Compose 2.17.3，这是兼容记录，不要求新机固定安装旧版本。

```bash
uname -m
docker version
docker compose version
docker info --format '{{.DockerRootDir}}'
df -h
free -h
ss -lnt
```

ZIP 约 1.07 GB，镜像归档约 650 MB；加载后的镜像、解包依赖和备份另外占用空间。容量按“交付包 + 镜像 + 最大上传暂存 + 报告产物 + 至少一轮备份”规划。同机部署数据库等依赖时还要另计其数据与内存，不能只按 ZIP 大小准备磁盘。

### 2.2 三项外部依赖必须先准备好

`deploy/compose.external.yml` 只创建 Web、Worker 和五个应用卷。MySQL、MinIO、Redis 的服务程序、数据及配置不在应用镜像内。新现场没有这些服务时，先由运维提供实例或既有实例中的平台专用空间，再继续。

| 依赖 | 部署前提供 | 核验要求 |
| --- | --- | --- |
| MySQL | 容器可达地址/端口、平台专用库、账号和凭据 | 库使用 utf8mb4；账号只在该库具有建表、加列、建索引与增删改查权限，不使用业务原始库 |
| MinIO | S3 API endpoint、平台专用 bucket、访问凭据、TLS 设置 | 预先创建 bucket，当前默认 `create_bucket=false`；使用 S3 API 端口，不填控制台端口 |
| Redis | TCP 地址/端口、ACL 用户（如有）、密码、唯一命名空间 | 按服务端配置连接成功，允许平台队列和心跳所需操作，不修改共享 Redis 配置解决权限问题 |

Redis 密码为可选配置：服务端有密码就填写，没有密码就留空，平台使用免密连接。ACL 用户名仅在配置密码时使用；密码错误不会退回免密。当前 Compose 提供普通 TCP Redis 配置；要求 TLS 的现场应先完成受控连接方案及兼容验收，不能只将 TLS 端口填入普通 TCP 配置。

### 2.3 网络与访问入口

浏览器应能访问选定的 Web 入口，平台容器应能访问 MySQL/MinIO/Redis、目标业务 API、SSH 和模型 API。依赖与平台在同一台物理机时，填写容器可达的宿主机地址；容器内 `127.0.0.1` 指向容器自身。依赖只监听宿主机回环地址时，由运维提供受限的可达入口。

`LIEMA_WEB_BIND` 必须是新机实际拥有的地址。局域网直接访问时选择该机内网地址，同机反向代理可使用回环绑定。从授权客户端实际验证连通性及访问范围；Docker 发布端口与宿主机防火墙存在专门的规则交互，不能只凭普通防火墙规则推断端口已经受限。见 [Docker 防火墙说明](https://docs.docker.com/engine/network/packet-filtering-firewalls/)。

## 3. 传包、校验、加载成品镜像

### 3.1 需要携带的文件

从发布记录登记的服务器版本目录，通过 SFTP/SCP 等受控通道取回以下两个文件；开发机已按空间清理要求不长期保留部署大包。目标账号、地址和目录由现场填写：

```text
liema-auto-2026.09.10.3-release.zip
liema-auto-2026.09.10.3-release.zip.sha256
```

交付包不含生产 `.env.production`、开发 `config.local.yml`、主密钥、密码和平台数据。旧平台换机所需的这些材料按第 8 节分别安全转移。

### 3.2 在新机器上执行

`/opt/liema-auto` 是可调整的应用目录示例，与任何特定服务器无关。以下命令在具有该目录和 Docker 操作权限的同一个 Bash 会话执行：

```bash
export LIEMA_DEPLOY_ROOT=/opt/liema-auto
export LIEMA_RELEASE=2026.09.10.3
export LIEMA_STACK=liema
export LIEMA_RELEASE_DIR="$LIEMA_DEPLOY_ROOT/releases/$LIEMA_RELEASE"
export LIEMA_PACKAGE_DIR="$LIEMA_DEPLOY_ROOT/packages/$LIEMA_RELEASE"
install -d -m 0750 "$LIEMA_RELEASE_DIR" "$LIEMA_PACKAGE_DIR"
```

将 ZIP 和外部 `.sha256` 放入 `LIEMA_PACKAGE_DIR`。首次解包使用新目录，已有同版本目录先核对用途，不能覆盖运行中的部署。

```bash
set -euo pipefail
cd "$LIEMA_PACKAGE_DIR"
sha256sum -c "liema-auto-$LIEMA_RELEASE-release.zip.sha256"
unzip "liema-auto-$LIEMA_RELEASE-release.zip"
sha256sum -c SHA256SUMS
tar -xzf "liema-auto-$LIEMA_RELEASE-source.tar.gz" -C "$LIEMA_RELEASE_DIR"
gzip -dc "liema-auto-$LIEMA_RELEASE-image.tar.gz" | docker load
docker image inspect "liema-auto:$LIEMA_RELEASE" \
  --format '{{.Os}}/{{.Architecture}} {{.Id}}'
```

期望为 `linux/amd64`，镜像 ID 与包内 `RELEASE.json` 一致。用包内 `RELEASE.json` 的镜像 ID 校验，不沿用旧版本的 ID。

加载完整成品镜像即可部署。`dependencies.tar.gz` 和 `evalscope.tar.gz` 用于离线重建；无需在新机联网安装 Python/EvalScope，也不能复制 Windows 虚拟环境代替 Linux 运行时。

## 4. 创建生产环境文件

新平台从模板创建受限文件；旧平台换机按第 8 节迁移受限配置，不能重置原主密钥：

```bash
umask 077
test ! -e "$LIEMA_DEPLOY_ROOT/.env.production"
install -m 0600 "$LIEMA_RELEASE_DIR/deploy/env.production.example" \
  "$LIEMA_DEPLOY_ROOT/.env.production"
```

通过服务器侧受控编辑器或企业密钥工具填写下表。凭据不写入命令行、聊天、Git 或普通交付包。

| 变量 | 填写说明 |
| --- | --- |
| `COMPOSE_PROJECT_NAME` | 与 `LIEMA_STACK` 一致；不得与同一 Docker 主机上的其他项目冲突 |
| `LIEMA_IMAGE` | 本版本为 `liema-auto:2026.09.10.3`，以后从对应 `RELEASE.json` 取值 |
| `LIEMA_WEB_BIND` | 本机实际绑定地址；局域网直接访问时不能保留回环绑定 |
| `LIEMA_WEB_PORT` | 经 `ss -lnt` 确认未占用的外部端口；容器内部固定 8080 |
| `LIEMA_SESSION_COOKIE_SECURE` | 最终用户入口为受控 HTTP 时 `false`，HTTPS 时 `true` |
| `LIEMA_MASTER_KEY` | 新平台使用密码学安全随机源生成并安全保管；迁移平台必须沿用原值 |
| `LIEMA_MYSQL_HOST` / `LIEMA_MYSQL_PORT` | 容器可达的 MySQL 地址和实际端口 |
| `LIEMA_MYSQL_DATABASE` / `LIEMA_MYSQL_USER` | 已创建的平台专用库与受限账号 |
| `LIEMA_MYSQL_PASSWORD` | MySQL 密码 |
| `LIEMA_MINIO_ENDPOINT` | S3 API 的 `主机:端口`，不附加 bucket、路径或控制台地址 |
| `LIEMA_MINIO_ACCESS_KEY` / `LIEMA_MINIO_SECRET_KEY` | 平台对象空间的受限凭据 |
| `LIEMA_MINIO_BUCKET` | 已存在的 bucket；迁移时与历史 `minio://` 引用一致 |
| `LIEMA_MINIO_SECURE` | S3 API 使用 HTTPS 为 `true`，HTTP 为 `false` |
| `LIEMA_REDIS_HOST` / `LIEMA_REDIS_PORT` | Redis 实际 TCP 地址和端口 |
| `LIEMA_REDIS_NAMESPACE` | 本平台唯一命名空间；同一套迁移保持一致，不同平台分别隔离 |
| `LIEMA_REDIS_USERNAME` | ACL 命名用户；使用默认用户时可留空 |
| `LIEMA_REDIS_PASSWORD` | 可选；有密码时填写有效密码，无密码时留空 |

含 `$` 等特殊字符的值按 Compose `.env` 语法用单引号保护，并按其规则转义值内单引号。不要执行 `source .env.production`。当前 shell 同名变量优先于 `--env-file`，部署前核对是否残留旧环境变量，避免覆盖新配置。见 [Compose 变量及引用规则](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)。

### 4.1 对象前缀、Redis DB 与其他非默认配置

默认对象前缀为 `liema/`，Redis DB 为 0。当前 Compose 没有映射 `LIEMA_MINIO_PREFIX` 或 `LIEMA_REDIS_DB`，不能只添加这两个环境变量就假设生效。

需要保留非默认前缀/DB 时，将发布源码的 **完整** `config/config.example.yml` 复制到服务器侧 `config/config.production.yml`，只调整必要非敏感字段，如 `artifact_storage.prefix`、`task_queue.redis_db`。加载器选用 local 文件后不会与 example 递归合并，不能只写局部 YAML。密码仍在受限环境文件中。

在环境文件所在目录创建 `compose.site.yml`，同时为 Web/Worker 挂载完整配置：

```yaml
services:
  web:
    volumes:
      - type: bind
        source: ${LIEMA_SITE_CONFIG:?set LIEMA_SITE_CONFIG}
        target: /app/config/config.local.yml
        read_only: true
  worker:
    volumes:
      - type: bind
        source: ${LIEMA_SITE_CONFIG:?set LIEMA_SITE_CONFIG}
        target: /app/config/config.local.yml
        read_only: true
```

在 `.env.production` 添加 `LIEMA_SITE_CONFIG`，填新机上该文件的绝对路径。容器身份为 UID/GID 10001，应允许其只读文件及遍历父目录，例如文件属组 10001、模式 `0640`，父目录给予该组遍历权限。SELinux 主机还需按现场策略设置专用挂载的容器可读标签。不要复制 Windows 路径或旧机绝对路径。

后续每次 `config/run/up/exec/stop` 都必须带上该 override。下一节的命令数组已统一处理此事。

## 5. 校验配置与依赖，不启动任务消费者

在同一 Bash 会话定义命令数组。新会话需要重设第 3 节变量并重新定义数组：

```bash
cd "$LIEMA_DEPLOY_ROOT"
LIEMA_COMPOSE=(docker compose -p "$LIEMA_STACK" \
  --env-file "$LIEMA_DEPLOY_ROOT/.env.production" \
  -f "$LIEMA_RELEASE_DIR/deploy/compose.external.yml")
if [ -f "$LIEMA_DEPLOY_ROOT/compose.site.yml" ]; then
  LIEMA_COMPOSE+=(-f "$LIEMA_DEPLOY_ROOT/compose.site.yml")
fi
"${LIEMA_COMPOSE[@]}" config --quiet
"${LIEMA_COMPOSE[@]}" config --services
stat -c '%a %n' "$LIEMA_DEPLOY_ROOT/.env.production"
```

期望只有 `web` 和 `worker`，环境文件权限为 `600`。完整 `docker compose config` 或 `config --environment` 可能展开凭据，不要写入公开日志。

执行一次同镜像、同配置的只读探针。入口被覆盖为 Python，不启动 Worker 或提交任务；仅创建/复用本平台声明的卷与网络：

```bash
"${LIEMA_COMPOSE[@]}" run --rm -T --no-deps --entrypoint python worker - <<'PY'
import os
import pymysql
import redis
from minio import Minio
from auto_test.common.config_loader import artifact_storage_cfg, task_queue_cfg

e = os.environ
db = pymysql.connect(host=e['LIEMA_MYSQL_HOST'], port=int(e['LIEMA_MYSQL_PORT']),
    user=e['LIEMA_MYSQL_USER'], password=e['LIEMA_MYSQL_PASSWORD'],
    database=e['LIEMA_MYSQL_DATABASE'], connect_timeout=5)
with db.cursor() as q:
    q.execute('SELECT 1')
    assert q.fetchone()[0] == 1
db.close()
objects = Minio(e['LIEMA_MINIO_ENDPOINT'], access_key=e['LIEMA_MINIO_ACCESS_KEY'],
    secret_key=e['LIEMA_MINIO_SECRET_KEY'], secure=e['LIEMA_MINIO_SECURE'].lower() == 'true')
assert objects.bucket_exists(e['LIEMA_MINIO_BUCKET']), 'Platform bucket does not exist'
prefix = str(artifact_storage_cfg().get('prefix') or 'liema').strip('/') + '/'
next(iter(objects.list_objects(e['LIEMA_MINIO_BUCKET'], prefix=prefix, recursive=True)), None)
queue = redis.Redis(host=e['LIEMA_REDIS_HOST'], port=int(e['LIEMA_REDIS_PORT']),
    username=e.get('LIEMA_REDIS_USERNAME') or None, password=e.get('LIEMA_REDIS_PASSWORD') or None,
    db=int(task_queue_cfg().get('redis_db') or 0), socket_connect_timeout=5, socket_timeout=5)
assert queue.ping()
print('MySQL SELECT 1, MinIO bucket/list, Redis connectivity (optional authentication): OK')
PY
```

此探针不证明数据库 DDL 权限、MinIO 写权限及 Redis 全部队列权限，仍需完成后续启动与功能验收。旧平台换机此时先完成第 8 节停机/备份/恢复，再启动服务。

## 6. 首次启动与功能验收

```bash
"${LIEMA_COMPOSE[@]}" up -d --no-build --no-deps worker
"${LIEMA_COMPOSE[@]}" up -d --no-build --no-deps web
"${LIEMA_COMPOSE[@]}" ps
"${LIEMA_COMPOSE[@]}" exec -T web python - <<'PY'
import json, urllib.request
with urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=5) as r:
    h = json.load(r)
assert h['status'] == 'ok'
assert (h['database_backend'], h['artifact_backend'], h['task_queue_backend'], h['task_execution_mode']) == ('mysql', 'minio', 'redis', 'external')
assert h['task_queue']['available'] and h['task_queue']['worker_available']
print(json.dumps(h, ensure_ascii=False))
PY
"${LIEMA_COMPOSE[@]}" logs --since 5m --tail 100 web worker
```

启动后给健康检查和心跳留出时间，再运行探针。回环 8080 是 **Web 容器内部探针**，不要求宿主机绑定同样地址/端口。

在被允许访问的客户端检查实际外部地址：

```bash
read -r -p '实际平台入口（含 HTTP/HTTPS 协议及端口）: ' LIEMA_PUBLIC_URL
LIEMA_PUBLIC_URL=${LIEMA_PUBLIC_URL%/}
curl --noproxy '*' --fail --silent --show-error "$LIEMA_PUBLIC_URL/health"
curl --noproxy '*' --silent --show-error -D - "$LIEMA_PUBLIC_URL/api/auth/status"
curl --noproxy '*' --silent --show-error -o /dev/null -w '%{http_code}\n' "$LIEMA_PUBLIC_URL/api/runs"
```

认证状态应为 200 且 `Cache-Control: no-store`，匿名业务 API 为 401。空库首次部署的 `setup_required=true` 正常，迁移旧库应为 `false`。

由现场指定人员在浏览器完成：

1. 新平台创建首位管理员与真实项目；迁移平台使用原账号登录，不能重新初始化替代原数据。
2. 检查项目、角色及历史记录归属；提交小型只读接口场景，确认入队、Worker 领取、完成及日志。
3. 生成并下载 DOCX/PDF，验证中文、对象写入与读取；迁移平台再下载一份历史报告。
4. 配置获授权的业务服务器和模型，验证连接及历史凭据解密。没有维护窗口授权时不做满载压测。
5. 记录版本/镜像 ID、入口、Compose 项目名、备份编号与结论，不记录凭据。

### 6.1 全量测评功能验收

进入“模型评测 → 评测运行”，选定被测模型，可选独立裁判和实际模型主机的已保存服务器资产，再点击“全量测评（全部维度）”。不需要新增环境变量或数据库迁移；完整镜像、模型配置和外部 Worker 沿用原配置。独立裁判不可与被测接口/模型相同。

默认顺序执行 9 个专项：基础非流式、基础流式、200 条中英翻译、标准问答、WMT 翻译、报告写作、情报生产、固定并发、深度性能。选择当前项目已发布测试集后，会把该版本全部用例加入第 10 个专项，不按快速方案抽样。内置质量范围为 236 条原生用例和 8 条适配器样例，性能请求另计。

质量默认输出预算 4096 Tokens、超时 120 秒；性能固定最高并发 16、输出预算最多 512、开放环 1/2/4/8 RPS，并包含突发、持续和负载回落阶段。根据现场业务安排获授权的时段执行。未选裁判或未绑定资源时仍可运行，报告明确标注缺测，不能把缺测当成通过。

在任务详情观察专项进度；停止后保留已完成结果。完成后从“评测报告”检查覆盖矩阵、规则和裁判各维度、12 个性能阶段、资源样本、失败证据及全部用例，验证各表分页、Word/PDF 和“全量证据 ZIP”下载。ZIP 内含输入快照、原始响应、逐例评分、最新人工复核及文件校验清单。

裁判必须原样返回全部配置维度及 0～1 数值；改名、遗漏、重复或无效数值会标记无效并保留原因。检查报告的“有效裁判 / 需裁判”覆盖数，不能把 HTTP 成功或历史 completed 状态当作完整评分。新版本已按实际维度生成输出模板；本次历史全量记录的缺项如实保留，修正后的独立复测见 [全量验收记录](model-full-acceptance-20260909.md)。

“全量”指当前平台全部内置能力与选定项目测试集；内置问答/WMT 各 4 条为适配器样例，不能用于官方榜单排名。报告各专项保持原始指标量纲，不将 ROUGE、BLEU、规则分和吞吐混成一个总分。具体口径见 [模型评测技术方案](model-evaluation-technical-design.md)。

## 7. EvalScope 运行环境验收

完整镜像已设置 `LIEMA_EVALSCOPE_PYTHON=/opt/evalscope/bin/python`，并带有 NLTK 离线资源。先检查实际 Worker：

```bash
"${LIEMA_COMPOSE[@]}" exec -T worker /opt/evalscope/bin/python -c \
  "import sys,importlib.metadata as m; print(sys.version.split()[0],m.version('evalscope')); assert m.version('evalscope')=='1.11.1'"
```

复验整条隔离链时，在未注入生产配置、无外网的临时容器中运行本地假模型 PoC：

```bash
docker run --rm --network none --read-only --tmpfs /tmp:rw,mode=1777 \
  --mount "type=bind,src=$LIEMA_RELEASE_DIR/scripts,dst=/validation/scripts,readonly" \
  -i "liema-auto:$LIEMA_RELEASE" python - <<'PY'
import importlib.util, sys
from pathlib import Path
import auto_test
spec = importlib.util.spec_from_file_location('installed_poc', '/validation/scripts/model_evaluation_poc.py')
poc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poc)
poc.SOURCE_ROOT = Path(auto_test.__file__).resolve().parent.parent
sys.argv = ['model_evaluation_poc.py', '--python', '/opt/evalscope/bin/python', '--output-dir', '/tmp/evaluation-validation']
raise SystemExit(poc.main())
PY
```

最后应输出 `eval=completed` 和 `perf=completed`，eval 包含标准评测与 WMT BLEU。该命令不访问真实模型或平台数据库，临时产物随容器删除；需要留证时另挂载仅供验收的可写目录。真实质量、容量和 GPU 满载结论另需获授权的模型/业务服务器及维护窗口。

## 8. 旧平台换机：数据与切换

仅复制镜像不会携带账号、项目、历史报告和暂存文件。更换 `COMPOSE_PROJECT_NAME` 会对应另一组卷，不等于搬迁已有数据。

### 8.1 迁移清单

| 材料 | 需要保留或调整的内容 |
| --- | --- |
| MySQL | 整个平台专用库，包括用户/项目/权限、任务、报告、资产、模型评测表；单事务备份及恢复验证 |
| MinIO | 数据库实际引用的 bucket/prefix 全部对象；保持对象 key 与引用一致，核对数量/字节数/哈希 |
| 五个卷 | data、runtime、artifacts、instance、logs；即使使用外部 MySQL/MinIO 也保留上传暂存和缓存 |
| 主密钥 | 原 `LIEMA_MASTER_KEY`；通过密钥系统或受限文件加密传输，不能重新生成 |
| 现场配置 | 依赖地址和认证、对象前缀、Redis DB/命名空间、站点 override；更新旧机路径、绑定地址、网络规则 |
| 回滚材料 | 原镜像、Compose 文件、卷映射、最终备份清单与切换前状态 |

旧数据跨两个平台前缀时，两处都要盘点；不能只按新配置默认前缀备份。若同时重命名 bucket/prefix，必须先制定数据库对象引用迁移方案，不能仅改 endpoint/bucket 后宣布迁移完成。

### 8.2 执行顺序

1. 新机完成镜像加载、依赖准备与第 5 节只读探针，先不启动 Web/Worker。
2. 旧平台暂停提交任务，确认业务自动化、接口场景、报告、压测、模型评测五类任务均无排队/运行记录；活动 SSH 会话由用户安全关闭。
3. 停止旧平台 Web/Worker，记录镜像和卷归属，完成最终数据库、对象、五个卷及受限配置备份，只操作平台专用资源。
4. 仅搬应用时继续用原外部存储及主密钥，并恢复五个卷；依赖也搬迁时，先恢复到新机的专用空库/对象空间，再核对表计数、对象清单、报告签名与哈希。
5. 新服务停止时创建目标卷并恢复快照。先用 Compose 配置/标签确认目标卷属于本平台，再通过受控恢复工具解包并保持原 UID/GID/权限；服务身份为 10001。不得把旧机整个 Docker 数据目录覆盖到新机，也不能覆盖既有业务卷。
6. 更新新机绑定地址及必要依赖地址，核对主密钥、bucket/prefix、Redis DB/命名空间，重跑第 5 节检查。
7. 按第 6 节启动新 Worker、Web，验收原账号、历史报告、健康及新只读任务闭环，再切换客户端入口/DNS。
8. 保留停机的旧平台、旧镜像与备份至回滚保留期结束，并建立不同故障域的备份副本。**新旧平台不得同时消费同一套正式任务；不同 Redis 命名空间仍会争用同一 MySQL 任务表。**

### 8.3 迁移失败时

先停新平台 Web/Worker，再检查是否已产生正式写入。确认数据兼容和回退点后才恢复旧平台，避免两边同时写入。新平台已产生任务/对象时，先再次备份新状态并确定数据处理方式，不能用旧备份直接覆盖现网。具体操作见 [备份与回滚手册](deployment-backup-rollback-runbook.md)。

## 9. 从源码重新构建（维护人员使用）

普通部署使用成品镜像即可。开发改动后先在 PyCharm 实际环境运行回归：

```powershell
..\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q
```

在 Linux/amd64 Docker 构建环境解包对应依赖并分别校验：

```bash
cd "$LIEMA_RELEASE_DIR"
tar -xzf "$LIEMA_PACKAGE_DIR/liema-auto-$LIEMA_RELEASE-dependencies.tar.gz"
(cd vendor && sha256sum -c SHA256SUMS)
tar -xzf "$LIEMA_PACKAGE_DIR/liema-auto-$LIEMA_RELEASE-evalscope.tar.gz"
(cd vendor/evalscope && sha256sum -c SHA256SUMS)
```

先可信加载官方 `python:3.12-slim` 构建基础镜像并记录实际 ID；该标签可能变化，精确复用历史运行环境优先使用已验证成品镜像或已归档原基础镜像。新构建采用新版本号，不覆盖已发布版本：

```bash
read -r -p '本次新构建版本号（不复用已发布版本）: ' LIEMA_BUILD_VERSION
test -n "$LIEMA_BUILD_VERSION"
docker build --network=none -f deploy/docker/Dockerfile.offline \
  -t "liema-auto:$LIEMA_BUILD_VERSION-platform" .
docker build --network=none \
  --build-arg "LIEMA_BASE_IMAGE=liema-auto:$LIEMA_BUILD_VERSION-platform" \
  -f deploy/docker/Dockerfile.evalscope-offline \
  -t "liema-auto:$LIEMA_BUILD_VERSION" .
docker save "liema-auto:$LIEMA_BUILD_VERSION" | gzip -1 \
  > "$LIEMA_PACKAGE_DIR/liema-auto-$LIEMA_BUILD_VERSION-image.tar.gz"
```

重建后以新镜像标签复跑第 7 节 PoC，重新生成 SHA-256 和发布记录。Windows 生成的校验清单使用 UTF-8/LF，行尾 CR 可能被 Linux 当作文件名的一部分。

## 10. 常见故障

| 现象 | 排查方向 |
| --- | --- |
| `exec format error` | 机器/镜像是否均为 amd64；本包不能直接作为 ARM64 镜像使用 |
| 镜像不存在或意外联网构建 | 成品镜像是否已加载，模板是否旧标签；使用 `up --no-build` |
| 端口占用/绑定地址不存在 | 外部端口与新机实际地址，不停止无关服务抢占端口 |
| 容器健康但客户端不通 | 真实外部地址/端口、回环绑定、路由/防火墙、Docker 发布规则 |
| 登录后立即返回登录页 | 最终入口 HTTP/HTTPS 与 Cookie Secure、系统时间 |
| 数据库失败 | 容器内连通性、实际库名/账号/权限、误用 `127.0.0.1` |
| MinIO 或历史报告不可用 | S3 API 端口/TLS、bucket 预建、原前缀/引用、对象恢复完整性 |
| Worker 不在线或不领取 | Web/Worker 使用相同环境和 override、Redis 密码/ACL/DB/命名空间、日志 |
| 历史凭据无法解密 | 原主密钥是否保留，是否误生成或被 shell 同名变量覆盖 |
| 目录 Permission denied | 卷恢复后的 UID/GID、父目录权限、站点挂载的 SELinux 标签 |
| EvalScope 找不到模块 | 是否完整镜像、独立解释器和 NLTK 资源是否存在，是否误填 Windows 路径 |

外网证书或软件源不可用时，使用已核验成品镜像/离线依赖并排查 CA/代理，保持证书校验开启。不要为排除部署故障而临时改业务主机驱动、CUDA、Docker 全局网络或共享数据库配置。


## 10. 模型测试报告与 AI 综合分析

报告包含总体结论、场景适用性、测试环境、方法与判定标准、版本化数据范围、分项全量指标和全部用例附录。进入“模型评测 → 已结束任务 → 报告”，选择综合分析模型并点击“生成 AI 综合分析”。默认复用“模型配置”中已启用的模型；可以为本份报告另选已有模型，不会修改全局启用状态。生成者需具备报告管理权限；浏览、Word/PDF 下载不会触发新的模型调用。

AI 仅分析聚合指标及证据引用，不参与本次原始用例评分。内置提示词禁止将调用成功当作质量正确、虚构生产门槛或忽略无效裁判/缺测。分析记录所用模型、生成时间、提示词版本和证据 SHA-256；人工复核或运行结果变化后，旧分析失效，需要重新生成。没有客户批准的质量门槛、目标负载和时延 SLA 时，报告只给出受控验证意见与准入条件。

新运行在开始时记录执行端操作系统、Python 和后端包版本，并对已绑定模型服务器进行只读硬件采集。历史任务缺失的环境标为“未采集”，不能以换机后的配置补写为历史事实。更换推理模型、量化精度或 GPU 配置后，应在新环境重新运行对应评测。

若模型生成失败，报告保留实测结果并明确失败状态，可选择其他已配置模型后重试。外部接口返回 HTML 通常需要检查网关或网络策略，不能当作有效模型结果。

### 10.1 企业网络的 TLS 信任配置

`LIEMA_MODEL_CA_BUNDLE` 默认为空，使用标准可信 CA。只有经过管理员核验的企业 CA 才能加入 PEM 信任包；可以将标准 CA 与核验后的企业 CA 合并，放入已有 instance 卷，例如容器内 `/app/instance/model-ca-bundle.pem`，并在部署服务器 `.env.production` 中设置该路径。Web/Worker 共用同一配置，重建应用容器后生效。此项只影响既有报告模型调用，不修改系统全局信任，也不关闭 TLS 验证。

换机时若配置此项，须随 instance 卷迁移对应 PEM，核验文件完整性及容器内可读路径；新网络不需要企业 CA 时保持为空。证书不随公共软件发布包分发。添加 CA 只解决证书链信任问题，不能绕过网关访问策略。

## 本版界面验收：分页与滚动（2026-09-10.3）

本版所有目录、历史报告、详情抽屉及评测报告章节均可选择每页条数，并通过页码输入框的回车或“跳转”按钮定位页面。统一提供 10、20、50、100 条，保留各列表原来的默认条数选项。条数调整后回到第一页；页内刷新和轮询保留当前设置，筛选、切换项目或详情对象会重置相关页码。无数据时页码为 1/1，跳转按钮不可用；输入超出范围或非整数时显示提示。

换机后用有多页数据的账号检查：选择每页 20 条、跳到末页、点击页内刷新，确认总数、当前范围与内容一致；选择 100 条时内容区可纵向滚动，宽表可横向滚动，分页栏位于内容区之外。测试集详情右侧展开后执行相同检查，关闭再切换对象应回到第一页。分别切换浅色/深色，并在桌面与窄屏检查控件没有遮挡。本次仅更新应用与界面，没有数据库结构迁移；备份按用户要求暂缓，生产环境配置和数据卷沿用现有值。
