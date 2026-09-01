"""Shared encryption helpers for secrets persisted by the platform."""

from __future__ import annotations

import base64
import hashlib

from auto_test.common.env import get_env


class SecretEncryptionError(RuntimeError):
    """Raised when a platform secret cannot be encrypted or decrypted."""


def _fernet():
    master = str(get_env("MASTER_KEY", "")).strip()
    if not master:
        raise SecretEncryptionError("未配置 LIEMA_MASTER_KEY，不能安全保存或读取凭据")
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise SecretEncryptionError("缺少 cryptography 依赖，不能安全加密凭据") from exc
    derived = base64.urlsafe_b64encode(hashlib.sha256(master.encode("utf-8")).digest())
    return Fernet(derived)


def encrypt_secret(secret: str) -> str:
    if not secret:
        return ""
    return _fernet().encrypt(secret.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except SecretEncryptionError:
        raise
    except Exception as exc:
        raise SecretEncryptionError("凭据无法解密，请检查 LIEMA_MASTER_KEY 是否与加密时一致") from exc
