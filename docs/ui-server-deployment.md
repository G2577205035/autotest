# UI 录制与回放：服务器部署手册

更新：2026-09-11。版本 `2026.09.11.2` 已部署，录制、保存、回放和下载通过真实服务器验收；进度入口为 `PROJECT_PLAN.md`。本手册扩展既有外部 MySQL、MinIO、Redis 部署。

体验修订 `20260911.rec4` 已随 `2026.09.11.2` 发布：工作区扩大、原始大小/全屏、验证指引，以及浏览器与 Inspector 等高排列已在独立容器中验证。本次同步更新 Web、UI Worker 代码与录制镜像 ID；仅刷新网页或只更新 Web 不能改变旧镜像的桌面排列。新镜像仍使用官方 Playwright 1.63.0，无新增依赖，现有回放镜像可复用。切换前等待活动录制、UI 运行结束，保留原镜像/配置以便回退；新建录制会话采用 1280×720 视口，旧套件按脚本内原视口回放。

## 普通测试用户

电脑只需浏览器，无需安装 Docker 或 Playwright。登录平台 → 选择项目 → UI 自动化 → 录制新套件 → 填写网站地址 → 打开网站并录制。在远端浏览器中操作，并用 Playwright 工具栏添加可见性、文字或输入值检查点。点击平台的“完成并保存”，关闭弹窗后运行该套件，查看结果及下载报告、截图、录像和 trace。

录制需项目管理员或平台管理员权限。新网站及它调用的接口服务必须先加入下述允许列表；改变页面结构或账号后可重新录制为新版本。关闭弹窗可在 90 秒内恢复，明确取消才会丢弃；每个会话最长 15 分钟，每人最多一个、平台同时最多两个。

## 服务组成

| 服务 | 职责与连接 |
| --- | --- |
| Web | 提供平台页面与认证后的录制 WebSocket 网关，接入默认网络和 UI Internal 网络 |
| 普通 Worker | 保留既有业务任务执行 |
| UI Worker | 领取录制/回放任务；使用宿主 Docker socket 创建受限浏览器容器 |
| 录制镜像 | Playwright 1.63.0 codegen、Chromium、Xvfb、Openbox、x11vnc、noVNC |
| 回放镜像 | 相同版本 Playwright/Chromium，执行保存的不可变脚本版本 |
| ui-proxy | 只允许明确列出的测试目标与端口，经默认网络访问目标 |

Web、UI Worker 使用相同平台库、对象存储和 `LIEMA_MASTER_KEY`。浏览器只接 Internal 网络，不挂载 Docker socket 或宿主目录，不暴露宿主端口；脚本和登录状态放在容器 tmpfs，录制源码及原始产物加密保存。正式环境不要丢失或替换主密钥。

## 准备镜像

联网构建需在项目根目录执行；离线安装优先载入经过 SHA-256 校验的发布镜像归档。Playwright 镜像版本必须与 npm 包版本一致，不要只更新其中一个。

```bash
docker build -f deploy/Dockerfile.ui-runner -t liema-ui-runner:2026.09.11.1 .
docker build -f deploy/docker/Dockerfile.ui-recorder -t liema-ui-recorder:2026.09.11.2 .
```

先构建/载入应用镜像，再准备仅含 Docker 客户端的 Worker 镜像。可以使用官方校验过的 Docker CLI，或复用部署主机已安装的 CLI；后者必须在最终镜像中验证动态库兼容性。不安装第二个 Docker daemon。

```bash
mkdir -p vendor/ui
cp "$(command -v docker)" vendor/ui/docker
DOCKER_CLI_SHA256=$(sha256sum vendor/ui/docker | cut -d ' ' -f 1)
docker build -f deploy/docker/Dockerfile.ui-worker \
  --build-arg LIEMA_APP_IMAGE=liema-auto:2026.09.11.2 \
  --build-arg DOCKER_CLI_SHA256="$DOCKER_CLI_SHA256" \
  -t liema-ui-worker:2026.09.11.2 .
docker run --rm --network none liema-ui-worker:2026.09.11.2 docker --version
stat -c '%g' /var/run/docker.sock
```

保留 CLI 来源与校验值。代理使用经检查的 Squid 镜像；下面配置要求 `squid` 可直接作为入口，兼容 Ubuntu Squid 镜像。录制、回放和代理均配置固定镜像 ID，通过 `docker image inspect 镜像标签 --format '{{.Id}}'` 获取。

## 配置允许访问的目标

在服务器受限部署目录生成配置。主机和端口由实际测试应用提供，网站可能调用其他主机或端口；需要一并加入。不要使用全网段或全端口放行。以下使用示例域名：

```bash
python3 scripts/prepare_ui_proxy.py \
  --allow-host test-app.example.internal --allow-host test-api.example.internal \
  --allow-port 80 --allow-port 443 --allow-port 8000 \
  --output /home/liema/ui-proxy.conf
```

配置默认拒绝其他目标，不记录访问 URL。更新允许列表后执行下述 Compose 命令重启 `ui-proxy`，并重新录制或回放验证。测试网站账号在录制页面填写，不放入代理配置或生产环境文件。

## 启动与升级

保留原生产环境文件，在其中补齐以下非敏感字段。镜像 ID 替换为实际值，Docker GID 使用上一步输出；配置文件使用绝对路径。

```dotenv
LIEMA_IMAGE=liema-auto:2026.09.11.2
LIEMA_UI_WORKER_IMAGE=liema-ui-worker:2026.09.11.2
LIEMA_UI_RUNNER_IMAGE=sha256:<回放镜像的64位摘要>
LIEMA_UI_RECORDER_IMAGE=sha256:<录制镜像的64位摘要>
LIEMA_UI_PROXY_IMAGE=sha256:<代理镜像的64位摘要>
LIEMA_UI_RUNNER_NETWORK=liema-ui-test
LIEMA_DOCKER_GID=<Docker socket所属组编号>
LIEMA_UI_PROXY_CONFIG=/home/liema/ui-proxy.conf
```

生产环境文件保持 `0600`，不要运行会把所有密码打印出来的 `docker compose config`；语法检查使用 `config --quiet`。Compose 项目名沿用原值，避免创建另一套空卷。

```bash
docker compose --env-file /home/liema/.env.production -p liema \
  -f deploy/compose.external.yml -f deploy/compose.ui.yml config --quiet
docker compose --env-file /home/liema/.env.production -p liema \
  -f deploy/compose.external.yml -f deploy/compose.ui.yml \
  up -d --no-build web worker ui-proxy ui-worker
```

升级前确认平台任务、SSH/压测会话及录制均已结束，保留旧镜像与部署配置，并按主部署手册保存平台数据。不得重建 MySQL/MinIO/Redis、删除应用卷或操作业务原始库。新增录制/套件/运行表仅属于平台库。

部署模式自动使用 `LIEMA_UI_RECORDER_CONNECT_MODE=network` 和 `LIEMA_UI_BROWSER_PROXY=http://ui-proxy:3128`。页面显示服务端管理，不要求测试用户执行本机安装命令。已有 PyCharm 本机方式仍可使用默认 `loopback`；不要在两种模式之间混用存量录制会话。

## 故障检查与回滚

“未就绪”时先检查 UI Worker 日志、固定镜像是否存在、Internal 网络和 Docker socket 权限，禁止把导入脚本改为在 Web 进程执行。网页显示代理拒绝时核对目标主机与端口；502/超时则核对代理到目标的网络路径。录制浏览器不会继承用户电脑的登录态、代理或证书。

UI Worker 异常退出后，确认旧容器和进程已停止再执行 `python -m auto_test.ui_worker --recover-stale`。正常停止会释放租约并清理活动录制；切勿让两个 Worker 同时强制接管同一平台库。

本次回退目标为 `2026.09.11.1`：等待录制/执行结束后，使用 `/home/liema/rollback/2026.09.11.2/environment.before` 和原版本两份 Compose 定义恢复 Web、普通 Worker、UI Worker，代理保持原配置。回退后仍支持录制/回放，但恢复旧窗口布局。保留主密钥、数据卷及套件，不执行数据库回滚；只有回退至尚未提供录制的更早版本时，才需停用相应 UI 服务。

## 验收记录

`2026.09.11.2`：发布前 399 项测试、194 项子测试通过（110.96 秒）。正式蓝鲨登录页实测新桌面和两窗等高、原始大小/适应/全屏、缺少验证时指引及恢复、补加可见性验证、保存、回放 1/1 通过；7 个产物下载通过。应用/UI Worker 各 118 个安装文件与源码一致，Web 和两个 Worker 健康，代理保持原状。未登录目标账号或写入业务数据，临时套件已清理。

当前发布目录为 `/home/liema/releases/2026.09.11.2`，当前回退配置位于 `/home/liema/rollback/2026.09.11.2`；镜像与交付哈希以发布目录的清单为准。下面保留初始录制交付与 SQL 快照的历史记录。

2026-09-11：398 项回归、194 项子测试通过（105.79 秒）。隔离实例完成官方 codegen → 蓝鲨登录 → 任务管理 → 两个检查点 → 保存 → 回放，1 个用例通过，约 16 秒；截图、录像、trace、原始结果及 Word/PDF 共 7 个产物授权下载通过。无检查点拒绝保存、重连、取消、90 秒无人连接回收及普通脚本导入执行均通过。正式入口使用相同流程再次录制并回放，产物进入现有 MinIO。

发布前 118 个安装文件与源码 SHA-256 一致，Compose 共享配置、主密钥、外部依赖和五个持久卷验证通过。正式 Web、普通 Worker、UI Worker、允许列表代理运行；原服务端口保持不变。

发布目录 `/home/liema/releases/2026.09.11.1`；旧镜像、旧环境和 Compose 保留。平台库 45 表快照加密保存在 `/home/liema/rollback/2026.09.11.1/platform.sql.gz.enc`，已在独立测试库恢复并验证候选初始化后记录数一致。它是平台 SQL 快照，未新增全量 MinIO/卷快照；现有对象和卷保留。快照格式为原主密钥加密的 Base64 gzip SQL，恢复应先按主回滚手册在隔离库校验，不直接覆盖业务数据。
