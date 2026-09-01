import time
from contextlib import ExitStack
from pathlib import Path

from auto_test.common import runtime_config as config
from auto_test.common.logging import log
from auto_test.common.runtime_config import FILES_PER_BATCH, SUPPORTED_EXTENSIONS, COMPRESSED_EXTENSIONS
from auto_test.integrations.api import upload_files as api_upload_files


def _is_supported(file_path):
    """检查文件扩展名是否在支持列表中（优先匹配复合扩展名如 .tar.gz）"""
    name = file_path.name.lower()
    for ext in sorted(SUPPORTED_EXTENSIONS, key=len, reverse=True):
        if name.endswith(ext):
            return True
    return False


def _is_compressed(file_path):
    """检查文件是否为压缩包/归档类型"""
    name = file_path.name.lower()
    for ext in sorted(COMPRESSED_EXTENSIONS, key=len, reverse=True):
        if name.endswith(ext):
            return True
    return False


def _collect_supported(path, desc):
    """收集目录下所有支持的文件，返回 (supported_files, unsupported_count)"""
    all_files = [f for f in path.rglob("*") if f.is_file()]
    supported = []
    unsupported = 0
    for f in all_files:
        if _is_supported(f):
            supported.append(f)
        else:
            unsupported += 1
            log.warning(f"[{desc}] 不支持的文件类型，跳过：{f.name}")
    return supported, unsupported


def _upload_one_batch(satoken, case_id, data_name, file_list):
    """上传一批文件，返回 batch_info dict（含上传耗时）"""
    t0 = time.time()
    # File handles stay open only for the request, avoiding a full batch in memory.
    with ExitStack() as stack:
        upload_files = [(f.name, stack.enter_context(open(f, "rb"))) for f in file_list]
        ul_id, file_number, dn = api_upload_files(
            config.HOST, satoken, case_id, config.CASE_NAME,
            data_name, config.TRANSLATE_NAME, config.ANALYSIS,
            upload_files,
        )
    upload_sec = time.time() - t0
    return {
        "ul_id": ul_id, "uploaded_count": len(file_list),
        "data_name": dn, "upload_sec": upload_sec,
    }


def _upload_groups(satoken, case_id, base_data_name, groups):
    """将分组逐批上传，返回 batch_infos 列表"""
    batch_infos = []
    total_sub_batches = sum(
        (len(files) + FILES_PER_BATCH - 1) // FILES_PER_BATCH
        for _, files in groups
    )
    batch_no = 0

    for desc, group_files in groups:
        chunk_count = (len(group_files) + FILES_PER_BATCH - 1) // FILES_PER_BATCH
        for ci in range(chunk_count):
            chunk_files = group_files[ci * FILES_PER_BATCH:(ci + 1) * FILES_PER_BATCH]
            batch_no += 1
            chunk_label = f"{desc}[{ci + 1}/{chunk_count}]" if chunk_count > 1 else desc
            log.info(f"上传第 {batch_no}/{total_sub_batches} 批 [{chunk_label}]，{len(chunk_files)} 个文件")

            info = _upload_one_batch(satoken, case_id, f"{base_data_name}#{batch_no}", chunk_files)
            info["desc"] = chunk_label
            batch_infos.append(info)
            log.info(f"第 {batch_no} 批 [{chunk_label}] 上传成功，ulId={info['ul_id']}")

    return batch_infos


def upload_folder(satoken, upload_path, case_id):
    """
    上传策略：
    - 普通文件 → 按子文件夹分组上传，dataName = 当天日期
    - 压缩包（zip/rar/7z/tar/pst/ost 等）→ 每个文件单独一批，dataName = 时间戳_文件名
    不支持的文件类型会被自动跳过。
    返回 (batch_infos, total_uploaded, unsupported_count)
      batch_infos: [{"ul_id": str, "uploaded_count": int, "data_name": str, "desc": str}, ...]
    """
    log.info(f"上传路径：{upload_path}")

    upload_dir = Path(upload_path)
    if not upload_dir.exists():
        raise FileNotFoundError(f"上传路径不存在：{upload_path}")

    # 单个文件 → 当作压缩包处理
    if upload_dir.is_file():
        if not _is_supported(upload_dir):
            raise RuntimeError(f"不支持的文件类型：{upload_dir.name}")
        cf_data_name = f"{time.strftime('%Y%m%d%H%M%S')}_{upload_dir.name}"
        info = _upload_one_batch(satoken, case_id, cf_data_name, [upload_dir])
        info["desc"] = f"[单文件] {upload_dir.name}"
        log.info(f"单文件上传成功：{upload_dir.name}，ulId={info['ul_id']}")
        return [info], 1, 0

    if not upload_dir.is_dir():
        raise FileNotFoundError(f"上传路径不是目录也不是支持的单个文件：{upload_path}")

    total_unsupported = 0

    # 收集所有支持的文件
    root_raw = [f for f in upload_dir.iterdir() if f.is_file()]
    all_regular = []
    all_compressed = []

    for f in root_raw:
        if not _is_supported(f):
            total_unsupported += 1
            log.warning(f"[根目录] 不支持的文件类型，跳过：{f.name}")
        elif _is_compressed(f):
            all_compressed.append(("根目录压缩包", f))
        else:
            all_regular.append(f)

    subdirs = [d for d in upload_dir.iterdir() if d.is_dir()]
    for subdir in subdirs:
        subdir_files, sub_unsupported = _collect_supported(subdir, subdir.name)
        total_unsupported += sub_unsupported
        for f in subdir_files:
            if _is_compressed(f):
                all_compressed.append((subdir.name, f))
            else:
                all_regular.append(f)

    # 构建普通文件分组（按所在子目录）
    groups = []
    if all_regular:
        groups.append(("根目录文件", all_regular))
    # 注意：这里把常规文件按根目录打了一个包，不再按子目录拆分
    # 如果需要子目录分开，可以改为按父目录分组，但当前简化处理

    total_regular = len(all_regular)
    total_compressed = len(all_compressed)
    log.info(f"共 {total_regular} 个普通文件 + {total_compressed} 个压缩包"
             f"（{len(subdirs)} 个子文件夹）")
    if total_unsupported > 0:
        log.info(f"跳过了 {total_unsupported} 个不支持的文件类型")

    all_batch_infos = []

    # 上传普通文件
    base_data_name = time.strftime("%Y%m%d")
    if groups:
        all_batch_infos.extend(_upload_groups(satoken, case_id, base_data_name, groups))

    # 上传压缩包 — 每个压缩包单独一批，dataName = 时间_文件名
    for origin, cf in all_compressed:
        ts = time.strftime("%Y%m%d%H%M%S")
        cf_data_name = f"{ts}_{cf.name}"
        log.info(f"上传压缩包 [{origin}]：{cf.name}，dataName={cf_data_name}")
        info = _upload_one_batch(satoken, case_id, cf_data_name, [cf])
        info["desc"] = f"[压缩包] {cf.name}"
        all_batch_infos.append(info)
        log.info(f"压缩包上传成功：{cf.name}，ulId={info['ul_id']}")

    total_uploaded = sum(b["uploaded_count"] for b in all_batch_infos)
    compressed_count = sum(1 for b in all_batch_infos if "[压缩包]" in b.get("desc", ""))
    if compressed_count:
        log.info(f"全部上传完成，共 {len(all_batch_infos)} 批（含 {compressed_count} 个压缩包），"
                 f"本次上传 {total_uploaded} 个文件/压缩包，"
                 f"压缩包解压后文件数待解析确认")
    else:
        log.info(f"全部上传完成，共 {len(all_batch_infos)} 批，上传文件数 {total_uploaded}")
    if total_unsupported:
        log.info(f"跳过不支持类型 {total_unsupported} 个")
    return all_batch_infos, total_uploaded, total_unsupported


def _progress_logger(label, total_bytes):
    """Return a throttled Paramiko/hash progress callback."""
    total_bytes = max(0, int(total_bytes or 0))
    last_percent = -5
    last_logged_at = 0.0

    def report(transferred, callback_total=None):
        nonlocal last_percent, last_logged_at
        total = max(total_bytes, int(callback_total or 0))
        if total <= 0:
            return
        percent = min(100, int(int(transferred or 0) * 100 / total))
        now = time.monotonic()
        if percent < 100 and percent < last_percent + 5 and now - last_logged_at < 15:
            return
        last_percent = percent
        last_logged_at = now
        log.info(f"{label}：{percent}%（{int(transferred or 0) / 1024 / 1024:.1f}/{total / 1024 / 1024:.1f} MB）")

    return report


def _checksum_timeout(file_size):
    """Give large remote checksums enough time without allowing an endless command."""
    mib = max(0, int(file_size or 0)) / 1024 / 1024
    return max(300, min(3600, int(mib / 8) + 1))


def _local_md5(filepath, progress_callback=None):
    """计算本地文件MD5，并可报告大文件读取进度。"""
    import hashlib
    h = hashlib.md5()
    processed = 0
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
            processed += len(chunk)
            if progress_callback:
                progress_callback(processed)
    return h.hexdigest()


def _remote_md5(app_ssh, remote_path, file_size):
    """Calculate a remote checksum with a size-aware timeout."""
    import shlex
    import threading

    timeout = _checksum_timeout(file_size)
    log.info(
        f"正在校验服务器已有文件：{Path(remote_path).name}，"
        f"允许最长 {timeout} 秒"
    )
    finished = threading.Event()

    def report_waiting():
        elapsed = 0
        while not finished.wait(30):
            elapsed += 30
            log.info(f"服务器已有文件仍在校验：{Path(remote_path).name}，已等待 {elapsed} 秒")

    reporter = threading.Thread(target=report_waiting, name="liema-remote-md5-progress", daemon=True)
    reporter.start()
    try:
        output, error = app_ssh.exec(
            f"md5sum {shlex.quote(remote_path)} | awk '{{print $1}}'",
            timeout=timeout,
        )
    finally:
        finished.set()
        reporter.join(timeout=1)
    digest = output.strip().splitlines()[0] if output.strip() else ""
    if len(digest) != 32:
        detail = error.strip() or "服务器未返回有效 MD5"
        raise RuntimeError(f"服务器文件校验失败：{detail}")
    return digest


def _sftp_put_with_progress(sftp, local_path, remote_path):
    """Upload one file and emit throttled progress visible in the live log."""
    size = local_path.stat().st_size
    label = f"正在传输 {local_path.name}"
    log.info(f"{label}（{size / 1024 / 1024:.1f} MB）")
    sftp.put(
        str(local_path),
        remote_path,
        callback=_progress_logger(label, size),
    )


def upload_folder_server(satoken, upload_path, case_id, app_ssh):
    """
    服务器直传模式：同一天重复执行时，通过MD5比对跳过已存在的文件，避免重复传输。
    返回 (batch_infos, total_uploaded, unsupported_count)
    """
    from auto_test.integrations.api import check_upload_disk as api_check_disk, listen_multi_fast_upload as api_fast_upload
    upload_dir = Path(upload_path)
    if not upload_dir.exists():
        raise FileNotFoundError(f"上传路径不存在：{upload_path}")

    # 单个文件 → 服务端直传
    if upload_dir.is_file():
        if not _is_supported(upload_dir):
            raise RuntimeError(f"不支持的文件类型：{upload_dir.name}")

        disk = api_check_disk(config.HOST, satoken)
        if not disk.get("allowed"):
            raise RuntimeError(f"服务器磁盘不足：已用 {disk.get('usagePercent')}%")

        today_prefix = f"测试{time.strftime('%Y%m%d')}"
        base_dir = f"/home/sftp/{today_prefix}{time.strftime('%H%M%S')}"
        existing, _ = app_ssh.exec(f"ls -d /home/sftp/{today_prefix}* 2>/dev/null | head -1")
        if existing.strip():
            base_dir = existing.strip()
            log.info(f"复用已有服务器目录：{base_dir}")
        else:
            app_ssh.exec(f"mkdir -p '{base_dir}'")

        remote_path = f"{base_dir}/{upload_dir.name}"
        # MD5 比对跳过
        sftp = app_ssh._client.open_sftp()
        try:
            skip = False
            try:
                if sftp.stat(remote_path).st_size == upload_dir.stat().st_size:
                    remote_md5 = _remote_md5(app_ssh, remote_path, upload_dir.stat().st_size)
                    local_md5 = _local_md5(
                        str(upload_dir),
                        _progress_logger(f"正在校验本机文件 {upload_dir.name}", upload_dir.stat().st_size),
                    )
                    if remote_md5 == local_md5:
                        skip = True
                        log.info(f"文件已存在且 MD5 一致，跳过重复传输：{upload_dir.name}")
                    else:
                        log.info(f"服务器同名文件 MD5 不一致，将重新传输：{upload_dir.name}")
            except FileNotFoundError:
                pass

            if not skip:
                _sftp_put_with_progress(sftp, upload_dir, remote_path)
            remote_size = sftp.stat(remote_path).st_size
            if remote_size != upload_dir.stat().st_size:
                raise RuntimeError(
                    f"服务器文件大小校验失败：本机 {upload_dir.stat().st_size} 字节，"
                    f"服务器 {remote_size} 字节"
                )
        finally:
            sftp.close()

        data_name = f"{time.strftime('%Y%m%d%H%M%S')}_{upload_dir.name}"
        ul_id, fn, dn = api_fast_upload(
            config.HOST, satoken, case_id, config.CASE_NAME,
            data_name, base_dir, config.TRANSLATE_NAME, config.ANALYSIS,
        )
        log.info(f"单文件导入成功，ulId={ul_id}")
        return [{"ul_id": ul_id, "uploaded_count": 1, "data_name": dn or data_name,
                 "upload_sec": 0, "desc": f"[服务器单文件] {upload_dir.name}"}], 1, 0

    if not upload_dir.is_dir():
        raise FileNotFoundError(f"上传路径不是目录也不是支持的单个文件：{upload_path}")

    # 检查磁盘
    disk = api_check_disk(config.HOST, satoken)
    if not disk.get("allowed"):
        raise RuntimeError(f"服务器磁盘不足：已用 {disk.get('usagePercent')}%")

    # 查找今天已有的目录，有则复用
    today_prefix = f"测试{time.strftime('%Y%m%d')}"
    base_dir = f"/home/sftp/{today_prefix}{time.strftime('%H%M%S')}"
    existing, _ = app_ssh.exec(f"ls -d /home/sftp/{today_prefix}* 2>/dev/null | head -1")
    if existing.strip():
        base_dir = existing.strip()
        log.info(f"复用已有服务器目录：{base_dir}")
    else:
        app_ssh.exec(f"mkdir -p '{base_dir}'")
        log.info(f"创建服务器目录：{base_dir}")

    sftp = app_ssh._client.open_sftp()
    all_batch_infos = []
    total_uploaded = 0
    total_unsupported = 0
    skipped_count = 0

    def _upload_file(local_path, remote_path):
        """上传单个文件，MD5匹配则跳过"""
        nonlocal skipped_count
        local_size = local_path.stat().st_size

        # 检查远程文件MD5
        try:
            remote_stat = sftp.stat(remote_path)
            if remote_stat.st_size == local_size:
                remote_md5 = _remote_md5(app_ssh, remote_path, local_size)
                local_md5 = _local_md5(
                    str(local_path),
                    _progress_logger(f"正在校验本机文件 {local_path.name}", local_size),
                )
                if remote_md5 == local_md5:
                    skipped_count += 1
                    return True  # 已存在且MD5一致，跳过
        except FileNotFoundError:
            pass  # 远程文件不存在，正常上传

        _sftp_put_with_progress(sftp, local_path, remote_path)
        if sftp.stat(remote_path).st_size == local_size:
            return True
        return False

    def _process_dir(dir_path, dir_name):
        """处理一个子目录，返回 (success, failed_list)"""
        all_files = [f for f in dir_path.rglob("*") if f.is_file()]
        if not all_files:
            return 0, []

        supported = [f for f in all_files if _is_supported(f)]
        unsup_count = len(all_files) - len(supported)
        for f in all_files:
            if not _is_supported(f):
                log.warning(f"[{dir_name}] 不支持的文件类型，跳过：{f.name}")

        if not supported:
            return 0, [], unsup_count

        remote_subdir = f"{base_dir}/{dir_name}"
        app_ssh.exec(f"mkdir -p '{remote_subdir}'")

        log.info(f"[{dir_name}] → {remote_subdir}，{len(supported)} 个文件")
        success, failed = 0, []
        for f in supported:
            rel_path = str(f.relative_to(dir_path)).replace("\\", "/")
            remote_path = f"{remote_subdir}/{rel_path}"
            # 确保子目录存在
            remote_parent = "/".join(remote_path.split("/")[:-1])
            try:
                sftp.stat(remote_parent)
            except FileNotFoundError:
                app_ssh.exec(f"mkdir -p '{remote_parent}'")
            if _upload_file(f, remote_path):
                success += 1
            else:
                failed.append(f.name)

        if failed:
            log.warning(f"[{dir_name}] 传输异常：{failed[:5]}")
        log.info(f"[{dir_name}] 完成：{success}/{len(supported)}（跳过 {skipped_count}）")
        return success, failed, unsup_count

    # 子目录
    subdirs = [d for d in upload_dir.iterdir() if d.is_dir()]
    for subdir in subdirs:
        subdir_name = subdir.name
        s_succ, s_fail, unsup = _process_dir(subdir, subdir_name)
        total_unsupported += unsup
        if not s_succ and not s_fail:
            continue

        # 调服务器直传接口
        data_name = f"{time.strftime('%Y%m%d%H%M%S')}_{subdir_name}"
        ul_id, fn, dn = api_fast_upload(
            config.HOST, satoken, case_id, config.CASE_NAME,
            data_name, f"{base_dir}/{subdir_name}", config.TRANSLATE_NAME, config.ANALYSIS,
        )
        all_batch_infos.append({
            "ul_id": ul_id, "uploaded_count": s_succ,
            "data_name": dn or data_name, "upload_sec": 0,
            "desc": f"[服务器] {subdir_name}",
        })
        total_uploaded += s_succ
        log.info(f"[{subdir_name}] 导入成功，ulId={ul_id}")

    # 根目录文件
    root_files = [f for f in upload_dir.iterdir() if f.is_file()]
    root_supported = [f for f in root_files if _is_supported(f)]
    total_unsupported += len(root_files) - len(root_supported)
    for f in root_files:
        if not _is_supported(f):
            log.warning(f"[根目录] 不支持的文件类型，跳过：{f.name}")

    if root_supported:
        dir_name = "根目录"
        remote_root = f"{base_dir}/{dir_name}"
        app_ssh.exec(f"mkdir -p '{remote_root}'")

        log.info(f"[{dir_name}] → {remote_root}，{len(root_supported)} 个文件")
        s_succ, s_fail = 0, []
        for f in root_supported:
            remote_path = f"{remote_root}/{f.name}"
            if _upload_file(f, remote_path):
                s_succ += 1
            else:
                s_fail.append(f.name)

        if s_fail:
            log.warning(f"[{dir_name}] 传输异常：{s_fail[:5]}")
        log.info(f"[{dir_name}] 完成：{s_succ}/{len(root_supported)}（跳过 {skipped_count}）")

        data_name = f"{time.strftime('%Y%m%d%H%M%S')}_根目录"
        ul_id, fn, dn = api_fast_upload(
            config.HOST, satoken, case_id, config.CASE_NAME,
            data_name, remote_root, config.TRANSLATE_NAME, config.ANALYSIS,
        )
        all_batch_infos.append({
            "ul_id": ul_id, "uploaded_count": s_succ,
            "data_name": dn or data_name, "upload_sec": 0,
            "desc": "[服务器] 根目录",
        })
        total_uploaded += s_succ
        log.info(f"[{dir_name}] 导入成功，ulId={ul_id}")

    sftp.close()
    skipped_msg = f"，跳过 {skipped_count} 个已存在文件" if skipped_count else ""
    log.info(f"服务器直传完成，共 {len(all_batch_infos)} 批，上传 {total_uploaded} 个文件{skipped_msg}")
    return all_batch_infos, total_uploaded, total_unsupported
