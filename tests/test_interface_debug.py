import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from starlette.requests import Request

from tests import bootstrap  # noqa: F401

from auto_test.platform.api import InterfaceDebugInput, create_platform_api
from auto_test.platform.artifact_storage import LocalArtifactStorage
from auto_test.platform.store import PlatformStore
from auto_test.platform.task_store import TaskStore


class InterfaceDebugEndpointTests(unittest.TestCase):
    def test_executes_resolved_request_and_redacts_sensitive_result_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = PlatformStore(root / "platform.db", recover_jobs=False)
            project = store.create_project("DEBUG", "接口调试")
            environment = store.save_interface_environment(
                project["id"],
                {"name": "联调", "base_url": "https://api.example.test"},
            )
            store.save_interface_variable(
                project["id"],
                {
                    "environment_id": environment["id"],
                    "key": "ORDER_ID",
                    "value": "42",
                    "is_secret": False,
                },
            )
            router, _, _, _ = create_platform_api(
                TaskStore(root / "tasks.db"),
                platform_store=store,
                artifact_storage=LocalArtifactStorage(root / "artifacts"),
            )
            endpoint = next(
                route.endpoint for route in router.routes if route.path == "/api/interface-debug"
            )
            request = Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/interface-debug",
                    "headers": [],
                    "client": ("127.0.0.1", 12345),
                    "state": {
                        "identity": {
                            "user": {"id": "tester"},
                            "current_project": {"id": project["id"]},
                        }
                    },
                }
            )
            upstream = MagicMock()
            upstream.__enter__.return_value = upstream
            upstream.__exit__.return_value = False
            upstream.iter_content.return_value = [b'{"ok":true}']
            upstream.encoding = "utf-8"
            upstream.status_code = 201
            upstream.reason = "Created"
            upstream.headers = {
                "Content-Type": "application/json",
                "Set-Cookie": "session=hidden",
            }
            upstream.request = SimpleNamespace(
                url="https://api.example.test/orders/42?expand=true",
                headers={"Authorization": "Bearer secret", "Accept": "application/json"},
            )
            payload = InterfaceDebugInput(
                method="POST",
                target="/orders/{{ORDER_ID}}",
                environment_id=environment["id"],
                headers={"Authorization": "Bearer secret"},
                query={"expand": "true"},
                body={"id": "{{ORDER_ID}}"},
                timeout_seconds=5,
            )

            with patch("auto_test.platform.api.requests.request", return_value=upstream) as send:
                result = asyncio.run(endpoint(payload, request))

            self.assertEqual(result["response"]["status_code"], 201)
            self.assertEqual(result["response"]["body"], '{"ok":true}')
            self.assertEqual(result["response"]["headers"]["Set-Cookie"], "••••••")
            self.assertEqual(result["request"]["headers"]["Authorization"], "••••••")
            sent = send.call_args.kwargs
            self.assertEqual(sent["url"], "https://api.example.test/orders/42")
            self.assertEqual(sent["json"], {"id": "42"})
            self.assertFalse(sent["allow_redirects"])
            audit = store.list_audit_events()
            self.assertEqual(audit[0]["action"], "interface.request.debug")
            self.assertNotIn("secret", str(audit[0]))


if __name__ == "__main__":
    unittest.main()
