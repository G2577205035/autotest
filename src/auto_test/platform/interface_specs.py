"""Safe parsing and runtime resolution for versioned endpoint definitions."""

from __future__ import annotations

import base64
import json
import re
import shlex
from typing import Any
from urllib.parse import parse_qsl, urlparse, urlsplit, urlunsplit

import yaml

from auto_test.common.paths import PROJECT_ROOT, prepare_runtime_layout
from auto_test.platform.persistence import create_platform_repository


_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def _curl_tokens(text: str) -> list[str]:
    normalized = str(text or "").strip()
    normalized = re.sub(r"(?:\\|\^|`)\s*\r?\n", " ", normalized)
    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError as exc:
        raise ValueError(f"cURL 引号或转义不完整：{exc}") from exc
    curl_index = next(
        (
            index
            for index, token in enumerate(tokens)
            if token.lower() in {"curl", "curl.exe"}
        ),
        None,
    )
    if curl_index is None:
        raise ValueError("粘贴内容不是以 curl 开始的命令")
    return [token for token in tokens[curl_index + 1 :] if token not in {"\\", "^", "`"}]


def _strip_nested_quotes(value: str) -> str:
    result = str(value)
    if len(result) >= 2 and result[0] == result[-1] and result[0] in {"'", '"'}:
        return result[1:-1]
    return result


def _split_curl_pair(value: str, label: str) -> tuple[str, str]:
    if "=" not in value:
        raise ValueError(f"{label} 必须使用 name=value 格式")
    key, raw_value = value.split("=", 1)
    key = key.strip()
    if not key:
        raise ValueError(f"{label}名称不能为空")
    return key, _strip_nested_quotes(raw_value)


def parse_curl_request(text: str) -> dict[str, Any]:
    """Parse a pasted cURL command without executing it or reading local files."""

    tokens = _curl_tokens(text)
    url = ""
    explicit_method = ""
    headers: dict[str, str] = {}
    form_values: dict[str, str] = {}
    data_values: list[str] = []
    query_values: list[str] = []
    warnings: list[str] = []
    use_get = False
    timeout_seconds = 30.0

    def require_value(index: int, option: str) -> tuple[str, int]:
        if index + 1 >= len(tokens):
            raise ValueError(f"cURL 参数 {option} 缺少值")
        return tokens[index + 1], index + 2

    index = 0
    while index < len(tokens):
        token = tokens[index]
        lower = token.lower()
        if lower in {"--url"}:
            url, index = require_value(index, token)
            continue
        if lower.startswith("--url="):
            url = token.split("=", 1)[1]
            index += 1
            continue
        if token in {"-X", "--request"}:
            explicit_method, index = require_value(index, token)
            continue
        if token.startswith("-X") and len(token) > 2:
            explicit_method = token[2:]
            index += 1
            continue
        if token in {"-H", "--header"}:
            header, index = require_value(index, token)
        elif lower.startswith("--header="):
            header = token.split("=", 1)[1]
            index += 1
        elif token.startswith("-H") and len(token) > 2:
            header = token[2:]
            index += 1
        else:
            header = None
        if header is not None:
            if ":" not in header:
                raise ValueError("请求头必须使用 Name: Value 格式")
            name, value = header.split(":", 1)
            name = name.strip()
            if not name:
                raise ValueError("请求头名称不能为空")
            if name.lower() == "content-length":
                warnings.append("已忽略 Content-Length，由平台按实际请求重新计算")
            else:
                headers[name] = value.lstrip()
            continue
        if token in {"-F", "--form", "--form-string"}:
            form, index = require_value(index, token)
        elif lower.startswith("--form=") or lower.startswith("--form-string="):
            form = token.split("=", 1)[1]
            index += 1
        elif token.startswith("-F") and len(token) > 2:
            form = token[2:]
            index += 1
        else:
            form = None
        if form is not None:
            key, value = _split_curl_pair(form, "表单字段")
            if token != "--form-string" and value.startswith(("@", "<")):
                raise ValueError(
                    f"表单字段 {key} 引用了本机文件；快速填充不会读取客户端文件路径"
                )
            if key in form_values:
                warnings.append(f"表单字段 {key} 重复，已保留最后一个值")
            form_values[key] = value
            continue
        data_option = token in {
            "-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "--json"
        }
        if data_option:
            value, index = require_value(index, token)
            if value.startswith("@") and token != "--data-raw":
                raise ValueError(f"{token} 引用了本机文件；快速填充不会读取客户端文件路径")
            data_values.append(value)
            if token == "--json":
                headers.setdefault("Content-Type", "application/json")
                headers.setdefault("Accept", "application/json")
            continue
        matched_data = next(
            (
                option
                for option in ("--data=", "--data-raw=", "--data-binary=", "--data-urlencode=", "--json=")
                if lower.startswith(option)
            ),
            "",
        )
        if matched_data:
            value = token.split("=", 1)[1]
            if value.startswith("@") and matched_data not in {"--data-raw="}:
                raise ValueError("cURL 数据引用了本机文件；快速填充不会读取客户端文件路径")
            data_values.append(value)
            if matched_data == "--json=":
                headers.setdefault("Content-Type", "application/json")
                headers.setdefault("Accept", "application/json")
            index += 1
            continue
        if token in {"--url-query"}:
            value, index = require_value(index, token)
            query_values.append(value)
            continue
        if lower.startswith("--url-query="):
            query_values.append(token.split("=", 1)[1])
            index += 1
            continue
        if token in {"-G", "--get"}:
            use_get = True
            index += 1
            continue
        if token in {"-I", "--head"}:
            explicit_method = "HEAD"
            index += 1
            continue
        if token in {"-b", "--cookie"}:
            value, index = require_value(index, token)
            if value.startswith("@"):
                raise ValueError("Cookie 引用了本机文件；快速填充不会读取客户端文件路径")
            headers["Cookie"] = value
            continue
        if token in {"-A", "--user-agent"}:
            value, index = require_value(index, token)
            headers["User-Agent"] = value
            continue
        if token in {"-e", "--referer"}:
            value, index = require_value(index, token)
            headers["Referer"] = value
            continue
        if token in {"-u", "--user"}:
            value, index = require_value(index, token)
            encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
            continue
        if token in {"--max-time"}:
            value, index = require_value(index, token)
            try:
                timeout_seconds = max(1.0, min(120.0, float(value)))
            except ValueError as exc:
                raise ValueError("--max-time 必须是秒数") from exc
            continue
        if lower.startswith("--max-time="):
            try:
                timeout_seconds = max(1.0, min(120.0, float(token.split("=", 1)[1])))
            except ValueError as exc:
                raise ValueError("--max-time 必须是秒数") from exc
            index += 1
            continue
        if token in {"-L", "--location"}:
            warnings.append("平台调试默认不跟随重定向，已忽略 --location")
            index += 1
            continue
        if token in {"-k", "--insecure"}:
            warnings.append("平台不会关闭 TLS 证书校验，已忽略 --insecure")
            index += 1
            continue
        if token in {"-T", "--upload-file"} or lower.startswith("--upload-file="):
            raise ValueError("快速填充暂不支持 --upload-file 本机文件上传")
        if re.match(r"^https?://", token, re.I):
            url = token
        index += 1

    if not url:
        raise ValueError("cURL 中未找到 http:// 或 https:// 请求地址")
    parsed_url = urlsplit(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        raise ValueError("cURL 请求地址必须是有效的 http:// 或 https:// URL")
    if parsed_url.username or parsed_url.password:
        raise ValueError("cURL 请求地址不能直接包含账号或密码")
    if parsed_url.fragment:
        raise ValueError("cURL 请求地址不能包含 Fragment")

    query: dict[str, str] = {}
    for key, value in parse_qsl(parsed_url.query, keep_blank_values=True):
        if key in query:
            warnings.append(f"Query 参数 {key} 重复，已保留最后一个值")
        query[key] = value
    for raw_query in query_values:
        key, value = _split_curl_pair(raw_query, "Query 参数")
        query[key] = value

    body: Any = None
    body_type = "none"
    if form_values and data_values:
        raise ValueError("同一条 cURL 同时包含 --form 和 --data，无法确定唯一请求体")
    if form_values:
        body_type = "multipart"
        body = form_values
        for name in list(headers):
            if name.lower() == "content-type" and headers[name].lower().startswith("multipart/form-data"):
                headers.pop(name)
                warnings.append("multipart 边界将由平台按实际表单重新生成")
    elif data_values:
        raw_data = "&".join(data_values)
        if use_get:
            for key, value in parse_qsl(raw_data, keep_blank_values=True):
                query[key] = value
        else:
            content_type = next(
                (value.lower() for key, value in headers.items() if key.lower() == "content-type"),
                "",
            )
            if "json" in content_type or raw_data.lstrip().startswith(("{", "[")):
                try:
                    body = json.loads(raw_data)
                    body_type = "json"
                except json.JSONDecodeError:
                    body = raw_data
                    body_type = "raw"
                    warnings.append("请求体看起来像 JSON，但解析失败，已按原始文本保留")
            elif "=" in raw_data:
                body = dict(parse_qsl(raw_data, keep_blank_values=True))
                body_type = "urlencoded"
            else:
                body = raw_data
                body_type = "raw"

    method = explicit_method.upper() if explicit_method else (
        "GET" if use_get else "POST" if body_type != "none" else "GET"
    )
    if method not in _METHODS:
        raise ValueError(f"平台暂不支持 cURL 中的 HTTP 方法：{method}")
    target = urlunsplit((parsed_url.scheme, parsed_url.netloc, parsed_url.path or "/", "", ""))
    return {
        "method": method,
        "target": target,
        "headers": headers,
        "query": query,
        "body_type": body_type,
        "body": body,
        "timeout_seconds": timeout_seconds,
        "warnings": list(dict.fromkeys(warnings)),
    }


def _from_url(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError("接口 URL 必须包含 http:// 或 https://")
    return {
        "scheme": parsed.scheme,
        "host": parsed.hostname,
        "port": parsed.port or (443 if parsed.scheme == "https" else 80),
        "path": parsed.path or "/",
    }


def parse_endpoint_text(text: str, logical_name: str, default_path: str = "") -> list[dict[str, Any]]:
    raw = text.strip()
    if not raw:
        raise ValueError("接口内容不能为空")
    if re.match(r"^\s*(?:\$\s*)?curl(?:\.exe)?\s", raw, re.I):
        curl_request = parse_curl_request(raw)
        result = _from_url(curl_request["target"])
        result.update({
            "logical_name": logical_name,
            "default_path": default_path,
            "method": curl_request["method"],
            "source_type": "curl",
            "spec": {
                "headers": curl_request["headers"],
                "query": curl_request["query"],
                "body_type": curl_request["body_type"],
                "body": curl_request["body"],
            },
        })
        return [_validate(result)]

    try:
        doc = json.loads(raw)
        source_type = "json"
    except json.JSONDecodeError:
        doc = yaml.safe_load(raw)
        source_type = "yaml"
    if not isinstance(doc, dict):
        raise ValueError("接口内容必须是 JSON/YAML 对象或 cURL")

    if isinstance(doc.get("paths"), dict):
        servers = doc.get("servers") or []
        base = _from_url(servers[0]["url"]) if servers and servers[0].get("url") else {
            "scheme": "http", "host": "", "port": None, "path": "/"
        }
        parsed_items = []
        for path, methods in doc["paths"].items():
            if not isinstance(methods, dict):
                continue
            for method, spec in methods.items():
                if method.upper() not in _METHODS:
                    continue
                op_name = str((spec or {}).get("operationId") or logical_name or f"{method}_{path}")
                parsed_items.append(_validate({
                    **base, "logical_name": op_name,
                    "default_path": default_path if len(doc["paths"]) == 1 else "",
                    "method": method.upper(), "path": str(path),
                    "source_type": f"openapi-{source_type}", "spec": spec or {},
                }))
        if not parsed_items:
            raise ValueError("OpenAPI 文档中没有可导入的 paths")
        return parsed_items

    url_data = _from_url(str(doc["url"])) if doc.get("url") else {
        "scheme": str(doc.get("scheme", "http")), "host": str(doc.get("host", "")),
        "port": doc.get("port"), "path": str(doc.get("path", "")),
    }
    result = {
        **url_data,
        "logical_name": str(doc.get("logical_name") or logical_name),
        "default_path": str(doc.get("default_path") or default_path),
        "method": str(doc.get("method", "GET")).upper(),
        "source_type": source_type,
        "spec": doc,
    }
    return [_validate(result)]


def _validate(item: dict[str, Any]) -> dict[str, Any]:
    if not item.get("logical_name"):
        raise ValueError("必须提供逻辑接口名称")
    if item.get("method") not in _METHODS:
        raise ValueError(f"不支持的 HTTP 方法：{item.get('method')}")
    path = str(item.get("path", ""))
    if not path.startswith("/") or ".." in path:
        raise ValueError("接口 path 必须以 / 开头且不能包含 ..")
    port = item.get("port")
    if port not in (None, "") and not 1 <= int(port) <= 65535:
        raise ValueError("端口必须在 1-65535 之间")
    item["port"] = int(port) if port not in (None, "") else None
    return item


def resolve_runtime_endpoint(default_path: str) -> dict[str, Any] | None:
    """Return a published address override; request bodies remain code-defined."""
    prepare_runtime_layout()
    try:
        return create_platform_repository(PROJECT_ROOT, recover_jobs=False).resolve_endpoint(default_path)
    except Exception:
        return None
