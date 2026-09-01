import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from starlette.requests import Request
from fastapi import HTTPException

from tests import bootstrap  # noqa: F401

from auto_test.platform.api import (
    InterfaceScenarioBatchInput,
    InterfaceScenarioExecuteInput,
    create_platform_api,
)
from auto_test.platform.artifact_storage import LocalArtifactStorage
from auto_test.core.interface_scenario_manager import InterfaceScenarioManager
from auto_test.platform.store import PlatformStore
from auto_test.platform.task_store import TaskStore


def _response(status, body, url, *, content_type="application/json"):
    upstream = MagicMock()
    upstream.__enter__.return_value = upstream
    upstream.__exit__.return_value = False
    upstream.iter_content.return_value = [body.encode("utf-8")]
    upstream.encoding = "utf-8"
    upstream.status_code = status
    upstream.reason = "OK"
    upstream.headers = {"Content-Type": content_type}
    upstream.request = SimpleNamespace(
        url=url,
        headers={"Authorization": "Bearer secret-token", "Accept": "application/json"},
    )
    return upstream


class InterfaceScenarioStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = PlatformStore(
            Path(self.temp_dir.name) / "platform.db", recover_jobs=False
        )
        self.project = self.store.create_project("SCENARIO", "接口场景")
        self.asset = self.store.save_interface_asset(
            self.project["id"],
            {"name": "健康检查", "method": "GET", "path": "https://api.example.test/health"},
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_scenario_definition_and_run_history_are_project_scoped(self):
        scenario = self.store.save_interface_scenario(
            self.project["id"],
            {
                "name": "冒烟场景",
                "description": "基础可用性",
                "parameters": {"TENANT": "demo"},
                "steps": [{"name": "检查服务", "asset_id": self.asset["id"]}],
            },
        )
        self.assertEqual(scenario["step_count"], 1)
        self.assertEqual(scenario["parameters"], {"TENANT": "demo"})

        run = self.store.create_interface_scenario_run(self.project["id"], scenario)
        finished = self.store.finish_interface_scenario_run(
            self.project["id"],
            run["id"],
            status="succeeded",
            summary={"total_steps": 1, "passed_steps": 1, "failed_steps": 0},
            result={"steps": [{"status": "passed"}]},
        )
        self.assertEqual(finished["status"], "succeeded")
        self.assertEqual(finished["result"]["steps"][0]["status"], "passed")
        self.assertEqual(self.store.list_interface_scenario_runs(self.project["id"])[0]["id"], run["id"])

        with self.assertRaisesRegex(ValueError, "场景"):
            self.store.delete_interface_asset(self.project["id"], self.asset["id"])

        other = self.store.create_project("OTHER_SCENARIO", "其他项目")
        self.assertEqual(self.store.list_interface_scenarios(other["id"]), [])
        self.assertIsNone(self.store.get_interface_scenario_run(other["id"], run["id"]))

    def test_queued_run_persists_snapshot_claims_and_stops_safely(self):
        scenario = self.store.save_interface_scenario(
            self.project["id"],
            {
                "name": "后台场景",
                "parameters": {},
                "steps": [{"name": "检查服务", "asset_id": self.asset["id"]}],
            },
        )
        queued = self.store.create_interface_scenario_run(
            self.project["id"],
            scenario,
            status="queued",
            parameters={"TENANT": "demo"},
        )
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["scenario_snapshot"]["name"], "后台场景")
        self.assertEqual(queued["parameters"], {"TENANT": "demo"})

        claimed = self.store.claim_interface_scenario_run()
        self.assertEqual(claimed["id"], queued["id"])
        self.assertEqual(claimed["status"], "running")
        stopping = self.store.request_stop_interface_scenario_run(
            self.project["id"], queued["id"]
        )
        self.assertEqual(stopping["status"], "running")
        self.assertTrue(
            self.store.is_interface_scenario_stop_requested(
                self.project["id"], queued["id"]
            )
        )

        second = self.store.create_interface_scenario_run(
            self.project["id"], scenario, status="queued"
        )
        stopped = self.store.request_stop_interface_scenario_run(
            self.project["id"], second["id"]
        )
        self.assertEqual(stopped["status"], "interrupted")

        limited_first = self.store.create_interface_scenario_run(
            self.project["id"],
            scenario,
            batch_id="serial-batch",
            status="queued",
            concurrency_limit=1,
        )
        limited_second = self.store.create_interface_scenario_run(
            self.project["id"],
            scenario,
            batch_id="serial-batch",
            status="queued",
            concurrency_limit=1,
        )
        self.assertEqual(self.store.claim_interface_scenario_run()["id"], limited_first["id"])
        self.assertIsNone(self.store.claim_interface_scenario_run())
        self.store.finish_interface_scenario_run(
            self.project["id"],
            limited_first["id"],
            status="interrupted",
            summary={},
            result={},
        )
        self.assertEqual(self.store.claim_interface_scenario_run()["id"], limited_second["id"])

    def test_new_run_explicitly_initializes_report_paths_for_strict_mysql(self):
        scenario = self.store.save_interface_scenario(
            self.project["id"],
            {
                "name": "MySQL 严格模式场景",
                "parameters": {},
                "steps": [{"name": "检查服务", "asset_id": self.asset["id"]}],
            },
        )
        statements = []
        original_connect = self.store._connect

        def traced_connect():
            connection = original_connect()
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(self.store, "_connect", side_effect=traced_connect):
            run = self.store.create_interface_scenario_run(
                self.project["id"], scenario, status="queued"
            )

        insert = next(
            statement
            for statement in statements
            if statement.startswith("INSERT INTO interface_scenario_runs")
        )
        self.assertIn("artifact_dir", insert)
        self.assertIn("docx_path", insert)
        self.assertIn("pdf_path", insert)
        self.assertEqual(run["artifact_dir"], "")
        self.assertEqual(run["docx_path"], "")
        self.assertEqual(run["pdf_path"], "")

    def test_worker_runtime_failure_finishes_claimed_run_without_leaking_message(self):
        scenario = self.store.save_interface_scenario(
            self.project["id"],
            {
                "name": "异常场景",
                "parameters": {},
                "steps": [{"asset_id": self.asset["id"]}],
            },
        )

        def fail_callback(*args, **kwargs):
            raise RuntimeError("secret-token-must-not-persist")

        manager = InterfaceScenarioManager(self.store, fail_callback, concurrency=1)
        queued = manager.submit(self.project["id"], scenario, {})
        finished = manager.run_once()
        self.assertEqual(finished["id"], queued["id"])
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["summary"]["failed_steps"], 1)
        self.assertNotIn("secret-token-must-not-persist", str(finished))


class InterfaceScenarioExecutionTests(unittest.TestCase):
    @staticmethod
    def _request(project_id, path):
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": path,
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "state": {
                    "identity": {
                        "user": {"id": "tester"},
                        "current_project": {"id": project_id},
                    }
                },
            }
        )

    def test_steps_extract_variables_chain_requests_assert_and_redact_secrets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = PlatformStore(root / "platform.db", recover_jobs=False)
            project = store.create_project("CHAIN", "接口串联")
            environment = store.save_interface_environment(
                project["id"],
                {"name": "测试环境", "base_url": "https://api.example.test"},
            )
            login = store.save_interface_asset(
                project["id"],
                {
                    "name": "登录",
                    "method": "POST",
                    "path": "/login",
                    "environment_id": environment["id"],
                    "request": {"body_type": "json", "body": {"tenant": "{{TENANT}}"}},
                },
            )
            detail = store.save_interface_asset(
                project["id"],
                {
                    "name": "订单详情",
                    "method": "GET",
                    "path": "/orders/{{ORDER_ID}}",
                    "environment_id": environment["id"],
                    "request": {"headers": {"Authorization": "Bearer {{ACCESS_TOKEN}}"}},
                },
            )
            scenario = store.save_interface_scenario(
                project["id"],
                {
                    "name": "登录后查询订单",
                    "environment_id": environment["id"],
                    "parameters": {"TENANT": "demo"},
                    "steps": [
                        {
                            "name": "获取上下文",
                            "asset_id": login["id"],
                            "extractions": [
                                {"name": "ACCESS_TOKEN", "source": "json", "expression": "$.token"},
                                {"name": "ORDER_ID", "source": "json", "expression": "$.order.id"},
                            ],
                        },
                        {
                            "name": "查询订单",
                            "asset_id": detail["id"],
                            "assertions": [
                                {
                                    "source": "json",
                                    "expression": "$.ok",
                                    "operator": "equals",
                                    "expected": True,
                                },
                                {
                                    "source": "status",
                                    "operator": "between",
                                    "expected": [200, 299],
                                },
                            ],
                        },
                    ],
                },
            )
            router, report_manager, _, _ = create_platform_api(
                TaskStore(root / "tasks.db"),
                platform_store=store,
                artifact_storage=LocalArtifactStorage(root / "artifacts"),
            )
            endpoint = next(
                route.endpoint
                for route in router.routes
                if route.path == "/api/interface-scenarios/{scenario_id}/execute"
            )
            request = self._request(
                project["id"], f"/api/interface-scenarios/{scenario['id']}/execute"
            )
            responses = [
                _response(
                    201,
                    '{"token":"secret-token","order":{"id":42}}',
                    "https://api.example.test/login",
                ),
                _response(200, '{"ok":true}', "https://api.example.test/orders/42"),
            ]
            with patch("auto_test.platform.api.requests.request", side_effect=responses) as send:
                queued = asyncio.run(
                    endpoint(scenario["id"], InterfaceScenarioExecuteInput(), request)
                )
                self.assertEqual(queued["status"], "queued")
                result = report_manager.interface_scenario_manager.run_once()

            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["summary"]["passed_steps"], 2)
            self.assertEqual(send.call_count, 2)
            second_request = send.call_args_list[1].kwargs
            self.assertEqual(second_request["url"], "https://api.example.test/orders/42")
            self.assertEqual(second_request["headers"]["Authorization"], "Bearer secret-token")
            self.assertNotIn("secret-token", str(result))
            self.assertEqual(
                result["result"]["steps"][0]["extractions"][0]["value"], "••••••"
            )
            self.assertTrue(result["result"]["steps"][1]["assertions"][0]["passed"])
            self.assertTrue(Path(result["docx_path"]).is_file())
            self.assertTrue(Path(result["pdf_path"]).is_file())
            from docx import Document
            from pypdf import PdfReader

            document_text = "\n".join(
                paragraph.text for paragraph in Document(result["docx_path"]).paragraphs
            )
            self.assertIn("接口自动化测试报告", document_text)
            self.assertGreaterEqual(len(PdfReader(result["pdf_path"]).pages), 1)

    def test_environment_values_resolve_assertions_and_required_extraction_stops_later_steps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = PlatformStore(root / "platform.db", recover_jobs=False)
            project = store.create_project("STOP", "失败停止")
            environment = store.save_interface_environment(
                project["id"],
                {"name": "验证环境", "base_url": "https://api.example.test"},
            )
            store.save_interface_variable(
                project["id"],
                {
                    "environment_id": environment["id"],
                    "key": "EXPECTED_CODE",
                    "value": "202",
                    "is_secret": False,
                },
            )
            first = store.save_interface_asset(
                project["id"],
                {"name": "创建", "method": "POST", "path": "/orders"},
            )
            second = store.save_interface_asset(
                project["id"],
                {"name": "查询", "method": "GET", "path": "/orders/next"},
            )
            scenario = store.save_interface_scenario(
                project["id"],
                {
                    "name": "必需变量失败即停止",
                    "environment_id": environment["id"],
                    "parameters": {},
                    "steps": [
                        {
                            "name": "创建订单",
                            "asset_id": first["id"],
                            "extractions": [
                                {
                                    "name": "ORDER_ID",
                                    "source": "json",
                                    "expression": "$.missing",
                                    "required": True,
                                }
                            ],
                            "assertions": [
                                {
                                    "source": "status",
                                    "operator": "equals",
                                    "expected": "{{EXPECTED_CODE}}",
                                }
                            ],
                        },
                        {"name": "后续查询", "asset_id": second["id"]},
                    ],
                },
            )
            router, report_manager, _, _ = create_platform_api(
                TaskStore(root / "tasks.db"),
                platform_store=store,
                artifact_storage=LocalArtifactStorage(root / "artifacts"),
            )
            endpoint = next(
                route.endpoint
                for route in router.routes
                if route.path == "/api/interface-scenarios/{scenario_id}/execute"
            )
            request = self._request(
                project["id"], f"/api/interface-scenarios/{scenario['id']}/execute"
            )
            with patch(
                "auto_test.platform.api.requests.request",
                return_value=_response(
                    202, '{"accepted":true}', "https://api.example.test/orders"
                ),
            ) as send:
                queued = asyncio.run(
                    endpoint(scenario["id"], InterfaceScenarioExecuteInput(), request)
                )
                self.assertEqual(queued["status"], "queued")
                result = report_manager.interface_scenario_manager.run_once()

            self.assertEqual(send.call_count, 1)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["summary"]["failed_steps"], 1)
            self.assertEqual(result["summary"]["skipped_steps"], 1)
            self.assertTrue(result["result"]["steps"][0]["assertions"][0]["passed"])
            self.assertFalse(result["result"]["steps"][0]["extractions"][0]["success"])
            self.assertEqual(result["result"]["steps"][1]["status"], "skipped")

    def test_sensitive_runtime_parameter_is_rejected_before_any_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = PlatformStore(root / "platform.db", recover_jobs=False)
            project = store.create_project("PARAM", "敏感参数")
            asset = store.save_interface_asset(
                project["id"],
                {"name": "检查", "method": "GET", "path": "https://api.example.test/check"},
            )
            scenario = store.save_interface_scenario(
                project["id"],
                {
                    "name": "参数保护",
                    "parameters": {},
                    "steps": [{"asset_id": asset["id"]}],
                },
            )
            router, report_manager, _, _ = create_platform_api(
                TaskStore(root / "tasks.db"),
                platform_store=store,
                artifact_storage=LocalArtifactStorage(root / "artifacts"),
            )
            endpoint = next(
                route.endpoint
                for route in router.routes
                if route.path == "/api/interface-scenarios/{scenario_id}/execute"
            )
            request = self._request(
                project["id"], f"/api/interface-scenarios/{scenario['id']}/execute"
            )
            with patch("auto_test.platform.api.requests.request") as send:
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(
                        endpoint(
                            scenario["id"],
                            InterfaceScenarioExecuteInput(
                                parameters={"ACCESS_TOKEN": "must-not-persist"}
                            ),
                            request,
                        )
                    )

            self.assertEqual(raised.exception.status_code, 400)
            self.assertNotIn("must-not-persist", str(raised.exception.detail))
            send.assert_not_called()

    def test_batch_execution_preserves_selection_order_and_generates_each_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = PlatformStore(root / "platform.db", recover_jobs=False)
            project = store.create_project("BATCH", "批量执行")
            scenarios = []
            for index in range(2):
                asset = store.save_interface_asset(
                    project["id"],
                    {
                        "name": f"检查 {index + 1}",
                        "method": "GET",
                        "path": f"https://api.example.test/check/{index + 1}",
                    },
                )
                scenarios.append(
                    store.save_interface_scenario(
                        project["id"],
                        {
                            "name": f"批量场景 {index + 1}",
                            "parameters": {},
                            "steps": [{"asset_id": asset["id"]}],
                        },
                    )
                )
            router, report_manager, _, _ = create_platform_api(
                TaskStore(root / "tasks.db"),
                platform_store=store,
                artifact_storage=LocalArtifactStorage(root / "artifacts"),
            )
            endpoint = next(
                route.endpoint
                for route in router.routes
                if route.path == "/api/interface-scenarios/batch-execute"
            )
            request = self._request(
                project["id"], "/api/interface-scenarios/batch-execute"
            )

            def upstream(**kwargs):
                return _response(200, '{"ok":true}', kwargs["url"])

            with patch(
                "auto_test.platform.api.requests.request", side_effect=upstream
            ) as send:
                result = asyncio.run(
                    endpoint(
                        InterfaceScenarioBatchInput(
                            scenario_ids=[item["id"] for item in scenarios],
                            concurrency=2,
                        ),
                        request,
                    )
                )
                self.assertTrue(all(run["status"] == "queued" for run in result["runs"]))
                self.assertTrue(
                    all(run["concurrency_limit"] == 2 for run in result["runs"])
                )
                for _ in scenarios:
                    report_manager.interface_scenario_manager.run_once()

            result["runs"] = [
                store.get_interface_scenario_run(project["id"], run["id"])
                for run in result["runs"]
            ]

            self.assertEqual(send.call_count, 2)
            self.assertEqual(
                [item["scenario_id"] for item in result["runs"]],
                [item["id"] for item in scenarios],
            )
            self.assertTrue(all(item["status"] == "succeeded" for item in result["runs"]))
            self.assertTrue(
                all(item["batch_id"] == result["batch_id"] for item in result["runs"])
            )
            for run in result["runs"]:
                self.assertTrue(Path(run["docx_path"]).is_file())
                self.assertTrue(Path(run["pdf_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
