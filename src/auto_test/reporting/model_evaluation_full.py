"""Evidence-led reporting for a complete evaluation bundle, without mixed scores."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from statistics import mean


def display(value, suffix="", digits=2):
    if value is None:
        return "未采集"
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            return "未采集"
        return f"{value:.{digits}f}".rstrip("0").rstrip(".") + suffix if digits else str(int(value)) + suffix
    return str(value) + suffix


def _flat(value, prefix=""):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _flat(child, (prefix + "." if prefix else "") + str(key))
    elif not isinstance(value, list):
        yield prefix, value


def valid_rubric(judge, expected):
    names = {str(item.get("name") or "") if isinstance(item, dict) else str(item) for item in expected}
    rows = {}
    for item in judge.get("rubric") or []:
        if not isinstance(item, dict):
            continue
        value, name = item.get("score"), str(item.get("name") or "")
        if name in names and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1:
            rows[name] = value
    return rows


def benchmark_metric_name(metric):
    name, dimensions = str(metric.get("name") or "未记录"), metric.get("dimensions") or {}
    if name == "rouge":
        return "ROUGE-" + str(dimensions.get("variant") or "").upper() + " " + {"recall": "召回", "precision": "精确", "fmeasure": "F1"}.get(dimensions.get("statistic"), str(dimensions.get("statistic") or "")) + "（原始值）"
    if name == "bleu":
        return "BLEU-" + str(dimensions.get("ngram") or "") + "（原始值）"
    return name + " / " + ", ".join(f"{key}={value}" for key, value in dimensions.items())


def extend_full_report(report, run, results):
    snapshot, summary = run.get("snapshot") or {}, run.get("summary") or {}
    configs = (snapshot.get("task_config") or {}).get("stages") or []
    actual = {s["id"]: s for s in summary.get("stages", [])}
    sections, coverage, native, benchmarks, provenance, parameters = [], [], [], [], [], []
    quality, rules, rubric, timing, usage, robustness, evidence = [], defaultdict(list), defaultdict(list), [], [], [], []
    missing = []
    all_cases = (snapshot.get("task_config") or {}).get("cases") or []
    case_catalog = {item["id"]: item for item in all_cases}
    by_stage = defaultdict(list)
    for item in results:
        score = item.get("score") or {}
        if (score.get("manual_review") or {}).get("status") == "completed" and isinstance(score.get("score"), (int, float)) and score["score"] < 0.8 and item.get("status") == "passed":
            item = {**item, "status": "failed"}
            for row in report["cases"]:
                if row["case_id"] == item["case_id"]:
                    row.update(status="failed", status_text="未达标")
        by_stage[(item.get("metrics") or {}).get("evaluation_stage") or str(item.get("case_id") or "").split(":", 1)[0]].append(item)
    statuses = {"completed": "已执行", "failed": "未达标 / 执行失败", "error": "调用异常", "passed": "达标", "stopped": "已停止", "skipped": "未执行", "pending": "未执行", "running": "执行中"}
    def section(title, headers, rows, note=""):
        sections.append({"title": title, "headers": list(headers), "rows": [["未采集" if value is None else value for value in row] for row in rows] or [["未采集"] + ["—"] * (len(headers) - 1)], "note": note})
    for config in configs:
        key, label = config["id"], config["label"]
        state = actual.get(key) or {}
        measured = state.get("summary") or {}
        status = state.get("status", "pending")
        stage_results = by_stage[key]
        expected = config.get("expected_cases", 0)
        completed = measured.get("completed_cases", len(stage_results))
        is_perf = config["mode"] == "perf"
        if is_perf:
            completed = measured.get("total_requests", 0)
        coverage.append((label, "受控负载" if is_perf else expected, completed, statuses.get(status, status), state.get("error_type") or "—"))
        if status != "completed" or (not is_perf and int(completed or 0) < expected):
            missing.append(label + "未完整执行")
        suite = config["suite"]
        provenance.append((label, str(suite["version"]), str(suite["content_sha256"]), str((suite.get("manifest") or {}).get("provenance") or (suite.get("upstream") or {}).get("source") or "平台固定样例")))
        task = config["task_config"]
        parameters.append((label, "流式" if config["stream"] else "非流式", task.get("max_tokens", (task.get("generation_config") or {}).get("max_tokens")), task.get("timeout", (task.get("_safety") or {}).get("request_timeout_seconds", (task.get("generation_config") or {}).get("timeout"))), task.get("temperature", (task.get("generation_config") or {}).get("temperature", "适配器默认"))))
        if config["backend"] == "native":
            native.extend(stage_results)
            categories = defaultdict(list)
            for item in stage_results:
                info = case_catalog.get(item["case_id"]) or {}
                categories[info.get("category", "未分类")].append(item)
                score = item.get("score") or {}
                for check in score.get("checks") or []:
                    name = str(check.get("name") or "未命名规则").split("：", 1)[0]
                    rules[(label, name)].append(bool(check.get("passed")))
                judge = score.get("judge") or {}
                if judge.get("status") == "completed":
                    for dimension, value in valid_rubric(judge, (info.get("payload") or {}).get("rubric") or []).items():
                        rubric[(label, dimension)].append(value * 100)
                failures = [str(c.get("name")) for c in score.get("checks", []) if not c.get("passed")]
                if item.get("status") in {"error", "failed"} or failures or (score.get("manual_review") or {}).get("comment"):
                    evidence.append((str((info.get("payload") or {}).get("name") or item["case_id"]), statuses.get(item.get("status"), item.get("status")), "；".join(failures) or str((item.get("metrics") or {}).get("error_type") or "—"), str((score.get("manual_review") or {}).get("comment") or "未人工复核")))
            for category, items in categories.items():
                scores = [float((r.get("score") or {})["score"]) * 100 for r in items if isinstance((r.get("score") or {}).get("score"), (int, float))]
                category_label = {"instruction": "指令遵循", "structured_output": "结构化输出", "extraction": "信息提取", "classification": "分类", "context": "上下文样例", "robustness": "扰动鲁棒性", "translation_zh_en": "中译英", "translation_en_zh": "英译中", "report_writing": "报告写作", "intelligence": "情报生产", "custom": "业务素材"}.get(category, category)
                quality.append((label + " / " + category_label, len(items), sum(r.get("status") == "passed" for r in items), sum(r.get("status") == "error" for r in items), display(mean(scores), "%") if scores else "未评分"))
            timing.append((label, display((measured.get("performance") or {}).get("latency_p50_ms")), display((measured.get("performance") or {}).get("latency_p95_ms")), display((measured.get("performance") or {}).get("latency_p99_ms")), display((measured.get("performance") or {}).get("output_tokens_per_second"))))
            ttft = measured.get("performance") or {}
            if config["stream"]:
                timing.append((label + " / TTFT", display(ttft.get("ttft_p50_ms")), display(ttft.get("ttft_p95_ms")), display(ttft.get("ttft_p99_ms")), "—"))
            robust = measured.get("robustness") or {}
            if config["run_kind"] == "foundation":
                robustness.append((label, display(robust.get("average_retention"), "%"), display(robust.get("worst_retention"), "%"), str(robust.get("group_count", len(robust.get("groups") or [])))))
        for benchmark in measured.get("benchmarks") or []:
            metric = benchmark.get("primary_metric") or {}
            benchmarks.append((label, benchmark.get("requested", 0), benchmark.get("succeeded", 0), benchmark_metric_name(metric), display(benchmark.get("score"), digits=4)))
            for metric_name, metric_value in _flat({"latency": benchmark.get("latency") or {}, "throughput": benchmark.get("throughput") or {}}):
                if metric_name.startswith("latency."):
                    statistic = metric_name.removeprefix("latency.")
                    metric_name = "时延 " + {"mean": "平均", "min": "最低", "max": "最高", "std": "标准差"}.get(statistic, "P" + statistic.rstrip("%")) + "（毫秒）"
                    metric_value = metric_value * 1000 if isinstance(metric_value, (int, float)) else metric_value
                else:
                    metric_name = {"throughput.avg_output_tps": "平均输出 Token/s", "throughput.avg_req_ps": "平均请求/秒"}.get(metric_name, metric_name)
                timing.append((label + " / " + metric_name, display(metric_value), "—", "—", "—"))
        counts = measured.get("token_usage") or {}
        usage.append((label, display(counts.get("input_tokens")), display(counts.get("output_tokens")), display(counts.get("total_tokens")), "; ".join(f"{k}: {v}" for k, v in (counts.get("source_counts") or {}).items()) or "未采集"))
    section("全量覆盖与执行状态", ("专项", "计划样本", "实际执行", "状态", "异常原因"), coverage, "全部维度指本平台当前内置能力范围。执行完成不等于质量达标；未执行、缺测和异常均保留。性能请求单独计数，含容量、突发、持续、恢复阶段，不含预热。")
    section("测试集版本与可追溯性", ("专项", "版本", "SHA-256", "来源"), provenance, "完整输入、参考答案、规则、组件参数和响应保存在全量证据包。内置 general_qa 和 WMT 为 4 条本地适配样例，不代表官方榜单或完整公开数据集；200 条翻译含模板变体，不能视为 200 类独立业务场景。")
    section("实际生成参数", ("专项", "响应方式", "最大输出 Tokens", "超时秒", "Temperature"), parameters, "性能阶段固定最高并发 16，输出预算不超过 512；基础能力固定覆盖两种响应方式。质量阶段使用表内输出预算，裁判使用独立配置。")
    section("能力分类与质量结果", ("专项 / 分类", "已执行", "用例达标", "执行异常", "平均综合分"), quality, "用例达标要求规则得分达到用例阈值，融合分至少 80%；人工低分复核会标记未达标；综合分逐例融合规则、有效裁判和人工复核。每个分类独立展示，未混合计算总质量分。缺少裁判的主观项不能据此宣称语义正确。")
    section("全量规则指标", ("专项", "规则指标", "通过 / 检查次数", "通过率"), [(label, name, f"{sum(values)}/{len(values)}", display(100 * sum(values) / len(values), "%")) for (label, name), values in rules.items()], "同名规则按每次断言计数，分母不是用例数。包括实际配置的结构、事实、数值、术语、格式、保护片段和禁止内容检查；未配置的规则不作为已测指标。")
    judge_rows, judge_valid, judge_required, reviewed = [], 0, 0, 0
    for config in configs:
        if config["backend"] != "native":
            continue
        stage_cases = config["task_config"].get("cases") or []
        required = sum(bool((c.get("payload") or {}).get("rubric")) for c in stage_cases)
        items = by_stage[config["id"]]
        counts = Counter(((r.get("score") or {}).get("judge") or {}).get("status", "not_configured") for r in items)
        valid = sum(
            ((r.get("score") or {}).get("judge") or {}).get("status") == "completed"
            and bool((case_catalog.get(r["case_id"], {}).get("payload") or {}).get("rubric"))
            and len(valid_rubric((r.get("score") or {}).get("judge") or {}, (case_catalog.get(r["case_id"], {}).get("payload") or {}).get("rubric") or [])) == len((case_catalog.get(r["case_id"], {}).get("payload") or {}).get("rubric") or [])
            for r in items
        )
        human = sum(((r.get("score") or {}).get("manual_review") or {}).get("status") == "completed" for r in items)
        judge_required += required
        judge_valid += valid
        reviewed += human
        judge_rows.append((config["label"], required, valid, human, "; ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "未执行"))
    if judge_valid < judge_required:
        missing.append(f"主观裁判有效结果不足（{judge_valid}/{judge_required}）")
    section("裁判与人工复核覆盖", ("专项", "需裁判", "有效裁判", "人工复核", "裁判状态分布"), judge_rows, "这是评分证据覆盖率，不是事实正确概率。独立小模型裁判可能漏判因果推断、虚构时间和来源；报告与情报用于业务决策前应对照原材料人工复核。")
    for config in configs:
        for case in config["task_config"].get("cases") or []:
            for dimension in (case.get("payload") or {}).get("rubric") or []:
                rubric.setdefault((config["label"], str(dimension.get("name") or "") if isinstance(dimension, dict) else str(dimension)), [])
    section("逐项主观指标", ("专项", "评分维度", "有效样本", "均分 / 100", "最低分 / 100"), [(label, dimension, len(values), display(mean(values)) if values else "未采集", display(min(values)) if values else "未采集") for (label, dimension), values in rubric.items()], "只统计名称匹配、范围 0～1 的有效维度评分；主观裁判覆盖要求该用例的全部 rubric 有效。缺失维度不按零或满分填充。逐例理由、裁判响应与失败断言见证据包。")
    section("标准适配器指标", ("专项", "请求", "调用成功", "指标及口径", "原始得分"), benchmarks, "调用成功与答案正确分开；general_qa 当前主指标为 ROUGE-L 召回，反映参考文本匹配，不能写成答案正确率。翻译为 BLEU-1，保留原始值，不与问答分数混算。")
    section("质量调用时延与吞吐", ("专项 / 指标", "P50 / 单项结果", "P95 毫秒", "P99 毫秒", "平均输出 Token/s"), timing, "原生时延与 TTFT 均为毫秒，耗时含网络、排队、生成；单请求输出吞吐为输出量除以端到端时长。适配器时延从秒换算为毫秒，单项吞吐单位写入指标名。非流式 TTFT 不适用；不得平均各阶段的百分位数。")
    section("Token 计数与来源", ("专项", "输入", "输出", "合计", "计数来源 / 请求数"), usage, "仅 api_usage 标记为服务端报告计数；估算和后端汇总明确区分。性能阶段平均输入/输出在性能用量表展示，不反推精确 Token 总量；裁判消耗未混入被测模型统计。")
    section("鲁棒性指标", ("专项", "平均保持率", "最差保持率", "对照组数"), robustness, "按基准与扰动配对比较。上下文只验证当前固定样例的检索与指令行为，未测模型最大上下文窗口；鲁棒变体不是全面对抗或安全认证。")
    perf_rows, latency_rows, ttft_rows, volume_rows = [], [], [], []
    for i, stage in enumerate(report.get("performance_stages") or [], 1):
        name = f"{i}. {'并发' if stage.get('evaluation_stage') == 'concurrency' else '深度'} / {stage.get('phase', 'capacity')}"
        perf_rows.append((name, stage.get("concurrency"), display(stage.get("target_rps")) if stage.get("target_rps") is not None else "固定并发", f"{stage.get('successful_requests', 0)}/{stage.get('total_requests', 0)}", stage.get("failed_requests", 0), display(stage.get("request_throughput"))))
        latency_rows.append((name, display(stage.get("latency_p50_ms")), display(stage.get("latency_p95_ms")), display(stage.get("latency_p99_ms")), display(stage.get("average_latency_ms"))))
        ttft_rows.append((name, display(stage.get("ttft_p50_ms")), display(stage.get("ttft_p95_ms")), display(stage.get("ttft_p99_ms")), display(stage.get("average_ttft_ms"))))
        volume_rows.append((name, display(stage.get("input_tokens_average")), display(stage.get("output_tokens_average")), display(stage.get("output_token_throughput")), display(stage.get("total_token_throughput"))))
    section("全部性能阶段：负载、成功率与请求吞吐", ("阶段", "并发上限", "目标 RPS", "成功 / 总数", "失败数", "实际 RPS"), perf_rows, "固定并发阶段没有目标 RPS。质量规则未达标与 HTTP/推理失败不混算；全部阶段结果包含错误率保护中止前已完成请求。")
    section("全部性能阶段：端到端时延（毫秒）", ("阶段", "P50", "P95", "P99", "平均"), latency_rows)
    section("全部性能阶段：首 Token 时延 TTFT（毫秒）", ("阶段", "P50", "P95", "P99", "平均"), ttft_rows)
    section("全部性能阶段：用量与吞吐", ("阶段", "平均输入 Tokens", "平均输出 Tokens", "输出 Token/s", "总 Token/s"), volume_rows, "吞吐按阶段总量 / 阶段耗时计算；这些负载和输出长度下的结果不直接代表其他业务长度的容量。")
    from datetime import datetime, timezone
    windows = []
    for window in (summary.get("performance_execution") or {}).get("stages") or []:
        start, end = window.get("started_at"), window.get("finished_at")
        windows.append((str(window.get("evaluation_stage") or "") + " / " + str(window.get("phase") or ""),
                        datetime.fromtimestamp(start, timezone.utc).isoformat(timespec="seconds") if start else "未采集",
                        datetime.fromtimestamp(end, timezone.utc).isoformat(timespec="seconds") if end else "未采集",
                        display(end - start) if start and end else "未采集", display(window.get("number"))))
    section("性能执行时间窗", ("专项 / 阶段", "开始 UTC", "结束 UTC", "实际时长秒", "请求预算"), windows, "时间以 UTC 标记，用于关联原始采样；实际执行时长与负载配置时长分别保留。预热请求未混入主测请求总数。")
    error_rows = []
    for config in configs:
        stage = actual.get(config["id"]) or {}
        errors = (stage.get("summary") or {}).get("error_types") or {}
        if errors:
            error_rows.extend((config["label"], name, value) for name, value in errors.items())
        elif stage.get("status") == "completed" and config["backend"] == "native":
            error_rows.append((config["label"], "无请求异常", 0))
        else:
            error_rows.append((config["label"], stage.get("error_type") or ("失败数见性能 / 适配器表；未单列错误类型" if stage.get("status") == "completed" else "未完整执行"), "未单列"))
    section("请求错误分类", ("专项", "错误类别 / 状态", "次数"), error_rows, "包括原生 HTTP、超时、空输出、截断和流式不完整等实际返回类别；无异常与未采集分开，裁判异常在裁判状态表列出。")
    capacity_rows = []
    for config in configs:
        if config["mode"] == "perf":
            measured = (actual.get(config["id"]) or {}).get("summary") or {}
            cap = measured.get("capacity") or {}
            knee = cap.get("capacity_knee") or {}
            knee_text = f"并发 {display(knee.get('concurrency'))} / 目标 {display(knee.get('target_rps'))} RPS" if knee else "当前范围未观测到" if cap.get("available") else "未采集"
            capacity_rows.append((config["label"], display(cap.get("max_stable_concurrency")), display(cap.get("max_stable_rps")), knee_text, cap.get("conclusion") or "缺少数据"))
    section("容量与稳定性结论", ("专项", "稳定并发", "稳定实际 RPS", "拐点", "结论"), capacity_rows, "稳定仅指当前阶梯内满足后端阈值（请求成功率至少 95%）的观察值。未出现拐点不是无限容量。recovery 为负载回落观察，不等于重启、断网或故障注入恢复时间。")
    resource = summary.get("resource_correlation") or {}
    field_labels = {"cpu_percent": "CPU 使用率", "memory_percent": "内存使用率", "gpu_percent": "GPU 使用率", "gpu_memory_percent": "显存使用率"}
    resource_rows = []
    for field, label in field_labels.items():
        stats = (resource.get("statistics") or {}).get(field) or {}
        count = stats.get("count", 0)
        resource_rows.append((label, count, display(stats.get("min"), "%"), display(stats.get("mean"), "%"), display(stats.get("max"), "%"), display((resource.get("throughput_correlations") or {}).get(field))))
        if not count:
            missing.append(label + "未采集")
    section("服务器资源与吞吐关联", ("指标", "有效采样数", "最低", "平均", "峰值", "与 RPS 相关系数"), resource_rows, f"采集目标：{resource.get('server_name') or '未绑定'}。主机级指标可能包含其他业务；相关不代表因果。缺失、常量或不足三组同时间窗样本不填零。{resource.get('unavailable_reason') or resource.get('note') or ''}")
    section("性能阶段资源对齐", ("专项 / 阶段", "采样数", "CPU 平均 %", "内存平均 %", "GPU 平均 %", "显存平均 %"), [(str(row.get("evaluation_stage") or "") + " / " + row.get("phase", ""), row.get("sample_count"), display(row.get("cpu_percent")), display(row.get("memory_percent")), display(row.get("gpu_percent")), display(row.get("gpu_memory_percent"))) for row in resource.get("aligned_stages") or []], "按实际阶段时间窗对齐，排除开始五秒的跨阶段采样；短暂阶段无样本将缺席此表，不补零。")
    section("失败断言与复核证据", ("用例", "执行状态", "失败检查 / 异常", "复核意见"), evidence[:30], f"异常或有复核意见共 {len(evidence)} 条，此处展示前 30 条；完整逐例结果见后续用例表，全部原文、规则期望与裁判理由见证据包。" if evidence else "未记录失败断言或复核意见；不表示模型回答已由人工确认。")
    total_native = len(native)
    passed = sum(item.get("status") == "passed" for item in native)
    finished = sum((actual.get(c["id"]) or {}).get("status") == "completed" for c in configs)
    report.update(schema_version="2.0", run_kind_text="全量测评（全部维度）", sections=sections, performance_rows=[], quality_score=None)
    report["conclusion"] = {"level": "warning" if missing or passed < total_native else "good", "title": f"已执行 {finished}/{len(configs)} 个专项；" + ("证据覆盖不完整" if missing else "指标已汇总，需结合业务阈值判断"), "summary": f"原生质量用例已执行 {total_native} 条，用例达标 {passed} 条；性能阶段独立列出全部请求、时延和吞吐。" + ("缺项：" + "；".join(missing) + "。" if missing else "") + "自动规则及裁判结论不等于事实正确保证。"}
    report["metrics"] = [
        {"key": "success_rate", "label": "原生用例达标率", "value": display(passed / total_native * 100, "%") if total_native else "未执行", "explanation": f"{passed}/{total_native} 条已执行原生用例；未混入 BLEU、问答和性能请求。", "assessment": "规则检查"},
        {"key": "quality_score", "label": "跨专项总质量分", "value": "不混算", "explanation": "分类得分、ROUGE、BLEU 与性能使用不同量纲，分别报告。", "assessment": "独立口径"},
        {"key": "coverage", "label": "专项执行覆盖", "value": f"{finished}/{len(configs)}", "explanation": "以专项终态和实际样本核验全量范围，缺测单独标注。", "assessment": "见覆盖矩阵"},
        {"key": "judge_coverage", "label": "有效主观裁判", "value": f"{judge_valid}/{judge_required}", "explanation": "分母为配置 rubric 的质量样本数，输出无效或未配置不当作有效评分。", "assessment": "评分证据"},
        {"key": "manual_review", "label": "人工已复核", "value": str(reviewed), "explanation": "仅用户提交的人工复核计数，自动裁判不充当人工验收。", "assessment": "业务复核"},
    ]
    report["case_summary"] = {"total": sum(c.get("expected_cases", 0) for c in configs if c["backend"] == "native"), "passed": passed, "failed": sum(r.get("status") == "failed" for r in native), "error": sum(r.get("status") == "error" for r in native)}
    adapter_ids = {c["id"] for c in configs if c["backend"] != "native" and c["mode"] != "perf"}
    for row in report["cases"]:
        if str(row["case_id"]).split(":", 1)[0] in adapter_ids:
            row["status_text"] = "调用成功" if row["status"] in {"passed", "completed", "succeeded"} else "调用异常"
            row["score"] = None  # Corpus-level adapter metrics belong in their own table.
    report["case_summary_text"] = f"全部明细 {len(report['cases'])} 条；原生质量 {total_native} 条（达标 {passed}、未达标 {report['case_summary']['failed']}、异常 {report['case_summary']['error']}）；适配器 {sum(len(by_stage[key]) for key in adapter_ids)} 条，调用成功不等于质量达标。"
    report["findings"] = [{"level": "warning" if missing else "good", "title": "覆盖核验", "detail": "；".join(missing) if missing else "全部专项已运行，裁判与四类资源指标有有效结果。"}, {"level": "warning", "title": "结论边界", "detail": "固定样例、模板变体和小规模适配器烟测适合回归，不构成公开基准排名。语义事实、因果推断与引用来源应结合原文复核。"}]
    report["recommendations"] = (["补齐未测项目：" + "；".join(missing) + "。"] if missing else []) + ["对照逐项指标制定本业务的验收阈值，按对应负载的 P95/P99 和错误率评估。", "复核报告与情报样本中的数字、时间、来源和因果推断；使用项目真实素材补充代表性。", "保留报告编号及证据包，以相同数据版本、生成参数和负载进行复测。"]
    report["notes"] = ["全量组合哈希：" + str((snapshot.get("task_config") or {}).get("bundle_sha256") or "未记录"), "被测模型：" + str((snapshot.get("model") or {}).get("model_name") or report["model_name"]), "独立裁判：" + str((snapshot.get("judge") or {}).get("model_name") or "未配置"), "未设置业务 SLA 时不自动宣布生产可用；未进行模型服务重启或最大上下文窗口探测。", "HTML、DOCX、PDF 使用同一报告数据模型。全量证据 ZIP 含各专项输入快照、原始响应、指标、资源采样和逐例评分；人工复核以包内最新结果为准。"]
    return report


def build_evidence_archive(root, run, results):
    """Export an allowlisted, self-contained evidence package with current reviews."""
    import hashlib
    import json
    import os
    import tempfile
    import zipfile
    from pathlib import Path

    root = Path(root).resolve()
    approved = {"full_evidence.json", "summary.json", "responses.jsonl", "performance_samples.csv", "resource_samples.csv", "resource_observations.json", "test_environment.json"}
    descriptor, temporary = tempfile.mkstemp(prefix="evidence-", suffix=".zip", dir=root)
    os.close(descriptor)
    destination = root / "model_evaluation_evidence.zip"
    try:
        manifest = []
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            def add(name, data):
                archive.writestr(name, data)
                manifest.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            for path in sorted(root.rglob("*")):
                if path.name in approved and path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root):
                    add(path.relative_to(root).as_posix(), path.read_bytes())
            # Saved backend evidence remains immutable; current manual reviews
            # and sampler summary are separately identifiable and authoritative.
            add("current_run.json", json.dumps(run, ensure_ascii=False, indent=2).encode())
            add("reviewed_results.json", json.dumps(results, ensure_ascii=False, indent=2).encode())
            from auto_test.reporting.model_evaluation import build_model_evaluation_report
            from auto_test.reporting.model_assessment import ANALYSIS_FILE, PROMPT_VERSION, SYSTEM_PROMPT, load_analysis
            current_report = build_model_evaluation_report(run, results)
            load_analysis(current_report, root)
            if current_report["analysis"].get("status") == "completed":
                add(ANALYSIS_FILE, json.dumps(current_report["analysis"], ensure_ascii=False, indent=2).encode())
                add("analysis_input.json", json.dumps({"decision": current_report["assessment_decision"], "evidence": current_report["assessment_evidence"]}, ensure_ascii=False, indent=2).encode())
                add("analysis_prompt.txt", (PROMPT_VERSION + "\n" + SYSTEM_PROMPT).encode())
            add("README.txt", "current_run.json 为下载时运行状态与资源汇总；reviewed_results.json 包含最新人工复核。full_evidence.json 与各专项原始文件保留执行时输入、响应、评分和指标。manifest.json 用于校验每个文件。".encode())
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        os.replace(temporary, destination)
        return destination
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()
