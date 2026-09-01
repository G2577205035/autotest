"""API routes for the local enterprise testing console."""

from __future__ import annotations

import json
import shutil
import re
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

import requests

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from auto_test.common.env import get_env, has_env
from auto_test.common.config_loader import (
    ai_checks_cfg,
    default_translate_name,
    export_cfg,
    monitor_cfg,
    stress_cfg,
    task_queue_cfg,
    upload_cfg,
)
from auto_test.common.paths import PROJECT_ROOT, UPLOADS_DIR, prepare_runtime_layout
from auto_test.monitoring.server_stress import ServerStressManager
from auto_test.monitoring.server_sessions import ServerSessionManager
from auto_test.platform.artifact_storage import ArtifactStorage, create_artifact_storage
from auto_test.platform.contracts import PlatformRepository, TaskRepository
from auto_test.platform.interface_specs import parse_curl_request, parse_endpoint_text
from auto_test.platform.models import ModelSecretError, public_profile, save_profile, test_profile
from auto_test.platform.persistence import create_platform_repository
from auto_test.platform.secrets import SecretEncryptionError, decrypt_secret, encrypt_secret
from auto_test.platform.store import DEFAULT_REPORT_SECTIONS
from auto_test.platform.task_store import TaskNotFoundError, TaskStore
from auto_test.platform.upload_ownership import write_upload_owner
from auto_test.reporting.interface_scenario import generate_interface_scenario_report
from auto_test.reporting.manager import ReportManager
from auto_test.core.task_queue import TaskSignalQueue
from auto_test.core.interface_scenario_manager import InterfaceScenarioManager


BASE_DIR = PROJECT_ROOT
DEFAULT_UPLOAD_MAX_FILES = 20_000
HARD_UPLOAD_MAX_FILES = 100_000
INTERFACE_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
INTERFACE_VARIABLE_PATTERN = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_.-]{0,127})\}\}")
INTERFACE_RESPONSE_PREVIEW_BYTES = 2 * 1024 * 1024
SENSITIVE_HEADER_PARTS = ("authorization", "cookie", "token", "secret", "api-key", "apikey")
INTERFACE_BODY_TYPES = {"none", "json", "raw", "urlencoded", "multipart"}
INTERFACE_SCENARIO_SOURCES = {"json", "header", "body", "status", "elapsed_ms"}
INTERFACE_ASSERTION_OPERATORS = {
    "equals", "not_equals", "contains", "not_contains", "exists", "not_exists",
    "greater_than", "greater_or_equal", "less_than", "less_or_equal", "matches",
    "between",
}
INTERFACE_SENSITIVE_NAME_PATTERN = re.compile(
    r"(?:pass(?:word)?|secret|token|cookie|authorization|api[-_.]?key|credential)", re.I
)


def validate_relative_interface_path(path: str, *, label: str = "接口 path") -> str:
    normalized = str(path or "").strip()
    parsed = urlparse(normalized)
    path_parts = PurePosixPath(unquote(parsed.path or "/")).parts
    if parsed.netloc or parsed.fragment or not normalized.startswith("/") or ".." in path_parts:
        raise HTTPException(
            status_code=400,
            detail=f"{label} 必须以 / 开头，且不能包含主机、Fragment 或上级路径 ..",
        )
    return normalized


def validate_interface_target(target: str) -> str:
    normalized = str(target or "").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="请求地址不能为空")
    parsed = urlparse(normalized)
    if parsed.scheme:
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise HTTPException(
                status_code=400,
                detail="请求地址必须是有效的 http://、https:// 完整 URL，或以 / 开头的相对路径",
            )
        if parsed.username or parsed.password:
            raise HTTPException(status_code=400, detail="请求地址不能包含账号或密码")
        if parsed.fragment:
            raise HTTPException(status_code=400, detail="请求地址不能包含 #fragment")
        if ".." in PurePosixPath(unquote(parsed.path or "/")).parts:
            raise HTTPException(status_code=400, detail="请求地址不能包含上级路径 ..")
        return normalized
    return validate_relative_interface_path(normalized, label="请求地址")


def resolve_interface_template(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {
            str(key): resolve_interface_template(item, variables)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [resolve_interface_template(item, variables) for item in value]
    if not isinstance(value, str):
        return value

    def substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            raise HTTPException(status_code=400, detail=f"未找到接口变量：{key}")
        return variables[key]

    return INTERFACE_VARIABLE_PATTERN.sub(substitute, value)


def redact_interface_headers(headers: dict[str, Any]) -> dict[str, str]:
    result = {}
    for name, value in headers.items():
        normalized_name = str(name)
        lowered = normalized_name.lower()
        result[normalized_name] = (
            "••••••"
            if any(part in lowered for part in SENSITIVE_HEADER_PARTS)
            else str(value)
        )
    return result


def interface_json_path_value(document: Any, expression: str) -> Any:
    """Resolve a deliberately small JSONPath subset: ``$.a[0].b`` / ``a.0.b``."""
    normalized = str(expression or "").strip()
    if normalized in {"", "$"}:
        return document
    if normalized.startswith("$"):
        normalized = normalized[1:]
    normalized = normalized.lstrip(".")
    normalized = re.sub(r"\[(['\"])(.*?)\1\]", lambda match: "." + match.group(2), normalized)
    normalized = re.sub(r"\[(\d+)\]", r".\1", normalized)
    current = document
    for part in [item for item in normalized.split(".") if item != ""]:
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
            continue
        raise KeyError(expression)
    return current


def interface_response_value(
    response: dict[str, Any], source: str, expression: str = ""
) -> Any:
    source = str(source or "").strip().lower()
    if source not in INTERFACE_SCENARIO_SOURCES:
        raise ValueError(f"不支持的响应取值来源：{source}")
    if source == "status":
        return int(response.get("status_code") or 0)
    if source == "elapsed_ms":
        return float(response.get("elapsed_ms") or 0)
    if source == "header":
        expected = str(expression or "").strip().lower()
        for name, value in (response.get("headers") or {}).items():
            if str(name).lower() == expected:
                return value
        raise KeyError(expression)
    body = str(response.get("body") or "")
    if source == "body":
        if not expression:
            return body
        match = re.search(str(expression), body)
        if not match:
            raise KeyError(expression)
        return match.group(1) if match.lastindex else match.group(0)
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise ValueError("响应体不是合法 JSON") from exc
    return interface_json_path_value(payload, expression)


def evaluate_interface_assertion(actual: Any, operator: str, expected: Any = None) -> bool:
    operator = str(operator or "").strip().lower()
    if operator == "exists":
        return actual is not None
    if operator == "not_exists":
        return actual is None
    if operator == "equals":
        return actual == expected or str(actual) == str(expected)
    if operator == "not_equals":
        return not evaluate_interface_assertion(actual, "equals", expected)
    if operator == "contains":
        return str(expected) in str(actual)
    if operator == "not_contains":
        return str(expected) not in str(actual)
    if operator == "matches":
        return re.search(str(expected), str(actual)) is not None
    if operator == "between":
        bounds = expected if isinstance(expected, list) else str(expected or "").split(",")
        if len(bounds) != 2:
            raise ValueError("区间断言期望值必须包含下限和上限")
        return float(bounds[0]) <= float(actual) <= float(bounds[1])
    comparisons = {
        "greater_than": lambda left, right: left > right,
        "greater_or_equal": lambda left, right: left >= right,
        "less_than": lambda left, right: left < right,
        "less_or_equal": lambda left, right: left <= right,
    }
    if operator in comparisons:
        return comparisons[operator](float(actual), float(expected))
    raise ValueError(f"不支持的断言方式：{operator}")


def redact_interface_values(value: Any, secret_values: set[str]) -> Any:
    """Redact known secret values recursively before persisting scenario results."""
    secrets = sorted(
        {str(item) for item in secret_values if item is not None and len(str(item)) >= 3},
        key=len,
        reverse=True,
    )
    if isinstance(value, dict):
        return {
            str(key): (
                "••••••"
                if INTERFACE_SENSITIVE_NAME_PATTERN.search(str(key))
                else redact_interface_values(item, secret_values)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_interface_values(item, secret_values) for item in value]
    if not isinstance(value, str):
        return value
    result = value
    for secret in secrets:
        result = result.replace(secret, "••••••")
    return result


def build_interface_request_options(prepared: dict[str, Any]) -> dict[str, Any]:
    """Build requests options while preserving the selected request body encoding."""
    options: dict[str, Any] = {
        "method": prepared["method"],
        "url": prepared["target"],
        "headers": prepared["headers"],
        "params": prepared["query"],
        "timeout": prepared["timeout_seconds"],
        "allow_redirects": False,
        "stream": True,
    }
    body_type = str(prepared.get("body_type") or "json")
    body = prepared.get("body")
    if body_type == "none" or body is None:
        return options
    if body_type == "json":
        options["json"] = body
    elif body_type == "raw":
        options["data"] = str(body)
    elif body_type == "urlencoded":
        options["data"] = body
    elif body_type == "multipart":
        files: list[tuple[str, tuple[None, str]]] = []
        for name, value in body.items():
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, (dict, list)):
                    item = json.dumps(item, ensure_ascii=False)
                elif item is None:
                    item = ""
                files.append((str(name), (None, str(item))))
        options["files"] = files
    else:
        raise ValueError(f"不支持的请求体类型：{body_type}")
    return options


class ModelProfileInput(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=80)
    provider: str = Field(default="openai-compatible", max_length=50)
    base_url: str = Field(min_length=8, max_length=500)
    model_name: str = Field(min_length=1, max_length=150)
    api_key: str = Field(default="", max_length=1000)
    temperature: float = Field(default=0.3, ge=0, le=2)
    max_tokens: int = Field(default=2000, ge=64, le=128000)
    system_prompt: str = Field(default="", max_length=10000)
    is_active: bool = False


class ReportTemplateInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    sections: list[dict[str, Any]]


class ReportRequest(BaseModel):
    title: str = Field(default="烈马自动化测试平台性能测试报告", max_length=200)
    report_number: str = Field(default="", max_length=100)
    prepared_by: str = Field(default="质量与性能测试团队", max_length=100)
    environment_notes: str = Field(default="", max_length=20000)
    custom_sections: dict[str, str] = Field(default_factory=dict)
    conclusion: str = Field(default="", max_length=30000)
    use_model_conclusion: bool = False


class EndpointImportInput(BaseModel):
    logical_name: str = Field(min_length=1, max_length=120)
    default_path: str = Field(default="", max_length=500)
    content: str = Field(min_length=1, max_length=2_000_000)
    module_id: str = Field(default="", max_length=64)
    environment_id: str = Field(default="", max_length=64)
    publish: bool = False


class InterfaceModuleInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    sort_order: int = Field(default=0, ge=0, le=10000)


class InterfaceEnvironmentInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(default="", max_length=1000)
    description: str = Field(default="", max_length=2000)
    is_default: bool = False


class InterfaceVariableInput(BaseModel):
    environment_id: str = Field(default="", max_length=64)
    key: str = Field(min_length=1, max_length=128)
    value: str = Field(default="", max_length=20000)
    is_secret: bool = False
    description: str = Field(default="", max_length=1000)


class InterfaceAssetInput(BaseModel):
    module_id: str = Field(default="", max_length=64)
    environment_id: str = Field(default="", max_length=64)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    method: str = Field(default="GET", min_length=3, max_length=16)
    path: str = Field(min_length=1, max_length=2000)
    default_path: str = Field(default="", max_length=500)
    request: dict[str, Any] = Field(default_factory=dict)
    publish: bool = False


class CurlParseInput(BaseModel):
    content: str = Field(min_length=1, max_length=2_000_000)


class InterfaceDebugInput(BaseModel):
    asset_id: str = Field(default="", max_length=64)
    method: str = Field(default="GET", min_length=3, max_length=16)
    target: str = Field(min_length=1, max_length=2000)
    environment_id: str = Field(default="", max_length=64)
    headers: dict[str, Any] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    body_type: str = Field(default="json", pattern="^(none|json|raw|urlencoded|multipart)$")
    body: Any = None
    timeout_seconds: float = Field(default=30, ge=1, le=120)


class InterfaceExtractionInput(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    source: str = Field(default="json", pattern="^(json|header|body|status|elapsed_ms)$")
    expression: str = Field(default="", max_length=1000)
    required: bool = True


class InterfaceAssertionInput(BaseModel):
    source: str = Field(default="status", pattern="^(json|header|body|status|elapsed_ms)$")
    expression: str = Field(default="", max_length=1000)
    operator: str = Field(
        default="equals",
        pattern="^(equals|not_equals|contains|not_contains|exists|not_exists|greater_than|greater_or_equal|less_than|less_or_equal|matches|between)$",
    )
    expected: Any = None
    message: str = Field(default="", max_length=500)


class InterfaceScenarioStepInput(BaseModel):
    id: str = Field(default="", max_length=64)
    name: str = Field(default="", max_length=120)
    asset_id: str = Field(min_length=1, max_length=64)
    environment_id: str = Field(default="", max_length=64)
    enabled: bool = True
    continue_on_failure: bool = False
    extractions: list[InterfaceExtractionInput] = Field(default_factory=list, max_length=30)
    assertions: list[InterfaceAssertionInput] = Field(default_factory=list, max_length=30)


class InterfaceScenarioInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    environment_id: str = Field(default="", max_length=64)
    parameters: dict[str, Any] = Field(default_factory=dict)
    steps: list[InterfaceScenarioStepInput] = Field(min_length=1, max_length=50)


class InterfaceScenarioExecuteInput(BaseModel):
    parameters: dict[str, Any] = Field(default_factory=dict)


class InterfaceScenarioBatchInput(BaseModel):
    scenario_ids: list[str] = Field(min_length=1, max_length=20)
    parameters: dict[str, Any] = Field(default_factory=dict)
    concurrency: int = Field(default=3, ge=1, le=5)


class StressJobInput(BaseModel):
    target: str = Field(default="server", pattern="^server$")
    session_id: str = Field(default="", max_length=64)
    server_name: str = Field(default="", max_length=200)
    host: str = Field(default="", max_length=300)
    port: int = Field(default=22, ge=1, le=65535)
    user: str = Field(default="", max_length=100)
    password: str = Field(default="", max_length=300)
    modes: list[str] = Field(default_factory=lambda: ["monitor"])
    modules: list[str] | str = Field(default_factory=list)
    duration: int = Field(default=60, ge=10, le=86400)
    workers: int = Field(default=0, ge=0, le=4096)
    cpu_load: int = Field(default=80, ge=1, le=100)
    gpu_burn_source: str = Field(default="", max_length=1000)
    gpu_burn_image: str = Field(default="", max_length=500)
    gpu_burn_blackwell_image: str = Field(default="", max_length=500)
    gpu_devices: str = Field(default="", max_length=200)
    allow_busy_gpu: bool = False
    test_preset: str = Field(default="custom", max_length=50)
    gpu_memory_percent: int = Field(default=90, ge=1, le=95)
    safety_enabled: bool = True
    cpu_temp_limit: float = Field(default=90, ge=40, le=110)
    gpu_temp_limit: float = Field(default=85, ge=40, le=110)
    memory_usage_limit: float = Field(default=95, ge=50, le=100)
    disk_usage_limit: float = Field(default=95, ge=50, le=100)
    metric_dimensions: list[str] = Field(default_factory=lambda: ["cpu", "gpu", "memory", "disk"])


class ServerCapabilityInput(BaseModel):
    target: str = Field(default="server", pattern="^server$")
    server_name: str = Field(default="", max_length=200)
    host: str = Field(min_length=1, max_length=300)
    port: int = Field(default=22, ge=1, le=65535)
    user: str = Field(min_length=1, max_length=100)
    password: str = Field(default="", max_length=300)
    modes: list[str] = Field(default_factory=lambda: ["monitor"])
    gpu_burn_source: str = Field(default="", max_length=1000)
    gpu_burn_image: str = Field(default="", max_length=500)
    gpu_burn_blackwell_image: str = Field(default="", max_length=500)
    gpu_devices: str = Field(default="", max_length=200)


class ServerProfileInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    host: str = Field(min_length=1, max_length=300)
    port: int = Field(default=22, ge=1, le=65535)
    user: str = Field(min_length=1, max_length=100)
    password: str = Field(default="", max_length=300)
    save_password: bool = False


class ServerSessionInput(BaseModel):
    profile_id: str = Field(default="", max_length=64)
    server_name: str = Field(default="", max_length=200)
    host: str = Field(default="", max_length=300)
    port: int = Field(default=22, ge=1, le=65535)
    user: str = Field(default="", max_length=100)
    password: str = Field(default="", max_length=300)


def _safe_upload_path(upload_id: str) -> Path:
    if not upload_id or any(ch not in "0123456789abcdef" for ch in upload_id.lower()):
        raise HTTPException(status_code=400, detail="invalid upload id")
    path = (UPLOADS_DIR / upload_id).resolve()
    try:
        path.relative_to(UPLOADS_DIR)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid upload path") from exc
    if not path.is_dir():
        raise HTTPException(status_code=404, detail="upload not found")
    return path


def _safe_filename(name: str) -> Path:
    normalized = name.replace("\\", "/")
    parts = [part for part in PurePosixPath(normalized).parts if part not in {"", ".", "/"}]
    if not parts or any(part == ".." for part in parts):
        raise HTTPException(status_code=400, detail=f"invalid filename: {name}")
    safe_parts = [part.replace(":", "_") for part in parts]
    return Path(*safe_parts)


def _upload_max_files() -> int:
    try:
        configured = int(get_env("UPLOAD_MAX_FILES", DEFAULT_UPLOAD_MAX_FILES))
    except (TypeError, ValueError):
        configured = DEFAULT_UPLOAD_MAX_FILES
    return max(1, min(configured, HARD_UPLOAD_MAX_FILES))


def create_platform_api(
    task_store: TaskRepository,
    *,
    platform_store: PlatformRepository | None = None,
    artifact_storage: ArtifactStorage | None = None,
    signal_queue: TaskSignalQueue | None = None,
) -> tuple[APIRouter, ReportManager, PlatformRepository, ServerStressManager]:
    prepare_runtime_layout(BASE_DIR)
    platform_store = platform_store or create_platform_repository(BASE_DIR)
    artifact_storage = artifact_storage or create_artifact_storage(BASE_DIR)
    report_manager = ReportManager(
        platform_store,
        task_store,
        artifact_storage,
        signal_queue=signal_queue,
    )
    server_session_manager = ServerSessionManager(platform_store)
    stress_manager = ServerStressManager(
        platform_store,
        artifact_storage,
        session_manager=server_session_manager,
    )
    router = APIRouter(prefix="/api")

    def identity_context(request: Request) -> dict[str, Any]:
        return getattr(request.state, "identity", {}) or {}

    def current_project_id(request: Request) -> str:
        return str((identity_context(request).get("current_project") or {}).get("id") or "")

    def legacy_visible(request: Request) -> bool:
        context = identity_context(request)
        if not bool((context.get("user") or {}).get("is_superuser")):
            return False
        legacy_project_id = str(context.get("legacy_project_id") or "")
        return bool(
            current_project_id(request)
            and (not legacy_project_id or current_project_id(request) == legacy_project_id)
        )

    def visible_in_project(item: dict[str, Any], request: Request) -> bool:
        project_id = str((item.get("options") or {}).get("_project_id") or "")
        if project_id:
            return project_id == current_project_id(request)
        return legacy_visible(request)

    def visible_server_session(item: dict[str, Any], request: Request) -> bool:
        project_id = str(item.get("project_id") or "")
        if project_id:
            return project_id == current_project_id(request)
        return legacy_visible(request)

    def get_visible_server_session(
        session_id: str, request: Request, *, touch: bool = False
    ) -> dict[str, Any]:
        try:
            session = server_session_manager.get(session_id, touch=touch)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="server session not found") from exc
        if not visible_server_session(session, request):
            raise HTTPException(status_code=404, detail="server session not found")
        return session

    def audit_interface_change(
        request: Request,
        action: str,
        target_type: str,
        target_id: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        context = identity_context(request)
        platform_store.add_audit_event(
            actor_user_id=str((context.get("user") or {}).get("id") or "") or None,
            project_id=current_project_id(request) or None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            outcome="success",
            ip_address=str(request.client.host if request.client else "")[:100],
            detail=detail or {},
        )

    def validate_base_url(base_url: str) -> str:
        normalized = str(base_url or "").strip().rstrip("/")
        if not normalized:
            return ""
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise HTTPException(status_code=400, detail="环境 Base URL 必须是有效的 http:// 或 https:// 地址")
        if parsed.username or parsed.password:
            raise HTTPException(status_code=400, detail="环境 Base URL 不能包含账号或密码")
        return normalized

    def interface_asset_data(payload: InterfaceAssetInput) -> dict[str, Any]:
        method = str(payload.method or "").upper()
        if method not in INTERFACE_HTTP_METHODS:
            raise HTTPException(status_code=400, detail=f"不支持的 HTTP 方法：{method}")
        data = payload.model_dump(exclude={"publish"})
        data["method"] = method
        data["path"] = validate_interface_target(payload.path)
        if payload.default_path:
            data["default_path"] = validate_relative_interface_path(
                payload.default_path, label="替换原 path"
            )
        return data

    def interface_environment(project_id: str, environment_id: str) -> dict[str, Any] | None:
        if not environment_id:
            return None
        return next(
            (
                item
                for item in platform_store.list_interface_environments(project_id)
                if item["id"] == environment_id
            ),
            None,
        )

    def interface_variable_values(project_id: str, environment_id: str) -> dict[str, str]:
        values: dict[str, str] = {}
        try:
            records = platform_store.get_interface_variable_records(
                project_id, environment_id
            )
            for record in records:
                key = str(record.get("variable_key") or "")
                if not key:
                    continue
                if record.get("is_secret"):
                    encrypted = str(record.get("secret_enc") or "")
                    if not encrypted:
                        raise HTTPException(status_code=409, detail=f"接口密钥变量尚未设置：{key}")
                    values[key] = decrypt_secret(encrypted)
                else:
                    values[key] = str(record.get("value_text") or "")
        except SecretEncryptionError as exc:
            raise HTTPException(status_code=503, detail="接口密钥暂时无法解密") from exc
        return values

    def prepare_interface_debug(
        project_id: str,
        payload: InterfaceDebugInput,
        runtime_variables: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        method = str(payload.method or "").upper()
        if method not in INTERFACE_HTTP_METHODS:
            raise HTTPException(status_code=400, detail=f"不支持的 HTTP 方法：{method}")
        environment = interface_environment(project_id, payload.environment_id)
        if payload.environment_id and not environment:
            raise HTTPException(status_code=400, detail="选择的运行环境不存在或不属于当前项目")
        variables = interface_variable_values(project_id, payload.environment_id)
        variables.update(runtime_variables or {})
        target = validate_interface_target(
            str(resolve_interface_template(str(payload.target or "").strip(), variables))
        )
        if not urlparse(target).scheme:
            base_url = str((environment or {}).get("base_url") or "").rstrip("/")
            if not base_url:
                raise HTTPException(status_code=400, detail="相对路径必须绑定带 Base URL 的运行环境")
            target = f"{base_url}{target}"
        target = validate_interface_target(target)
        headers = resolve_interface_template(payload.headers, variables)
        query = resolve_interface_template(payload.query, variables)
        body = resolve_interface_template(payload.body, variables)
        body_type = str(payload.body_type or "json")
        if body_type not in INTERFACE_BODY_TYPES:
            raise HTTPException(status_code=400, detail=f"不支持的请求体类型：{body_type}")
        if body_type in {"urlencoded", "multipart"} and body is not None and not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="表单请求体必须是 JSON 对象")
        if body_type == "raw" and body is not None and not isinstance(body, str):
            raise HTTPException(status_code=400, detail="原始请求体必须是文本")
        normalized_headers: dict[str, str] = {}
        for name, value in headers.items():
            header_name = str(name).strip()
            header_value = str(value)
            if not header_name or any(char in header_name + header_value for char in "\r\n"):
                raise HTTPException(status_code=400, detail="请求头名称和值不能为空或包含换行符")
            if header_name.lower() == "content-length":
                continue
            if body_type == "multipart" and header_name.lower() == "content-type":
                continue
            normalized_headers[header_name] = header_value
        return {
            "method": method,
            "target": target,
            "headers": normalized_headers,
            "query": query,
            "body_type": body_type,
            "body": body,
            "timeout_seconds": float(payload.timeout_seconds),
        }

    def execute_interface_debug(prepared: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            request_options = build_interface_request_options(prepared)
            with requests.request(**request_options) as response:
                content = bytearray()
                truncated = False
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    remaining = INTERFACE_RESPONSE_PREVIEW_BYTES - len(content)
                    if remaining <= 0:
                        truncated = True
                        break
                    content.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        truncated = True
                        break
                elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                encoding = response.encoding or "utf-8"
                try:
                    body_text = bytes(content).decode(encoding, errors="replace")
                except LookupError:
                    body_text = bytes(content).decode("utf-8", errors="replace")
                response_headers = {
                    str(name): ("••••••" if name.lower() == "set-cookie" else str(value))
                    for name, value in response.headers.items()
                }
                return {
                    "request": {
                        "method": prepared["method"],
                        "url": str(response.request.url),
                        "headers": redact_interface_headers(dict(response.request.headers)),
                    },
                    "response": {
                        "status_code": int(response.status_code),
                        "reason": str(response.reason or ""),
                        "elapsed_ms": elapsed_ms,
                        "size_bytes": len(content),
                        "content_type": str(response.headers.get("Content-Type") or ""),
                        "headers": response_headers,
                        "body": body_text,
                        "truncated": truncated,
                    },
                }
        except requests.Timeout as exc:
            raise HTTPException(
                status_code=504,
                detail=f"请求超过 {prepared['timeout_seconds']:g} 秒仍未响应",
            ) from exc
        except requests.RequestException as exc:
            raise HTTPException(
                status_code=502,
                detail=f"请求目标连接失败（{exc.__class__.__name__}），请检查地址、端口、网络和服务状态",
            ) from exc

    def normalize_scenario_parameters(parameters: dict[str, Any]) -> dict[str, str]:
        if not isinstance(parameters, dict):
            raise ValueError("场景用例参数必须是 JSON 对象")
        if len(parameters) > 100:
            raise ValueError("单个场景最多支持 100 个用例参数")
        normalized: dict[str, str] = {}
        for raw_key, raw_value in parameters.items():
            key = str(raw_key or "").strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", key):
                raise ValueError(f"用例参数名不合法：{key or '空名称'}")
            if INTERFACE_SENSITIVE_NAME_PATTERN.search(key):
                raise ValueError(f"敏感参数 {key} 请改用环境与变量中的加密密钥")
            if isinstance(raw_value, (dict, list)):
                value = json.dumps(raw_value, ensure_ascii=False)
            elif raw_value is None:
                value = ""
            else:
                value = str(raw_value)
            if len(value) > 20_000:
                raise ValueError(f"用例参数 {key} 超过 20000 字符")
            normalized[key] = value
        if len(json.dumps(normalized, ensure_ascii=False)) > 100_000:
            raise ValueError("场景用例参数总大小不能超过 100 KB")
        return normalized

    def execute_interface_scenario(
        project_id: str,
        scenario: dict[str, Any],
        runtime_parameters: dict[str, Any],
        *,
        batch_id: str = "",
        created_by: str = "",
        existing_run: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run = existing_run or platform_store.create_interface_scenario_run(
            project_id,
            scenario,
            batch_id=batch_id,
            created_by=created_by,
            parameters=runtime_parameters,
        )
        run_id = str(run["id"])
        started = time.perf_counter()
        steps: list[dict[str, Any]] = []
        step_results: list[dict[str, Any]] = []
        failed_steps = 0
        passed_steps = 0
        skipped_steps = 0
        secret_values: set[str] = set()

        def finish_run(
            status: str, summary: dict[str, Any], result: dict[str, Any]
        ) -> dict[str, Any]:
            report_snapshot = {
                **run,
                "status": status,
                "summary": summary,
                "result": result,
                "finished_at": time.time(),
            }
            artifacts: dict[str, str] = {}
            try:
                artifacts = generate_interface_scenario_report(
                    report_snapshot, artifact_storage
                )
            except Exception as report_exc:
                result["report_error"] = f"接口报告生成失败：{report_exc}"
            return platform_store.finish_interface_scenario_run(
                project_id,
                run_id,
                status=status,
                summary=summary,
                result=result,
                artifacts=artifacts,
            )

        try:
            if platform_store.is_interface_scenario_stop_requested(project_id, run_id):
                raise InterruptedError("用户请求停止接口场景")
            variables = normalize_scenario_parameters(scenario.get("parameters") or {})
            overrides = normalize_scenario_parameters(runtime_parameters or {})
            variables.update(overrides)
            steps = [step for step in scenario.get("steps") or [] if step.get("enabled", True)]
            assets: dict[str, dict[str, Any]] = {}
            environment_ids = {str(scenario.get("environment_id") or "")}
            for index, step in enumerate(steps, start=1):
                asset = platform_store.get_interface_asset(project_id, str(step.get("asset_id") or ""))
                if not asset:
                    raise ValueError(f"第 {index} 个步骤引用的接口已不存在")
                assets[str(asset["id"])] = asset
                environment_ids.add(str(step.get("environment_id") or ""))
                environment_ids.add(str(asset.get("environment_id") or ""))

            secret_names: set[str] = set()
            for environment_id in environment_ids:
                for record in platform_store.get_interface_variable_records(
                    project_id, environment_id
                ):
                    if not record.get("is_secret"):
                        continue
                    name = str(record.get("variable_key") or "")
                    secret_names.add(name)
                    encrypted = str(record.get("secret_enc") or "")
                    if encrypted:
                        secret_values.add(decrypt_secret(encrypted))
            forbidden_overrides = sorted(set(variables) & secret_names)
            if forbidden_overrides:
                raise ValueError(
                    "用例参数不能覆盖加密密钥：" + "、".join(forbidden_overrides)
                )

            stopped = False
            for index, step in enumerate(steps, start=1):
                if platform_store.is_interface_scenario_stop_requested(
                    project_id, run_id
                ):
                    raise InterruptedError("用户请求停止接口场景")
                if stopped:
                    skipped_steps += 1
                    step_results.append(
                        {
                            "index": index,
                            "name": str(step.get("name") or f"步骤 {index}"),
                            "status": "skipped",
                            "message": "前序步骤失败，当前步骤未执行",
                        }
                    )
                    continue
                asset = assets[str(step.get("asset_id") or "")]
                request_definition = (
                    asset.get("request") if isinstance(asset.get("request"), dict) else {}
                )
                environment_id = str(
                    step.get("environment_id")
                    or scenario.get("environment_id")
                    or asset.get("environment_id")
                    or ""
                )
                body_present = (
                    "body" in request_definition and request_definition.get("body") is not None
                )
                body_type = str(
                    request_definition.get("body_type")
                    or ("json" if body_present else "none")
                )
                payload = InterfaceDebugInput(
                    asset_id=str(asset["id"]),
                    method=str(asset.get("method") or "GET"),
                    target=str(asset.get("path") or ""),
                    environment_id=environment_id,
                    headers=request_definition.get("headers") or {},
                    query=request_definition.get("query") or {},
                    body_type=body_type,
                    body=request_definition.get("body"),
                    timeout_seconds=float(request_definition.get("timeout_seconds") or 30),
                )
                step_started = time.perf_counter()
                try:
                    assertion_variables = interface_variable_values(
                        project_id, environment_id
                    )
                    assertion_variables.update(variables)
                    prepared = prepare_interface_debug(
                        project_id, payload, runtime_variables=variables
                    )
                    exchange = execute_interface_debug(prepared)
                    if platform_store.is_interface_scenario_stop_requested(
                        project_id, run_id
                    ):
                        raise InterruptedError("用户请求停止接口场景")
                    response = exchange.get("response") or {}
                    extraction_results: list[dict[str, Any]] = []
                    extraction_failed = False
                    seen_extractions: set[str] = set()
                    for extraction in step.get("extractions") or []:
                        name = str(extraction.get("name") or "").strip()
                        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", name):
                            raise ValueError(f"提取变量名不合法：{name or '空名称'}")
                        if name in seen_extractions:
                            raise ValueError(f"步骤内存在重复提取变量：{name}")
                        if name in secret_names:
                            raise ValueError(f"提取变量不能覆盖加密密钥：{name}")
                        seen_extractions.add(name)
                        try:
                            value = interface_response_value(
                                response,
                                str(extraction.get("source") or "json"),
                                str(extraction.get("expression") or ""),
                            )
                            variables[name] = (
                                json.dumps(value, ensure_ascii=False)
                                if isinstance(value, (dict, list))
                                else str(value)
                            )
                            sensitive = bool(INTERFACE_SENSITIVE_NAME_PATTERN.search(name))
                            if sensitive:
                                secret_values.add(variables[name])
                            extraction_results.append(
                                {
                                    "name": name,
                                    "source": extraction.get("source") or "json",
                                    "expression": extraction.get("expression") or "",
                                    "success": True,
                                    "sensitive": sensitive,
                                    "value": "••••••" if sensitive else redact_interface_values(value, secret_values),
                                }
                            )
                        except (KeyError, ValueError, TypeError) as exc:
                            required = bool(extraction.get("required", True))
                            extraction_results.append(
                                {
                                    "name": name,
                                    "source": extraction.get("source") or "json",
                                    "expression": extraction.get("expression") or "",
                                    "success": not required,
                                    "required": required,
                                    "message": f"未能提取变量：{exc}",
                                }
                            )
                            extraction_failed = extraction_failed or required

                    assertions = list(step.get("assertions") or [])
                    if not assertions:
                        assertions = [
                            {
                                "source": "status",
                                "expression": "",
                                "operator": "between",
                                "expected": [200, 399],
                                "message": "HTTP 状态码应在 200～399",
                            }
                        ]
                    assertion_results: list[dict[str, Any]] = []
                    assertion_failed = False
                    for assertion in assertions:
                        source = str(assertion.get("source") or "status")
                        expression = str(assertion.get("expression") or "")
                        operator = str(assertion.get("operator") or "equals")
                        expected = resolve_interface_template(
                            assertion.get("expected"), assertion_variables
                        )
                        missing = False
                        try:
                            actual = interface_response_value(response, source, expression)
                        except (KeyError, ValueError, TypeError):
                            actual = None
                            missing = True
                        try:
                            passed = evaluate_interface_assertion(actual, operator, expected)
                        except (ValueError, TypeError) as exc:
                            passed = False
                            assertion_error = str(exc)
                        else:
                            assertion_error = "" if not missing or operator == "not_exists" else "响应取值不存在"
                            if assertion_error:
                                passed = False
                        assertion_failed = assertion_failed or not passed
                        assertion_results.append(
                            {
                                "source": source,
                                "expression": expression,
                                "operator": operator,
                                "expected": redact_interface_values(expected, secret_values),
                                "actual": redact_interface_values(actual, secret_values),
                                "passed": passed,
                                "message": str(assertion.get("message") or assertion_error or ""),
                            }
                        )

                    passed = not extraction_failed and not assertion_failed
                    if passed:
                        passed_steps += 1
                    else:
                        failed_steps += 1
                    persisted_exchange = redact_interface_values(exchange, secret_values)
                    response_body = str(
                        ((persisted_exchange.get("response") or {}).get("body") or "")
                    )
                    if len(response_body) > 100_000:
                        persisted_exchange["response"]["body"] = response_body[:100_000]
                        persisted_exchange["response"]["result_truncated"] = True
                    step_results.append(
                        {
                            "index": index,
                            "id": str(step.get("id") or ""),
                            "name": str(step.get("name") or asset.get("name") or f"步骤 {index}"),
                            "asset": {
                                "id": asset["id"],
                                "name": asset.get("name") or "",
                                "version": int(asset.get("current_version") or 1),
                                "method": asset.get("method") or "GET",
                                "path": asset.get("path") or "",
                            },
                            "environment_id": environment_id,
                            "status": "passed" if passed else "failed",
                            "elapsed_ms": round((time.perf_counter() - step_started) * 1000, 1),
                            "exchange": persisted_exchange,
                            "extractions": extraction_results,
                            "assertions": assertion_results,
                        }
                    )
                    if not passed and not step.get("continue_on_failure"):
                        stopped = True
                except InterruptedError:
                    raise
                except Exception as exc:
                    failed_steps += 1
                    message = exc.detail if isinstance(exc, HTTPException) else str(exc)
                    step_results.append(
                        {
                            "index": index,
                            "id": str(step.get("id") or ""),
                            "name": str(step.get("name") or asset.get("name") or f"步骤 {index}"),
                            "asset": {
                                "id": asset["id"],
                                "name": asset.get("name") or "",
                                "version": int(asset.get("current_version") or 1),
                                "method": asset.get("method") or "GET",
                                "path": asset.get("path") or "",
                            },
                            "environment_id": environment_id,
                            "status": "failed",
                            "elapsed_ms": round((time.perf_counter() - step_started) * 1000, 1),
                            "message": redact_interface_values(str(message), secret_values),
                            "extractions": [],
                            "assertions": [],
                        }
                    )
                    if not step.get("continue_on_failure"):
                        stopped = True

            summary = {
                "total_steps": len(steps),
                "passed_steps": passed_steps,
                "failed_steps": failed_steps,
                "skipped_steps": skipped_steps,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                "parameter_count": len(variables),
            }
            result = {
                "schema_version": "1.0",
                "scenario": {
                    "id": scenario.get("id") or "",
                    "name": scenario.get("name") or "接口场景",
                    "description": scenario.get("description") or "",
                    "environment_id": scenario.get("environment_id") or "",
                },
                "summary": summary,
                "steps": step_results,
            }
            return finish_run("succeeded" if failed_steps == 0 else "failed", summary, result)
        except InterruptedError as exc:
            summary = {
                "total_steps": len(steps)
                or int(((run.get("summary") or {}).get("total_steps") or 0)),
                "passed_steps": passed_steps,
                "failed_steps": failed_steps,
                "skipped_steps": max(
                    skipped_steps,
                    max(0, len(steps) - len(step_results)),
                ),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            }
            return finish_run(
                "interrupted",
                summary,
                {
                    "schema_version": "1.0",
                    "scenario": {
                        "id": scenario.get("id") or "",
                        "name": scenario.get("name") or "接口场景",
                    },
                    "summary": summary,
                    "steps": step_results,
                    "error": str(exc),
                },
            )
        except Exception as exc:
            message = exc.detail if isinstance(exc, HTTPException) else str(exc)
            summary = {
                "total_steps": int(((run.get("summary") or {}).get("total_steps") or 0)),
                "passed_steps": 0,
                "failed_steps": 1,
                "skipped_steps": 0,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            }
            return finish_run(
                "failed",
                summary,
                {
                    "schema_version": "1.0",
                    "scenario": {
                        "id": scenario.get("id") or "",
                        "name": scenario.get("name") or "接口场景",
                    },
                    "summary": summary,
                    "steps": [],
                    "error": str(message),
                },
            )

    interface_scenario_manager = InterfaceScenarioManager(
        platform_store,
        execute_interface_scenario,
        signal_queue=signal_queue,
        concurrency=int(task_queue_cfg().get("interface_concurrency") or 5),
    )
    setattr(report_manager, "interface_scenario_manager", interface_scenario_manager)

    def runtime_override_spec(
        project_id: str, asset: dict[str, Any]
    ) -> dict[str, Any] | None:
        default_path = str(asset.get("default_path") or "")
        if not default_path:
            return None
        base_url = str(asset.get("base_url") or "")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise HTTPException(
                status_code=409,
                detail="发布运行时 path 覆盖前，请为接口绑定带有效 Base URL 的环境",
            )
        asset_path = str(asset.get("path") or "/")
        base_path = parsed.path.rstrip("/")
        return {
            "logical_name": f"project:{project_id}:{asset.get('name') or asset.get('id')}",
            "method": str(asset.get("method") or "GET"),
            "scheme": parsed.scheme,
            "host": parsed.hostname,
            "port": parsed.port or (443 if parsed.scheme == "https" else 80),
            "path": f"{base_path}{asset_path}" or "/",
            "default_path": default_path,
            "source_type": "interface-asset",
            "spec": {"asset_id": asset.get("id"), "request": asset.get("request") or {}},
        }

    def sync_runtime_override(project_id: str, asset: dict[str, Any]) -> None:
        override = runtime_override_spec(project_id, asset)
        if override:
            platform_store.save_endpoint_spec(override, publish=True)

    def validate_runtime_override_input(
        project_id: str, data: dict[str, Any]
    ) -> None:
        if not data.get("default_path"):
            return
        environment_id = str(data.get("environment_id") or "")
        environment = next(
            (
                item
                for item in platform_store.list_interface_environments(project_id)
                if item["id"] == environment_id
            ),
            None,
        )
        runtime_override_spec(
            project_id,
            {**data, "base_url": str((environment or {}).get("base_url") or "")},
        )

    def ensure_run_visible(run_id: str, request: Request) -> dict[str, Any]:
        try:
            run = task_store.get_run(run_id)
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        project_id = str((run.get("metadata") or {}).get("project_id") or "")
        if project_id and project_id == current_project_id(request):
            return run
        if not project_id and legacy_visible(request):
            return run
        raise HTTPException(status_code=404, detail="run not found")

    @router.get("/bootstrap")
    async def bootstrap(request: Request):
        return {
            "security": {
                "master_key_configured": has_env("MASTER_KEY"),
                "model_keys_encrypted": True,
            },
            "defaults": {
                "translate_name": default_translate_name(),
            },
            "monitor": monitor_cfg(),
            "monitor_containers": monitor_cfg().get("containers", []),
            "upload": upload_cfg(),
            "export": export_cfg(),
            "ai_checks": ai_checks_cfg(),
            "stress": stress_cfg(),
            "server_sessions": {
                "idle_timeout_seconds": int(server_session_manager.idle_timeout),
            },
            "identity": {
                "user": identity_context(request).get("user"),
                "current_project": identity_context(request).get("current_project"),
                "project_role": identity_context(request).get("project_role", ""),
                "permissions": identity_context(request).get("permissions", []),
            },
            "report_sections": DEFAULT_REPORT_SECTIONS,
        }

    @router.post("/uploads", status_code=201)
    async def upload_test_files(request: Request):
        # Starlette defaults to 1,000 multipart files and rejects larger folders
        # with HTTP 400 before FastAPI can enter a File(...) endpoint.
        form = await request.form(max_files=_upload_max_files())
        files = [item for item in form.getlist("files") if isinstance(item, UploadFile)]
        if not files:
            raise HTTPException(status_code=400, detail="no files uploaded")
        upload_id = uuid.uuid4().hex
        target_dir = (UPLOADS_DIR / upload_id).resolve()
        target_dir.mkdir(parents=True, exist_ok=False)
        max_bytes = int(float(get_env("UPLOAD_LIMIT_GB", "20")) * 1024 ** 3)
        total = 0
        saved = []
        try:
            for item in files:
                relative = _safe_filename(item.filename or "unnamed.bin")
                target = (target_dir / relative).resolve()
                try:
                    target.relative_to(target_dir)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail="invalid upload path") from exc
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as output:
                    while True:
                        chunk = await item.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise HTTPException(status_code=413, detail="upload exceeds configured limit")
                        output.write(chunk)
                saved.append({"name": str(relative).replace("\\", "/"), "size": target.stat().st_size})
            context = identity_context(request)
            write_upload_owner(
                UPLOADS_DIR,
                upload_id,
                project_id=current_project_id(request),
                user_id=str((context.get("user") or {}).get("id") or ""),
            )
        except Exception:
            shutil.rmtree(target_dir, ignore_errors=True)
            (UPLOADS_DIR / f".{upload_id}.owner.json").unlink(missing_ok=True)
            raise
        finally:
            for item in files:
                await item.close()
        return {"upload_id": upload_id, "file_count": len(saved), "total_bytes": total, "files": saved[:200]}

    @router.get("/runs/{run_id}/metrics")
    async def run_metrics(
        run_id: str,
        request: Request,
        after_id: int = Query(default=0, ge=0),
        limit: int = Query(default=5000, ge=1, le=20000),
    ):
        await run_in_threadpool(ensure_run_visible, run_id, request)
        metrics = await run_in_threadpool(task_store.get_metrics, run_id, after_id, limit)
        return {"metrics": metrics, "last_id": metrics[-1]["id"] if metrics else after_id}

    @router.get("/model-profiles")
    async def list_models():
        return {"profiles": [public_profile(item) for item in platform_store.list_model_profiles()]}

    @router.post("/model-profiles", status_code=201)
    async def create_or_update_model(payload: ModelProfileInput):
        try:
            return save_profile(platform_store, payload.model_dump())
        except ModelSecretError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/model-profiles/{profile_id}/activate")
    async def activate_model(profile_id: str):
        try:
            platform_store.activate_model(profile_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="model profile not found") from exc
        return {"success": True}

    @router.post("/model-profiles/{profile_id}/test")
    async def test_model_connection(profile_id: str):
        try:
            return await run_in_threadpool(test_profile, platform_store, profile_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="model profile not found") from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get("/report-template")
    async def get_report_template():
        return platform_store.get_report_template()

    @router.put("/report-template")
    async def save_report_template(payload: ReportTemplateInput):
        required = {item["key"] for item in DEFAULT_REPORT_SECTIONS}
        actual = [str(item.get("key", "")) for item in payload.sections]
        if set(actual) != required or len(actual) != len(required):
            raise HTTPException(status_code=400, detail="报告模板必须保留固定的六个章节且章节 key 不可重复")
        for section in payload.sections:
            if section.get("mode") not in {"auto", "custom", "mixed"}:
                raise HTTPException(status_code=400, detail="invalid report section mode")
        return platform_store.save_report_template(payload.name, payload.sections)

    @router.post("/runs/{run_id}/reports", status_code=202)
    async def create_report(run_id: str, payload: ReportRequest, request: Request):
        await run_in_threadpool(ensure_run_visible, run_id, request)
        options = payload.model_dump()
        options["_project_id"] = current_project_id(request)
        options["_created_by_user_id"] = str(
            (identity_context(request).get("user") or {}).get("id") or ""
        )
        try:
            return await run_in_threadpool(report_manager.submit, run_id, options)
        except TaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/reports")
    async def list_reports(request: Request, limit: int = Query(default=100, ge=1, le=500)):
        reports = [
            item for item in platform_store.list_report_jobs(500)
            if visible_in_project(item, request)
        ][:limit]
        return {"reports": reports}

    @router.get("/reports/{job_id}")
    async def get_report_job(job_id: str, request: Request):
        job = platform_store.get_report_job(job_id)
        if not job or not visible_in_project(job, request):
            raise HTTPException(status_code=404, detail="report job not found")
        return job

    @router.get("/reports/{job_id}/download/{format_name}")
    async def download_report(job_id: str, format_name: str, request: Request):
        job = platform_store.get_report_job(job_id)
        if not job or not visible_in_project(job, request):
            raise HTTPException(status_code=404, detail="report job not found")
        if job["status"] != "succeeded":
            raise HTTPException(status_code=409, detail="report is not ready")
        key = {"docx": "docx_path", "pdf": "pdf_path"}.get(format_name.lower())
        if not key:
            raise HTTPException(status_code=400, detail="format must be docx or pdf")
        try:
            artifact_dir = artifact_storage.resolve(job["artifact_dir"])
            path = artifact_storage.resolve_file(job[key], container=artifact_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="artifact not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid artifact path") from exc
        media = "application/pdf" if format_name.lower() == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        return FileResponse(str(path), media_type=media, filename=path.name)

    @router.get("/interface-assets/workspace")
    async def interface_workspace(request: Request):
        project_id = current_project_id(request)
        def load_workspace() -> dict[str, Any]:
            return {
                "modules": platform_store.list_interface_modules(project_id),
                "environments": platform_store.list_interface_environments(project_id),
                "variables": platform_store.list_interface_variables(project_id),
                "assets": platform_store.list_interface_assets(project_id),
                "scenarios": platform_store.list_interface_scenarios(project_id),
                "scenario_runs": platform_store.list_interface_scenario_runs(project_id),
            }

        return await run_in_threadpool(load_workspace)

    @router.post("/interface-modules", status_code=201)
    async def create_interface_module(payload: InterfaceModuleInput, request: Request):
        try:
            item = platform_store.save_interface_module(
                current_project_id(request), payload.model_dump()
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit_interface_change(
            request, "interface.module.create", "interface_module", item["id"],
            {"name": item["name"]},
        )
        return item

    @router.put("/interface-modules/{module_id}")
    async def update_interface_module(
        module_id: str, payload: InterfaceModuleInput, request: Request
    ):
        try:
            item = platform_store.save_interface_module(
                current_project_id(request), payload.model_dump(), module_id
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="接口模块不存在") from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit_interface_change(
            request, "interface.module.update", "interface_module", item["id"],
            {"name": item["name"]},
        )
        return item

    @router.delete("/interface-modules/{module_id}")
    async def delete_interface_module(module_id: str, request: Request):
        try:
            deleted = platform_store.delete_interface_module(
                current_project_id(request), module_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="接口模块不存在")
        audit_interface_change(
            request, "interface.module.delete", "interface_module", module_id
        )
        return {"success": True}

    @router.post("/interface-environments", status_code=201)
    async def create_interface_environment(
        payload: InterfaceEnvironmentInput, request: Request
    ):
        data = payload.model_dump()
        data["base_url"] = validate_base_url(payload.base_url)
        try:
            item = platform_store.save_interface_environment(
                current_project_id(request), data
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit_interface_change(
            request, "interface.environment.create", "interface_environment", item["id"],
            {"name": item["name"], "is_default": item["is_default"]},
        )
        return item

    @router.put("/interface-environments/{environment_id}")
    async def update_interface_environment(
        environment_id: str, payload: InterfaceEnvironmentInput, request: Request
    ):
        data = payload.model_dump()
        data["base_url"] = validate_base_url(payload.base_url)
        try:
            item = platform_store.save_interface_environment(
                current_project_id(request), data, environment_id
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="接口环境不存在") from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit_interface_change(
            request, "interface.environment.update", "interface_environment", item["id"],
            {"name": item["name"], "is_default": item["is_default"]},
        )
        return item

    @router.delete("/interface-environments/{environment_id}")
    async def delete_interface_environment(environment_id: str, request: Request):
        try:
            deleted = platform_store.delete_interface_environment(
                current_project_id(request), environment_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="接口环境不存在")
        audit_interface_change(
            request, "interface.environment.delete", "interface_environment", environment_id
        )
        return {"success": True}

    @router.post("/interface-variables", status_code=201)
    async def create_interface_variable(
        payload: InterfaceVariableInput, request: Request
    ):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", payload.key):
            raise HTTPException(status_code=400, detail="变量名需以字母或下划线开头，只能包含字母、数字、点、横线和下划线")
        try:
            encrypted = encrypt_secret(payload.value) if payload.is_secret and payload.value else None
            item = platform_store.save_interface_variable(
                current_project_id(request), payload.model_dump(), secret_enc=encrypted
            )
        except SecretEncryptionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit_interface_change(
            request, "interface.variable.create", "interface_variable", item["id"],
            {"key": item["variable_key"], "scope": item["environment_id"] or "project", "is_secret": item["is_secret"]},
        )
        return item

    @router.put("/interface-variables/{variable_id}")
    async def update_interface_variable(
        variable_id: str, payload: InterfaceVariableInput, request: Request
    ):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", payload.key):
            raise HTTPException(status_code=400, detail="变量名需以字母或下划线开头，只能包含字母、数字、点、横线和下划线")
        try:
            encrypted = encrypt_secret(payload.value) if payload.is_secret and payload.value else None
            item = platform_store.save_interface_variable(
                current_project_id(request), payload.model_dump(), variable_id,
                secret_enc=encrypted,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="接口变量不存在") from exc
        except SecretEncryptionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit_interface_change(
            request, "interface.variable.update", "interface_variable", item["id"],
            {"key": item["variable_key"], "scope": item["environment_id"] or "project", "is_secret": item["is_secret"]},
        )
        return item

    @router.delete("/interface-variables/{variable_id}")
    async def delete_interface_variable(variable_id: str, request: Request):
        if not platform_store.delete_interface_variable(
            current_project_id(request), variable_id
        ):
            raise HTTPException(status_code=404, detail="接口变量不存在")
        audit_interface_change(
            request, "interface.variable.delete", "interface_variable", variable_id
        )
        return {"success": True}

    @router.post("/interface-scenarios", status_code=201)
    async def create_interface_scenario(payload: InterfaceScenarioInput, request: Request):
        project_id = current_project_id(request)
        data = payload.model_dump()
        try:
            data["parameters"] = normalize_scenario_parameters(data.get("parameters") or {})
            item = platform_store.save_interface_scenario(project_id, data)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit_interface_change(
            request,
            "interface.scenario.create",
            "interface_scenario",
            item["id"],
            {"name": item["name"], "step_count": item["step_count"]},
        )
        return item

    @router.put("/interface-scenarios/{scenario_id}")
    async def update_interface_scenario(
        scenario_id: str, payload: InterfaceScenarioInput, request: Request
    ):
        project_id = current_project_id(request)
        data = payload.model_dump()
        try:
            data["parameters"] = normalize_scenario_parameters(data.get("parameters") or {})
            item = platform_store.save_interface_scenario(project_id, data, scenario_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="接口场景不存在") from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit_interface_change(
            request,
            "interface.scenario.update",
            "interface_scenario",
            item["id"],
            {"name": item["name"], "step_count": item["step_count"]},
        )
        return item

    @router.delete("/interface-scenarios/{scenario_id}")
    async def delete_interface_scenario(scenario_id: str, request: Request):
        project_id = current_project_id(request)
        if not platform_store.delete_interface_scenario(project_id, scenario_id):
            raise HTTPException(status_code=404, detail="接口场景不存在")
        audit_interface_change(
            request,
            "interface.scenario.delete",
            "interface_scenario",
            scenario_id,
        )
        return {"success": True}

    @router.post("/interface-scenarios/batch-execute", status_code=202)
    async def batch_execute_interface_scenarios(
        payload: InterfaceScenarioBatchInput, request: Request
    ):
        project_id = current_project_id(request)
        scenario_ids = list(dict.fromkeys(str(item) for item in payload.scenario_ids))
        scenarios = []
        for scenario_id in scenario_ids:
            scenario = platform_store.get_interface_scenario(project_id, scenario_id)
            if not scenario:
                raise HTTPException(status_code=404, detail=f"接口场景不存在：{scenario_id}")
            scenarios.append(scenario)
        try:
            parameters = normalize_scenario_parameters(payload.parameters)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        batch_id = uuid.uuid4().hex
        actor_id = str((identity_context(request).get("user") or {}).get("id") or "")

        runs = await run_in_threadpool(
            interface_scenario_manager.submit_batch,
            project_id,
            scenarios,
            parameters,
            batch_id=batch_id,
            created_by=actor_id,
            concurrency=payload.concurrency,
        )
        audit_interface_change(
            request,
            "interface.scenario.batch_execute",
            "interface_scenario_batch",
            batch_id,
            {
                "scenario_count": len(scenarios),
                "requested_concurrency": min(payload.concurrency, len(scenarios)),
                "effective_concurrency": min(
                    payload.concurrency,
                    interface_scenario_manager.concurrency,
                    len(scenarios),
                ),
                "queued": len(runs),
            },
        )
        return {"batch_id": batch_id, "runs": runs}

    @router.post("/interface-scenarios/{scenario_id}/execute", status_code=202)
    async def execute_interface_scenario_route(
        scenario_id: str, payload: InterfaceScenarioExecuteInput, request: Request
    ):
        project_id = current_project_id(request)
        scenario = platform_store.get_interface_scenario(project_id, scenario_id)
        if not scenario:
            raise HTTPException(status_code=404, detail="接口场景不存在")
        try:
            parameters = normalize_scenario_parameters(payload.parameters)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        actor_id = str((identity_context(request).get("user") or {}).get("id") or "")
        run = await run_in_threadpool(
            interface_scenario_manager.submit,
            project_id,
            scenario,
            parameters,
            created_by=actor_id,
        )
        audit_interface_change(
            request,
            "interface.scenario.execute",
            "interface_scenario_run",
            run["id"],
            {
                "scenario_id": scenario_id,
                "status": run["status"],
                "queue_backend": interface_scenario_manager.signal_queue.backend,
            },
        )
        return run

    @router.post("/interface-scenario-runs/{run_id}/stop")
    async def stop_interface_scenario_run(run_id: str, request: Request):
        run = await run_in_threadpool(
            interface_scenario_manager.stop_run,
            current_project_id(request),
            run_id,
        )
        if not run:
            raise HTTPException(status_code=404, detail="接口场景执行记录不存在")
        return run

    @router.get("/interface-scenario-runs/{run_id}")
    async def get_interface_scenario_run(run_id: str, request: Request):
        run = await run_in_threadpool(
            platform_store.get_interface_scenario_run,
            current_project_id(request),
            run_id,
        )
        if not run:
            raise HTTPException(status_code=404, detail="接口场景执行记录不存在")
        return run

    @router.get("/interface-scenario-batches/{batch_id}")
    async def get_interface_scenario_batch(batch_id: str, request: Request):
        runs = await run_in_threadpool(
            platform_store.list_interface_scenario_batch_runs,
            current_project_id(request),
            batch_id,
        )
        return {"batch_id": batch_id, "runs": runs}

    @router.get("/interface-scenario-runs/{run_id}/download/{format_name}")
    async def download_interface_scenario_report(
        run_id: str, format_name: str, request: Request
    ):
        run = platform_store.get_interface_scenario_run(
            current_project_id(request), run_id
        )
        if not run:
            raise HTTPException(status_code=404, detail="接口场景执行记录不存在")
        key = {"docx": "docx_path", "pdf": "pdf_path"}.get(format_name.lower())
        if not key:
            raise HTTPException(status_code=400, detail="报告格式必须是 docx 或 pdf")
        if not run.get(key):
            raise HTTPException(status_code=409, detail="本次执行尚未生成对应报告")
        try:
            artifact_dir = artifact_storage.resolve(run.get("artifact_dir") or "")
            path = artifact_storage.resolve_file(run[key], container=artifact_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="接口报告文件不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="接口报告路径无效") from exc
        media = (
            "application/pdf"
            if format_name.lower() == "pdf"
            else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        return FileResponse(
            str(path), media_type=media, filename=f"interface-automation-{run_id[:8]}.{format_name.lower()}"
        )

    @router.post("/interface-debug")
    async def debug_interface_request(payload: InterfaceDebugInput, request: Request):
        project_id = current_project_id(request)
        if payload.asset_id and not platform_store.get_interface_asset(
            project_id, payload.asset_id
        ):
            raise HTTPException(status_code=404, detail="接口资产不存在")
        prepared = prepare_interface_debug(project_id, payload)
        result = await run_in_threadpool(execute_interface_debug, prepared)
        parsed_target = urlparse(prepared["target"])
        audit_interface_change(
            request,
            "interface.request.debug",
            "interface_asset" if payload.asset_id else "interface_request",
            payload.asset_id or "quick-request",
            {
                "method": prepared["method"],
                "host": parsed_target.hostname or "",
                "path": parsed_target.path or "/",
                "status_code": result["response"]["status_code"],
                "elapsed_ms": result["response"]["elapsed_ms"],
            },
        )
        return result

    @router.post("/interface-assets/parse-curl")
    async def parse_interface_curl(payload: CurlParseInput):
        try:
            return parse_curl_request(payload.content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/interface-assets", status_code=201)
    async def create_interface_asset(payload: InterfaceAssetInput, request: Request):
        project_id = current_project_id(request)
        try:
            data = interface_asset_data(payload)
            if payload.publish:
                validate_runtime_override_input(project_id, data)
            item = platform_store.save_interface_asset(
                project_id, data, publish=False,
            )
            if payload.publish:
                item = platform_store.publish_interface_asset(project_id, item["id"])
                sync_runtime_override(project_id, item)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit_interface_change(
            request, "interface.asset.create", "interface_asset", item["id"],
            {"name": item["name"], "version": item["current_version"], "published": payload.publish},
        )
        return item

    @router.put("/interface-assets/{asset_id}")
    async def update_interface_asset(
        asset_id: str, payload: InterfaceAssetInput, request: Request
    ):
        project_id = current_project_id(request)
        try:
            data = interface_asset_data(payload)
            if payload.publish:
                validate_runtime_override_input(project_id, data)
            item = platform_store.save_interface_asset(
                project_id, data, asset_id, publish=False,
            )
            if payload.publish:
                item = platform_store.publish_interface_asset(project_id, item["id"])
                sync_runtime_override(project_id, item)
        except HTTPException:
            raise
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="接口资产不存在") from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit_interface_change(
            request, "interface.asset.update", "interface_asset", item["id"],
            {"name": item["name"], "version": item["current_version"], "published": payload.publish},
        )
        return item

    @router.post("/interface-assets/{asset_id}/publish")
    async def publish_interface_asset(asset_id: str, request: Request):
        project_id = current_project_id(request)
        try:
            current = platform_store.get_interface_asset(project_id, asset_id)
            if not current:
                raise KeyError(asset_id)
            runtime_override_spec(project_id, current)
            item = platform_store.publish_interface_asset(project_id, asset_id)
            sync_runtime_override(project_id, item)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="接口资产不存在") from exc
        audit_interface_change(
            request, "interface.asset.publish", "interface_asset", item["id"],
            {"name": item["name"], "version": item["current_version"]},
        )
        return item

    @router.delete("/interface-assets/{asset_id}")
    async def delete_interface_asset(asset_id: str, request: Request):
        try:
            deleted = platform_store.delete_interface_asset(
                current_project_id(request), asset_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="接口资产不存在")
        audit_interface_change(
            request, "interface.asset.delete", "interface_asset", asset_id
        )
        return {"success": True}

    @router.get("/interface-assets/{asset_id}/versions")
    async def list_interface_asset_versions(asset_id: str, request: Request):
        project_id = current_project_id(request)
        if not platform_store.get_interface_asset(project_id, asset_id):
            raise HTTPException(status_code=404, detail="接口资产不存在")
        return {"versions": platform_store.list_interface_asset_versions(project_id, asset_id)}

    @router.get("/interface-specs")
    async def list_interfaces(
        request: Request, limit: int = Query(default=200, ge=1, le=500)
    ):
        assets = platform_store.list_interface_assets(current_project_id(request), limit)
        return {
            "specs": [
                {
                    "id": item["id"],
                    "logical_name": item["name"],
                    "version": item["current_version"],
                    "method": item["method"],
                    "path": item["path"],
                    "default_path": item["default_path"],
                    "status": item["status"],
                    "environment_name": item.get("environment_name") or "",
                    "base_url": item.get("base_url") or "",
                }
                for item in assets
            ]
        }

    @router.post("/interface-specs/import", status_code=201)
    async def import_interfaces(payload: EndpointImportInput, request: Request):
        project_id = current_project_id(request)
        if payload.default_path:
            validate_relative_interface_path(payload.default_path, label="替换原 path")
        try:
            parsed = parse_endpoint_text(
                payload.content, payload.logical_name, payload.default_path
            )
            assets = []
            legacy_specs = []
            for item in parsed:
                asset_data = {
                    "module_id": payload.module_id,
                    "environment_id": payload.environment_id,
                    "name": item["logical_name"],
                    "description": "由接口定义导入",
                    "method": item["method"],
                    "path": item["path"],
                    "default_path": item.get("default_path") or "",
                    "request": {"definition": item.get("spec") or {}},
                    "source_type": item.get("source_type") or "import",
                    "target": {
                        "scheme": item.get("scheme") or "",
                        "host": item.get("host") or "",
                        "port": item.get("port"),
                    },
                }
                assets.append(
                    platform_store.save_interface_asset(
                        project_id, asset_data, publish=payload.publish, upsert_by_name=True
                    )
                )
                legacy_specs.append(platform_store.save_endpoint_spec(item, payload.publish))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit_interface_change(
            request, "interface.asset.import", "interface_asset", assets[0]["id"],
            {"count": len(assets), "published": payload.publish, "source_types": sorted({str(item.get("source_type") or "") for item in parsed})},
        )
        return {
            "count": len(assets),
            "assets": assets,
            "specs": legacy_specs,
            "runtime_effect": payload.publish,
        }

    @router.get("/stress-jobs")
    async def list_stress_jobs(request: Request, limit: int = Query(default=100, ge=1, le=500)):
        jobs = [
            item for item in platform_store.list_stress_jobs(500)
            if visible_in_project(item, request)
        ][:limit]
        return {"jobs": jobs}

    @router.get("/server-profiles")
    async def list_server_profiles():
        return {"profiles": server_session_manager.list_profiles()}

    @router.post("/server-profiles", status_code=201)
    async def create_server_profile(payload: ServerProfileInput):
        try:
            return server_session_manager.save_profile(payload.model_dump())
        except SecretEncryptionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.put("/server-profiles/{profile_id}")
    async def update_server_profile(profile_id: str, payload: ServerProfileInput):
        try:
            return server_session_manager.save_profile(
                {"id": profile_id, **payload.model_dump()}
            )
        except SecretEncryptionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/server-profiles/{profile_id}")
    async def delete_server_profile(profile_id: str):
        try:
            deleted = server_session_manager.delete_profile(profile_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="server profile not found")
        return {"success": True}

    @router.get("/server-sessions")
    async def list_server_sessions(request: Request):
        return {
            "sessions": [
                item
                for item in server_session_manager.list_sessions()
                if visible_server_session(item, request)
            ]
        }

    @router.post("/server-sessions", status_code=201)
    async def connect_server_session(payload: ServerSessionInput, request: Request):
        try:
            data = payload.model_dump()
            data["_project_id"] = current_project_id(request)
            data["_created_by_user_id"] = str(
                (identity_context(request).get("user") or {}).get("id") or ""
            )
            return server_session_manager.connect(data)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="server profile not found") from exc
        except SecretEncryptionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/server-sessions/{session_id}")
    async def get_server_session(session_id: str, request: Request):
        return get_visible_server_session(session_id, request)

    @router.post("/server-sessions/{session_id}/heartbeat")
    async def heartbeat_server_session(session_id: str, request: Request):
        return get_visible_server_session(session_id, request, touch=True)

    @router.get("/server-sessions/{session_id}/metrics")
    async def get_server_session_metrics(
        session_id: str,
        request: Request,
        after_id: int = Query(default=0, ge=0),
        limit: int = Query(default=5000, ge=1, le=20000),
    ):
        get_visible_server_session(session_id, request)
        return server_session_manager.metrics(session_id, after_id, limit)

    @router.post("/server-sessions/{session_id}/stop")
    async def stop_server_session(session_id: str, request: Request):
        get_visible_server_session(session_id, request)
        try:
            return await run_in_threadpool(server_session_manager.close, session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="server session not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/server-capabilities/probe")
    async def probe_server_capabilities(payload: ServerCapabilityInput):
        data = payload.model_dump()
        data["modes"] = [item for item in data.get("modes", []) if item in {"monitor", "cpu", "gpu"}]
        if not data["modes"]:
            data["modes"] = ["monitor"]
        try:
            return stress_manager.probe(data)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/stress-jobs", status_code=202)
    async def create_stress_job(payload: StressJobInput, request: Request):
        data = payload.model_dump()
        data["_project_id"] = current_project_id(request)
        data["_created_by_user_id"] = str(
            (identity_context(request).get("user") or {}).get("id") or ""
        )
        if isinstance(data.get("modules"), str):
            data["modules"] = [item.strip() for item in data["modules"].split(",") if item.strip()]
        data["modes"] = [item for item in data.get("modes", []) if item in {"monitor", "cpu", "gpu"}]
        if not data["modes"]:
            raise HTTPException(status_code=400, detail="至少选择一种压测模式")
        if data.get("session_id"):
            try:
                session = get_visible_server_session(
                    data["session_id"], request, touch=True
                )
            except HTTPException as exc:
                raise HTTPException(status_code=409, detail="服务器会话已关闭，请重新连接") from exc
            data.update({
                "server_name": session.get("name") or data.get("server_name") or "",
                "host": session.get("host") or "",
                "port": session.get("port") or 22,
                "user": session.get("user") or "",
                "password": "",
            })
        elif not data.get("host") or not data.get("user"):
            raise HTTPException(status_code=400, detail="请先连接服务器会话")
        data["target_name"] = data.get("server_name") or data["host"]
        try:
            return stress_manager.submit(data)
        except KeyError as exc:
            raise HTTPException(status_code=409, detail="服务器会话已关闭，请重新连接") from exc
        except SecretEncryptionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get("/stress-jobs/{job_id}")
    async def get_stress_job(job_id: str, request: Request):
        job = platform_store.get_stress_job(job_id)
        if not job or not visible_in_project(job, request):
            raise HTTPException(status_code=404, detail="stress job not found")
        return job

    @router.get("/stress-jobs/{job_id}/metrics")
    async def get_stress_metrics(
        job_id: str,
        request: Request,
        after_id: int = Query(default=0, ge=0),
        limit: int = Query(default=5000, ge=1, le=20000),
    ):
        job = platform_store.get_stress_job(job_id)
        if not job or not visible_in_project(job, request):
            raise HTTPException(status_code=404, detail="stress job not found")
        metrics = platform_store.get_stress_samples(job_id, after_id, limit)
        return {"metrics": metrics, "last_id": metrics[-1]["id"] if metrics else after_id}

    @router.post("/stress-jobs/{job_id}/stop")
    async def stop_stress_job(job_id: str, request: Request):
        job = platform_store.get_stress_job(job_id)
        if not job or not visible_in_project(job, request):
            raise HTTPException(status_code=404, detail="stress job not found")
        try:
            return stress_manager.request_stop(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="stress job not found") from exc

    def _stress_report_response(job_id: str, format_name: str, request: Request):
        job = platform_store.get_stress_job(job_id)
        if not job or not visible_in_project(job, request):
            raise HTTPException(status_code=404, detail="stress job not found")
        normalized = format_name.lower()
        if normalized not in {"docx", "pdf"}:
            raise HTTPException(status_code=400, detail="format must be docx or pdf")
        try:
            artifact_value = str(job.get("artifact_dir") or "server_stress/" + job_id)
            artifact_dir = artifact_storage.resolve(artifact_value)
            report_value = str(artifact_dir / "report" / f"server_performance_report.{normalized}")
            path = artifact_storage.resolve_file(report_value, container=artifact_dir)
        except (ValueError, FileNotFoundError) as exc:
            if isinstance(exc, FileNotFoundError):
                raise HTTPException(status_code=409, detail="stress report has not been generated") from exc
            raise HTTPException(status_code=400, detail="invalid stress report path") from exc
        media = "application/pdf" if normalized == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        return FileResponse(str(path), media_type=media, filename=path.name)

    @router.get("/stress-jobs/{job_id}/download")
    async def download_stress_report(job_id: str, request: Request):
        return _stress_report_response(job_id, "docx", request)

    @router.get("/stress-jobs/{job_id}/download/{format_name}")
    async def download_stress_report_format(job_id: str, format_name: str, request: Request):
        return _stress_report_response(job_id, format_name, request)

    return router, report_manager, platform_store, stress_manager
