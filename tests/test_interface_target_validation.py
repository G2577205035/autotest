import unittest

from fastapi import HTTPException

from tests import bootstrap  # noqa: F401

from auto_test.platform.api import (
    redact_interface_headers,
    resolve_interface_template,
    validate_interface_target,
)


class InterfaceTargetValidationTests(unittest.TestCase):
    def test_accepts_full_http_urls_and_relative_paths(self):
        self.assertEqual(
            validate_interface_target(
                "http://172.16.102.91:8036/v3/translate_a?stream=true"
            ),
            "http://172.16.102.91:8036/v3/translate_a?stream=true",
        )
        self.assertEqual(validate_interface_target("/v3/translate_a"), "/v3/translate_a")

    def test_rejects_unsafe_or_unsupported_targets(self):
        for target in (
            "ftp://example.test/file",
            "http://user:password@example.test/a",
            "https://example.test/a#section",
            "/v1/../admin",
        ):
            with self.subTest(target=target), self.assertRaises(HTTPException):
                validate_interface_target(target)

    def test_resolves_nested_variables_and_rejects_missing_values(self):
        resolved = resolve_interface_template(
            {
                "url": "https://{{HOST}}/v1/{{RESOURCE}}",
                "headers": ["Bearer {{TOKEN}}"],
            },
            {"HOST": "api.example.test", "RESOURCE": "orders", "TOKEN": "secret"},
        )
        self.assertEqual(resolved["url"], "https://api.example.test/v1/orders")
        self.assertEqual(resolved["headers"], ["Bearer secret"])
        with self.assertRaisesRegex(HTTPException, "MISSING"):
            resolve_interface_template("{{MISSING}}", {})

    def test_redacts_sensitive_request_headers(self):
        self.assertEqual(
            redact_interface_headers(
                {"Authorization": "Bearer secret", "X-Api-Key": "key", "Accept": "json"}
            ),
            {"Authorization": "••••••", "X-Api-Key": "••••••", "Accept": "json"},
        )


if __name__ == "__main__":
    unittest.main()
