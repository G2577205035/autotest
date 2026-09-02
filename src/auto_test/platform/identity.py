"""Local identity, session and project-role services for the Web console."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from auto_test.common.env import get_env
from auto_test.platform.contracts import PlatformRepository


SESSION_COOKIE = "liema_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW_SECONDS = 5 * 60
PROJECT_ROLES = {
    "viewer": {
        "label": "只读成员",
        "permissions": {"project:view", "evaluation:view"},
    },
    "tester": {
        "label": "测试执行者",
        "permissions": {
            "project:view",
            "test:execute",
            "report:manage",
            "server:operate",
            "evaluation:view",
            "evaluation:operate",
        },
    },
    "project_admin": {
        "label": "项目管理员",
        "permissions": {
            "project:view",
            "test:execute",
            "report:manage",
            "server:operate",
            "project:members",
            "interface:manage",
            "evaluation:view",
            "evaluation:manage",
            "evaluation:operate",
        },
    },
}
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
PROJECT_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_-]{1,39}$")


def normalize_username(value: str) -> str:
    username = str(value or "").strip().casefold()
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError("用户名需为 3–64 位小写字母、数字、点、横线或下划线")
    return username


def normalize_project_key(value: str) -> str:
    project_key = str(value or "").strip().upper()
    if not PROJECT_KEY_PATTERN.fullmatch(project_key):
        raise ValueError("项目标识需为 2–40 位大写字母、数字、横线或下划线，且以字母开头")
    return project_key


def validate_password(password: str) -> None:
    value = str(password or "")
    if len(value) < 12:
        raise ValueError("密码至少需要 12 个字符")
    if len(value) > 200:
        raise ValueError("密码不能超过 200 个字符")
    categories = sum(
        bool(pattern.search(value))
        for pattern in (re.compile(r"[a-z]"), re.compile(r"[A-Z]"), re.compile(r"\d"), re.compile(r"[^A-Za-z0-9]"))
    )
    if categories < 3:
        raise ValueError("密码需包含大写字母、小写字母、数字、特殊字符中的至少三类")


def hash_password(password: str) -> str:
    validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=64
    )
    return "scrypt$16384$8$1${}${}".format(
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_n, raw_r, raw_p, raw_salt, raw_digest = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(raw_salt.encode("ascii"))
        expected = base64.urlsafe_b64decode(raw_digest.encode("ascii"))
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(raw_n),
            r=int(raw_r),
            p=int(raw_p),
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _client_ip(request: Request) -> str:
    return str(request.client.host if request.client else "")[:100]


def _set_session_cookie(response: Response, token: str) -> None:
    secure_value = str(get_env("SESSION_COOKIE_SECURE", "false")).strip().lower()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=secure_value in {"1", "true", "yes", "on"},
        samesite="lax",
        path="/",
    )


@dataclass
class AuthenticatedSession:
    token_hash: str
    session: dict[str, Any]


class LoginAttemptLimiter:
    """Small process-local throttle for repeated failures per IP and username."""

    def __init__(
        self,
        *,
        limit: int = LOGIN_FAILURE_LIMIT,
        window_seconds: float = LOGIN_FAILURE_WINDOW_SECONDS,
    ):
        self.limit = max(1, int(limit))
        self.window_seconds = max(1.0, float(window_seconds))
        self._failures: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(request: Request, username: str) -> tuple[str, str]:
        return _client_ip(request), str(username or "").strip().casefold()[:64]

    def retry_after(self, key: tuple[str, str], *, now: float | None = None) -> int:
        current = time.time() if now is None else float(now)
        cutoff = current - self.window_seconds
        with self._lock:
            recent = [value for value in self._failures.get(key, []) if value > cutoff]
            if recent:
                self._failures[key] = recent
            else:
                self._failures.pop(key, None)
            if len(recent) < self.limit:
                return 0
            return max(1, int(self.window_seconds - (current - recent[0])))

    def record_failure(self, key: tuple[str, str], *, now: float | None = None) -> None:
        current = time.time() if now is None else float(now)
        cutoff = current - self.window_seconds
        with self._lock:
            recent = [value for value in self._failures.get(key, []) if value > cutoff]
            recent.append(current)
            self._failures[key] = recent[-self.limit :]

    def reset(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._failures.pop(key, None)


class IdentityService:
    def __init__(self, store: PlatformRepository):
        self.store = store
        self._setup_lock = threading.Lock()

    @property
    def setup_required(self) -> bool:
        return self.store.count_users() == 0

    def create_initial_admin(
        self,
        **data: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        with self._setup_lock:
            return self._create_initial_admin(**data)

    def _create_initial_admin(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
        project_key: str,
        project_name: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.setup_required:
            raise RuntimeError("平台已完成管理员初始化")
        normalized = normalize_username(username)
        key = normalize_project_key(project_key)
        display = str(display_name or normalized).strip()[:120]
        name = str(project_name or "默认项目").strip()[:120]
        if not name:
            raise ValueError("请填写项目名称")
        user = self.store.create_user(
            normalized,
            display,
            hash_password(password),
            is_superuser=True,
        )
        try:
            project = self.store.create_project(key, name, "平台初始化项目")
        except Exception:
            self.store.delete_user(user["id"])
            raise
        self.store.set_project_membership(project["id"], user["id"], "project_admin")
        return user, project

    def create_user(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
        is_superuser: bool = False,
    ) -> dict[str, Any]:
        normalized = normalize_username(username)
        display = str(display_name or normalized).strip()[:120]
        if not display:
            raise ValueError("请填写用户姓名")
        return self.store.create_user(
            normalized,
            display,
            hash_password(password),
            is_superuser=is_superuser,
        )

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        try:
            normalized = normalize_username(username)
        except ValueError:
            normalized = ""
        user = self.store.get_user_by_username(normalized, include_password=True) if normalized else None
        if not user or not verify_password(password, str(user.get("password_hash") or "")):
            return None
        if not bool(user.get("is_active")):
            return None
        self.store.update_user(user["id"], last_login_at=time.time())
        return self.store.get_user(user["id"])

    def create_session(self, user_id: str) -> tuple[str, str]:
        token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        self.store.create_auth_session(
            _token_hash(token),
            user_id,
            csrf_token,
            time.time() + SESSION_TTL_SECONDS,
        )
        return token, csrf_token

    def authenticate_token(self, token: str) -> AuthenticatedSession | None:
        if not token:
            return None
        digest = _token_hash(token)
        session = self.store.get_auth_session(digest)
        if not session or not bool(session.get("is_active")):
            return None
        return AuthenticatedSession(token_hash=digest, session=session)

    def context(
        self, authenticated: AuthenticatedSession, project_id: str = ""
    ) -> dict[str, Any]:
        session = authenticated.session
        user = {
            "id": session["user_id"],
            "username": session["username"],
            "display_name": session["display_name"],
            "is_superuser": bool(session.get("is_superuser")),
        }
        projects = self.store.list_user_projects(
            user["id"], superuser=user["is_superuser"]
        )
        selected = None
        if project_id:
            selected = next((item for item in projects if item["id"] == project_id), None)
            if selected is None:
                raise PermissionError("当前用户无权访问所选项目")
        elif projects:
            selected = projects[0]
        permissions = {"platform:manage"} if user["is_superuser"] else set()
        project_role = ""
        if selected:
            project_role = "project_admin" if user["is_superuser"] else str(selected.get("role") or "viewer")
            permissions.update(PROJECT_ROLES.get(project_role, PROJECT_ROLES["viewer"])["permissions"])
        return {
            "authenticated": True,
            "user": user,
            "projects": projects,
            "current_project": selected,
            "legacy_project_id": (
                str(min(projects, key=lambda item: (item["created_at"], item["id"]))["id"])
                if user["is_superuser"] and projects
                else ""
            ),
            "project_role": project_role,
            "permissions": sorted(permissions),
            "csrf_token": session["csrf_token"],
            "session_expires_at": session["expires_at"],
        }

    def has_permission(self, context: dict[str, Any], permission: str) -> bool:
        return bool(context.get("user", {}).get("is_superuser")) or permission in set(
            context.get("permissions") or []
        )

    def audit(self, request: Request, **event: Any) -> None:
        event.setdefault("ip_address", _client_ip(request))
        self.store.add_audit_event(**event)


class SetupInput(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=12, max_length=200)
    project_key: str = Field(default="LIEMA", min_length=2, max_length=40)
    project_name: str = Field(default="烈马测试项目", min_length=1, max_length=120)


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class UserInput(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=12, max_length=200)
    is_superuser: bool = False


class UserUpdateInput(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    password: str | None = Field(default=None, min_length=12, max_length=200)
    is_active: bool | None = None
    is_superuser: bool | None = None


class ProjectInput(BaseModel):
    project_key: str = Field(min_length=2, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)


class ProjectUpdateInput(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    is_active: bool | None = None


class MembershipInput(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    role: str | None = Field(default=None, pattern="^(viewer|tester|project_admin)$")


def _request_context(request: Request) -> dict[str, Any]:
    context = getattr(request.state, "identity", None)
    if not context:
        raise HTTPException(status_code=401, detail="请先登录")
    return context


def _require_superuser(request: Request) -> dict[str, Any]:
    context = _request_context(request)
    if not context["user"]["is_superuser"]:
        raise HTTPException(status_code=403, detail="此操作仅限平台管理员")
    return context


def _require_project_admin(request: Request, project_id: str) -> dict[str, Any]:
    context = _request_context(request)
    if context["user"]["is_superuser"]:
        return context
    membership = next((item for item in context["projects"] if item["id"] == project_id), None)
    if not membership or membership.get("role") != "project_admin":
        raise HTTPException(status_code=403, detail="此操作仅限项目管理员")
    return context


def create_identity_api(
    store: PlatformRepository,
) -> tuple[APIRouter, IdentityService]:
    service = IdentityService(store)
    login_limiter = LoginAttemptLimiter()
    router = APIRouter(prefix="/api")

    @router.get("/auth/status")
    async def auth_status(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        result: dict[str, Any] = {
            "setup_required": service.setup_required,
            "authenticated": False,
        }
        authenticated = service.authenticate_token(request.cookies.get(SESSION_COOKIE, ""))
        if not authenticated:
            return result
        try:
            return {
                "setup_required": False,
                **service.context(authenticated, request.headers.get("X-Project-ID", "")),
            }
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @router.post("/auth/setup", status_code=201)
    async def setup(payload: SetupInput, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        try:
            user, project = service.create_initial_admin(**payload.model_dump())
            token, _ = service.create_session(user["id"])
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=409, detail="用户名或项目标识已存在") from exc
        _set_session_cookie(response, token)
        service.audit(
            request,
            actor_user_id=user["id"],
            project_id=project["id"],
            action="identity.setup",
            target_type="user",
            target_id=user["id"],
            detail={"project_key": project["project_key"]},
        )
        authenticated = service.authenticate_token(token)
        return service.context(authenticated, project["id"]) if authenticated else {}

    @router.post("/auth/login")
    async def login(payload: LoginInput, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        attempt_key = login_limiter.key(request, payload.username)
        retry_after = login_limiter.retry_after(attempt_key)
        if retry_after:
            service.audit(
                request,
                actor_user_id=None,
                project_id=None,
                action="identity.login",
                outcome="denied",
                detail={
                    "username": str(payload.username).strip().casefold(),
                    "reason": "rate_limited",
                },
            )
            raise HTTPException(
                status_code=429,
                detail="登录失败次数过多，请稍后重试",
                headers={
                    "Retry-After": str(retry_after),
                    "Cache-Control": "no-store",
                },
            )
        user = service.authenticate(payload.username, payload.password)
        if not user:
            login_limiter.record_failure(attempt_key)
            service.audit(
                request,
                actor_user_id=None,
                project_id=None,
                action="identity.login",
                outcome="denied",
                detail={"username": str(payload.username).strip().casefold()},
            )
            raise HTTPException(
                status_code=401,
                detail="用户名或密码错误",
                headers={"Cache-Control": "no-store"},
            )
        login_limiter.reset(attempt_key)
        token, _ = service.create_session(user["id"])
        _set_session_cookie(response, token)
        authenticated = service.authenticate_token(token)
        context = service.context(authenticated) if authenticated else {}
        service.audit(
            request,
            actor_user_id=user["id"],
            project_id=(context.get("current_project") or {}).get("id"),
            action="identity.login",
            target_type="user",
            target_id=user["id"],
        )
        return context

    @router.post("/auth/logout")
    async def logout(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        authenticated = service.authenticate_token(request.cookies.get(SESSION_COOKIE, ""))
        if authenticated:
            service.store.delete_auth_session(authenticated.token_hash)
            service.audit(
                request,
                actor_user_id=authenticated.session["user_id"],
                project_id=None,
                action="identity.logout",
                target_type="user",
                target_id=authenticated.session["user_id"],
            )
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"success": True}

    @router.get("/identity/roles")
    async def list_roles(request: Request):
        _request_context(request)
        return {
            "roles": [
                {"key": key, "label": value["label"], "permissions": sorted(value["permissions"])}
                for key, value in PROJECT_ROLES.items()
            ]
        }

    @router.get("/identity/users")
    async def list_users(request: Request):
        _require_superuser(request)
        return {"users": store.list_users()}

    @router.post("/identity/users", status_code=201)
    async def create_user(payload: UserInput, request: Request):
        context = _require_superuser(request)
        try:
            user = service.create_user(**payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=409, detail="用户名已存在") from exc
        service.audit(
            request,
            actor_user_id=context["user"]["id"],
            project_id=(context.get("current_project") or {}).get("id"),
            action="identity.user.create",
            target_type="user",
            target_id=user["id"],
            detail={"username": user["username"], "is_superuser": user["is_superuser"]},
        )
        return user

    @router.patch("/identity/users/{user_id}")
    async def update_user(user_id: str, payload: UserUpdateInput, request: Request):
        context = _require_superuser(request)
        current = store.get_user(user_id)
        if not current:
            raise HTTPException(status_code=404, detail="用户不存在")
        changes = payload.model_dump(exclude_none=True)
        if changes.get("password"):
            try:
                changes["password_hash"] = hash_password(changes.pop("password"))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        if current["is_superuser"] and (changes.get("is_active") is False or changes.get("is_superuser") is False):
            if store.count_active_superusers() <= 1:
                raise HTTPException(status_code=409, detail="不能停用或降级最后一位平台管理员")
        try:
            user = store.update_user(user_id, **changes)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="用户不存在") from exc
        service.audit(
            request,
            actor_user_id=context["user"]["id"],
            project_id=(context.get("current_project") or {}).get("id"),
            action="identity.user.update",
            target_type="user",
            target_id=user_id,
            detail={key: value for key, value in changes.items() if key != "password_hash"},
        )
        return user

    @router.get("/identity/projects")
    async def list_projects(request: Request):
        context = _request_context(request)
        projects = store.list_projects() if context["user"]["is_superuser"] else context["projects"]
        return {"projects": projects}

    @router.post("/identity/projects", status_code=201)
    async def create_project(payload: ProjectInput, request: Request):
        context = _require_superuser(request)
        try:
            project = store.create_project(
                normalize_project_key(payload.project_key), payload.name.strip(), payload.description.strip()
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=409, detail="项目标识已存在") from exc
        store.set_project_membership(project["id"], context["user"]["id"], "project_admin")
        service.audit(
            request,
            actor_user_id=context["user"]["id"],
            project_id=project["id"],
            action="identity.project.create",
            target_type="project",
            target_id=project["id"],
            detail={"project_key": project["project_key"]},
        )
        return project

    @router.patch("/identity/projects/{project_id}")
    async def update_project(project_id: str, payload: ProjectUpdateInput, request: Request):
        context = _require_project_admin(request, project_id)
        changes = payload.model_dump(exclude_none=True)
        if "is_active" in changes and not context["user"]["is_superuser"]:
            raise HTTPException(status_code=403, detail="只有平台管理员可以停用项目")
        try:
            project = store.update_project(project_id, **changes)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="项目不存在") from exc
        service.audit(
            request,
            actor_user_id=context["user"]["id"],
            project_id=project_id,
            action="identity.project.update",
            target_type="project",
            target_id=project_id,
            detail=changes,
        )
        return project

    @router.get("/identity/projects/{project_id}/members")
    async def list_members(project_id: str, request: Request):
        _require_project_admin(request, project_id)
        if not store.get_project(project_id):
            raise HTTPException(status_code=404, detail="项目不存在")
        return {"members": store.list_project_members(project_id)}

    @router.put("/identity/projects/{project_id}/members")
    async def set_membership(project_id: str, payload: MembershipInput, request: Request):
        context = _require_project_admin(request, project_id)
        if not store.get_project(project_id):
            raise HTTPException(status_code=404, detail="项目不存在")
        if not store.get_user(payload.user_id):
            raise HTTPException(status_code=404, detail="用户不存在")
        if payload.user_id == context["user"]["id"] and payload.role is None and not context["user"]["is_superuser"]:
            raise HTTPException(status_code=409, detail="项目管理员不能移除自己的项目成员关系")
        store.set_project_membership(project_id, payload.user_id, payload.role)
        service.audit(
            request,
            actor_user_id=context["user"]["id"],
            project_id=project_id,
            action="identity.membership.update",
            target_type="user",
            target_id=payload.user_id,
            detail={"role": payload.role or "removed"},
        )
        return {"success": True, "members": store.list_project_members(project_id)}

    @router.get("/identity/audit-events")
    async def list_audit_events(
        request: Request,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=10, le=100),
        limit: int | None = Query(default=None, ge=1, le=1000),
    ):
        _require_superuser(request)
        if limit is not None:
            page = 1
            page_size = limit
        total = store.count_audit_events()
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        return {
            "events": store.list_audit_events(page_size, (page - 1) * page_size),
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        }

    return router, service


PUBLIC_PATHS = {
    "/health",
    "/api/auth/status",
    "/api/auth/setup",
    "/api/auth/login",
}


def _required_permission(method: str, path: str) -> str:
    if method in {"GET", "HEAD", "OPTIONS"} and path.startswith(
        "/api/model-evaluation/"
    ):
        return "evaluation:view"
    if method in {"GET", "HEAD", "OPTIONS"}:
        return "project:view"
    if re.fullmatch(r"/api/runs/[^/]+/reports", path):
        return "report:manage"
    if path == "/api/interface-scenarios/batch-execute" or re.fullmatch(
        r"/api/interface-scenarios/[^/]+/execute", path
    ):
        return "test:execute"
    if path.startswith("/api/model-evaluation/runs"):
        return "evaluation:operate"
    if path.startswith("/api/model-evaluation/suites"):
        return "evaluation:manage"
    project_interface_assets = (
        "/api/interface-assets",
        "/api/interface-modules",
        "/api/interface-environments",
        "/api/interface-variables",
        "/api/interface-scenarios",
        "/api/interface-specs/import",
    )
    if any(path == prefix or path.startswith(prefix + "/") for prefix in project_interface_assets):
        return "interface:manage"
    platform_managed = (
        "/api/model-profiles",
        "/api/report-template",
        "/api/server-profiles",
    )
    if any(path == prefix or path.startswith(prefix + "/") for prefix in platform_managed):
        return "platform:manage"
    server_operations = (
        "/api/server-sessions",
        "/api/server-capabilities/probe",
        "/api/stress-jobs",
    )
    if any(path == prefix or path.startswith(prefix + "/") for prefix in server_operations):
        return "server:operate"
    return "test:execute"


def install_identity_guard(app, service: IdentityService) -> None:
    """Protect application APIs while leaving the login shell and assets reachable."""

    @app.middleware("http")
    async def identity_guard(request: Request, call_next):
        path = request.url.path
        if path == "/" or path.startswith("/static/") or path in PUBLIC_PATHS:
            return await call_next(request)

        if not (path.startswith("/api/") or path in {"/docs", "/redoc", "/openapi.json"}):
            return await call_next(request)

        authenticated = service.authenticate_token(request.cookies.get(SESSION_COOKIE, ""))
        if not authenticated:
            detail = "请先完成管理员初始化" if service.setup_required else "登录已失效，请重新登录"
            return JSONResponse({"detail": detail}, status_code=401)
        try:
            context = service.context(authenticated, request.headers.get("X-Project-ID", ""))
        except PermissionError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=403)
        request.state.identity = context

        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            provided = request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(provided, str(context.get("csrf_token") or "")):
                return JSONResponse({"detail": "安全令牌失效，请刷新页面后重试"}, status_code=403)

        if path.startswith("/api/identity/") or path == "/api/auth/logout":
            return await call_next(request)

        permission = _required_permission(request.method, path)
        if not service.has_permission(context, permission):
            return JSONResponse({"detail": "当前项目角色没有执行此操作的权限"}, status_code=403)
        if not context.get("current_project"):
            return JSONResponse({"detail": "当前账号尚未加入可用项目"}, status_code=403)

        response = await call_next(request)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            try:
                service.audit(
                    request,
                    actor_user_id=context["user"]["id"],
                    project_id=context["current_project"]["id"],
                    action=f"api.{request.method.lower()}",
                    target_type="route",
                    target_id=path[:160],
                    outcome="success" if response.status_code < 400 else "failed",
                    detail={"status_code": response.status_code},
                )
            except Exception:
                # Audit persistence must not turn a completed business request into a 500.
                pass
        return response
