import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from auto_test.common import runtime_config as config
from auto_test.common.logging import log
from auto_test.integrations.api import (list_upload_logs as api_list_upload_logs,
                 list_file_info as api_list_file_info,
                 get_upload_status as api_get_upload_status)


# 解析完成的终止状态
_PARSE_DONE_STATUSES = {"已完成", "解析完成"}
# 异常终止状态（文件损坏/无法解压等）
_PARSE_ERROR_STATUSES = {"解析失败", "失败"}
# 总超时：10 分钟
_PARSE_TIMEOUT_SEC = 600


def _parse_hhmmss(s):
    """将 "HH:MM:SS" 或 "00:00:59" 转为秒数"""
    if not s:
        return 0
    parts = s.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return 0


def wait_for_parse(satoken, batch_infos):
    """
    轮询解析状态，等待全部批次完成。
    返回 (total_files, done_ul_ids, stuck_info)
    """
    ul_id_map = {b["ul_id"]: b["data_name"] for b in batch_infos}
    target_ul_ids = set(ul_id_map.keys())
    log.info(f"开始等待解析完成，共 {len(target_ul_ids)} 个批次...")
    t_start = time.time()
    last_heartbeat = t_start
    last_statuses = {}
    batch_done_time = {}

    while True:
        elapsed = time.time() - t_start
        try:
            all_logs = api_list_upload_logs(config.HOST, satoken)
        except Exception as e:
            log.error(f"解析轮询时服务器连接中断：{e}")
            return 0, [], []

        our_logs = [l for l in all_logs if l.get("id") in target_ul_ids]

        all_done = True
        total_files = 0
        status_summary = []

        for log_entry in our_logs:
            status = log_entry.get("status", "")
            fn = int(log_entry.get("fileNumber", 0))
            ul_id = log_entry.get("id", "")
            label = ul_id_map.get(ul_id, ul_id)
            total_files += fn

            if status in _PARSE_DONE_STATUSES:
                status_summary.append(f"{label}=已完成({fn}文件)")
                if ul_id not in batch_done_time:
                    batch_done_time[ul_id] = elapsed
            elif status in _PARSE_ERROR_STATUSES:
                log.warning(f"[{label}] 异常：{status}，文件数：{fn}")
                status_summary.append(f"{label}={status}({fn}文件)")
            else:
                all_done = False
                prev = last_statuses.get(ul_id, "")
                if status != prev:
                    log.info(f"[{label}] 状态：{status}，文件数：{fn}")
                    last_statuses[ul_id] = status
                status_summary.append(f"{label}={status}({fn}文件)")

        if all_done:
            log.info(f"全部批次解析完成，服务端文件总数：{total_files}")
            svr_by_id = {l.get("id"): l for l in our_logs}
            for b in batch_infos:
                le = svr_by_id.get(b["ul_id"], {})
                b["svr_file_count"] = le.get("fileNumber", "?")
                b["parse_sec"] = int(batch_done_time.get(b["ul_id"], 0))
            return total_files, list(target_ul_ids), []

        # 超时兜底
        if elapsed > _PARSE_TIMEOUT_SEC:
            log.warning(f"解析等待超时（{_PARSE_TIMEOUT_SEC}s），使用已完成批次继续")
            done_ul_ids = list(batch_done_time.keys())
            stuck_info = []
            for uid in target_ul_ids - set(done_ul_ids):
                fn = 0
                for l in our_logs:
                    if l.get("id") == uid:
                        fn = int(l.get("fileNumber", 0))
                        break
                stuck_info.append((ul_id_map.get(uid, uid), fn))
            svr_by_id = {l.get("id"): l for l in our_logs}
            for b in batch_infos:
                le = svr_by_id.get(b["ul_id"], {})
                if b["ul_id"] in batch_done_time:
                    b["svr_file_count"] = le.get("fileNumber", "?")
                    b["parse_sec"] = int(batch_done_time[b["ul_id"]])
            return total_files, done_ul_ids, stuck_info

        # 每 30 秒心跳
        if time.time() - last_heartbeat >= 30:
            log.info(f"解析等待中… 已等待 {elapsed:.0f}s，"
                     f"已完成 {len(batch_done_time)}/{len(target_ul_ids)}，"
                     f"当前：{'; '.join(status_summary)}")
            last_heartbeat = time.time()

        time.sleep(5)


def _fetch_ids_for_ul(ul_id, satoken):
    """获取单个批次的文件ID列表，等待解析完成后拉取"""
    # 先查一次，已有结果直接返回
    file_ids = api_list_file_info(config.HOST, satoken, ul_id)
    if file_ids:
        log.info(f"批次 {ul_id} 获取到 {len(file_ids)} 个文件ID")
        return file_ids

    log.info(f"批次 {ul_id} 暂无文件ID，等待解析完成...")
    for attempt in range(60):
        time.sleep(10)
        status_info = api_get_upload_status(config.HOST, satoken, ul_id)
        st = status_info["status"]
        fn = status_info.get("fileNumber", 0)
        if st in _PARSE_DONE_STATUSES:
            file_ids = api_list_file_info(config.HOST, satoken, ul_id)
            if file_ids:
                log.info(f"批次 {ul_id} 解析完成，获取到 {len(file_ids)} 个文件ID")
                return file_ids
        if st in _PARSE_ERROR_STATUSES or (fn == 0 and st == "无文件"):
            log.warning(f"批次 {ul_id} 状态={st} 文件数={fn}，跳过")
            return []

    log.warning(f"批次 {ul_id} 等待10分钟后仍未获取到文件ID")
    return []


def get_file_ids(satoken, ul_ids, expected_count=0):
    """并发获取所有批次下所有文件的ID列表（带重试）"""
    log.info(f"获取文件ID列表，共 {len(ul_ids)} 个批次...")
    all_file_ids = []

    with ThreadPoolExecutor(max_workers=min(len(ul_ids), 10)) as executor:
        futures = {executor.submit(_fetch_ids_for_ul, uid, satoken): uid for uid in ul_ids}
        for future in as_completed(futures):
            all_file_ids.extend(future.result())

    log.info(f"共获取到 {len(all_file_ids)} 个文件ID")
    return all_file_ids


def poll_translate(satoken, ul_ids, file_id_list):
    """
    轮询所有批次翻译进度，直到全部完成或连续5次不变
    返回 (translate_success_list, translate_progress_final)
    """
    log.info(f"开始翻译轮询，共 {len(ul_ids)} 个批次...")
    total = len(file_id_list)
    success_list = []
    last_success_count = -1
    stable_count = 0

    while True:
        total_translate_progress = 0
        for ul_id in ul_ids:
            status = api_get_upload_status(config.HOST, satoken, ul_id)
            total_translate_progress += status["translateProgress"]

        for i in range(min(total_translate_progress, total)):
            fid = file_id_list[i]
            if fid not in success_list:
                success_list.append(fid)

        current_count = len(success_list)
        log.info(f"翻译进度：{current_count}/{total}")

        if total_translate_progress >= total:
            for fid in file_id_list:
                if fid not in success_list:
                    success_list.append(fid)
            log.info(f"翻译完成：{len(success_list)}/{total}")
            return success_list, total_translate_progress

        if current_count == last_success_count:
            stable_count += 1
        else:
            stable_count = 0

        if stable_count >= 5:
            log.warning("翻译成功数量连续5次未变化，停止轮询")
            return success_list, total_translate_progress

        last_success_count = current_count
        time.sleep(10)
