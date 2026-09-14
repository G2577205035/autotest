"""Encrypted, page-managed model configuration and OpenAI-compatible calls."""

from __future__ import annotations

from typing import Any

import requests

from auto_test.common.paths import PROJECT_ROOT, prepare_runtime_layout
from auto_test.common.env import get_env
from auto_test.platform.contracts import PlatformRepository
from auto_test.platform.persistence import create_platform_repository
from auto_test.platform.secrets import (
    SecretEncryptionError,
    decrypt_secret as decrypt_platform_secret,
    encrypt_secret as encrypt_platform_secret,
)


class ModelSecretError(SecretEncryptionError):
    pass


class ModelResponseError(RuntimeError):
    """A safe diagnostic; never include remote response bodies or credentials."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def encrypt_secret(secret: str) -> str:
    try:
        return encrypt_platform_secret(secret)
    except SecretEncryptionError as exc:
        raise ModelSecretError(str(exc).replace("凭据", "模型 Key")) from exc


def decrypt_secret(ciphertext: str) -> str:
    try:
        return decrypt_platform_secret(ciphertext)
    except SecretEncryptionError as exc:
        raise ModelSecretError(str(exc).replace("凭据", "模型 Key")) from exc


def public_profile(profile: dict[str, Any]) -> dict[str, Any]:
    item = {key: value for key, value in profile.items() if key != "api_key_enc"}
    item["has_api_key"] = bool(profile.get("api_key_enc"))
    item["api_key_masked"] = "••••••••" if profile.get("api_key_enc") else ""
    return item


def save_profile(store: PlatformRepository, data: dict[str, Any]) -> dict[str, Any]:
    raw_key = str(data.pop("api_key", "") or "").strip()
    encrypted = encrypt_secret(raw_key) if raw_key else None
    return public_profile(store.save_model_profile(data, encrypted))


def get_active_model_config(store: PlatformRepository | None = None, *, project_id: str = "") -> dict[str, Any]:
    if store is None:
        prepare_runtime_layout()
    store = store or create_platform_repository(PROJECT_ROOT, recover_jobs=False)
    from auto_test.platform.model_access import ProjectModelStore
    profile = ProjectModelStore(store, project_id).active_model_profile()
    if not profile:
        return {}
    item = dict(profile)
    item["api_key"] = decrypt_secret(item.pop("api_key_enc", ""))
    item["api_url"] = _chat_url(str(item.get("base_url", "")))
    item["model"] = item.get("model_name", "")
    return item


def _chat_url(base_url: str) -> str:
    url = base_url.rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return f"{url}/chat/completions"
    return f"{url}/v1/chat/completions"


def call_model(
    profile: dict[str, Any], prompt: str, *, system_prompt: str | None = None,
    timeout: float = 60,
) -> str:
    api_key = profile.get("api_key") or decrypt_secret(profile.get("api_key_enc", ""))
    if not api_key:
        raise ModelSecretError("模型配置中没有 API Key")
    url = _chat_url(str(profile["base_url"]))
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": profile["model_name"],
            "messages": [
                {"role": "system", "content": system_prompt or profile.get("system_prompt") or "你是企业软件测试报告助手。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": float(profile.get("temperature", 0.3)),
            "max_tokens": int(profile.get("max_tokens", 2000)),
        },
        timeout=timeout,
        verify=str(get_env("MODEL_CA_BUNDLE", "")).strip() or True,
    )
    if response.status_code != 200:
        raise ModelResponseError("http_error", f"模型接口返回 HTTP {response.status_code}，请检查接口地址、认证和服务状态")
    try:
        data = response.json()
    except ValueError as exc:
        raise ModelResponseError("invalid_response_json", "模型接口返回了非 JSON 内容（可能是网关或登录页面），请检查模型地址和网络代理") from exc
    try:
        choice = data["choices"][0]
        message = choice["message"]
        if choice.get("finish_reason") == "length":
            raise ModelResponseError("output_truncated", "模型输出达到长度上限，分析内容被截断；请提高模型配置的最大 Tokens，或选择能在该预算内完成输出的模型")
        if choice.get("finish_reason") == "content_filter" or message.get("refusal"):
            raise ModelResponseError("output_refused", "模型未提供分析正文，请检查所选模型的内容限制")
        content = message.get("content")
        if isinstance(content, list):
            # Some compatible services return text content blocks. Reasoning
            # blocks are not final answers and must never become report text.
            content = "".join(block["text"] for block in content if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str))
        if not isinstance(content, str) or not content.strip():
            raise ModelResponseError("empty_content", "模型未返回有效正文（可能仅返回思考内容），请检查模型输出设置或选择其他模型")
        return content.strip()
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ModelResponseError("invalid_response_structure", "模型接口响应缺少有效的 choices[0].message，请检查 OpenAI 兼容接口配置") from exc


def test_profile(store: PlatformRepository, profile_id: str) -> dict[str, Any]:
    profile = store.get_model_profile(profile_id)
    if not profile:
        raise KeyError(profile_id)
    from auto_test.platform.model_access import ProjectModelStore
    invoke = store.call_model if isinstance(store, ProjectModelStore) else call_model
    answer = invoke(
        profile,
        "只回复：连接成功",
        system_prompt="你是模型连通性检测助手，只输出四个汉字：连接成功。",
        timeout=30,
    )
    return {"success": True, "message": answer[:120]}
