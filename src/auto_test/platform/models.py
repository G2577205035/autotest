"""Encrypted, page-managed model configuration and OpenAI-compatible calls."""

from __future__ import annotations

from typing import Any

import requests

from auto_test.common.paths import PROJECT_ROOT, prepare_runtime_layout
from auto_test.platform.contracts import PlatformRepository
from auto_test.platform.persistence import create_platform_repository
from auto_test.platform.secrets import (
    SecretEncryptionError,
    decrypt_secret as decrypt_platform_secret,
    encrypt_secret as encrypt_platform_secret,
)


class ModelSecretError(SecretEncryptionError):
    pass


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


def get_active_model_config(store: PlatformRepository | None = None) -> dict[str, Any]:
    if store is None:
        prepare_runtime_layout()
    store = store or create_platform_repository(PROJECT_ROOT)
    profile = store.active_model_profile()
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
    )
    if response.status_code != 200:
        raise RuntimeError(f"模型接口返回 HTTP {response.status_code}: {(response.text or '')[:300]}")
    data = response.json()
    try:
        return str(data["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("模型接口响应中缺少 choices[0].message.content") from exc


def test_profile(store: PlatformRepository, profile_id: str) -> dict[str, Any]:
    profile = store.get_model_profile(profile_id)
    if not profile:
        raise KeyError(profile_id)
    answer = call_model(
        profile,
        "只回复：连接成功",
        system_prompt="你是模型连通性检测助手，只输出四个汉字：连接成功。",
        timeout=30,
    )
    return {"success": True, "message": answer[:120]}
