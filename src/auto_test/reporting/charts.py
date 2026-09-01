"""
图表生成 — 性能时间线、批次耗时、结果摘要、失败类型分布。
全部输出为 PNG 到 charts/ 目录。
"""

import csv
from pathlib import Path

from auto_test.common.logging import log

# 中文字体设置
import matplotlib
matplotlib.use("Agg")
from auto_test.reporting.font_support import configure_matplotlib_cjk

configure_matplotlib_cjk()
import matplotlib.pyplot as plt


CHART_FONT_RENDER_VERSION = "cjk-font-v1"
CHART_FONT_RENDER_MARKER = ".font-render-version"


def generate_all(run_dir, batch_infos=None, file_number=0, timeout_count=0, fail_count=0,
                 cn_success=0, cn_total=0, perf_csv_app=None, perf_csv_gpu=None, ext_fails=None,
                 stage_data=None, perf_chart_types=None):
    """
    生成全部图表到 {run_dir}/charts/
    stage_data: dict 各阶段统计，用于阶段概览图
    perf_chart_types: None=全部, set如{"cpu"}只生成CPU图
    """
    charts_dir = Path(run_dir) / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)

    if perf_csv_app and Path(perf_csv_app).exists():
        _perf_timeline(perf_csv_app, charts_dir, prefix="perf_app", title_suffix="(APP)", chart_types=perf_chart_types)
    if perf_csv_gpu and Path(perf_csv_gpu).exists():
        _perf_timeline(perf_csv_gpu, charts_dir, prefix="perf_gpu", title_suffix="(GPU)", chart_types=perf_chart_types)

    if batch_infos:
        _batch_timing(batch_infos, charts_dir)

    if stage_data:
        _stage_summary(stage_data, charts_dir)

    if ext_fails:
        _filetype_failures(ext_fails, charts_dir)

    (charts_dir / CHART_FONT_RENDER_MARKER).write_text(
        CHART_FONT_RENDER_VERSION, encoding="ascii"
    )
    log.info(f"图表已生成：{charts_dir}")


def regenerate_performance_charts(run_dir) -> list[Path]:
    """Rebuild saved performance charts before composing a report.

    This lets a historical run produce a corrected report after a renderer or
    font fix without rerunning the customer's automation task.
    """

    root = Path(run_dir).resolve()
    generated: list[Path] = []
    report_dirs = sorted({path.parent for path in root.glob("**/report/*.csv")})
    for report_dir in report_dirs:
        charts_dir = report_dir.parent / "charts"
        charts_dir.mkdir(parents=True, exist_ok=True)
        app_csv = next(
            (
                path for path in (
                    report_dir / "perf_app.csv",
                    report_dir / "perf.csv",
                    report_dir / "stress_perf_app.csv",
                )
                if path.is_file()
            ),
            None,
        )
        gpu_csv = next(
            (
                path for path in (
                    report_dir / "perf_gpu.csv",
                    report_dir / "stress_perf_gpu.csv",
                )
                if path.is_file()
            ),
            None,
        )
        if app_csv:
            _perf_timeline(app_csv, charts_dir, prefix="perf_app", title_suffix="(APP)")
            generated.extend(sorted(charts_dir.glob("perf_app_*.png")))
        if gpu_csv:
            _perf_timeline(gpu_csv, charts_dir, prefix="perf_gpu", title_suffix="(GPU)")
            generated.extend(sorted(charts_dir.glob("perf_gpu_*.png")))
    return list(dict.fromkeys(generated))


def charts_have_current_cjk_font(run_dir) -> bool:
    root = Path(run_dir).resolve()
    return any(
        marker.is_file()
        and marker.read_text(encoding="ascii", errors="ignore").strip()
        == CHART_FONT_RENDER_VERSION
        for marker in root.glob(f"**/charts/{CHART_FONT_RENDER_MARKER}")
    )


def _perf_timeline(perf_csv, charts_dir, prefix="perf", title_suffix="", chart_types=None):
    """拆分为独立折线图，chart_types=None=全部, set如{"cpu"}只生成CPU图"""
    times, cpu, cpu_temp, gpu, gpu_temp, mem = [], [], [], [], [], []
    gpu_cards = {}  # {i: {"pct": [], "temp": []}}

    with open(perf_csv, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            times.append(row.get("time", ""))
            cpu.append(_f(row.get("cpu_pct(%)", row.get("cpu_pct", ""))))
            cpu_temp.append(_f(row.get("cpu_temp(°C)", row.get("cpu_temp_c", ""))))
            gpu.append(_f(row.get("gpu_pct(%)", row.get("gpu_pct", ""))))
            gpu_temp.append(_f(row.get("gpu_temp(°C)", row.get("gpu_temp_c", ""))))
            mem.append(_f(row.get("mem_used_gb", "")))
            for i in range(8):  # 最多 8 卡
                pct_k = f"gpu{i}_pct(%)"
                tmp_k = f"gpu{i}_temp(°C)"
                mem_k = f"gpu{i}_mem_mb"
                if pct_k in row:
                    gpu_cards.setdefault(i, {"pct": [], "temp": [], "mem": []})
                    gpu_cards[i]["pct"].append(_f(row[pct_k]))
                    gpu_cards[i]["temp"].append(_f(row[tmp_k]))
                    if mem_k in row:
                        gpu_cards[i]["mem"].append(_f(row[mem_k]))

    x = range(len(times))
    # X轴自适应：无论数据多少，最多显示12个标签
    _max_ticks = 12
    _tick_step = max(1, len(x) // _max_ticks)
    xtick_labels = [times[i] for i in x][::_tick_step]
    xtick_pos = list(x)[::_tick_step]

    def _should_gen(t):
        return chart_types is None or t in chart_types

    def _apply_xticks(ax):
        ax.set_xticks(xtick_pos)
        ax.set_xticklabels(xtick_labels, rotation=45, fontsize=7)

    # 1. CPU 使用率 + 温度
    if _should_gen("cpu") and any(cpu):
        cpu_avg = round(sum(cpu) / len(cpu), 1)
        cpu_max = round(max(cpu), 1)
        info_parts = [f"CPU使用率 均值{cpu_avg}% / 峰值{cpu_max}%"]
        if any(cpu_temp):
            t_avg = round(sum(cpu_temp) / len(cpu_temp), 1)
            t_max = round(max(cpu_temp), 1)
            info_parts.append(f"CPU温度 均值{t_avg}°C / 峰值{t_max}°C")

        fig, ax1 = plt.subplots(figsize=(10, 3.5))
        ax1.plot(x, cpu, "#2196F3", linewidth=1.5, label="CPU 使用率 %")
        ax1.set_ylabel("使用率 %")
        if any(cpu_temp):
            ax2 = ax1.twinx()
            ax2.plot(x, cpu_temp, "#FF5722", linewidth=1.2, linestyle=":", label="CPU 温度 °C")
            ax2.set_ylabel("温度 °C")
            lines1, labels1 = ax1.get_legend_handles_labels()
            lines2, labels2 = ax2.get_legend_handles_labels()
            ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left", fontsize=7)
        else:
            ax1.legend(loc="upper left", fontsize=7)
        ax1.set_title(f"CPU 使用率 & 温度 {title_suffix}\n{'  |  '.join(info_parts)}", fontsize=10)
        _apply_xticks(ax1)
        fig.tight_layout()
        fig.savefig(charts_dir / f"{prefix}_cpu.png", dpi=120)
        plt.close(fig)

    if not _should_gen("gpu") and not _should_gen("mem"):
        return

    # 解析 nvidia-smi 进程信息
    gpu_process_info = {}
    proc_file = charts_dir.parent / "report" / "gpu_processes.txt"
    if proc_file.exists():
        import re
        with open(proc_file, "r", encoding="utf-8") as pf:
            for line in pf:
                m = re.match(r"\|\s+(\d+)\s+N/A\s+N/A\s+(\d+)\s+\S+\s+(.+)\s+(\d+)MiB\s*\|", line)
                if m:
                    gpu_process_info.setdefault(int(m.group(1)), []).append(
                        (m.group(2), m.group(3).strip(), m.group(4)))
                else:
                    # 兜底：匹配含 MiB 的行，任意格式都抓
                    m2 = re.search(r"(\d+)MiB", line)
                    if m2 and "Processes" not in line and "===" not in line:
                        parts = line.split()
                        if len(parts) >= 5:
                            try:
                                gpu_idx = int(parts[1]) if parts[1] != "N/A" else None
                            except ValueError:
                                gpu_idx = None
                            if gpu_idx is not None:
                                gpu_process_info.setdefault(gpu_idx, []).append(
                                    (parts[3], parts[-2] if len(parts) > 5 else "?", m2.group(1)))

    # 2. 每张 GPU 卡独立一张图
    if _should_gen("gpu"):
        for i in sorted(gpu_cards.keys()):
            pcts = gpu_cards[i]["pct"]
            temps = gpu_cards[i]["temp"]
            mems = gpu_cards[i].get("mem", [])
            if not pcts:
                continue

            fig, ax1 = plt.subplots(figsize=(10, 3.5))
            ax1.plot(x, pcts, "#4CAF50", linewidth=1.5, label=f"GPU{i} 使用率 %")
            ax1.set_ylabel("使用率 %", color="#4CAF50")
            ax1.tick_params(axis="y", labelcolor="#4CAF50")

            if any(temps):
                ax2 = ax1.twinx()
                ax2.plot(x, temps, "#FF5722", linewidth=1.2, linestyle=":", label=f"GPU{i} 温度 °C")
                ax2.set_ylabel("温度 °C", color="#FF5722")
                ax2.tick_params(axis="y", labelcolor="#FF5722")
                lines1, labels1 = ax1.get_legend_handles_labels()
                lines2, labels2 = ax2.get_legend_handles_labels()
                ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left", fontsize=7)
            else:
                ax1.legend(loc="upper left", fontsize=7)

            # 标注利用率 + 显存
            info_parts = []
            if pcts:
                info_parts.append(f"GPU使用率 均值{round(sum(pcts)/len(pcts),1)}% / 峰值{round(max(pcts),1)}%")
            if temps:
                info_parts.append(f"GPU温度 均值{round(sum(temps)/len(temps),1)}°C / 峰值{round(max(temps),1)}°C")
            if mems:
                avg_mem = round(sum(mems) / len(mems) / 1024, 1)
                max_mem = round(max(mems) / 1024, 1)
                info_parts.append(f"显存 均值{avg_mem}GB / 峰值{max_mem}GB")
            ax1.set_title(f"GPU{i} 使用率 & 温度 {title_suffix}\n{'  |  '.join(info_parts)}", fontsize=10)

            _apply_xticks(ax1)
            fig.tight_layout()
            fig.savefig(charts_dir / f"{prefix}_gpu{i}.png", dpi=120)
            plt.close(fig)

    # GPU 进程汇总表
    if _should_gen("gpu") and gpu_process_info:
        _gpu_process_table(gpu_process_info, charts_dir)

    # 3. 内存
    if _should_gen("mem") and any(mem):
        mem_avg = round(sum(mem) / len(mem), 1)
        mem_max = round(max(mem), 1)
        fig, ax = plt.subplots(figsize=(10, 3.5))
        ax.plot(x, mem, "#9C27B0", linewidth=1.5, label="已用内存 GB")
        ax.set_ylabel("GB")
        ax.set_title(f"内存使用量 {title_suffix}\n均值 {mem_avg} GB / 峰值 {mem_max} GB", fontsize=10)
        ax.legend(loc="upper left", fontsize=7)
        _apply_xticks(ax)
        fig.tight_layout()
        fig.savefig(charts_dir / f"{prefix}_mem.png", dpi=120)
        plt.close(fig)


def _gpu_process_table(gpu_process_info, charts_dir):
    """GPU 进程信息表"""
    rows = []
    for gpu_idx in sorted(gpu_process_info.keys()):
        procs = sorted(gpu_process_info[gpu_idx], key=lambda x: int(x[2]), reverse=True)
        for pid, name, mem_mb in procs:
            rows.append([f"GPU{gpu_idx}", name, f"{mem_mb}MiB"])

    if not rows:
        return

    fig, ax = plt.subplots(figsize=(8, max(2, len(rows) * 0.4 + 0.8)))
    ax.axis("off")

    col_labels = ["GPU", "进程名称", "显存占用"]
    table = ax.table(cellText=rows, colLabels=col_labels,
                     cellLoc="left", loc="center",
                     colWidths=[0.12, 0.60, 0.18])
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1, 1.4)

    # 表头样式
    for i in range(3):
        table[0, i].set_facecolor("#2196F3")
        table[0, i].set_text_props(color="white", fontweight="bold")

    ax.set_title("GPU 进程信息", fontsize=12, fontweight="bold", pad=10)
    fig.tight_layout()
    fig.savefig(charts_dir / "gpu_processes.png", dpi=120)
    plt.close(fig)


def _batch_timing(batch_infos, charts_dir):
    """批次耗时堆叠柱状图"""
    names = [b["data_name"][:25] for b in batch_infos]
    uploads = [b.get("upload_sec", 0) for b in batch_infos]
    parses = [b.get("parse_sec", 0) for b in batch_infos]
    translates = [b.get("translate_sec", 0) for b in batch_infos]

    if not any(uploads) and not any(translates):
        return

    fig, ax = plt.subplots(figsize=(10, 5))
    y = range(len(names))
    ax.barh(y, uploads, label="上传", color="#4CAF50")
    ax.barh(y, parses, left=uploads, label="解析", color="#2196F3")
    left2 = [u + p for u, p in zip(uploads, parses)]
    ax.barh(y, translates, left=left2, label="翻译", color="#FF9800")

    ax.set_yticks(y)
    ax.set_yticklabels(names)
    ax.set_xlabel("耗时 (s)")
    ax.set_title("各批次耗时")
    ax.legend()
    ax.invert_yaxis()

    fig.tight_layout()
    fig.savefig(charts_dir / "batch_timing.png", dpi=120)
    plt.close(fig)


def _stage_summary(stage_data, charts_dir):
    """各阶段统计表格"""
    stages = ["上传", "解析", "翻译", "中文断言"]
    success_vals = [
        stage_data.get("total", 0),
        stage_data.get("parsed", 0),
        stage_data.get("status_passed", 0),
        stage_data.get("cn_passed", 0),
    ]
    fail_vals = [0, 0, stage_data.get("status_failed", 0), stage_data.get("cn_failed", 0)]
    unstaged = stage_data.get("unstaged", 0)

    if stage_data.get("summary_passed") is not None:
        stages.append("摘要")
        success_vals.append(stage_data["summary_passed"])
        fail_vals.append(stage_data.get("summary_failed", 0))

    rows = []
    for i, stage in enumerate(stages):
        total = success_vals[i] + fail_vals[i]
        rate = f"{success_vals[i] * 100.0 / total:.1f}%" if total > 0 else "-"
        rows.append([stage, str(total), str(success_vals[i]), str(fail_vals[i]), rate])

    fig, ax = plt.subplots(figsize=(7, max(2, len(stages) * 0.45 + 0.8)))
    ax.axis("off")

    col_labels = ["阶段", "总数", "成功", "失败", "通过率"]
    table = ax.table(cellText=rows, colLabels=col_labels,
                     cellLoc="center", loc="center",
                     colWidths=[0.18, 0.18, 0.18, 0.18, 0.18])
    table.auto_set_font_size(False)
    table.set_fontsize(10)
    table.scale(1, 1.6)

    for i in range(len(col_labels)):
        table[0, i].set_facecolor("#2196F3")
        table[0, i].set_text_props(color="white", fontweight="bold")

    ax.set_title(f"各阶段统计（未处理：{unstaged} 个文件）", fontsize=13, fontweight="bold", pad=10)
    fig.tight_layout()
    fig.savefig(charts_dir / "stage_summary.png", dpi=120)
    plt.close(fig)


def _result_summary(file_number, timeout_count, fail_count, cn_success, cn_total, charts_dir):
    """翻译结果饼图"""
    success = file_number - timeout_count - fail_count
    wp = {"linewidth": 1, "edgecolor": "white"}

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    # 翻译结果
    sizes1 = [success, timeout_count, fail_count]
    labels1 = [f"成功 {success}", f"超时 {timeout_count}", f"失败 {fail_count}"]
    colors1 = ["#4CAF50", "#FF9800", "#F44336"]
    patches1, texts1, autotexts1 = ax1.pie(
        sizes1, labels=labels1, colors=colors1, autopct="%1.1f%%",
        startangle=90, pctdistance=0.6, labeldistance=1.15,
        wedgeprops=wp, textprops={"fontsize": 10},
    )
    for t in autotexts1:
        t.set_fontsize(9)
    ax1.set_title("翻译结果分布")

    # 中文断言
    cn_fail = cn_total - cn_success
    sizes2 = [cn_success, cn_fail]
    labels2 = [f"中文通过 {cn_success}", f"中文失败 {cn_fail}"]
    colors2 = ["#4CAF50", "#F44336"]
    patches2, texts2, autotexts2 = ax2.pie(
        sizes2, labels=labels2, colors=colors2, autopct="%1.1f%%",
        startangle=90, pctdistance=0.6, labeldistance=1.15,
        wedgeprops=wp, textprops={"fontsize": 10},
    )
    for t in autotexts2:
        t.set_fontsize(9)
    ax2.set_title("中文断言结果")

    fig.tight_layout(pad=2)
    fig.savefig(charts_dir / "result_summary.png", dpi=120)
    plt.close(fig)


def _filetype_failures(ext_fails, charts_dir):
    """失败文件类型横向柱状图"""
    if not ext_fails:
        return

    items = sorted(ext_fails.items(), key=lambda x: x[1])
    labels = [f".{k}" for k, _ in items]
    values = [v for _, v in items]

    fig, ax = plt.subplots(figsize=(6, max(3, len(labels) * 0.4)))
    ax.barh(labels, values, color="#F44336")
    ax.set_xlabel("失败数量")
    ax.set_title("失败文件类型分布")
    ax.invert_yaxis()

    fig.tight_layout()
    fig.savefig(charts_dir / "filetype_failures.png", dpi=120)
    plt.close(fig)


def _f(val):
    """安全转浮点数"""
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0
