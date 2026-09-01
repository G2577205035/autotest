import time
from collections import Counter
from pathlib import Path

from auto_test.common import runtime_config as config
from auto_test.common.logging import log

def _file_ext(label):
    """从文件路径提取真实扩展名，无后缀的默认为邮件类型"""
    name = label.rsplit("/", 1)[-1] if "/" in label else label
    if "." in name:
        ext = name.rsplit(".", 1)[-1].lower()
        if len(ext) <= 10 and " " not in ext:
            return ext
    return "eml"


def _write_report(run_dir, lines):
    """将汇总内容写入 run_report.txt"""
    report_dir = Path(run_dir) / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    filepath = report_dir / "run_report.txt"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log.info(f"测试报告已保存：{filepath}")


def _build_failure_section(status_fails, cn_fails, summary_fails, stuck_batch_info):
    """构建失败文件明细，返回 (日志摘要, 报告明细)"""
    sf = summary_fails or []
    stuck = stuck_batch_info or []
    total = len(status_fails) + len(cn_fails) + len(sf) + sum(fn for _, fn in stuck)
    if total == 0:
        return [], []

    summary = []
    detail = []
    header = ["-" * 72, f"失败文件明细（共 {total} 个）", "-" * 72]
    summary += header
    detail += header

    # 解析卡住
    if stuck:
        stuck_count = sum(fn for _, fn in stuck)
        summary.append(f"  [解析卡住] {len(stuck)} 个批次、{stuck_count} 个文件")
        detail.append(f"  [解析卡住] {len(stuck)} 个批次、{stuck_count} 个文件")
        for label, fn in stuck:
            detail.append(f"    批次：{label}（{fn} 个文件）")

    # 翻译状态失败
    if status_fails:
        ext = Counter()
        reason_ct = Counter()
        for fid, label, reason in status_fails:
            ext[_file_ext(label)] += 1
            reason_ct[reason] += 1
        info = f"  [翻译状态失败] {len(status_fails)} 个"
        summary.append(info)
        summary.append(f"    文件类型：{', '.join(f'.{k}({v})' for k, v in ext.most_common())}")
        detail.append(info)
        detail.append(f"    文件类型：{', '.join(f'.{k}({v})' for k, v in ext.most_common())}")
        for fid, label, reason in status_fails:
            name = label.rsplit("/", 1)[-1] if "/" in label else label
            detail.append(f"    {name}  [{_file_ext(label)}]  {reason}")

    # 中文断言失败
    if cn_fails:
        ext = Counter()
        for fid, label, reason in cn_fails:
            ext[_file_ext(label)] += 1
        info = f"  [中文断言失败] {len(cn_fails)} 个"
        summary.append(info)
        summary.append(f"    文件类型：{', '.join(f'.{k}({v})' for k, v in ext.most_common())}")
        detail.append(info)
        detail.append(f"    文件类型：{', '.join(f'.{k}({v})' for k, v in ext.most_common())}")
        for fid, label, reason in cn_fails:
            name = label.rsplit("/", 1)[-1] if "/" in label else label
            detail.append(f"    {name}  [{_file_ext(label)}]  {reason}")

    # 摘要失败
    if sf:
        ext = Counter()
        for fid, label, reason in sf:
            ext[_file_ext(label)] += 1
        info = f"  [摘要未生成] {len(sf)} 个"
        summary.append(info)
        summary.append(f"    文件类型：{', '.join(f'.{k}({v})' for k, v in ext.most_common())}")
        detail.append(info)
        detail.append(f"    文件类型：{', '.join(f'.{k}({v})' for k, v in ext.most_common())}")
        for fid, label, reason in sf:
            name = label.rsplit("/", 1)[-1] if "/" in label else label
            detail.append(f"    {name}  [{_file_ext(label)}]")

    return summary, detail


def print_summary(file_number, unsupported_count, parse_success,
                  status_fails, cn_fails,
                  cn_input_count, timeout_count, fail_count,
                  upload_sec, total_sec, stuck_sec_count=0,
                  batch_infos=None, run_dir=None, perf_summary=None,
                  server_error=False, server_error_at="",
                  file_id_count=0, translate_success_count=0, cn_success_count=0,
                  summary_fails=None, summary_success_count=0,
                  stuck_batch_info=None,
                  stress_summary=None,
                  export_results=None,
                  export_success_count=0,
                  export_fail_count=0,
                  ai_check_results=None):
    """打印全流程结果汇总，输出到日志和 run_report.txt"""

    def pct(a, b):
        return f"{a * 100.0 / b:.1f}%" if b > 0 else "N/A"

    report_lines = []
    def out(msg):
        log.info(msg)
        report_lines.append(msg)

    out("=" * 50)
    out(f"烈马自动化测试平台报告 — {time.strftime('%Y-%m-%d %H:%M:%S')}")
    out("=" * 50)

    if server_error:
        out(f"⚠ 服务器连接中断（{server_error_at}），以下数据不完整！")
        out(f"已上传文件/压缩包：{file_number} 个")
        out(f"不支持文件类型：{unsupported_count} 个")
        out(f"解析到：{parse_success} 个文件")
        if file_id_count:
            out(f"获取到：{file_id_count} 个文件ID")
        if translate_success_count:
            out(f"翻译轮询通过：{translate_success_count}/{file_id_count}")
        if timeout_count or fail_count:
            out(f"翻译状态确认：成功{cn_input_count}，超时{timeout_count}，失败{fail_count}")
        elif cn_input_count:
            out(f"翻译状态确认：成功{cn_input_count}")
        if cn_success_count or cn_fails:
            out(f"中文断言通过：{cn_success_count}/{cn_input_count}")
            if cn_fails:
                out(f"中文断言失败：{len(cn_fails)} 个")
        if summary_success_count or summary_fails:
            sf = summary_fails or []
            out(f"摘要检查通过：{summary_success_count}/{summary_success_count + len(sf)}")
            if sf:
                out(f"摘要检查失败：{len(sf)} 个")
        if stuck_batch_info:
            stuck_count = sum(fn for _, fn in stuck_batch_info)
            out(f"解析卡住批次：{len(stuck_batch_info)} 个、{stuck_count} 个文件")
        out(f"总耗时：{total_sec}s")
        out("=" * 50)

        sum_lines, det_lines = _build_failure_section(status_fails, cn_fails, summary_fails, stuck_batch_info)
        for line in sum_lines:
            out(line)
        report_lines += det_lines

        _write_report(run_dir, report_lines)
        return

    total_files = file_number
    parsed_count = file_id_count
    status_passed = cn_input_count
    status_checked = status_passed + len(status_fails)
    cn_passed = cn_input_count - len(cn_fails)
    sf = summary_fails or []
    summary_passed = summary_success_count
    summary_checked = summary_passed + len(sf)

    # 阶段汇总表
    sep72 = "-" * 72
    out(sep72)
    out(f"{'阶段':<12} {'成功':>6} {'失败':>6} {'未处理':>6} {'总计':>6} {'通过率':>8}")
    out(sep72)
    out(f"{'上传':<12} {file_number:>6} {0:>6} {0:>6} {file_number:>6} {pct(file_number, file_number):>8}")
    out(f"{'解析':<12} {parsed_count:>6} {0:>6} {total_files - parsed_count:>6} {total_files:>6} {pct(parsed_count, total_files):>8}")
    out(f"{'翻译':<12} {status_passed:>6} {len(status_fails):>6} {total_files - status_checked:>6} {total_files:>6} {pct(status_passed, total_files):>8}")
    out(f"{'中文断言':<12} {cn_passed:>6} {len(cn_fails):>6} {'-':>6} {status_passed:>6} {pct(cn_passed, status_passed):>8}  (总计=翻译成功数)")
    if "summary" in config.ANALYSIS:
        out(f"{'摘要检查':<12} {summary_passed:>6} {len(sf):>6} {total_files - summary_checked:>6} {total_files:>6} {pct(summary_passed, total_files):>8}")
    out(sep72)
    if unsupported_count:
        out(f"不支持文件类型：{unsupported_count} 个")

    # 批次耗时
    if batch_infos:
        sep80 = "-" * 80
        out(sep80)
        out(f"{'批次':<40} {'上传':>6} {'解析':>6} {'翻译':>6} {'文件':>5}")
        out(sep80)
        for b in batch_infos:
            up = b.get('upload_sec', 0)
            parse = b.get('parse_sec', 0)
            trans = b.get('translate_sec', 0)
            fn = b.get('svr_file_count', '?')
            out(f"{b['data_name']:<40} {up:>5.1f}s {parse:>5}s {trans:>5}s {fn:>5}")
        out(sep80)

    time_info = f"总耗时：{total_sec}s"
    if stuck_sec_count:
        time_info += f"（{stuck_sec_count} 批未完成）"
    out(time_info)

    # 性能指标
    if perf_summary:
        def _v(ps_dict, key, unit=""):
            val = ps_dict.get(key)
            if val is None or val == "":
                return "无数据"
            return f"{val}{unit}"

        def _show(label, ps):
            out(f"性能指标均值 ({label})：")
            out(f"  CPU 使用率：{_v(ps, 'cpu_pct_avg', '%')}  "
                f"CPU 温度：{_v(ps, 'cpu_temp_avg', '°C')}  "
                f"内存：{_v(ps, 'mem_used_gb_avg', '')} / {_v(ps, 'mem_total_gb', '')} GB "
                f"磁盘利用率：{_v(ps, 'disk_util_avg', '%')}")
            if ps.get("gpu_pct_avg") is not None:
                out(f"  GPU 整体：使用率 {_v(ps, 'gpu_pct_avg', '%')}  温度 {_v(ps, 'gpu_temp_avg', '°C')}")
                i = 0
                while f"gpu{i}_pct_avg" in ps:
                    out(f"    GPU{i}：使用率 {_v(ps, f'gpu{i}_pct_avg', '%')}  "
                        f"温度 {_v(ps, f'gpu{i}_temp_avg', '°C')}")
                    i += 1

        if "app" in perf_summary:
            _show("APP", perf_summary["app"])
        if "gpu" in perf_summary:
            _show("GPU", perf_summary["gpu"])

    # CPU 压测
    if stress_summary:
        out("-" * 30)
        out("CPU 压测结果 (stress-ng)：")
        out(f"  耗时：{stress_summary.get('elapsed_s', 0)}s")
        out(f"  bogo ops (real): {stress_summary.get('bogo_ops_real', 0)}")
        out(f"  bogo ops (usr):  {stress_summary.get('bogo_ops_usr', 0)}")

    # 导出
    if export_results:
        out("-" * 30)
        out(f"导出测试结果：成功 {export_success_count}，失败 {export_fail_count}")
        for r in export_results:
            result = r.get("result", "?")
            icon = "✅" if result == "success" else ("❌" if result == "fail" else "⏰")
            out(f"  {icon} {r['task_name']}: {r['status']} "
                f"({r['success_number']}/{r['file_number']})")

    if ai_check_results:
        passed = sum(1 for r in ai_check_results if r.get("success"))
        out("-" * 30)
        out(f"AI功能接口检查：通过 {passed}，失败 {len(ai_check_results) - passed}")
        for r in ai_check_results:
            icon = "✅" if r.get("success") else "❌"
            out(f"  {icon} {r.get('name', '?')}：{r.get('detail', '')} "
                f"({r.get('elapsed_s', 0)}s)")

    # 失败文件明细
    sum_lines, det_lines = _build_failure_section(status_fails, cn_fails, summary_fails, stuck_batch_info)
    for line in sum_lines:
        out(line)
    report_lines += det_lines

    out("=" * 50)

    _write_report(run_dir, report_lines)
