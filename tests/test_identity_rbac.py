import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests import bootstrap  # noqa: F401

from auto_test.platform.identity import (
    _set_session_cookie,
    IdentityService,
    _required_permission,
    create_identity_api,
    hash_password,
    install_identity_guard,
    verify_password,
)
from auto_test.platform.store import PlatformStore


ADMIN_PASSWORD = "StrongAdmin!2026"
USER_PASSWORD = "StrongTester!2026"


class IdentityStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = PlatformStore(
            Path(self.temp_dir.name) / "platform.db", recover_jobs=False
        )
        self.service = IdentityService(self.store)

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_admin(self):
        return self.service.create_initial_admin(
            username="admin.liema",
            display_name="平台管理员",
            password=ADMIN_PASSWORD,
            project_key="LIEMA",
            project_name="烈马测试项目",
        )

    def test_password_hash_is_salted_and_verifiable(self):
        first = hash_password(ADMIN_PASSWORD)
        second = hash_password(ADMIN_PASSWORD)

        self.assertNotEqual(first, second)
        self.assertTrue(verify_password(ADMIN_PASSWORD, first))
        self.assertFalse(verify_password("WrongPassword!2026", first))
        self.assertNotIn(ADMIN_PASSWORD, first)

    def test_weak_password_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "至少需要 12"):
            hash_password("short")
        with self.assertRaisesRegex(ValueError, "至少三类"):
            hash_password("onlylowercaseletters")

    def test_initial_admin_receives_project_admin_context(self):
        user, project = self.create_admin()
        token, _ = self.service.create_session(user["id"])
        authenticated = self.service.authenticate_token(token)

        context = self.service.context(authenticated, project["id"])

        self.assertTrue(context["user"]["is_superuser"])
        self.assertEqual(context["project_role"], "project_admin")
        self.assertIn("platform:manage", context["permissions"])
        self.assertIn("test:execute", context["permissions"])
        self.assertEqual(context["legacy_project_id"], project["id"])

    def test_initial_project_remains_the_legacy_data_home_after_new_project_creation(self):
        user, initial_project = self.create_admin()
        newer_project = self.store.create_project("NEWER", "新项目")
        token, _ = self.service.create_session(user["id"])

        context = self.service.context(
            self.service.authenticate_token(token), newer_project["id"]
        )

        self.assertEqual(context["current_project"]["id"], newer_project["id"])
        self.assertEqual(context["legacy_project_id"], initial_project["id"])

    def test_project_role_controls_permissions(self):
        _, project = self.create_admin()
        user = self.service.create_user(
            username="viewer.user",
            display_name="只读用户",
            password=USER_PASSWORD,
        )
        self.store.set_project_membership(project["id"], user["id"], "viewer")
        token, _ = self.service.create_session(user["id"])

        viewer = self.service.context(self.service.authenticate_token(token), project["id"])
        self.assertIn("project:view", viewer["permissions"])
        self.assertNotIn("test:execute", viewer["permissions"])

        self.store.set_project_membership(project["id"], user["id"], "tester")
        tester = self.service.context(self.service.authenticate_token(token), project["id"])
        self.assertIn("test:execute", tester["permissions"])
        self.assertNotIn("project:members", tester["permissions"])

        self.store.set_project_membership(project["id"], user["id"], "project_admin")
        project_admin = self.service.context(
            self.service.authenticate_token(token), project["id"]
        )
        self.assertIn("interface:manage", project_admin["permissions"])

    def test_user_cannot_select_an_unassigned_project(self):
        _, project = self.create_admin()
        other = self.store.create_project("OTHER", "其他项目")
        user = self.service.create_user(
            username="tester.user",
            display_name="测试用户",
            password=USER_PASSWORD,
        )
        self.store.set_project_membership(project["id"], user["id"], "tester")
        token, _ = self.service.create_session(user["id"])

        with self.assertRaisesRegex(PermissionError, "无权访问"):
            self.service.context(self.service.authenticate_token(token), other["id"])

    def test_disabling_user_revokes_existing_sessions(self):
        self.create_admin()
        user = self.service.create_user(
            username="disabled.user",
            display_name="待停用用户",
            password=USER_PASSWORD,
        )
        token, _ = self.service.create_session(user["id"])
        self.assertIsNotNone(self.service.authenticate_token(token))

        self.store.update_user(user["id"], is_active=False)

        self.assertIsNone(self.service.authenticate_token(token))

    def test_password_change_revokes_existing_sessions(self):
        self.create_admin()
        user = self.service.create_user(
            username="reset.user",
            display_name="重置密码用户",
            password=USER_PASSWORD,
        )
        token, _ = self.service.create_session(user["id"])
        self.assertIsNotNone(self.service.authenticate_token(token))

        self.store.update_user(
            user["id"], password_hash=hash_password("NewStrongPassword!2026")
        )

        self.assertIsNone(self.service.authenticate_token(token))

    def test_write_routes_use_their_declared_capability_permissions(self):
        self.assertEqual(
            _required_permission("POST", "/api/runs/run-1/reports"),
            "report:manage",
        )
        self.assertEqual(
            _required_permission("POST", "/api/server-sessions"),
            "server:operate",
        )
        self.assertEqual(
            _required_permission("POST", "/api/stress-jobs/job-1/stop"),
            "server:operate",
        )
        self.assertEqual(
            _required_permission("POST", "/api/interface-assets"),
            "interface:manage",
        )
        self.assertEqual(
            _required_permission("POST", "/api/model-profiles"),
            "platform:manage",
        )
        self.assertEqual(
            _required_permission("DELETE", "/api/model-evaluation/runs/run-1"),
            "evaluation:manage",
        )
        self.assertEqual(
            _required_permission("POST", "/api/model-evaluation/suites"),
            "evaluation:manage",
        )
        self.assertEqual(
            _required_permission("POST", "/api/model-evaluation/comparisons"),
            "evaluation:operate",
        )
        self.assertEqual(
            _required_permission(
                "POST", "/api/model-evaluation/runs/run-1/results/result-1/reviews"
            ),
            "evaluation:manage",
        )


class IdentityApiTests(unittest.TestCase):
    def test_session_cookie_secure_flag_is_explicit_for_lan_http_or_https(self):
        from fastapi import Response

        lan_response = Response()
        with patch.dict(os.environ, {"LIEMA_SESSION_COOKIE_SECURE": "false"}):
            _set_session_cookie(lan_response, "lan-token")
        self.assertNotIn("; Secure", lan_response.headers["set-cookie"])
        self.assertIn("HttpOnly", lan_response.headers["set-cookie"])

        https_response = Response()
        with patch.dict(os.environ, {"LIEMA_SESSION_COOKIE_SECURE": "true"}):
            _set_session_cookie(https_response, "https-token")
        self.assertIn("; Secure", https_response.headers["set-cookie"])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = PlatformStore(
            Path(self.temp_dir.name) / "platform.db", recover_jobs=False
        )
        self.app = FastAPI()
        router, self.service = create_identity_api(self.store)
        self.app.include_router(router)

        @self.app.get("/health")
        async def health():
            return {"status": "ok"}

        @self.app.get("/api/protected")
        async def protected_get():
            return {"success": True}

        @self.app.post("/api/protected")
        async def protected_post():
            return {"success": True}

        install_identity_guard(self.app, self.service)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.temp_dir.cleanup()

    def setup_admin(self):
        response = self.client.post(
            "/api/auth/setup",
            json={
                "username": "admin.liema",
                "display_name": "平台管理员",
                "password": ADMIN_PASSWORD,
                "project_key": "LIEMA",
                "project_name": "烈马测试项目",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_first_run_setup_and_csrf_guard(self):
        self.assertTrue(self.client.get("/api/auth/status").json()["setup_required"])
        self.assertEqual(self.client.get("/api/protected").status_code, 401)

        identity = self.setup_admin()

        self.assertEqual(self.client.get("/api/protected").status_code, 200)
        self.assertEqual(self.client.post("/api/protected").status_code, 403)
        allowed = self.client.post(
            "/api/protected",
            headers={"X-CSRF-Token": identity["csrf_token"]},
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertIn("liema_session", self.client.cookies)

        project_id = identity["current_project"]["id"]
        renamed = self.client.patch(
            f"/api/identity/projects/{project_id}",
            headers={"X-CSRF-Token": identity["csrf_token"]},
            json={"name": "翻译服务回归", "description": "业务项目而非平台品牌"},
        )
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["project_key"], "LIEMA")
        self.assertEqual(renamed.json()["name"], "翻译服务回归")
        self.assertEqual(
            self.client.get("/api/auth/status").json()["current_project"]["name"],
            "翻译服务回归",
        )

    def test_viewer_can_read_but_cannot_execute(self):
        admin_identity = self.setup_admin()
        project_id = admin_identity["current_project"]["id"]
        user = self.service.create_user(
            username="viewer.user",
            display_name="只读用户",
            password=USER_PASSWORD,
        )
        self.store.set_project_membership(project_id, user["id"], "viewer")
        self.client.cookies.clear()

        login = self.client.post(
            "/api/auth/login",
            json={"username": "viewer.user", "password": USER_PASSWORD},
        )
        self.assertEqual(login.status_code, 200)
        identity = login.json()

        self.assertEqual(self.client.get("/api/protected").status_code, 200)
        denied = self.client.post(
            "/api/protected", headers={"X-CSRF-Token": identity["csrf_token"]}
        )
        self.assertEqual(denied.status_code, 403)

    def test_failed_login_is_audited_without_password(self):
        self.setup_admin()
        self.client.cookies.clear()

        response = self.client.post(
            "/api/auth/login",
            json={"username": "admin.liema", "password": "not-the-password"},
        )

        self.assertEqual(response.status_code, 401)
        events = self.store.list_audit_events()
        self.assertEqual(events[0]["action"], "identity.login")
        self.assertEqual(events[0]["outcome"], "denied")
        self.assertNotIn("password", events[0]["detail"])

    def test_audit_events_are_returned_with_server_side_pagination(self):
        identity = self.setup_admin()
        for index in range(25):
            self.store.add_audit_event(
                actor_user_id=identity["user"]["id"],
                project_id=identity["current_project"]["id"],
                action=f"test.audit.{index}",
            )

        response = self.client.get(
            "/api/identity/audit-events?page=2&page_size=2"
        )

        self.assertEqual(response.status_code, 422)
        response = self.client.get(
            "/api/identity/audit-events?page=2&page_size=10"
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        expected = self.store.list_audit_events(10, 10)
        self.assertEqual(payload["events"], expected)
        self.assertEqual(payload["page"], 2)
        self.assertEqual(payload["page_size"], 10)
        self.assertEqual(payload["total"], self.store.count_audit_events())
        self.assertEqual(payload["total_pages"], 3)

    def test_repeated_login_failures_are_rate_limited_without_account_disclosure(self):
        self.setup_admin()
        self.client.cookies.clear()

        for _ in range(5):
            response = self.client.post(
                "/api/auth/login",
                json={"username": "unknown.user", "password": "wrong-password"},
            )
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers["Cache-Control"], "no-store")

        blocked = self.client.post(
            "/api/auth/login",
            json={"username": "unknown.user", "password": "wrong-password"},
        )

        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.headers["Cache-Control"], "no-store")
        self.assertGreaterEqual(int(blocked.headers["Retry-After"]), 1)
        event = self.store.list_audit_events()[0]
        self.assertEqual(event["detail"]["reason"], "rate_limited")
        self.assertNotIn("password", event["detail"])

    def test_authentication_responses_disable_caching(self):
        status = self.client.get("/api/auth/status")
        self.assertEqual(status.headers["Cache-Control"], "no-store")

        identity = self.setup_admin()
        self.assertTrue(identity["authenticated"])
        status = self.client.get("/api/auth/status")
        self.assertEqual(status.headers["Cache-Control"], "no-store")

    def test_last_platform_admin_cannot_be_disabled(self):
        identity = self.setup_admin()
        user_id = identity["user"]["id"]

        response = self.client.patch(
            f"/api/identity/users/{user_id}",
            headers={"X-CSRF-Token": identity["csrf_token"]},
            json={"is_active": False},
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("最后一位平台管理员", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
