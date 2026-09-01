"""
导出测试模块 — 发起原文/译文导出 + 轮询状态，记录结果。
每种导出类型独立追踪进度，分别判断完成/超时/失败。
"""

import time

from auto_test.common import runtime_config as config
from auto_test.common.logging import log
from auto_test.integrations.api import export_translation, export_original, list_download_tasks

_MAX_NO_CHANGE = 5
_POLL_INTERVAL = 10
_UPLOAD_TIMEOUT = 300  # 文件100%后等待上传到minio的超时时间

_TYPE_MAP = {
    "batchDownloadsTranByUlIds": "译文",
    "exportEmailByCaseIds": "原文",
}


def run_export(satoken, case_id, export_types=""):
    types = [t.strip() for t in export_types.split(",") if t.strip()]
    if not types:
        return [], 0, 0

    before_ms = int(time.time() * 1000)

    for t in types:
        if t == "tran":
            export_translation(config.HOST, satoken, case_id)
        elif t == "original":
            export_original(config.HOST, satoken, case_id)
    time.sleep(2)

    type_state = {}  # etype → {last_success, no_change, done, full_since, result}
    log.info(f"开始轮询导出状态（{len(types)} 种类型）...")

    while True:
        try:
            all_tasks = list_download_tasks(config.HOST, satoken)
            tasks = [
                t for t in all_tasks
                if (t.get("date") or 0) > before_ms
                and case_id in (t.get("requestParams") or "")
            ]
        except Exception as e:
            log.error(f"导出状态轮询异常：{e}")
            break

        groups = {}
        for t in tasks:
            groups.setdefault(t.get("exportType", "?"), []).append(t)

        all_done = True
        for etype, etype_tasks in groups.items():
            if etype not in type_state:
                type_state[etype] = {
                    "last_success": -1, "no_change": 0,
                    "done": False, "full_since": None, "result": None,
                }
            st = type_state[etype]

            success_sum = sum(t.get("successNumber", 0) or 0 for t in etype_tasks)
            file_sum = sum(t.get("fileNumber", 0) or 0 for t in etype_tasks)
            label = _TYPE_MAP.get(etype, etype)
            statuses = {t.get("status", "") for t in etype_tasks}

            # 失败 → 立即结束
            if "失败" in statuses:
                log.warning(f"  {label}导出：{success_sum}/{file_sum} ❌ 失败")
                st["done"] = True
                st["result"] = "fail"
                continue

            # 完成 → 成功
            if "完成" in statuses:
                log.info(f"  {label}导出：{success_sum}/{file_sum} ✅ 完成")
                st["done"] = True
                st["result"] = "success"
                continue

            # 文件100%了但还在上传中 → 记录开始时间
            full = (file_sum > 0 and success_sum >= file_sum)
            if full and st["full_since"] is None:
                st["full_since"] = time.time()
                log.info(f"  {label}导出：{success_sum}/{file_sum}（文件全部就绪，等待上传...）")

            # 等待上传超时
            if st["full_since"] and time.time() - st["full_since"] > _UPLOAD_TIMEOUT:
                log.warning(f"  {label}导出：文件全部就绪但 {_UPLOAD_TIMEOUT}s 未完成上传，超时")
                st["done"] = True
                st["result"] = "timeout"
                continue

            # successNumber 变化检测
            if success_sum == st["last_success"]:
                st["no_change"] += 1
                if st["no_change"] >= _MAX_NO_CHANGE:
                    log.warning(f"  {label}导出：{success_sum}/{file_sum} 连续 {_MAX_NO_CHANGE} 次未变化，停止")
                    st["done"] = True
                    st["result"] = "timeout"
                    continue
            else:
                st["no_change"] = 0
            st["last_success"] = success_sum

            log.info(f"  {label}导出：{success_sum}/{file_sum}")
            all_done = False

        if all_done:
            break

        time.sleep(_POLL_INTERVAL)

    # 汇总
    result_list = []
    success_count = 0
    fail_count = 0
    for t in tasks:
        etype = t.get("exportType", "")
        st = type_state.get(etype, {})
        result = st.get("result", "timeout")
        status = t.get("status", "?")
        total = t.get("fileNumber", 0) or 0
        sn = t.get("successNumber", 0) or 0
        fn = t.get("failNumber", 0) or 0

        if result == "success":
            ok = True
            success_count += 1
        elif result == "fail":
            ok = False
            fail_count += 1
        else:
            ok = False
            fail_count += 1

        result_list.append({
            "task_name": t.get("taskName", ""),
            "export_type": etype,
            "status": status,
            "file_number": total,
            "success_number": sn,
            "fail_number": fn,
            "ok": ok,
            "result": result,
        })

    log.info(f"导出测试完成：成功 {success_count}，失败 {fail_count}")
    return result_list, success_count, fail_count
