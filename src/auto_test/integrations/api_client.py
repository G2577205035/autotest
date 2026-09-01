"""Reliable HTTP client for the target-platform service APIs."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable
import json
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from auto_test.common.logging import log
from auto_test.common.utils import json_extract, safe_int


DEFAULT_HEADERS = {
    "User-Agent": "auto-test/1.0",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}


@dataclass(frozen=True)
class ApiPorts:
    auth: int = 8000
    file: int = 7999
    upload: int = 8014
    other: int = 8003


class TargetPlatformApiError(RuntimeError):
    """Base error raised by :class:`TargetPlatformClient`."""


class TargetPlatformConnectionError(TargetPlatformApiError):
    """The remote service could not be reached."""


class TargetPlatformResponseError(TargetPlatformApiError):
    """The remote service returned an invalid or unsuccessful response."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class TargetPlatformClient:
    """A reusable, timeout-aware client for all target-platform HTTP APIs.

    GET requests are retried for transient failures. Mutating POST requests are
    deliberately not retried automatically because most upstream endpoints do
    not expose idempotency keys.
    """

    def __init__(
        self,
        host: str,
        *,
        scheme: str = "http",
        ports: ApiPorts | None = None,
        connect_timeout: float = 10,
        read_timeout: float = 60,
        upload_timeout: float = 300,
        retry_total: int = 2,
        trust_env: bool = False,
        session: requests.Session | None = None,
    ):
        parsed = urlparse(host if "://" in host else f"{scheme}://{host}")
        if not parsed.hostname:
            raise ValueError("Target platform host cannot be empty")

        self.host = parsed.hostname
        self.scheme = parsed.scheme or scheme
        self.ports = ports or ApiPorts()
        self.timeout = (connect_timeout, read_timeout)
        self.upload_timeout = (connect_timeout, upload_timeout)
        self.session = session or requests.Session()
        # Target-platform APIs are normally private-network services.  Inheriting
        # desktop/PyCharm proxy variables can silently route 172.16/10/192.168
        # traffic through a localhost proxy and turn a healthy target into a
        # 60-second read timeout.  Proxy inheritance therefore requires an
        # explicit opt-in from the environment configuration.
        self.session.trust_env = bool(trust_env)
        self.session.headers.update(DEFAULT_HEADERS)

        retry = Retry(
            total=retry_total,
            connect=retry_total,
            read=retry_total,
            status=retry_total,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.host}"

    def close(self) -> None:
        self.session.close()

    def _url(self, port: int, path: str) -> str:
        return f"{self.origin}:{port}{path}"

    def _headers(
        self,
        token: str | None = None,
        *,
        content_type: str | None = None,
        cookie: bool = False,
    ) -> dict[str, str]:
        headers = {"Origin": self.origin, "Referer": f"{self.origin}/"}
        if token:
            headers["satoken"] = token
            if cookie:
                headers["Cookie"] = f"satoken={token}"
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _request(
        self,
        method: str,
        port: int,
        path: str,
        *,
        expected_statuses: Iterable[int] = (200,),
        timeout: tuple[float, float] | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        # Published endpoint specs can hot-swap transport details without
        # changing request payload code. Draft specs never affect production calls.
        try:
            from auto_test.platform.interface_specs import resolve_runtime_endpoint
            override = resolve_runtime_endpoint(path)
        except Exception:
            override = None
        if override:
            method = str(override.get("method") or method).upper()
            port = int(override.get("port") or port)
            path = str(override.get("path") or path)
            override_host = str(override.get("host") or "").strip()
            override_scheme = str(override.get("scheme") or self.scheme)
            url = (
                f"{override_scheme}://{override_host}:{port}{path}"
                if override_host else self._url(port, path)
            )
        else:
            url = self._url(port, path)
        try:
            response = self.session.request(
                method,
                url,
                timeout=timeout or self.timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise TargetPlatformConnectionError(f"{method} {path} failed: {exc}") from exc

        if response.status_code not in set(expected_statuses):
            body = (response.text or "")[:300].replace("\n", " ")
            raise TargetPlatformResponseError(
                f"{method} {path} returned HTTP {response.status_code}: {body}",
                status_code=response.status_code,
            )
        return response

    @staticmethod
    def _json(response: requests.Response, operation: str) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise TargetPlatformResponseError(f"{operation} returned non-JSON data") from exc
        if not isinstance(data, dict):
            raise TargetPlatformResponseError(f"{operation} returned an unexpected JSON structure")
        return data

    @staticmethod
    def _find_value(data: Any, keys: set[str]) -> Any:
        if isinstance(data, dict):
            for key, value in data.items():
                if str(key).lower() in keys:
                    return value
            for value in data.values():
                found = TargetPlatformClient._find_value(value, keys)
                if found is not None:
                    return found
        elif isinstance(data, list):
            for item in data:
                found = TargetPlatformClient._find_value(item, keys)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _business_result(data: dict[str, Any]) -> tuple[bool | None, str]:
        message = TargetPlatformClient._find_value(data, {"msg", "message", "error", "errmsg", "detail"})
        message = "" if message is None else str(message)
        text = json.dumps(data, ensure_ascii=False)
        failure_words = (
            "失败", "错误", "异常", "已存在", "重复", "禁用", "无权限", "参数",
            "fail", "failed", "error", "invalid", "exist", "duplicate", "denied",
        )
        success_words = ("成功", "success", "ok")
        if any(word in message.lower() for word in failure_words) or any(word in text.lower() for word in failure_words):
            return False, message or text[:300]

        code = TargetPlatformClient._find_value(data, {"code", "statuscode", "status_code"})
        if code is not None:
            normalized = str(code).strip().lower()
            if normalized in {"0", "00", "000000", "200", "success", "ok", "true"}:
                return True, message
            return False, message or f"code={code}"

        success = TargetPlatformClient._find_value(data, {"success", "succeeded", "result"})
        if isinstance(success, bool):
            return success, message
        if isinstance(success, str) and success.strip().lower() in {"true", "success", "ok"}:
            return True, message

        if any(word in message.lower() for word in success_words):
            return True, message
        return None, message or text[:300]

    @staticmethod
    def _is_transient_error(reason: str) -> bool:
        text = str(reason or "").lower()
        transient_words = (
            "服务器忙", "稍后", "稍候", "繁忙", "忙", "超时", "网关",
            "timeout", "timed out", "temporarily", "unavailable", "busy",
            "try again", "gateway", "502", "503", "504",
        )
        return any(word in text for word in transient_words)

    # Authentication -------------------------------------------------

    def login(self, username: str, password: str) -> str | None:
        log.info(f"登录：{username} @ {self.host}")
        try:
            response = self._request(
                "POST",
                self.ports.auth,
                "/system/login/loginIn",
                data={"username": username, "password": password},
                headers=self._headers(
                    content_type="application/x-www-form-urlencoded;charset=UTF-8"
                ),
            )
        except TargetPlatformResponseError as exc:
            log.warning(f"登录失败：{exc}")
            return None

        data = self._json(response, "login")
        token = json_extract(data, "$..satoken")
        if not token or token == "NOT_FOUND":
            log.warning(f"未提取到 satoken，账号可能不存在、密码错误或账号不可用：{username}")
            return None
        log.info("登录成功")
        return str(token)

    def create_user(
        self,
        admin_token: str,
        username: str,
        password: str,
        *,
        retries: int = 3,
        retry_delay: float = 2.0,
    ) -> bool:
        log.info(f"创建用户：{username}")
        now_ms = str(int(time.time() * 1000))
        far_future_ms = str(int(time.time() * 1000) + 100 * 365 * 24 * 3600 * 1000)
        attempts = max(1, int(retries))
        last_reason = ""
        had_transient_error = False
        payload = {
            "username": username,
            "password": password,
            "unitName": "",
            "email": "",
            "idCard": "",
            "createDate": now_ms,
            "phone": "",
            "lastLoginDate": "",
            "headImage": "",
            "startDate": now_ms,
            "endDate": far_future_ms,
            "lastLoginIp": "",
            "roleId": "1",
            "remark": "",
            "departmentId": "1",
            "translatePriority": "9",
        }

        for attempt in range(1, attempts + 1):
            try:
                response = self._request(
                    "POST",
                    self.ports.auth,
                    "/system/users/saveOrUpdateUser",
                    data=payload,
                    headers=self._headers(
                        admin_token,
                        content_type="application/x-www-form-urlencoded",
                        cookie=True,
                    ),
                )
                data = self._json(response, "create user")
                ok, reason = self._business_result(data)
                last_reason = reason
                if ok is False:
                    if attempt < attempts and self._is_transient_error(reason):
                        had_transient_error = True
                        log.warning(f"创建用户暂时失败（第 {attempt}/{attempts} 次）：{reason}，准备重试")
                        time.sleep(retry_delay)
                        continue
                    log.error(f"创建用户失败：{reason}")
                    break
                if ok is None:
                    log.warning(f"创建用户接口返回无法确认成功，按 HTTP 成功继续：{reason}")
                return True
            except TargetPlatformApiError as exc:
                last_reason = str(exc)
                if attempt < attempts and self._is_transient_error(last_reason):
                    had_transient_error = True
                    log.warning(f"创建用户请求暂时异常（第 {attempt}/{attempts} 次）：{last_reason}，准备重试")
                    time.sleep(retry_delay)
                    continue
                log.error(f"创建用户失败：{exc}")
                break

        if had_transient_error:
            log.warning("创建用户接口返回过临时错误，复查新账号是否已经可登录")
            if self.login(username, password):
                log.info("复查登录成功，认为用户已创建")
                return True
        if last_reason:
            log.error(f"创建用户最终失败：{last_reason}")
        return False

    def check_upload_disk(self, token: str) -> dict[str, Any]:
        response = self._request(
            "GET",
            self.ports.auth,
            "/system/diskStorage/checkUpload",
            headers=self._headers(token),
        )
        return self._json(response, "check upload disk").get("data") or {}

    # Cases ----------------------------------------------------------

    def list_cases(self, token: str) -> list[dict[str, Any]]:
        response = self._request(
            "GET",
            self.ports.file,
            "/analysis/case_tree/listEmailCaseTreeParentEmail",
            params={"ishandle": "0", "keyword": "", "daterange": ""},
            headers=self._headers(token),
        )
        return self._json(response, "list cases").get("data") or []

    def create_case(self, token: str, case_name: str) -> bool:
        log.info(f"创建案件：{case_name}")
        try:
            self._request(
                "POST",
                self.ports.file,
                "/analysis/case-label/create",
                json={"id": "", "caseName": case_name, "labelColor": "#ea731b", "labelList": []},
                headers=self._headers(token, content_type="application/json"),
            )
            return True
        except TargetPlatformApiError as exc:
            log.error(f"创建案件失败：{exc}")
            return False

    # Upload ---------------------------------------------------------

    def upload_files(
        self,
        token: str,
        case_id: str,
        case_name: str,
        data_name: str,
        translate_name: str,
        analysis: str,
        file_list: list[tuple[str, Any]],
    ) -> tuple[str, Any, str]:
        files = [("files", (name, data, "application/octet-stream")) for name, data in file_list]
        response = self._request(
            "POST",
            self.ports.upload,
            "/upload/upload_log_thread/multiFileInfoUpload",
            data={
                "caseId": case_id,
                "caseName": case_name,
                "filePath": "undefined",
                "dataDescribe": "",
                "dataName": data_name,
                "translateName": translate_name,
                "analysis": analysis,
            },
            files=files,
            headers=self._headers(token),
            timeout=self.upload_timeout,
        )
        data = self._json(response, "upload files")
        upload_id = json_extract(data, "$..ulId")
        if not upload_id or upload_id == "NOT_FOUND":
            raise TargetPlatformResponseError("upload response does not contain ulId")
        file_number = json_extract(data, "$..fileNumber")
        returned_name = json_extract(data, "$..dataName") or json_extract(data, "$..data_name")
        return str(upload_id), file_number, returned_name or data_name

    def listen_multi_fast_upload(
        self,
        token: str,
        case_id: str,
        case_name: str,
        data_name: str,
        file_path: str,
        translate_name: str,
        analysis: str,
    ) -> tuple[str, Any, str]:
        self._request(
            "POST",
            self.ports.upload,
            "/upload/upload_log_thread/listenMultiFileFastUpload",
            data={
                "caseId": case_id,
                "caseName": case_name,
                "filePath": file_path,
                "dataDescribe": "",
                "dataName": data_name,
                "translateName": translate_name,
                "analysis": analysis,
            },
            headers=self._headers(token),
            timeout=self.upload_timeout,
        )
        for _ in range(10):
            time.sleep(2)
            for item in self.list_upload_logs(token, data_name=data_name):
                if item.get("dataName") == data_name:
                    return str(item.get("id")), item.get("fileNumber", 0), data_name
        raise TargetPlatformResponseError(f"服务器直传后未找到批次：{data_name}")

    # Job and file status -------------------------------------------

    def get_upload_status(self, token: str, upload_id: str) -> dict[str, Any]:
        response = self._request(
            "GET",
            self.ports.other,
            "/other/upload_log/getUploadById",
            params={"id": upload_id},
            headers=self._headers(token),
        )
        data = self._json(response, "get upload status")
        return {
            "parseProgress": safe_int(json_extract(data, "$..parseProgress")),
            "translateProgress": safe_int(json_extract(data, "$..translateProgress")),
            "status": json_extract(data, "$..status") or "",
            "fileNumber": safe_int(json_extract(data, "$..fileNumber")),
        }

    def list_upload_logs(self, token: str, data_name: str = "") -> list[dict[str, Any]]:
        response = self._request(
            "POST",
            self.ports.other,
            "/other/upload_log/listUploadLogs",
            data={
                "dataName": data_name,
                "uploadDate": "",
                "uploadDateEnd": "",
                "currentPage": "1",
                "pageSize": "100",
                "total": "0",
                "parseStatus": "",
            },
            headers=self._headers(
                token,
                content_type="application/x-www-form-urlencoded",
                cookie=True,
            ),
        )
        return self._json(response, "list upload logs").get("data") or []

    def list_file_info(self, token: str, upload_id: str) -> list[Any]:
        page_size = 200
        all_ids: list[Any] = []
        page = 1
        while True:
            params = {
                "emailInfoSearch": "", "keyword": "", "isCollection": "",
                "isLabel": "", "isAttach": "", "isReader": "", "isNotReader": "",
                "isEncryptAttach": "", "isVirus": "", "isEmail": "", "isDocument": "",
                "isImage": "", "isAttachment": "", "ulId": upload_id, "caseId": "",
                "objId": "", "total": "0", "notreadtotal": "0", "groupParam": "",
                "startDate": "", "endDate": "", "md5": "",
                "contentRemoveRepetition": "", "sender": "", "receiver": "",
                "fileName": "", "notTranslate": "", "isTranslated": "",
                "orderName": "date", "orderway": "desc", "fileIds": "",
                "searchScope": "subject,content,attachment", "searchTab": "",
                "domainNameKey": "", "folderId": "", "page": str(page),
                "pageSize": str(page_size),
            }
            response = self._request(
                "GET",
                self.ports.file,
                "/analysis/file_info/listFileInfo",
                params=params,
                headers=self._headers(token),
            )
            data = self._json(response, "list file info")
            page_ids = json_extract(data, "$.data[*].id") or []
            all_ids.extend(page_ids)
            if len(page_ids) < page_size:
                break
            payload = data.get("data")
            if isinstance(payload, dict) and payload.get("total") is not None:
                if len(all_ids) >= int(payload["total"]):
                    break
            page += 1
        return all_ids

    def get_file_detail(self, token: str, file_id: Any) -> dict[str, Any]:
        response = self._request(
            "GET",
            self.ports.file,
            "/analysis/file_info/getFileInfoDetail",
            params={"id": file_id},
            headers=self._headers(token, cookie=True),
        )
        data = self._json(response, "get file detail")
        return {
            "translateStatus": json_extract(data, "$..translateStatus") or "",
            "fileName": json_extract(data, "$..fileName") or "",
            "filePath": json_extract(data, "$..filePath") or "",
            "summary": json_extract(data, "$..summary"),
        }

    def get_translate_content(self, token: str, file_id: Any) -> dict[str, str]:
        response = self._request(
            "GET",
            self.ports.file,
            "/analysis/file_info/getTranslateContentSummaryById",
            params={"id": file_id},
            headers=self._headers(token),
        )
        data = self._json(response, "get translation content")
        return {"translateContent": json_extract(data, "$..translateContent") or ""}

    # Exports --------------------------------------------------------

    def _start_export(self, token: str, path: str, case_id: str, upload_ids: str) -> bool:
        response = self._request(
            "POST",
            self.ports.file,
            path,
            data={
                "caseIds": str(case_id),
                "objIds": "",
                "ulIds": upload_ids,
                "watermarkName": "",
            },
            headers=self._headers(
                token,
                content_type="application/x-www-form-urlencoded",
                cookie=True,
            ),
        )
        return self._json(response, "start export").get("code") == 200

    def export_translation(self, token: str, case_id: str, upload_ids: str = "") -> bool:
        log.info("发起导出译文批次...")
        return self._start_export(
            token,
            "/analysis/file_info/batchDownloadsTranByUlIds",
            case_id,
            upload_ids,
        )

    def export_original(self, token: str, case_id: str, upload_ids: str = "") -> bool:
        log.info("发起导出原文批次...")
        return self._start_export(
            token,
            "/analysis/file_info/exportEmailByCaseIds",
            case_id,
            upload_ids,
        )

    def list_download_tasks(self, token: str, page_size: int = 100) -> list[dict[str, Any]]:
        response = self._request(
            "POST",
            self.ports.file,
            "/analysis/download_task/listDownloadTasks",
            data={
                "taskName": "",
                "status": "",
                "startDate": "",
                "endDateFilter": "",
                "currentPage": "1",
                "pageSize": str(page_size),
                "total": "0",
            },
            headers=self._headers(
                token,
                content_type="application/x-www-form-urlencoded",
                cookie=True,
            ),
        )
        return self._json(response, "list download tasks").get("data") or []


_thread_clients = threading.local()


def client_for(host: str) -> TargetPlatformClient:
    """Return one reusable client per host and worker thread."""
    clients = getattr(_thread_clients, "clients", None)
    if clients is None:
        clients = {}
        _thread_clients.clients = clients
    if host not in clients:
        from auto_test.common.config_loader import api_cfg

        settings = api_cfg()
        use_system_proxy = settings.get("use_system_proxy", False)
        if not isinstance(use_system_proxy, bool):
            use_system_proxy = str(use_system_proxy).strip().lower() in {
                "1", "true", "yes", "y", "on", "启用",
            }
        port_settings = settings.get("ports", {})
        ports = ApiPorts(
            auth=int(port_settings.get("auth", 8000)),
            file=int(port_settings.get("file", 7999)),
            upload=int(port_settings.get("upload", 8014)),
            other=int(port_settings.get("other", 8003)),
        )
        clients[host] = TargetPlatformClient(
            host,
            scheme=settings.get("scheme", "http"),
            ports=ports,
            connect_timeout=float(settings.get("connect_timeout", 10)),
            read_timeout=float(settings.get("read_timeout", 60)),
            upload_timeout=float(settings.get("upload_timeout", 300)),
            retry_total=int(settings.get("retry_total", 2)),
            trust_env=use_system_proxy,
        )
    return clients[host]
