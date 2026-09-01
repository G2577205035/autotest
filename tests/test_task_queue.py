import os
import unittest
from unittest.mock import patch

from tests import bootstrap  # noqa: F401

from auto_test.core.task_queue import (
    LocalTaskSignalQueue,
    RedisTaskSignalQueue,
    create_task_signal_queue,
    redis_connection_settings,
    task_execution_mode,
)


class _FakePipeline:
    def __init__(self, client):
        self.client = client
        self.calls = []

    def lpush(self, key, value):
        self.calls.append(("lpush", key, value))
        return self

    def ltrim(self, key, start, end):
        self.calls.append(("ltrim", key, start, end))
        return self

    def expire(self, key, seconds):
        self.calls.append(("expire", key, seconds))
        return self

    def ping(self):
        self.calls.append(("ping",))
        return self

    def get(self, key):
        self.calls.append(("get", key))
        return self

    def execute(self):
        self.client.pipeline_calls.append(self.calls)
        return [
            True if call[0] == "ping" else self.client.values.get(call[1]) if call[0] == "get" else 1
            for call in self.calls
        ]


class _FakeRedis:
    def __init__(self):
        self.pipeline_calls = []
        self.wait_result = ("queue", "signal")
        self.closed = False
        self.values = {}

    def pipeline(self, transaction=False):
        self.transaction = transaction
        return _FakePipeline(self)

    def brpop(self, key, timeout):
        self.wait_call = (key, timeout)
        return self.wait_result

    def ping(self):
        return True

    def set(self, key, value, ex):
        self.values[key] = value
        self.last_set = (key, value, ex)
        return True

    def delete(self, key):
        self.values.pop(key, None)
        return 1

    def close(self):
        self.closed = True


class TaskSignalQueueTests(unittest.TestCase):
    def test_local_queue_wakes_only_the_selected_topic(self):
        queue = LocalTaskSignalQueue()
        queue.notify("automation")
        self.assertTrue(queue.wait("automation", 0.01))
        self.assertFalse(queue.wait("reports", 0.01))
        self.assertEqual(
            queue.status(),
            {"backend": "local", "available": True, "worker_available": None},
        )

    def test_redis_queue_publishes_small_wake_signal_and_blocks_for_topic(self):
        client = _FakeRedis()
        queue = RedisTaskSignalQueue(
            "redis://127.0.0.1:6379/0", namespace="liema-test", client=client
        )
        self.assertTrue(queue.notify("automation"))
        calls = client.pipeline_calls[0]
        self.assertEqual(calls[0][0:2], ("lpush", "liema-test:wake:automation"))
        self.assertEqual(calls[1], ("ltrim", "liema-test:wake:automation", 0, 99))
        self.assertTrue(queue.wait("automation", 0.1))
        self.assertEqual(client.wait_call, ("liema-test:wake:automation", 1))
        self.assertTrue(queue.heartbeat("primary", 20))
        status = queue.status()
        self.assertTrue(status["available"])
        self.assertTrue(status["worker_available"])
        queue.clear_heartbeat("primary")
        queue.close()
        self.assertTrue(client.closed)

    def test_redis_outage_degrades_to_database_polling_without_raising(self):
        client = _FakeRedis()
        client.pipeline = lambda transaction=False: (_ for _ in ()).throw(
            ConnectionError("redis unavailable")
        )
        client.brpop = lambda key, timeout: (_ for _ in ()).throw(
            ConnectionError("redis unavailable")
        )
        queue = RedisTaskSignalQueue("redis://127.0.0.1:6379/0", client=client)
        self.assertFalse(queue.notify("reports"))
        self.assertFalse(queue.wait("reports", 0.01))
        self.assertIsNotNone(queue.status()["last_error_at"])

    def test_factory_defaults_keep_local_embedded_mode(self):
        self.assertIsInstance(create_task_signal_queue({"backend": "local"}), LocalTaskSignalQueue)
        self.assertEqual(task_execution_mode({"execution_mode": "embedded"}), "embedded")
        self.assertEqual(task_execution_mode({"execution_mode": "external"}), "external")
        with self.assertRaisesRegex(ValueError, "embedded"):
            task_execution_mode({"execution_mode": "invalid"})

    def test_split_service_environment_keeps_password_out_of_url(self):
        environment = {
            "LIEMA_REDIS_URL": "",
            "LIEMA_REDIS_HOST": "",
            "LIEMA_REDIS_PORT": "",
            "LIEMA_REDIS_PASSWORD": "",
            "SERVICE_REDIS_IP": "192.0.2.20",
            "SERVICE_REDIS_PORT": "6380",
            "SERVICE_REDIS_PASSWORD": "test-only-secret",
        }
        with patch.dict(os.environ, environment, clear=False):
            settings = redis_connection_settings({"redis_db": 2})
        self.assertEqual(settings["url"], "redis://192.0.2.20:6380/2")
        self.assertEqual(settings["password"], "test-only-secret")
        self.assertNotIn("test-only-secret", settings["url"])

    def test_factory_passes_secret_as_separate_client_argument(self):
        environment = {
            "LIEMA_REDIS_URL": "",
            "LIEMA_REDIS_HOST": "",
            "LIEMA_REDIS_PASSWORD": "application-secret",
            "SERVICE_REDIS_IP": "192.0.2.30",
            "SERVICE_REDIS_PORT": "6379",
        }
        with patch.dict(os.environ, environment, clear=False), patch(
            "auto_test.core.task_queue.RedisTaskSignalQueue"
        ) as queue_type:
            create_task_signal_queue({"backend": "redis", "namespace": "test"})
        args, kwargs = queue_type.call_args
        self.assertEqual(args[0], "redis://192.0.2.30:6379/0")
        self.assertEqual(kwargs["password"], "application-secret")
        self.assertNotIn("application-secret", args[0])

    def test_factory_accepts_environment_namespace_for_shared_redis_isolation(self):
        environment = {
            "LIEMA_REDIS_NAMESPACE": "liema-auto-production",
            "LIEMA_REDIS_PASSWORD": "",
        }
        with patch.dict(os.environ, environment, clear=False), patch(
            "auto_test.core.task_queue.RedisTaskSignalQueue"
        ) as queue_type:
            create_task_signal_queue({"backend": "redis", "redis_url": "redis://127.0.0.1:6379/0"})
        self.assertEqual(queue_type.call_args.kwargs["namespace"], "liema-auto-production")


if __name__ == "__main__":
    unittest.main()
