import asyncio
import json
from tests import bootstrap  # noqa: F401
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

_WEB_RUNTIME = tempfile.TemporaryDirectory()
with patch.dict(
    os.environ,
    {
        "LIEMA_DATABASE_BACKEND": "sqlite",
        "LIEMA_SQLITE_TASKS_PATH": str(Path(_WEB_RUNTIME.name) / "tasks.db"),
        "LIEMA_SQLITE_PLATFORM_PATH": str(Path(_WEB_RUNTIME.name) / "platform.db"),
        "LIEMA_ARTIFACT_BACKEND": "local",
        "LIEMA_TASK_QUEUE_BACKEND": "local",
        "LIEMA_TASK_EXECUTION_MODE": "external",
    },
):
    from auto_test import web
from auto_test.platform.secrets import decrypt_secret
from auto_test.platform.upload_ownership import write_upload_owner


class WebDirectUploadTests(unittest.TestCase):
    def setUp(self):
        self.master_key = patch.dict(
            os.environ, {"LIEMA_MASTER_KEY": "web-unit-test-master-key"}
        )
        self.master_key.start()

    def tearDown(self):
        self.master_key.stop()

    @staticmethod
    def identity_request(
        project_id: str = "project-a", *, is_superuser: bool = False,
        legacy_project_id: str = "",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            state=SimpleNamespace(
                identity={
                    "user": {"id": "user-a", "is_superuser": is_superuser},
                    "current_project": {"id": project_id},
                    "legacy_project_id": legacy_project_id,
                }
            )
        )

    def test_legacy_web_launcher_imports_without_retired_page_key(self):
        import web_main

        source = (Path(__file__).resolve().parents[1] / "web_main.py").read_text(encoding="utf-8")
        self.assertIs(web_main.app, web.app)
        self.assertNotIn("_LOCAL_DEV_API_KEY", source)
        self.assertNotIn("页面连接 Key：", source)

    def test_api_routers_have_no_page_key_dependency(self):
        self.assertEqual(web.api.dependencies, [])
        self.assertEqual(web.platform_api.dependencies, [])

    def test_server_mode_queues_project_owned_browser_upload(self):
        upload_id = "b" * 32
        with tempfile.TemporaryDirectory() as temp_dir:
            upload_root = Path(temp_dir)
            uploaded_dir = upload_root / upload_id
            uploaded_dir.mkdir()
            (uploaded_dir / "sample.txt").write_text("test", encoding="utf-8")
            write_upload_owner(
                upload_root,
                upload_id,
                project_id="project-a",
                user_id="user-a",
            )
            request = web.RunRequest(
                username="tester",
                password="business-secret",
                upload_mode="server",
                upload_id=upload_id,
            )
            task = {"id": "run-1", "status": "queued"}
            with (
                patch.object(web, "UPLOADS_DIR", upload_root),
                patch.object(web.manager, "submit", return_value=task) as submit,
                patch.object(web, "default_translate_name", return_value="es:zh-CHS"),
            ):
                response = web._submit_run(request, self.identity_request())

        submitted = submit.call_args.args[0]
        self.assertEqual(submitted["options"]["upload_path"], str(uploaded_dir.resolve()))
        self.assertEqual(submitted["options"]["translate_name"], "es:zh-CHS")
        self.assertEqual(submitted["options"]["upload_id"], upload_id)
        self.assertNotIn("password", submitted["options"])
        encrypted = submit.call_args.kwargs["secret_enc"]
        self.assertEqual(json.loads(decrypt_secret(encrypted))["password"], "business-secret")
        self.assertEqual(response["run_id"], "run-1")

    def test_run_is_rejected_before_queueing_when_no_translation_language_exists(self):
        request = web.RunRequest(username="tester", password="business-secret")

        with patch.object(web, "default_translate_name", return_value=""), patch.object(
            web.manager, "submit"
        ) as submit, self.assertRaises(HTTPException) as raised:
            asyncio.run(web.run_test(request))

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("翻译语种", raised.exception.detail)
        submit.assert_not_called()

    def test_server_mode_rejects_platform_local_path_without_browser_upload(self):
        local_path = str(Path(tempfile.gettempdir()).resolve())
        request = web.RunRequest(
            username="tester",
            password="business-secret",
            upload_mode="server",
            upload_path=local_path,
        )

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(web.run_test(request))

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("不能引用平台服务器本地路径", raised.exception.detail)

    def test_server_asset_credentials_are_resolved_into_encrypted_task_secret(self):
        request = web.RunRequest(
            username="tester",
            password="business-secret",
            server_profile_id="server-1",
        )
        profile = {
            "id": "server-1",
            "host": "192.0.2.10",
            "user": "deploy",
            "credential_enc": web.encrypt_secret("saved-ssh-secret"),
        }
        task = {"id": "run-2", "status": "queued"}
        with (
            patch.object(web.platform_store, "get_server_profile", return_value=profile),
            patch.object(web.manager, "submit", return_value=task) as submit,
            patch.object(web, "default_translate_name", return_value="es:zh-CHS"),
        ):
            asyncio.run(web.run_test(request))

        submitted = submit.call_args.args[0]
        self.assertEqual(submitted["options"]["host"], "192.0.2.10")
        self.assertEqual(submitted["options"]["ssh_user"], "deploy")
        self.assertNotIn("ssh_password", submitted["options"])
        secrets = json.loads(decrypt_secret(submit.call_args.kwargs["secret_enc"]))
        self.assertEqual(secrets["ssh_password"], "saved-ssh-secret")
        self.assertEqual(secrets["password"], "business-secret")

    def test_legacy_artifact_timestamps_are_visible_only_to_platform_admins(self):
        viewer = self.identity_request(is_superuser=False)
        admin = self.identity_request(is_superuser=True)

        with self.assertRaises(HTTPException) as raised:
            web._authorize_run_artifact_identifier("20260821_120000", viewer)

        self.assertEqual(raised.exception.status_code, 404)
        web._authorize_run_artifact_identifier("20260821_120000", admin)

        new_project_admin = self.identity_request(
            "project-b", is_superuser=True, legacy_project_id="project-a"
        )
        with self.assertRaises(HTTPException) as raised:
            web._authorize_run_artifact_identifier(
                "20260821_120000", new_project_admin
            )
        self.assertEqual(raised.exception.status_code, 404)

    def test_unscoped_runs_are_hidden_when_admin_switches_to_a_new_project(self):
        legacy_run = {"metadata": {}}
        initial_project = self.identity_request(
            "project-a", is_superuser=True, legacy_project_id="project-a"
        )
        new_project = self.identity_request(
            "project-b", is_superuser=True, legacy_project_id="project-a"
        )

        self.assertTrue(web._run_visible(legacy_run, initial_project))
        self.assertFalse(web._run_visible(legacy_run, new_project))

    def test_legacy_artifact_directory_listing_is_hidden_from_project_members(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            legacy = Path(temp_dir) / "20260821_120000" / "translate" / "report"
            legacy.mkdir(parents=True)
            with patch.object(web, "RUNS_DIR", Path(temp_dir)):
                viewer_result = asyncio.run(
                    web.list_dirs(self.identity_request(is_superuser=False))
                )
                admin_result = asyncio.run(
                    web.list_dirs(self.identity_request(is_superuser=True))
                )

        self.assertEqual(viewer_result, [])
        self.assertEqual(admin_result[0]["ts"], "20260821_120000")

    def test_uploaded_files_cannot_be_reused_from_another_project(self):
        upload_id = "a" * 32
        request = web.RunRequest(
            username="tester",
            password="business-secret",
            upload_id=upload_id,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            upload_root = Path(temp_dir)
            (upload_root / upload_id).mkdir()
            write_upload_owner(
                upload_root,
                upload_id,
                project_id="project-a",
                user_id="user-a",
            )
            with (
                patch.object(web, "UPLOADS_DIR", upload_root),
                patch.object(web.manager, "submit") as submit,
                patch.object(web, "default_translate_name", return_value="es:zh-CHS"),
                self.assertRaises(HTTPException) as raised,
            ):
                web._submit_run(request, self.identity_request("project-b"))

        self.assertEqual(raised.exception.status_code, 404)
        submit.assert_not_called()


def tearDownModule():
    _WEB_RUNTIME.cleanup()


if __name__ == "__main__":
    unittest.main()
