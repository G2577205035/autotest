import asyncio
from tests import bootstrap  # noqa: F401
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from starlette.datastructures import UploadFile

from auto_test.platform import api as platform_api
from auto_test.platform.artifact_storage import create_artifact_storage
from auto_test.platform.store import PlatformStore
from auto_test.platform.task_store import TaskStore
from auto_test.platform.upload_ownership import read_upload_owner


class PlatformUploadTests(unittest.TestCase):
    def test_directory_upload_accepts_more_than_starlette_default_file_limit(self):
        class FormDataStub:
            def __init__(self, files):
                self.files = files

            def getlist(self, name):
                return self.files if name == "files" else []

        class RequestStub:
            def __init__(self, files):
                self.files = files
                self.max_files = None
                self.state = SimpleNamespace(
                    identity={
                        "user": {"id": "user-a"},
                        "current_project": {"id": "project-a"},
                    }
                )

            async def form(self, *, max_files):
                self.max_files = max_files
                return FormDataStub(self.files)

        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            uploads_dir = base_dir / "runtime" / "uploads"
            store = TaskStore(base_dir / "tasks.db")
            platform_store = PlatformStore(base_dir / "platform.db", recover_jobs=False)
            artifact_storage = create_artifact_storage(
                base_dir, {"backend": "local", "root": "artifacts"}
            )

            with (
                patch.object(platform_api, "BASE_DIR", base_dir),
                patch.object(platform_api, "UPLOADS_DIR", uploads_dir),
                patch.dict(os.environ, {"LIEMA_UPLOAD_MAX_FILES": "20000"}),
            ):
                router, _, _, _ = platform_api.create_platform_api(
                    store,
                    platform_store=platform_store,
                    artifact_storage=artifact_storage,
                )
                files = [
                    UploadFile(BytesIO(b"x"), filename=f"large-folder/file-{index:04d}.txt")
                    for index in range(1001)
                ]
                request = RequestStub(files)
                endpoint = next(
                    route.endpoint for route in router.routes if route.path == "/api/uploads"
                )
                payload = asyncio.run(endpoint(request))

            self.assertEqual(request.max_files, 20000)
            self.assertEqual(payload["file_count"], 1001)
            self.assertTrue(
                (uploads_dir / payload["upload_id"] / "large-folder" / "file-1000.txt").is_file()
            )
            self.assertEqual(
                read_upload_owner(uploads_dir, payload["upload_id"]),
                {
                    "version": 1,
                    "project_id": "project-a",
                    "created_by_user_id": "user-a",
                },
            )


class PlatformProjectIsolationTests(unittest.TestCase):
    @staticmethod
    def request(
        project_id: str, *, is_superuser: bool = False, legacy_project_id: str = ""
    ):
        return SimpleNamespace(
            state=SimpleNamespace(
                identity={
                    "user": {"id": "user-a", "is_superuser": is_superuser},
                    "current_project": {"id": project_id},
                    "legacy_project_id": legacy_project_id,
                }
            )
        )

    def test_server_session_routes_hide_other_projects_and_tag_new_sessions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            task_store = TaskStore(base_dir / "tasks.db")
            platform_store = PlatformStore(base_dir / "platform.db", recover_jobs=False)
            artifact_storage = create_artifact_storage(
                base_dir, {"backend": "local", "root": "artifacts"}
            )
            with patch.object(platform_api, "BASE_DIR", base_dir):
                router, _, _, stress_manager = platform_api.create_platform_api(
                    task_store,
                    platform_store=platform_store,
                    artifact_storage=artifact_storage,
                )
            manager = stress_manager.session_manager
            list_endpoint = next(
                route.endpoint
                for route in router.routes
                if route.path == "/api/server-sessions" and "GET" in route.methods
            )
            get_endpoint = next(
                route.endpoint
                for route in router.routes
                if route.path == "/api/server-sessions/{session_id}" and "GET" in route.methods
            )
            connect_endpoint = next(
                route.endpoint
                for route in router.routes
                if route.path == "/api/server-sessions" and "POST" in route.methods
            )
            sessions = [
                {"id": "a", "project_id": "project-a"},
                {"id": "b", "project_id": "project-b"},
                {"id": "legacy", "project_id": ""},
            ]

            with patch.object(manager, "list_sessions", return_value=sessions):
                viewer = asyncio.run(list_endpoint(self.request("project-a")))
                admin = asyncio.run(
                    list_endpoint(self.request("project-a", is_superuser=True))
                )
                new_project_admin = asyncio.run(
                    list_endpoint(
                        self.request(
                            "project-b",
                            is_superuser=True,
                            legacy_project_id="project-a",
                        )
                    )
                )

            self.assertEqual([item["id"] for item in viewer["sessions"]], ["a"])
            self.assertEqual(
                [item["id"] for item in admin["sessions"]], ["a", "legacy"]
            )
            self.assertEqual(
                [item["id"] for item in new_project_admin["sessions"]], ["b"]
            )

            with patch.object(
                manager, "get", return_value={"id": "b", "project_id": "project-b"}
            ), self.assertRaises(platform_api.HTTPException) as raised:
                asyncio.run(get_endpoint("b", self.request("project-a")))
            self.assertEqual(raised.exception.status_code, 404)

            payload = platform_api.ServerSessionInput(
                host="127.0.0.1", user="tester", password="temporary"
            )
            with patch.object(manager, "connect", side_effect=lambda data: data):
                created = asyncio.run(
                    connect_endpoint(payload, self.request("project-a"))
                )
            self.assertEqual(created["_project_id"], "project-a")
            self.assertEqual(created["_created_by_user_id"], "user-a")


if __name__ == "__main__":
    unittest.main()
