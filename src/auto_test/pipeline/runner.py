import time
from pathlib import Path

from auto_test.common import runtime_config as config
from auto_test.common.env import get_env
from auto_test.common.config_loader import monitor_cfg, upload_cfg, stress_cfg, export_cfg, ai_checks_cfg, default_translate_name
from auto_test.common.logging import log
from auto_test.common.paths import RUNS_DIR, prepare_runtime_layout
from auto_test.integrations.api import list_upload_logs as api_list_upload_logs
from auto_test.integrations.ssh import SSHClient
from auto_test.monitoring.cpu_stress import CpuStressRunner
from auto_test.monitoring.docker_monitor import DockerMonitor
from auto_test.monitoring.log_analyzer import analyze_errors
from auto_test.pipeline.ai_checks import run_ai_feature_checks
from auto_test.pipeline.auth import login, create_user
from auto_test.pipeline.case import resolve_case
from auto_test.pipeline.export import run_export
from auto_test.pipeline.poll import wait_for_parse, get_file_ids, poll_translate, _parse_hhmmss
from auto_test.pipeline.upload import upload_folder, upload_folder_server
from auto_test.pipeline.verify import check_translate_status, chinese_assertion
from auto_test.platform.models import get_active_model_config, ModelSecretError
from auto_test.reporting.charts import generate_all
from auto_test.reporting.summary import print_summary


def _apply_user_config(u, options=None):
    """将测试账号及本次页面输入写入兼容层配置。"""
    options = options or {}
    config.HOST = options.get("host") or u.get("host", "")
    config.CASE_NAME = options.get("case_name") or f"测试{time.strftime('%Y%m%d')}"
    config.TRANSLATE_NAME = (
        options.get("translate_name")
        or u.get("translate_name", "")
        or default_translate_name()
    )
    analysis = options.get("analysis")
    config.ANALYSIS = ",".join(analysis) if isinstance(analysis, list) else (analysis or u.get("analysis", ""))


def _cleanup_old_runs(runs_dir, keep_days=3):
    """删除超过 keep_days 天的日志目录"""
    import datetime
    import shutil

    if not runs_dir.exists():
        return
    today = datetime.date.today()
    for d in runs_dir.iterdir():
        if not d.is_dir():
            continue
        try:
            date_str = d.name[:8]
            d_date = datetime.datetime.strptime(date_str, "%Y%m%d").date()
            if (today - d_date).days > keep_days:
                shutil.rmtree(d)
                log.info(f"已清理过期日志：{d.name}")
        except (ValueError, OSError):
            pass


def _connect_ssh(app_srv, gpu_srv):
    """连接 APP 和 GPU 服务器，返回 (app_ssh, gpu_ssh)"""
    if not app_srv.get("host"):
        return None, None
    app_ssh = SSHClient(app_srv["host"], app_srv.get("user", "root"), app_srv.get("password", ""))
    if not app_ssh.connect():
        return None, None
    gpu_same = (gpu_srv == app_srv)
    if gpu_same:
        return app_ssh, app_ssh
    gpu_ssh = SSHClient(gpu_srv["host"], gpu_srv.get("user", "root"), gpu_srv.get("password", ""))
    gpu_ssh.connect()
    return app_ssh, gpu_ssh


def _as_list(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _option_bool(options, key, default):
    if key not in options or options.get(key) is None:
        return default
    value = options.get(key)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "启用"}


def _runtime_monitor_cfg(options=None):
    options = options or {}
    cfg = dict(monitor_cfg())
    if options.get("monitor_modules") not in (None, "", []):
        cfg["containers"] = _as_list(options.get("monitor_modules"))
    cfg["enable_perf"] = _option_bool(options, "monitor_enable_perf", cfg.get("enable_perf", False))
    cfg["enable_log_monitor"] = _option_bool(options, "monitor_enable_log", cfg.get("enable_log_monitor", False))
    return cfg


def _runtime_upload_cfg(options=None):
    options = options or {}
    cfg = dict(upload_cfg())
    if options.get("upload_mode") not in (None, ""):
        cfg["mode"] = str(options.get("upload_mode")).strip()
    if options.get("upload_size_limit_mb") not in (None, ""):
        cfg["size_limit_mb"] = int(options.get("upload_size_limit_mb"))
    return cfg


def _runtime_export_cfg(options=None):
    options = options or {}
    cfg = dict(export_cfg())
    cfg["enable"] = _option_bool(options, "export_enable", cfg.get("enable", False))
    if options.get("export_types") not in (None, "", []):
        cfg["types"] = ",".join(_as_list(options.get("export_types")))
    return cfg


def _runtime_ai_checks_cfg(options=None):
    options = options or {}
    cfg = dict(ai_checks_cfg())
    mapping = {
        "ai_checks_enable": "enable",
        "ai_simple_translation_enable": "simple_translation_enable",
        "ai_xiaoyi_translation_enable": "xiaoyi_translation_enable",
        "ai_xiaoyi_summary_enable": "xiaoyi_summary_enable",
    }
    for option_key, cfg_key in mapping.items():
        cfg[cfg_key] = _option_bool(options, option_key, cfg.get(cfg_key, False))
    if options.get("ai_sample_path") not in (None, ""):
        cfg["sample_path"] = str(options.get("ai_sample_path")).strip()
    if options.get("ai_max_file_ids") not in (None, ""):
        cfg["max_file_ids"] = int(options.get("ai_max_file_ids"))
    return cfg


def _runtime_stress_cfg(options=None):
    options = options or {}
    cfg = dict(stress_cfg())
    cfg["enable"] = _option_bool(options, "stress_enable", cfg.get("enable", False))
    if options.get("stress_workers") not in (None, ""):
        cfg["workers"] = int(options.get("stress_workers"))
    if options.get("stress_cpu_load") not in (None, ""):
        cfg["cpu_load"] = int(options.get("stress_cpu_load"))
    if options.get("stress_duration") not in (None, ""):
        cfg["duration"] = int(options.get("stress_duration"))
    return cfg


def _runtime_servers(
    target_host="",
    ssh_user="",
    ssh_password="",
    gpu_host="",
    gpu_ssh_user="",
    gpu_ssh_password="",
):
    """Resolve SSH targets for this run.

    The Web form's target host is the target-platform business host for this run.
    The GPU target follows the same server by default.  A separate GPU server is
    used only when the run explicitly submits a different ``gpu_host``.
    """
    host = str(target_host or "").strip()
    user = str(ssh_user or "").strip()
    password = str(ssh_password or "")
    app_srv = {"host": host, "user": user, "password": password}

    compute_host = str(gpu_host or "").strip()
    if not compute_host or compute_host == host:
        return app_srv, dict(app_srv)

    gpu_srv = {
        "host": compute_host,
        "user": str(gpu_ssh_user or user).strip(),
        "password": str(gpu_ssh_password or password),
    }
    return app_srv, gpu_srv


def _exception_detail(exc):
    """Keep exception types visible even when their message is empty."""
    message = str(exc).strip()
    if not message and isinstance(exc, TimeoutError):
        message = "等待服务器响应超时"
    if not message:
        message = "异常未提供详细信息"
    return f"{type(exc).__name__}: {message}"


def _cleanup_failed_run_resources(monitor, *connections):
    """Stop partial monitoring and close every distinct SSH connection."""
    if monitor:
        log.info("上传未完成，正在停止监控并保存已采集的部分性能数据")
        try:
            monitor.stop_perf()
        except Exception as exc:
            log.warning(f"停止性能采集失败：{_exception_detail(exc)}")
        try:
            monitor.stop_logs()
        except Exception as exc:
            log.warning(f"停止日志监控失败：{_exception_detail(exc)}")

    closed = []
    for connection in connections:
        if connection is None or any(connection is item for item in closed):
            continue
        closed.append(connection)
        try:
            connection.disconnect()
        except Exception as exc:
            log.warning(f"关闭 SSH 连接失败：{_exception_detail(exc)}")


def _run_stress_phase(scfg, base_dir, metric_callback=None, options=None):
    """阶段一：CPU 压测（独立于翻译流程），返回 (stress_summary, run_dir)"""
    options = options or {}
    app_srv, gpu_srv = _runtime_servers(
        config.HOST,
        options.get("ssh_user", ""),
        options.get("ssh_password", ""),
        options.get("gpu_host", ""),
        options.get("gpu_ssh_user", ""),
        options.get("gpu_ssh_password", ""),
    )
    if not app_srv.get("host"):
        log.error("压测需要配置 app 服务器")
        return None, None

    run_dir = base_dir / "stress"
    mcfg = _runtime_monitor_cfg(options)

    log.info("=" * 50)
    log.info("阶段一：CPU 压测")
    log.info("=" * 50)

    # 连接
    app_ssh, gpu_ssh = _connect_ssh(app_srv, gpu_srv)
    if not app_ssh:
        log.error("SSH 连接失败，跳过压测")
        return None, None

    # 安装
    stress = CpuStressRunner(app_ssh)
    if not stress.install():
        app_ssh.disconnect()
        if gpu_ssh is not app_ssh:
            gpu_ssh.disconnect()
        return None, None

    # 启动性能采集（prefix="stress_" 区分文件名）
    monitor = None
    try:
        monitor = DockerMonitor(
            app_ssh, gpu_ssh,
            ",".join(mcfg.get("containers", [])),
            enable_log=False,
            enable_perf=True,
            run_dir=str(run_dir),
            perf_prefix="stress_",
            sample_callback=(
                (lambda label, sample: metric_callback(f"STRESS_{label}", sample))
                if metric_callback else None
            ),
        )
        monitor.start()
    except Exception as e:
        log.warning(f"压测监控启动失败：{e}")

    # 同步执行压测（阻塞等待完成）
    stress.run_sync(
        workers=scfg.get("workers", 0),
        cpu_load=scfg.get("cpu_load", 100),
        duration=scfg.get("duration", 300),
        run_dir=str(run_dir),
    )

    # 停止性能采集
    perf_csv_app = None
    perf_csv_gpu = None
    if monitor:
        monitor.stop_perf()
        monitor.stop_logs()

        # 找 perf CSV 路径（用于温度检查）
        for fname, var in [("stress_perf_app.csv", "app"), ("stress_perf_gpu.csv", "gpu")]:
            p = run_dir / "report" / fname
            if p.exists():
                if var == "app":
                    perf_csv_app = str(p)
                else:
                    perf_csv_gpu = str(p)

    # 健康检查 + 保存结果
    stress.check_health(perf_csv=perf_csv_app)
    stress.save_results()

    # 断开
    app_ssh.disconnect()
    if gpu_ssh is not app_ssh:
        gpu_ssh.disconnect()

    stress_summary = stress.summary()
    log.info(f"压测完成：{stress_summary['elapsed_s']}s "
             f"bogo_ops(real)={stress_summary['bogo_ops_real']} "
             f"健康：{stress._health.get('overall', '?')}")

    # 生成压测阶段图表
    generate_all(
        str(run_dir), batch_infos=[], file_number=0,
        timeout_count=0, fail_count=0, cn_success=0, cn_total=0,
        perf_csv_app=perf_csv_app, perf_csv_gpu=perf_csv_gpu, ext_fails={},
        stage_data=None,
        perf_chart_types={"cpu"},
    )

    return stress_summary, run_dir


def run_pipeline(progress_callback=None, metric_callback=None, run_id=None, options=None):
    """Execute one complete automation run and report major stage changes."""

    def progress(stage, message, percent):
        if progress_callback:
            progress_callback(stage, message, percent)
    options = options or {}
    prepare_runtime_layout()
    _cleanup_old_runs(RUNS_DIR)

    ts = time.strftime("%Y%m%d_%H%M%S")
    base_dir = RUNS_DIR / ts
    progress("preparing", "运行目录和配置已准备", 3)
    if options.get("host") not in (None, ""):
        config.HOST = str(options.get("host") or "").strip()

    # ===== 阶段一：CPU 压测（在翻译测试之前独立执行）=====
    scfg = _runtime_stress_cfg(options)
    stress_summary = None
    stress_run_dir = None
    if scfg.get("enable"):
        progress("stress", "正在执行 CPU 压力测试", 8)
        stress_summary, stress_run_dir = _run_stress_phase(scfg, base_dir, metric_callback, options)

    # ===== 阶段二：翻译导入测试 =====
    username = str(options.get("username") or "").strip()
    password = str(options.get("password") or "")
    if not username or not password:
        raise ValueError("业务系统账号和密码必须由本次任务授权")
    user_list = [{
        "username": username,
        "password": password,
        "upload_path": str(options.get("upload_path") or ""),
        "host": str(options.get("host") or ""),
        "gpu_host": str(options.get("gpu_host") or ""),
        "translate_name": str(options.get("translate_name") or ""),
        "analysis": options.get("analysis") or "",
        "ssh_user": str(options.get("ssh_user") or ""),
        "ssh_password": str(options.get("ssh_password") or ""),
        "gpu_ssh_user": str(options.get("gpu_ssh_user") or ""),
        "gpu_ssh_password": str(options.get("gpu_ssh_password") or ""),
    }]

    for i, u in enumerate(user_list):
        username = u.get("username", "")
        password = u.get("password", "")
        upload_path = u.get("upload_path", "")

        _apply_user_config(u, options)
        account_base = 12 + int(i * 78 / max(1, len(user_list)))
        account_span = 78 / max(1, len(user_list))
        account_progress = lambda ratio: account_base + int(account_span * ratio)
        progress("authentication", f"正在处理测试账号 {username}", account_base)

        log.info(f"========== 第 {i+1} 条数据 ==========")
        log.info(f"用户：{username}，上传路径：{upload_path}")
        log.info(f"配置：host={config.HOST} caseName={config.CASE_NAME} "
                 f"translateName={config.TRANSLATE_NAME} analysis={config.ANALYSIS}")

        # 1. 登录
        satoken = login(username, password)
        if not satoken:
            administrator = {
                "username": str(options.get("administrator_username") or "").strip(),
                "password": str(options.get("administrator_password") or ""),
            }
            if not administrator["username"] or not administrator["password"]:
                raise RuntimeError(
                    "业务账号登录失败；如需自动创建账号，请在本次任务中提供业务管理员授权"
                )
            log.warning(f"用户 {username} 无法登录，尝试使用本次管理员授权创建账号...")
            admin_token = login(administrator["username"], administrator["password"])
            if not admin_token:
                raise RuntimeError("业务管理员授权验证失败")
            if create_user(admin_token, username, password):
                log.info(f"用户 {username} 创建成功")
            else:
                raise RuntimeError(f"业务账号 {username} 创建失败")
            satoken = login(username, password)
            if not satoken:
                raise RuntimeError(f"业务账号 {username} 创建后仍无法登录")

        # 2. 确认案件
        try:
            resolved_case_id = resolve_case(satoken, config.CASE_NAME)
        except Exception as e:
            log.error(f"案件服务连接失败（端口 7999），跳过此用户：{e}")
            continue
        if not resolved_case_id:
            log.error("无法获取案件ID，跳过此用户")
            continue
        log.info(f"使用案件ID：{resolved_case_id}")

        # 3. 启动监控
        run_dir = base_dir / "translate"
        monitor = None
        mcfg = _runtime_monitor_cfg(options)
        app_srv, gpu_srv = _runtime_servers(
            config.HOST,
            options.get("ssh_user") or u.get("ssh_user", ""),
            options.get("ssh_password") or u.get("ssh_password", ""),
            options.get("gpu_host") or u.get("gpu_host", ""),
            options.get("gpu_ssh_user") or u.get("gpu_ssh_user", ""),
            options.get("gpu_ssh_password") or u.get("gpu_ssh_password", ""),
        )
        app_ssh = None
        gpu_ssh = None
        mon_app_ssh = None
        mon_gpu_ssh = None

        if app_srv.get("host"):
            log.info(
                f"监控/服务器直传 SSH 目标：app={app_srv.get('host')} "
                f"gpu={gpu_srv.get('host')} user={app_srv.get('user', 'root')}"
            )
            app_ssh = SSHClient(app_srv["host"], app_srv.get("user", "root"), app_srv.get("password", ""))
            if not app_ssh.connect():
                app_ssh = None
            gpu_same = (gpu_srv == app_srv)
            if gpu_same and app_ssh:
                gpu_ssh = app_ssh
            elif not gpu_same and gpu_srv.get("host"):
                gpu_ssh = SSHClient(gpu_srv["host"], gpu_srv.get("user", "root"), gpu_srv.get("password", ""))
                if not gpu_ssh.connect():
                    gpu_ssh = None

            # 监控用独立 SSH 连接，避免重连干扰上传
            if app_ssh:
                mon_app_ssh = SSHClient(app_srv["host"], app_srv.get("user", "root"), app_srv.get("password", ""))
                if not mon_app_ssh.connect():
                    mon_app_ssh = None
                if gpu_same and mon_app_ssh:
                    mon_gpu_ssh = mon_app_ssh
                elif not gpu_same and gpu_srv.get("host"):
                    mon_gpu_ssh = SSHClient(gpu_srv["host"], gpu_srv.get("user", "root"), gpu_srv.get("password", ""))
                    if not mon_gpu_ssh.connect():
                        mon_gpu_ssh = None

                if mon_app_ssh:
                    try:
                        monitor = DockerMonitor(
                            mon_app_ssh, mon_gpu_ssh,
                            ",".join(mcfg.get("containers", [])),
                            enable_log=mcfg.get("enable_log_monitor", False),
                            enable_perf=mcfg.get("enable_perf", False),
                            run_dir=str(run_dir),
                            sample_callback=metric_callback,
                        )
                        monitor.start()
                    except Exception as e:
                        log.warning(f"监控启动失败：{e}")
                else:
                    log.warning("监控 SSH 连接失败，跳过容器/性能监控")
            else:
                log.warning("业务服务器 SSH 连接失败，跳过服务器直传和容器/性能监控")

        # 4. 上传（根据配置决定本地上传 / 服务器直传 / 自动检测）
        progress("upload", f"正在上传测试账号 {username} 的文件", account_progress(0.15))
        t_start = time.time()
        t0 = time.time()
        ucfg = _runtime_upload_cfg(options)
        upload_mode = ucfg.get("mode", "auto")
        size_limit = ucfg.get("size_limit_mb", 100)

        _server_mode = (upload_mode == "server")
        if upload_mode == "auto" and app_ssh and app_ssh._client:
            import os as _os
            upload_dir = Path(upload_path)
            check_files = [upload_dir] if upload_dir.is_file() else list(upload_dir.rglob("*"))
            for f in check_files:
                if f.is_file() and f.stat().st_size > size_limit * 1024 * 1024:
                    _server_mode = True
                    log.info(f"检测到大文件 {f.name}（{f.stat().st_size / 1024 / 1024:.1f}MB），启用服务器直传模式")
                    break

        try:
            if _server_mode:
                if not app_ssh or not app_ssh._client:
                    raise RuntimeError("服务器直传需要可用的业务服务器 SSH 连接")
                batch_infos, uploaded_count, unsupported_count = upload_folder_server(
                    satoken, upload_path, resolved_case_id, app_ssh
                )
            else:
                batch_infos, uploaded_count, unsupported_count = upload_folder(
                    satoken, upload_path, resolved_case_id
                )
        except Exception as e:
            detail = _exception_detail(e)
            log.error(f"测试账号 {username} 上传失败：{detail}")
            _cleanup_failed_run_resources(
                monitor, mon_app_ssh, mon_gpu_ssh, app_ssh, gpu_ssh,
            )
            raise RuntimeError(f"测试账号 {username} 上传失败：{detail}") from e
        t1 = time.time()
        upload_sec = t1 - t0
        time.sleep(2)

        # 5~10. 服务器交互流程（每步独立保护，中断时保留已完成数据）
        server_error = False
        server_error_at = ""
        real_file_number = 0
        file_id_list = []
        translate_status_list = []
        translate_success_count = 0
        status_fails = []
        cn_fails = []
        fid_to_label = {}
        timeout_count = 0
        fail_count = 0
        consuming_sum = 0
        cn_success_count = 0
        summary_fails = []
        summary_success_count = 0
        export_results = []
        export_success_count = 0
        export_fail_count = 0
        ai_check_results = []

        # 5. 等待解析（返回已完成和卡住的批次）
        progress("parsing", "正在等待文件解析", account_progress(0.35))
        stuck_batch_info = []
        try:
            real_file_number, done_ul_ids, stuck_batch_info = wait_for_parse(satoken, batch_infos)
        except Exception as e:
            log.error(f"等待解析时异常：{e}")
            real_file_number, done_ul_ids, stuck_batch_info = 0, [], []
        if stuck_batch_info:
            stuck_count = sum(fn for _, fn in stuck_batch_info)
            log.warning(f"以下批次解析超时，跳过：{len(stuck_batch_info)} 个批次、{stuck_count} 个文件")
        t2 = time.time()

        # 6. 获取文件ID（仅已完成解析的批次）
        if done_ul_ids:
            try:
                file_id_list = get_file_ids(satoken, done_ul_ids)
            except Exception as e:
                log.error(f"获取文件ID时服务器连接中断：{e}")
                server_error = True
                server_error_at = "获取文件ID"
        else:
            log.warning("无可用批次（全部解析卡住），跳过翻译流程")

        # 7. 翻译轮询
        progress("translation", "正在等待翻译任务完成", account_progress(0.52))
        if not server_error and file_id_list:
            try:
                translate_success_list, translate_progress_final = poll_translate(
                    satoken, done_ul_ids, file_id_list
                )
                translate_success_count = len(translate_success_list)
            except Exception as e:
                log.error(f"翻译轮询时服务器连接中断：{e}")
                server_error = True
                server_error_at = "翻译轮询"
        t_translate_end = time.time()

        # 8. 翻译状态确认（含摘要检查）
        progress("verification", "正在校验翻译状态和摘要", account_progress(0.66))
        if not server_error and file_id_list:
            try:
                translate_status_list, status_fails, fid_to_label, fid_to_status, timeout_count, fail_count, summary_success_list, summary_fails = check_translate_status(
                    satoken, file_id_list
                )
                summary_success_count = len(summary_success_list)
            except Exception as e:
                log.error(f"翻译状态确认时服务器连接中断：{e}")
                server_error = True
                server_error_at = "翻译状态确认"

        # 9. consuming 汇总 + 总耗时计算
        total_sec = 0
        stuck_sec_count = 0
        stuck_names = []
        if not server_error and file_id_list:
            try:
                final_logs = api_list_upload_logs(config.HOST, satoken)
                final_by_id = {l.get("id"): l for l in final_logs}
                for b in batch_infos:
                    le = final_by_id.get(b["ul_id"], {})
                    c = le.get("consuming") or ""
                    sec = _parse_hhmmss(c) if c else 0
                    b["consuming_sec"] = sec
                    b["translate_sec"] = max(0, sec - b.get("parse_sec", 0))
                    if sec > 0:
                        consuming_sum += sec
                    else:
                        stuck_sec_count += 1
                        stuck_names.append(b.get("data_name", b.get("ul_id", "?")))

                completed_secs = [b["consuming_sec"] for b in batch_infos if b["consuming_sec"] > 0]
                total_sec = max(completed_secs) if completed_secs else 0
                if stuck_sec_count:
                    stuck_cost = int(t_translate_end - t_start)
                    total_sec = max(total_sec, stuck_cost)
                    log.warning(f"{stuck_sec_count} 个批次 consuming 为空（已耗时 {stuck_cost}s）：{', '.join(stuck_names)}")
            except Exception as e:
                log.error(f"consuming 汇总时服务器连接中断：{e}")
                server_error = True
                server_error_at = "consuming 汇总"

        # 10. 中文断言
        if not server_error and translate_status_list:
            try:
                cn_success_list, cn_fails = chinese_assertion(
                    satoken, translate_status_list, fid_to_label
                )
                cn_success_count = len(cn_success_list)
            except Exception as e:
                log.error(f"中文断言时服务器连接中断：{e}")
                server_error = True
                server_error_at = "中文断言"

        # 10.2. AI 功能接口检查（各项独立，单项失败不影响其他项）
        progress("ai_checks", "正在检查 AI 功能接口", account_progress(0.77))
        if not server_error:
            ai_check_results = run_ai_feature_checks(
                satoken, file_id_list, _runtime_ai_checks_cfg(options)
            )

        # 10.5. 导出批次
        progress("export", "正在检查导出任务", account_progress(0.84))
        ecfg = _runtime_export_cfg(options)
        if not server_error and ecfg.get("enable") and resolved_case_id:
            try:
                export_results, export_success_count, export_fail_count = run_export(
                    satoken, resolved_case_id, ecfg.get("types", ""),
                )
            except Exception as e:
                log.error(f"导出测试异常：{e}")

        # 11. 停止性能采集（先写 perf.csv 再汇总）
        if monitor:
            monitor.stop_perf()
            gpu_procs = monitor.get_gpu_processes()
            if gpu_procs:
                proc_path = run_dir / "report" / "gpu_processes.txt"
                proc_path.parent.mkdir(parents=True, exist_ok=True)
                with open(proc_path, "w", encoding="utf-8") as pf:
                    for label, text in gpu_procs.items():
                        pf.write(f"[{label}]\n{text}\n\n")

        # 12. 汇总
        progress("reporting", "正在生成测试汇总和图表", account_progress(0.90))
        perf_summary = monitor.get_perf_summary() if monitor else {}
        _fn = uploaded_count if server_error else real_file_number
        print_summary(_fn, unsupported_count, real_file_number,
                      status_fails, cn_fails,
                      len(translate_status_list), timeout_count, fail_count,
                      upload_sec, total_sec, stuck_sec_count,
                      batch_infos, run_dir=str(run_dir), perf_summary=perf_summary,
                      server_error=server_error, server_error_at=server_error_at,
                      file_id_count=len(file_id_list),
                      translate_success_count=translate_success_count,
                      cn_success_count=cn_success_count,
                      summary_fails=summary_fails,
                      summary_success_count=summary_success_count,
                      stuck_batch_info=stuck_batch_info,
                      stress_summary=stress_summary,
                      export_results=export_results,
                      export_success_count=export_success_count,
                      export_fail_count=export_fail_count,
                      ai_check_results=ai_check_results)

        # 13. 图表
        ext_fails = {}
        for _, label, _ in status_fails + cn_fails + summary_fails:
            ext = label.rsplit(".", 1)[-1].lower() if "." in label else "eml"
            ext_fails[ext] = ext_fails.get(ext, 0) + 1
        perf_csv_app = None
        perf_csv_gpu = None
        for fname, var in [("perf_app.csv", "app"), ("perf_gpu.csv", "gpu"), ("perf.csv", "app")]:
            p = run_dir / "report" / fname
            if p.exists():
                if var == "app":
                    perf_csv_app = str(p)
                else:
                    perf_csv_gpu = str(p)
        chart_file_number = len(translate_status_list) + len(status_fails)
        stuck_file_count = sum(fn for _, fn in stuck_batch_info)
        total_for_chart = max(real_file_number, chart_file_number + stuck_file_count)
        stage_data = {
            "total": total_for_chart,
            "parsed": len(file_id_list),
            "status_passed": len(translate_status_list),
            "status_failed": len(status_fails),
            "cn_passed": cn_success_count,
            "cn_failed": len(cn_fails),
            "summary_passed": summary_success_count,
            "summary_failed": len(summary_fails),
            "unstaged": stuck_file_count + (total_for_chart - len(file_id_list) - stuck_file_count),
        }
        generate_all(
            str(run_dir), batch_infos=batch_infos,
            file_number=chart_file_number,
            timeout_count=timeout_count, fail_count=fail_count,
            cn_success=cn_success_count,
            cn_total=len(translate_status_list),
            perf_csv_app=perf_csv_app, perf_csv_gpu=perf_csv_gpu, ext_fails=ext_fails,
            stage_data=stage_data,
        )

        # 14. 停止日志监控 + LLM
        if monitor:
            error_files = monitor.stop_logs()
            if mon_app_ssh:
                mon_app_ssh.disconnect()
            if mon_gpu_ssh and mon_gpu_ssh is not mon_app_ssh:
                mon_gpu_ssh.disconnect()
            if app_ssh:
                app_ssh.disconnect()
            if gpu_ssh and gpu_ssh is not app_ssh:
                gpu_ssh.disconnect()
            try:
                from auto_test.platform.persistence import create_platform_repository
                from auto_test.platform.model_access import ProjectModelStore
                from auto_test.common.paths import PROJECT_ROOT
                model_store = create_platform_repository(PROJECT_ROOT, recover_jobs=False)
                scoped_models = ProjectModelStore(model_store, str(options.get('_project_id') or ''), str(options.get('_created_by_user_id') or ''))
                llm = get_active_model_config(model_store, project_id=scoped_models.project_id)
            except ModelSecretError as exc:
                llm = {}
                log.warning(f"模型配置不可用，跳过错误日志智能分析：{exc}")
            if error_files and llm.get("api_key"):
                stress_summary = run_dir.parent / "stress" / "report" / "stress_summary.txt"
                analyze_errors(
                    error_files, llm["api_key"], llm["api_url"],
                    llm["model"], str(run_dir),
                    stress_summary=str(stress_summary) if stress_summary.exists() else None,
                    model_call=lambda prompt: scoped_models.call_model(llm, prompt, system_prompt='你是一个运维分析助手，请用中文简洁回答，输出格式使用 Markdown。', timeout=120),
                )


    return {
        "run_id": run_id,
        "timestamp": ts,
        "run_dir": str(base_dir),
        "account_count": len(user_list),
    }


def main():
    """CLI entry point with one-time credentials instead of YAML accounts."""
    import getpass
    import sys

    from auto_test.core.automation import AutomationService

    def value(name, prompt, *, secret=False, required=False):
        configured = str(get_env(name, "") or "")
        if configured:
            return configured
        if not sys.stdin.isatty():
            if required:
                raise RuntimeError(f"非交互运行必须设置 LIEMA_{name}")
            return ""
        entered = getpass.getpass(prompt) if secret else input(prompt)
        entered = entered.strip() if not secret else entered
        if required and not entered:
            raise RuntimeError(f"{prompt.rstrip('：: ')}不能为空")
        return entered

    options = {
        "username": value("RUN_USERNAME", "业务系统用户名：", required=True),
        "password": value("RUN_PASSWORD", "业务系统密码：", secret=True, required=True),
        "host": value("RUN_HOST", "业务服务器地址：", required=True),
        "ssh_user": value("RUN_SSH_USER", "SSH 用户（可留空）："),
        "ssh_password": value("RUN_SSH_PASSWORD", "SSH 密码（可留空）：", secret=True),
        "gpu_host": value("RUN_GPU_HOST", "独立 GPU 服务器（留空表示同机）："),
        "gpu_ssh_user": value("RUN_GPU_SSH_USER", "GPU SSH 用户（可留空）："),
        "gpu_ssh_password": value("RUN_GPU_SSH_PASSWORD", "GPU SSH 密码（可留空）：", secret=True),
        "upload_path": value("RUN_UPLOAD_PATH", "测试数据路径：", required=True),
        "case_name": value("RUN_CASE_NAME", "案件名称（可留空）："),
        "translate_name": value(
            "RUN_TRANSLATE_NAME",
            "翻译语种：",
            required=not bool(default_translate_name()),
        ) or default_translate_name(),
        "administrator_username": value("RUN_ADMIN_USERNAME", "业务管理员账号（可留空）："),
        "administrator_password": value("RUN_ADMIN_PASSWORD", "业务管理员密码（可留空）：", secret=True),
    }
    return AutomationService(pipeline=run_pipeline).run(options=options)

if __name__ == "__main__":
    main()
