import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests import bootstrap  # noqa: F401

from auto_test.platform.api import create_platform_api
from auto_test.platform.artifact_storage import LocalArtifactStorage
from auto_test.platform.identity import create_identity_api, install_identity_guard
from auto_test.platform.store import PlatformStore
from auto_test.platform.task_store import TaskStore


ADMIN_PASSWORD = "StrongAdmin!2026"
VIEWER_PASSWORD = "StrongViewer!2026"


class InterfaceAssetStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = PlatformStore(
            Path(self.temp_dir.name) / "platform.db", recover_jobs=False
        )
        self.project = self.store.create_project("INTERFACE", "接口资产项目")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_project_assets_are_versioned_and_isolated(self):
        module = self.store.save_interface_module(
            self.project["id"], {"name": "认证", "description": "登录与令牌"}
        )
        environment = self.store.save_interface_environment(
            self.project["id"],
            {"name": "测试环境", "base_url": "https://api.example.test", "is_default": True},
        )
        variable = self.store.save_interface_variable(
            self.project["id"],
            {
                "environment_id": environment["id"],
                "key": "ACCESS_TOKEN",
                "value": "must-not-leak",
                "is_secret": True,
            },
            secret_enc="encrypted-value",
        )

        self.assertTrue(variable["has_secret"])
        self.assertEqual(variable["value"], "")
        self.assertNotIn("secret_enc", variable)
        self.assertNotIn("must-not-leak", str(variable))

        first = self.store.save_interface_asset(
            self.project["id"],
            {
                "module_id": module["id"],
                "environment_id": environment["id"],
                "name": "用户登录",
                "method": "POST",
                "path": "/v1/login",
                "request": {"headers": {"Authorization": "Bearer {{ACCESS_TOKEN}}"}},
            },
        )
        second = self.store.save_interface_asset(
            self.project["id"],
            {
                "module_id": module["id"],
                "environment_id": environment["id"],
                "name": "用户登录",
                "method": "POST",
                "path": "/v2/login",
                "request": {"body": {"username": "demo"}},
            },
            first["id"],
        )
        published = self.store.publish_interface_asset(self.project["id"], first["id"])
        versions = self.store.list_interface_asset_versions(self.project["id"], first["id"])

        self.assertEqual(second["current_version"], 2)
        self.assertEqual(published["status"], "published")
        self.assertEqual([item["version"] for item in versions], [2, 1])
        self.assertEqual(versions[0]["status"], "published")
        self.assertEqual(versions[0]["definition"]["path"], "/v2/login")

        other = self.store.create_project("OTHER", "其他项目")
        self.assertEqual(self.store.list_interface_assets(other["id"]), [])
        self.assertIsNone(self.store.get_interface_asset(other["id"], first["id"]))

        with self.assertRaisesRegex(ValueError, "仍有接口"):
            self.store.delete_interface_module(self.project["id"], module["id"])
        with self.assertRaisesRegex(ValueError, "仍被接口引用"):
            self.store.delete_interface_environment(self.project["id"], environment["id"])


class InterfaceAssetApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.store = PlatformStore(root / "platform.db", recover_jobs=False)
        self.app = FastAPI()
        platform_router, _, _, _ = create_platform_api(
            TaskStore(root / "tasks.db"),
            platform_store=self.store,
            artifact_storage=LocalArtifactStorage(root / "artifacts"),
        )
        identity_router, self.identity_service = create_identity_api(self.store)
        self.app.include_router(platform_router)
        self.app.include_router(identity_router)
        install_identity_guard(self.app, self.identity_service)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.temp_dir.cleanup()

    def setup_admin(self):
        response = self.client.post(
            "/api/auth/setup",
            json={
                "username": "admin.interface",
                "display_name": "接口管理员",
                "password": ADMIN_PASSWORD,
                "project_key": "INTERFACE",
                "project_name": "接口资产项目",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_workspace_crud_encrypts_secret_and_writes_audit(self):
        identity = self.setup_admin()
        headers = {"X-CSRF-Token": identity["csrf_token"]}
        module = self.client.post(
            "/api/interface-modules", headers=headers, json={"name": "订单"}
        )
        self.assertEqual(module.status_code, 201, module.text)
        environment = self.client.post(
            "/api/interface-environments",
            headers=headers,
            json={"name": "联调", "base_url": "https://api.example.test", "is_default": True},
        )
        self.assertEqual(environment.status_code, 201, environment.text)

        with patch.dict(os.environ, {"LIEMA_MASTER_KEY": "interface-test-key"}, clear=False):
            variable = self.client.post(
                "/api/interface-variables",
                headers=headers,
                json={
                    "environment_id": environment.json()["id"],
                    "key": "CLIENT_SECRET",
                    "value": "top-secret-value",
                    "is_secret": True,
                },
            )
        self.assertEqual(variable.status_code, 201, variable.text)
        self.assertEqual(variable.json()["value"], "")
        self.assertTrue(variable.json()["has_secret"])
        self.assertNotIn("top-secret-value", variable.text)

        asset = self.client.post(
            "/api/interface-assets",
            headers=headers,
            json={
                "module_id": module.json()["id"],
                "environment_id": environment.json()["id"],
                "name": "创建订单",
                "method": "POST",
                "path": "/v1/orders",
                "default_path": "/legacy/orders",
                "request": {"headers": {"X-Client-Secret": "{{CLIENT_SECRET}}"}},
                "publish": True,
            },
        )
        self.assertEqual(asset.status_code, 201, asset.text)
        self.assertEqual(asset.json()["status"], "published")
        runtime_override = self.store.resolve_endpoint("/legacy/orders")
        self.assertEqual(runtime_override["host"], "api.example.test")
        self.assertEqual(runtime_override["path"], "/v1/orders")
        workspace = self.client.get("/api/interface-assets/workspace")
        self.assertEqual(workspace.status_code, 200, workspace.text)
        self.assertEqual(len(workspace.json()["assets"]), 1)
        self.assertNotIn("top-secret-value", workspace.text)

        events = self.store.list_audit_events()
        self.assertIn("interface.asset.create", {item["action"] for item in events})
        self.assertNotIn("top-secret-value", str(events))

    def test_viewer_can_read_workspace_but_cannot_edit(self):
        admin = self.setup_admin()
        project_id = admin["current_project"]["id"]
        viewer = self.identity_service.create_user(
            username="viewer.interface",
            display_name="只读接口用户",
            password=VIEWER_PASSWORD,
        )
        self.store.set_project_membership(project_id, viewer["id"], "viewer")
        self.client.cookies.clear()
        login = self.client.post(
            "/api/auth/login",
            json={"username": "viewer.interface", "password": VIEWER_PASSWORD},
        )
        self.assertEqual(login.status_code, 200, login.text)

        self.assertEqual(self.client.get("/api/interface-assets/workspace").status_code, 200)
        denied = self.client.post(
            "/api/interface-modules",
            headers={"X-CSRF-Token": login.json()["csrf_token"]},
            json={"name": "不允许创建"},
        )
        self.assertEqual(denied.status_code, 403)

    def test_manual_asset_accepts_an_independent_absolute_http_url(self):
        identity = self.setup_admin()
        headers = {"X-CSRF-Token": identity["csrf_token"]}

        asset = self.client.post(
            "/api/interface-assets",
            headers=headers,
            json={
                "name": "内网 LMT",
                "method": "POST",
                "path": "http://172.16.102.91:8036/v3/translate_a",
                "request": {
                    "headers": {"Content-Type": "application/json"},
                    "query": {},
                    "body": {"text": "hello"},
                },
            },
        )

        self.assertEqual(asset.status_code, 201, asset.text)
        self.assertEqual(
            asset.json()["path"],
            "http://172.16.102.91:8036/v3/translate_a",
        )
        self.assertEqual(asset.json()["environment_id"], "")
        self.assertEqual(asset.json()["default_path"], "")

        invalid = self.client.post(
            "/api/interface-assets",
            headers=headers,
            json={"name": "错误协议", "method": "GET", "path": "ftp://example.test/a"},
        )
        self.assertEqual(invalid.status_code, 400, invalid.text)
        self.assertIn("http://、https://", invalid.json()["detail"])

    def test_debug_request_resolves_environment_variables_and_returns_response_details(self):
        identity = self.setup_admin()
        headers = {"X-CSRF-Token": identity["csrf_token"]}
        environment = self.client.post(
            "/api/interface-environments",
            headers=headers,
            json={"name": "联调", "base_url": "https://api.example.test"},
        ).json()
        variable = self.client.post(
            "/api/interface-variables",
            headers=headers,
            json={
                "environment_id": environment["id"],
                "key": "ORDER_ID",
                "value": "42",
                "is_secret": False,
            },
        )
        self.assertEqual(variable.status_code, 201, variable.text)

        upstream = MagicMock()
        upstream.__enter__.return_value = upstream
        upstream.__exit__.return_value = False
        upstream.iter_content.return_value = [b'{"ok":true}']
        upstream.encoding = "utf-8"
        upstream.status_code = 201
        upstream.reason = "Created"
        upstream.headers = {"Content-Type": "application/json", "Set-Cookie": "hidden=1"}
        upstream.request = SimpleNamespace(
            url="https://api.example.test/orders/42?expand=true",
            headers={"Authorization": "Bearer secret", "Accept": "application/json"},
        )
        with patch("auto_test.platform.api.requests.request", return_value=upstream) as send:
            response = self.client.post(
                "/api/interface-debug",
                headers=headers,
                json={
                    "method": "POST",
                    "target": "/orders/{{ORDER_ID}}",
                    "environment_id": environment["id"],
                    "headers": {"Authorization": "Bearer secret"},
                    "query": {"expand": "true"},
                    "body": {"id": "{{ORDER_ID}}"},
                    "timeout_seconds": 5,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["response"]["status_code"], 201)
        self.assertEqual(result["response"]["body"], '{"ok":true}')
        self.assertEqual(result["response"]["headers"]["Set-Cookie"], "••••••")
        self.assertEqual(result["request"]["headers"]["Authorization"], "••••••")
        sent = send.call_args.kwargs
        self.assertEqual(sent["url"], "https://api.example.test/orders/42")
        self.assertEqual(sent["json"], {"id": "42"})
        self.assertFalse(sent["allow_redirects"])


if __name__ == "__main__":
    unittest.main()
