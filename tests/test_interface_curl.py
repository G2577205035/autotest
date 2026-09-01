import unittest

from tests import bootstrap  # noqa: F401

from auto_test.platform.api import build_interface_request_options
from auto_test.platform.interface_specs import parse_curl_request, parse_endpoint_text


class InterfaceCurlParserTests(unittest.TestCase):
    def test_parses_pasted_multipart_curl_from_interface_workbench(self):
        command = "\n".join(
            [
                "curl --location 'http://172.16.102.91:8036/v3/translate_a' \\",
                "  --form 'q=\"hello \"' \\",
                "  --form 'from=\"auto\"' \\",
                "  --form 'to=\"zh-CHS\"'",
            ]
        )

        parsed = parse_curl_request(command)

        self.assertEqual(parsed["method"], "POST")
        self.assertEqual(parsed["target"], "http://172.16.102.91:8036/v3/translate_a")
        self.assertEqual(parsed["body_type"], "multipart")
        self.assertEqual(
            parsed["body"],
            {"q": "hello ", "from": "auto", "to": "zh-CHS"},
        )
        self.assertIn("已忽略 --location", "；".join(parsed["warnings"]))

    def test_parses_json_headers_and_url_query_from_curl_exe(self):
        command = (
            '$ curl.exe "https://api.example.test/orders?expand=true&empty=" '
            '-X POST -H "Accept: application/json" '
            '-H "Content-Type: application/json" --data-raw \'{"id":42}\''
        )

        parsed = parse_curl_request(command)

        self.assertEqual(parsed["method"], "POST")
        self.assertEqual(parsed["target"], "https://api.example.test/orders")
        self.assertEqual(parsed["query"], {"expand": "true", "empty": ""})
        self.assertEqual(parsed["headers"]["Accept"], "application/json")
        self.assertEqual(parsed["body_type"], "json")
        self.assertEqual(parsed["body"], {"id": 42})

    def test_get_data_is_filled_as_query_instead_of_request_body(self):
        parsed = parse_curl_request(
            "curl -G 'https://api.example.test/search' "
            "--data-urlencode 'q=hello world' --data 'page=2'"
        )

        self.assertEqual(parsed["method"], "GET")
        self.assertEqual(parsed["query"], {"q": "hello world", "page": "2"})
        self.assertEqual(parsed["body_type"], "none")
        self.assertIsNone(parsed["body"])

    def test_rejects_local_file_references_in_form_data(self):
        with self.assertRaisesRegex(ValueError, "不会读取客户端文件路径"):
            parse_curl_request(
                "curl 'https://api.example.test/upload' --form 'file=@C:/secret.txt'"
            )

    def test_endpoint_import_recognizes_prompted_curl_exe_and_keeps_structure(self):
        parsed = parse_endpoint_text(
            "$ curl.exe 'https://api.example.test/items?limit=10' -H 'X-Trace: demo'",
            "items",
        )

        self.assertEqual(len(parsed), 1)
        item = parsed[0]
        self.assertEqual(item["source_type"], "curl")
        self.assertEqual(item["method"], "GET")
        self.assertEqual(item["spec"]["query"], {"limit": "10"})
        self.assertEqual(item["spec"]["headers"], {"X-Trace": "demo"})
        self.assertNotIn("curl", str(item["spec"]).lower())

    def test_request_options_preserve_selected_body_encoding(self):
        base = {
            "method": "POST",
            "target": "https://api.example.test/submit",
            "headers": {},
            "query": {},
            "timeout_seconds": 30,
        }

        multipart = build_interface_request_options(
            {**base, "body_type": "multipart", "body": {"q": "hello", "tag": ["a", "b"]}}
        )
        urlencoded = build_interface_request_options(
            {**base, "body_type": "urlencoded", "body": {"q": "hello"}}
        )
        raw = build_interface_request_options(
            {**base, "body_type": "raw", "body": "plain text"}
        )
        empty = build_interface_request_options(
            {**base, "body_type": "none", "body": None}
        )

        self.assertEqual(
            multipart["files"],
            [("q", (None, "hello")), ("tag", (None, "a")), ("tag", (None, "b"))],
        )
        self.assertEqual(urlencoded["data"], {"q": "hello"})
        self.assertEqual(raw["data"], "plain text")
        self.assertNotIn("json", empty)
        self.assertNotIn("data", empty)
        self.assertNotIn("files", empty)


if __name__ == "__main__":
    unittest.main()
