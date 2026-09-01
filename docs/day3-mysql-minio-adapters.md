# 第三工作日：MySQL与MinIO适配代码

## 已完成

- 实现MySQL任务仓储和平台仓储，包含建表、事务、跨进程任务领取锁和SQLite语句兼容层。
- 实现MinIO产物存储，采用本地暂存生成、目录上传、按需下载缓存和路径边界校验。
- 历史运行目录支持从MinIO整目录冷启动恢复，数据库中的产物路径可转换为 `minio://` 引用。
- 自动化运行、企业报告、服务器压测完成后均会同步产物；下载接口兼容本地路径和 `minio://` 引用。
- 实现SQLite一致性备份、SQLite到MySQL迁移、表级计数核验、本地到MinIO迁移、MinIO反向恢复和SQLite回滚。
- 迁移命令默认dry-run；实际写入需要显式添加 `--execute`。MySQL正式迁移前会自动生成SQLite备份。

## 联调前需要的环境

MySQL：host、port、database、user、password，以及账号的建表和读写权限。

MinIO：endpoint、access key、secret key、bucket、是否HTTPS；bucket建议由运维预先创建。

敏感值建议使用以下环境变量，不写入YAML：

```text
LIEMA_MYSQL_HOST
LIEMA_MYSQL_PORT
LIEMA_MYSQL_DATABASE
LIEMA_MYSQL_USER
LIEMA_MYSQL_PASSWORD
LIEMA_MINIO_ENDPOINT
LIEMA_MINIO_ACCESS_KEY
LIEMA_MINIO_SECRET_KEY
LIEMA_MINIO_BUCKET
LIEMA_MINIO_SECURE
```

## 联调顺序

1. 保持当前服务使用SQLite和本地存储，停止写入任务。
2. 先执行迁移dry-run，核对源数据量、MySQL连接和MinIO文件数。
3. 执行MySQL迁移与MinIO上传，核对表级数量、报告下载和对象数量。
4. 修改后端配置并重启，执行任务、报告和压测冒烟测试。
5. 出现异常时切回SQLite/local；数据库备份和MinIO反向恢复工具可用于回滚。

## 命令

```powershell
python -m auto_test.platform.migration backup --output backups\manual
python -m auto_test.platform.migration sqlite-to-mysql
python -m auto_test.platform.migration sqlite-to-mysql --execute
python -m auto_test.platform.migration local-to-minio
python -m auto_test.platform.migration local-to-minio --execute
python -m auto_test.platform.migration minio-to-local --output artifacts_restored --execute
python -m auto_test.platform.migration restore-sqlite --output backups\manual --overwrite --execute
```

当前机器尚未安装 `PyMySQL` 和 `minio` 包，也没有真实服务参数，因此今日使用模拟客户端完成代码路径验证。拿到环境后需安装项目依赖并进行真实联调。
