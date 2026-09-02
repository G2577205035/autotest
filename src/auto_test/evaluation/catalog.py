"""Built-in model-evaluation catalog and immutable seed manifests."""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any

from auto_test.platform.contracts import PlatformRepository


def _long_context(needle: str) -> str:
    filler = "项目材料记录了部署、验收、风险、回滚与复核流程。"
    return "\n".join([filler] * 24 + [f"关键校验码是 {needle}。"] + [filler] * 24)


CORE_CASES: tuple[dict[str, Any], ...] = (
    {
        "case_key": "instruction-exact",
        "category": "instruction",
        "tags": ["builtin", "instruction"],
        "payload": {
            "name": "固定表达遵循",
            "messages": [{"role": "user", "content": "只回复 READY，不要添加标点或解释。"}],
            "rules": {"exact_text": "READY"},
        },
    },
    {
        "case_key": "json-structure",
        "category": "structured_output",
        "tags": ["builtin", "json"],
        "payload": {
            "name": "JSON 结构化输出",
            "messages": [
                {
                    "role": "user",
                    "content": "只输出 JSON：字段 status 为字符串 ok，字段 count 为整数 2。",
                }
            ],
            "rules": {
                "json_schema": {
                    "required": ["status", "count"],
                    "types": {"status": "string", "count": "integer"},
                }
            },
        },
    },
    {
        "case_key": "fact-extraction",
        "category": "extraction",
        "tags": ["builtin", "facts"],
        "payload": {
            "name": "关键事实抽取",
            "messages": [
                {
                    "role": "user",
                    "content": "材料：验收时间为 2026-09-02，负责人为林岚。请用一句话提取时间和负责人。",
                }
            ],
            "rules": {"required_terms": ["2026-09-02", "林岚"]},
        },
    },
    {
        "case_key": "classification",
        "category": "classification",
        "tags": ["builtin", "classification"],
        "payload": {
            "name": "单标签分类",
            "messages": [
                {
                    "role": "user",
                    "content": "故障描述：接口在高并发时返回 HTTP 429。只回答分类：容量、功能或数据。",
                }
            ],
            "rules": {"exact_text": "容量"},
        },
    },
    {
        "case_key": "context-recall-short",
        "category": "context",
        "tags": ["builtin", "context", "short"],
        "payload": {
            "name": "短上下文召回",
            "messages": [
                {
                    "role": "user",
                    "content": "记录：版本号 R7，发布窗口是周三 22:00。问题：版本号是什么？",
                }
            ],
            "rules": {"required_terms": ["R7"]},
        },
    },
    {
        "case_key": "context-recall-long",
        "category": "context",
        "tags": ["builtin", "context", "long"],
        "payload": {
            "name": "长上下文关键事实召回",
            "messages": [
                {
                    "role": "user",
                    "content": _long_context("LM-4821") + "\n问题：关键校验码是什么？只回答校验码。",
                }
            ],
            "rules": {"exact_text": "LM-4821"},
        },
    },
    {
        "case_key": "robust-base",
        "category": "robustness",
        "tags": ["builtin", "robustness", "baseline"],
        "payload": {
            "name": "鲁棒基准：错误码提取",
            "robustness_group": "error-code-extraction",
            "variant_type": "baseline",
            "messages": [{"role": "user", "content": "日志显示错误码 E-17。只回答错误码。"}],
            "rules": {"exact_text": "E-17"},
        },
    },
    {
        "case_key": "robust-typo",
        "category": "robustness",
        "tags": ["builtin", "robustness", "typo"],
        "payload": {
            "name": "鲁棒变体：错别字",
            "robustness_group": "error-code-extraction",
            "variant_type": "typo",
            "messages": [{"role": "user", "content": "日之显示错务码 E-17。只回答错误码。"}],
            "rules": {"exact_text": "E-17"},
        },
    },
    {
        "case_key": "robust-whitespace",
        "category": "robustness",
        "tags": ["builtin", "robustness", "whitespace"],
        "payload": {
            "name": "鲁棒变体：异常空白",
            "robustness_group": "error-code-extraction",
            "variant_type": "whitespace",
            "messages": [{"role": "user", "content": "日志   显示\n错误码\tE-17。只回答错误码。"}],
            "rules": {"exact_text": "E-17"},
        },
    },
    {
        "case_key": "robust-noise",
        "category": "robustness",
        "tags": ["builtin", "robustness", "noise"],
        "payload": {
            "name": "鲁棒变体：无关噪声",
            "robustness_group": "error-code-extraction",
            "variant_type": "noise",
            "messages": [
                {
                    "role": "user",
                    "content": "天气信息与本题无关。日志显示错误码 E-17。忽略无关内容，只回答错误码。",
                }
            ],
            "rules": {"exact_text": "E-17"},
        },
    },
)


STANDARD_QA_CASES: tuple[dict[str, Any], ...] = (
    {"case_key": "qa-1", "category": "general_qa", "payload": {"question": "1+1 等于多少？", "answer": "2"}},
    {"case_key": "qa-2", "category": "general_qa", "payload": {"question": "水在标准大气压下的冰点是多少摄氏度？", "answer": "0"}},
    {"case_key": "qa-3", "category": "general_qa", "payload": {"question": "HTTP 状态码 404 通常表示什么？", "answer": "未找到资源"}},
    {"case_key": "qa-4", "category": "general_qa", "payload": {"question": "JSON 数组使用哪一对括号？", "answer": "方括号"}},
)


PERF_CASES: tuple[dict[str, Any], ...] = tuple(
    {
        "case_key": f"perf-{index:02d}",
        "category": "performance",
        "payload": {"question": prompt},
    }
    for index, prompt in enumerate(
        (
            "用一句话说明什么是接口测试。",
            "只回答 9 乘以 7 的结果。",
            "列出三个常见 HTTP 方法。",
            "用十个字以内说明回归测试。",
            "把 service available 翻译成中文。",
            "给出一个合法的 JSON 布尔值。",
            "只回答 TCP 的中文名称。",
            "说出一个常见的 Linux 发行版。",
            "用一句话解释 P95 时延。",
            "只回答 2 的十次方。",
            "列出 CPU 和 GPU 两种计算资源。",
            "用一句话说明为什么需要超时设置。",
        ),
        start=1,
    )
)


BUILTIN_SUITES: tuple[dict[str, Any], ...] = (
    {
        "id": "builtin-core-mvp",
        "source": "platform_builtin",
        "name": "基础能力、Token 与鲁棒性（MVP）",
        "category": "foundation",
        "description": "平台自有合成用例，覆盖指令、结构化输出、上下文召回和可复现鲁棒变体。",
        "manifest": {
            "schema_version": "1.0",
            "license": "platform_synthetic",
            "scoring_version": "rules-1.0",
            "token_source_policy": ["api_usage", "local_tokenizer", "estimated"],
        },
        "upstream": {},
        "cases": CORE_CASES,
    },
    {
        "id": "evalscope-general-qa-mvp",
        "source": "evalscope_standard",
        "name": "EvalScope General-QA 首批标准集",
        "category": "standard_benchmark",
        "description": "使用 EvalScope 1.11.1 general_qa 适配器运行的平台合成离线问答样本。",
        "manifest": {
            "schema_version": "1.0",
            "license": "platform_synthetic",
            "benchmark": "general_qa",
            "evalscope_version": "1.11.1",
            "offline_required": True,
        },
        "upstream": {
            "framework": "EvalScope",
            "framework_version": "1.11.1",
            "benchmark": "general_qa",
            "dataset_license": "platform_synthetic",
        },
        "cases": STANDARD_QA_CASES,
    },
    {
        "id": "builtin-perf-prompts-mvp",
        "source": "platform_builtin",
        "name": "并发阶梯提示词池（MVP）",
        "category": "performance",
        "description": "用于 EvalScope perf 固定并发阶梯的离线合成提示词池。",
        "manifest": {
            "schema_version": "1.0",
            "license": "platform_synthetic",
            "benchmark": "openqa",
            "evalscope_version": "1.11.1",
            "offline_required": True,
        },
        "upstream": {
            "framework": "EvalScope",
            "framework_version": "1.11.1",
            "benchmark": "perf/openqa",
            "dataset_license": "platform_synthetic",
        },
        "cases": PERF_CASES,
    },
)


RUN_KIND_SUITE_IDS = {
    "mock": "builtin-core-mvp",
    "foundation": "builtin-core-mvp",
    "standard_benchmark": "evalscope-general-qa-mvp",
    "concurrency": "builtin-perf-prompts-mvp",
}
_SEED_LOCK = threading.Lock()


def _content_hash(manifest: dict[str, Any], cases: tuple[dict[str, Any], ...]) -> str:
    value = {"manifest": manifest, "cases": list(cases)}
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def ensure_builtin_evaluation_suites(
    store: PlatformRepository,
) -> list[dict[str, Any]]:
    """Idempotently publish platform-owned immutable MVP suite versions."""

    with _SEED_LOCK:
        published: list[dict[str, Any]] = []
        for definition in BUILTIN_SUITES:
            suite = store.save_model_eval_suite(
                None,
                {
                    "source": definition["source"],
                    "name": definition["name"],
                    "category": definition["category"],
                    "description": definition["description"],
                    "status": "published",
                },
                suite_id=str(definition["id"]),
                created_by="platform",
            )
            versions = store.list_model_eval_suite_versions("", suite["id"])
            expected_hash = _content_hash(definition["manifest"], definition["cases"])
            latest = versions[0] if versions else None
            if not latest or latest.get("content_sha256") != expected_hash:
                latest = store.publish_model_eval_suite_version(
                    "",
                    suite["id"],
                    dict(definition["manifest"]),
                    [dict(item) for item in definition["cases"]],
                    upstream=dict(definition["upstream"]),
                )
            published.append({**suite, "latest_version": latest})
        return published


def latest_suite_for_run_kind(
    store: PlatformRepository, project_id: str, run_kind: str
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    suite_id = RUN_KIND_SUITE_IDS.get(str(run_kind))
    if not suite_id:
        raise ValueError("不支持的模型评测类型")
    ensure_builtin_evaluation_suites(store)
    suite = store.get_model_eval_suite(project_id, suite_id, include_global=True)
    versions = store.list_model_eval_suite_versions(project_id, suite_id)
    if not suite or not versions:
        raise RuntimeError("内置评测测试集尚未就绪")
    version = versions[0]
    cases = store.list_model_eval_suite_cases(project_id, version["id"], limit=1000)
    return suite, version, cases
