"""
调用 LLM 分析 Docker 错误日志。
"""

from auto_test.common.logging import log


def analyze_errors(error_files, llm_api_key, llm_api_url, llm_model, run_dir,
                   stress_summary=None, *, model_call=None):
    """
    分析 Docker 错误日志 + 压测报告，合并写入 analysis.md。
    """
    if not error_files and not stress_summary:
        return ""
    if not llm_api_key:
        return ""

    import os
    report_dir = os.path.join(run_dir, "report")
    os.makedirs(report_dir, exist_ok=True)
    analysis_path = os.path.join(report_dir, "analysis.md")

    api_url = llm_api_url.rstrip("/")
    all_answers = []

    def invoke(prompt):
        if model_call is None:
            return _call_llm(api_url, llm_api_key, llm_model, prompt)
        try:
            return model_call(prompt)
        except Exception as exc:
            log.warning('模型日志分析未完成：%s', type(exc).__name__)
            return ''

    # Docker 错误日志分析
    sections = []
    for fpath in (error_files or []):
        with open(fpath, "r", encoding="utf-8") as f:
            content = f.read()
        name = fpath.rsplit("errors_", 1)[-1].replace(".log", "")
        sections.append(f"{name}：\n{content[:5000]}\n")

    if sections:
        prompt = (
            "以下是蓝鲨系统本次测试中各 Docker 容器的错误日志，请分析：\n"
            "1. 每个容器的错误类型和根因\n"
            "2. 哪些错误与本次测试失败直接相关\n"
            "3. 建议的处理措施\n"
            "注：容器日志中的时间戳为 UTC 时区（比北京时间晚 8 小时），分析报告中请标注为 UTC。\n\n"
            + "\n".join(sections)
        )
        log.info(f"LLM 分析（错误日志）：调用 {llm_model}")
        answer = invoke(prompt)
        if answer:
            all_answers.append("## 错误日志分析\n\n" + answer)

    # 压测结果分析
    if stress_summary:
        try:
            stress_path = os.path.join(
                os.path.dirname(os.path.dirname(run_dir)), "stress", "report", "stress_summary.txt"
            ) if not os.path.exists(stress_summary) else stress_summary
            full_stress_path = os.path.join(run_dir, "..", "stress", "report", "stress_summary.txt")
            if os.path.exists(full_stress_path):
                stress_summary = full_stress_path
        except Exception:
            pass

        if os.path.exists(stress_summary or ""):
            with open(stress_summary, "r", encoding="utf-8") as f:
                stress_content = f.read()
            prompt = (
                "以下是服务器压测结果报告，请分析：\n"
                "1. CPU温度是否正常\n"
                "2. 所有系统硬件错误（segfault）的严重性和可能原因\n"
                "3. 建议处理措施\n\n"
                + stress_content
            )
            log.info(f"LLM 分析（压测报告）：调用 {llm_model}")
            answer = invoke(prompt)
            if answer:
                all_answers.append("## 压测结果分析\n\n" + answer)

    if not all_answers:
        return ""

    with open(analysis_path, "w", encoding="utf-8") as f:
        f.write("\n\n---\n\n".join(all_answers))
    log.info(f"LLM 分析完成，已保存：{analysis_path}")
    return "\n\n---\n\n".join(all_answers)


def _call_llm(api_url, api_key, model, prompt):
    try:
        import requests
        resp = requests.post(
            api_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "你是一个运维分析助手，请用中文简洁回答，输出格式使用 Markdown。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 2000,
            },
            timeout=120,
        )
        if resp.status_code != 200:
            log.error(f"LLM 调用失败：HTTP {resp.status_code} — {resp.text[:300]}")
            return ""
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.error(f"LLM 调用异常：{e}")
        return ""
