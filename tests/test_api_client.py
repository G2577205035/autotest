import unittest
from tests import bootstrap  # noqa: F401
from unittest.mock import Mock, patch

from auto_test.integrations.api_client import TargetPlatformClient, TargetPlatformResponseError


def make_session(response):
    session = Mock()
    session.headers = {}
    session.request.return_value = response
    return session


def make_session_sequence(responses):
    session = Mock()
    session.headers = {}
    session.request.side_effect = responses
    return session


class TargetPlatformClientTests(unittest.TestCase):
    def test_login_returns_token_without_logging_or_exposing_it(self):
        response = Mock(status_code=200, text="")
        response.json.return_value = {"data": {"satoken": "secret-token"}}
        client = TargetPlatformClient("127.0.0.1", session=make_session(response))

        self.assertEqual(client.login("tester", "password"), "secret-token")
        _, url = client.session.request.call_args.args[:2]
        self.assertEqual(url, "http://127.0.0.1:8000/system/login/loginIn")

    def test_non_success_response_raises_typed_error(self):
        response = Mock(status_code=503, text="temporarily unavailable")
        client = TargetPlatformClient("127.0.0.1", session=make_session(response))

        with self.assertRaises(TargetPlatformResponseError) as caught:
            client.list_cases("token")

        self.assertEqual(caught.exception.status_code, 503)

    def test_invalid_host_is_rejected(self):
        with self.assertRaises(ValueError):
            TargetPlatformClient("")

    def test_private_target_ignores_system_proxy_by_default(self):
        session = make_session(Mock(status_code=200, text=""))

        TargetPlatformClient("172.16.0.10", session=session)

        self.assertFalse(session.trust_env)

    def test_system_proxy_requires_explicit_opt_in(self):
        session = make_session(Mock(status_code=200, text=""))

        TargetPlatformClient("proxy-required.example", trust_env=True, session=session)

        self.assertTrue(session.trust_env)

    def test_create_user_rejects_business_failure_with_http_200(self):
        response = Mock(status_code=200, text="")
        response.json.return_value = {"code": 500, "message": "用户名已存在"}
        client = TargetPlatformClient("127.0.0.1", session=make_session(response))

        self.assertFalse(client.create_user("admin-token", "tester", "password"))

    def test_create_user_accepts_business_success(self):
        response = Mock(status_code=200, text="")
        response.json.return_value = {"code": 200, "message": "保存成功"}
        client = TargetPlatformClient("127.0.0.1", session=make_session(response))

        self.assertTrue(client.create_user("admin-token", "tester", "password"))

    def test_create_user_retries_when_server_is_busy(self):
        busy = Mock(status_code=200, text="")
        busy.json.return_value = {"code": 500, "message": "服务器忙，请稍后在试"}
        success = Mock(status_code=200, text="")
        success.json.return_value = {"code": 200, "message": "保存成功"}
        client = TargetPlatformClient("127.0.0.1", session=make_session_sequence([busy, success]))

        with patch("auto_test.integrations.api_client.time.sleep", return_value=None):
            self.assertTrue(client.create_user("admin-token", "tester", "password", retries=2, retry_delay=0))
        self.assertEqual(client.session.request.call_count, 2)

    def test_create_user_rechecks_login_after_transient_failures(self):
        busy1 = Mock(status_code=200, text="")
        busy1.json.return_value = {"code": 500, "message": "服务器忙，请稍后在试"}
        busy2 = Mock(status_code=200, text="")
        busy2.json.return_value = {"code": 500, "message": "服务器忙，请稍后在试"}
        login_success = Mock(status_code=200, text="")
        login_success.json.return_value = {"data": {"satoken": "created-token"}}
        client = TargetPlatformClient("127.0.0.1", session=make_session_sequence([busy1, busy2, login_success]))

        with patch("auto_test.integrations.api_client.time.sleep", return_value=None):
            self.assertTrue(client.create_user("admin-token", "tester", "password", retries=2, retry_delay=0))
        self.assertEqual(client.session.request.call_count, 3)


if __name__ == "__main__":
    unittest.main()
