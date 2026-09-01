import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from auto_test.common import runtime_config as config
from auto_test.common.logging import log
from auto_test.integrations.api import get_file_detail as api_get_file_detail, get_translate_content as api_get_translate_content


def _summary_ok(detail):
    s = detail.get("summary")
    return s is not None and str(s).strip()


def _check_one_status(satoken, fid):
    """检查单个文件翻译状态+摘要"""
    detail = api_get_file_detail(config.HOST, satoken, fid)
    status = detail["translateStatus"].strip()
    name = detail["fileName"] or fid
    path = detail["filePath"] or ""
    label = f"{path}/{name}" if path else name

    if status == "已完成" and _summary_ok(detail):
        return (fid, label, True, "", "", True)

    # 漏网之鱼，短暂等待（翻译 或 翻译已完成但摘要未生成）
    if status in ("翻译中", "上传中") or (status == "已完成" and not _summary_ok(detail)):
        for retry in range(3):
            time.sleep(5)
            detail = api_get_file_detail(config.HOST, satoken, fid)
            status = detail["translateStatus"].strip()
            if status == "已完成" and _summary_ok(detail):
                return (fid, label, True, "", "", True)
            if status not in ("翻译中", "上传中") and (status != "已完成" or _summary_ok(detail)):
                break

    s_ok = _summary_ok(detail) if status == "已完成" else False
    if status == "已完成":
        return (fid, label, True, "", "", s_ok)
    elif status == "失败":
        return (fid, label, False, "翻译失败", "fail", s_ok)
    elif status in ("翻译中", "上传中"):
        return (fid, label, False, "翻译超时", "timeout", s_ok)
    else:
        return (fid, label, False, f"翻译超时（{status}）", "timeout", s_ok)


def check_translate_status(satoken, file_ids):
    """
    并发检查翻译状态+摘要，"已完成"为成功，"翻译中"会重试等待。
    返回 (success_list, fail_details, fid_to_label, fid_to_status,
           timeout_count, fail_count, summary_success_list, summary_fails)
    """
    total = len(file_ids)
    log.info(f"检查翻译状态（含摘要）… 共 {total} 个文件")
    success_list = []
    fail_details = []
    summary_success_list = []
    summary_fails = []
    fid_to_label = {}
    fid_to_status = {}
    timeout_count = 0
    fail_count = 0
    done = 0

    with ThreadPoolExecutor(max_workers=min(total, 10)) as executor:
        futures = {executor.submit(_check_one_status, satoken, fid): fid for fid in file_ids}
        for future in as_completed(futures):
            fid, label, is_done, reason, category, s_ok = future.result()
            done += 1
            fid_to_label[fid] = label
            fid_to_status[fid] = reason if not is_done else "已完成"
            if is_done:
                success_list.append(fid)
            else:
                fail_details.append((fid, label, reason))
                if category == "timeout":
                    timeout_count += 1
                else:
                    fail_count += 1
                log.warning(f"翻译状态失败：{label}  {reason}")

            if s_ok:
                summary_success_list.append(fid)
            else:
                summary_fails.append((fid, label, "摘要未生成"))

            if done % max(total // 5, 1) == 0 or done == total:
                log.info(f"翻译状态检查进度：{done}/{total}，成功：{len(success_list)}，摘要：{len(summary_success_list)}")

    log.info(f"翻译状态检查完成：成功 {len(success_list)}，超时 {timeout_count}，失败 {fail_count}，摘要 {len(summary_success_list)}/{total}")
    return success_list, fail_details, fid_to_label, fid_to_status, timeout_count, fail_count, summary_success_list, summary_fails


def _assert_one_chinese(satoken, fid, label):
    """中文断言单个文件，供线程池调用"""
    content = api_get_translate_content(config.HOST, satoken, fid)
    text = content["translateContent"]

    if not text or not text.strip():
        return (fid, label, False, "内容为空")

    clean_text = re.sub(r"[\s\d\Wa-zA-Z_]", "", text)
    if not clean_text:
        return (fid, label, False, "过滤后无中文字符")

    chinese_pattern = re.compile(r"[一-龥]")
    chinese_count = len(chinese_pattern.findall(clean_text))
    total_count = len(clean_text)
    ratio = chinese_count / total_count if total_count > 0 else 0

    if ratio >= 0.9:
        return (fid, label, True, "")
    else:
        return (fid, label, False, f"中文占比仅 {ratio:.1%}")


def _check_one_summary(satoken, fid, label):
    """检查单个文件摘要是否生成，供线程池调用"""
    detail = api_get_file_detail(config.HOST, satoken, fid)
    summary = detail.get("summary")
    if summary is not None and str(summary).strip():
        return (fid, label, True, "")
    else:
        return (fid, label, False, "摘要未生成")


def check_summary(satoken, file_ids, fid_to_label):
    """并发检查翻译状态成功的文件，看摘要是否生成"""
    total = len(file_ids)
    log.info(f"摘要检查… 共 {total} 个文件")
    success_list = []
    fail_details = []
    done = 0

    with ThreadPoolExecutor(max_workers=min(total, 10)) as executor:
        futures = {
            executor.submit(_check_one_summary, satoken, fid, fid_to_label.get(fid, fid)): fid
            for fid in file_ids
        }
        for future in as_completed(futures):
            fid, label, success, reason = future.result()
            done += 1
            if success:
                success_list.append(fid)
            else:
                fail_details.append((fid, label, reason))
                log.warning(f"摘要检查失败：{label}  {reason}")
            if done % max(total // 5, 1) == 0 or done == total:
                log.info(f"摘要检查进度：{done}/{total}，成功：{len(success_list)}")

    log.info(f"摘要检查完成：成功 {len(success_list)}，失败 {len(fail_details)}")
    return success_list, fail_details


def chinese_assertion(satoken, file_ids, fid_to_label):
    """
    并发检查翻译结果中文占比>=90%
    返回 (success_list, fail_details)
    """
    total = len(file_ids)
    log.info(f"中文断言… 共 {total} 个文件")
    success_list = []
    fail_details = []
    done = 0

    with ThreadPoolExecutor(max_workers=min(total, 10)) as executor:
        futures = {
            executor.submit(_assert_one_chinese, satoken, fid, fid_to_label.get(fid, fid)): fid
            for fid in file_ids
        }
        for future in as_completed(futures):
            fid, label, success, reason = future.result()
            done += 1
            if success:
                success_list.append(fid)
            else:
                fail_details.append((fid, label, reason))
                log.warning(f"中文断言失败：{label}  {reason}")

            if done % max(total // 5, 1) == 0 or done == total:
                log.info(f"中文断言进度：{done}/{total}，成功：{len(success_list)}")

    log.info(f"中文断言完成：成功 {len(success_list)}，失败 {len(fail_details)}")
    return success_list, fail_details
