import re
import tomllib
import unittest
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_ROOT = PROJECT_ROOT / "deploy"
DOCKER_ROOT = DEPLOY_ROOT / "docker"


class ProductionDeploymentAssetsTests(unittest.TestCase):
    def test_ui_overlay_confines_docker_control_and_browser_network(self):
        compose = yaml.safe_load((DEPLOY_ROOT / 'compose.ui.yml').read_text(encoding='utf-8'))
        self.assertTrue(compose['networks']['ui-test']['internal'])
        services = compose['services']
        self.assertEqual(services['ui-worker']['extends']['service'], 'worker')
        self.assertEqual(services['web']['environment'], services['ui-worker']['environment'])
        self.assertEqual(services['web']['environment']['LIEMA_UI_RECORDER_CONNECT_MODE'], 'network')
        for name, service in services.items():
            self.assertNotIn('ports', service)
            if name != 'ui-worker':
                self.assertNotIn('/var/run/docker.sock', str(service))
        self.assertIn('/var/run/docker.sock:/var/run/docker.sock', services['ui-worker']['volumes'])

    def test_ui_proxy_requires_explicit_hosts_ports_and_denies_other_destinations(self):
        from scripts.prepare_ui_proxy import configuration
        config = configuration(['app.example.test', '127.0.0.2'], [443, 80, 443])
        self.assertIn('acl approved_ports port 80 443', config)
        self.assertIn('acl approved_addresses dst 127.0.0.2', config)
        self.assertIn('acl approved_domains dstdomain app.example.test', config)
        self.assertTrue(config.endswith('http_access deny all\n'))
        self.assertIn('access_log none', config)
        for hosts, ports in [([], [80]), (['app.test'], []), (['app.test'], [65536]), (['*.test'], [80]), (['app.test\nhttp_access allow all'], [80])]:
            with self.subTest(hosts=hosts, ports=ports), self.assertRaises(ValueError):
                configuration(hosts, ports)

    def test_external_compose_reuses_dependencies_without_defining_redis(self):
        compose = yaml.safe_load(
            (DEPLOY_ROOT / "compose.external.yml").read_text(encoding="utf-8")
        )
        local_compose = yaml.safe_load(
            (DEPLOY_ROOT / "compose.yml").read_text(encoding="utf-8")
        )

        self.assertEqual(set(compose["services"]), {"web", "worker"})
        web = compose["services"]["web"]
        worker = compose["services"]["worker"]
        expected_build = {
            "context": "..",
            "dockerfile": "deploy/docker/Dockerfile",
        }
        self.assertEqual(web["build"], expected_build)
        self.assertEqual(worker["build"], expected_build)
        self.assertEqual(local_compose["services"]["web"]["build"], expected_build)
        self.assertEqual(local_compose["services"]["worker"]["build"], expected_build)
        self.assertTrue((DOCKER_ROOT / "Dockerfile").is_file())
        self.assertTrue((DOCKER_ROOT / "Dockerfile.offline").is_file())
        for name in ("Dockerfile.runtime", "Dockerfile.universal", "Dockerfile.blackwell"):
            self.assertTrue((DOCKER_ROOT / "gpu-burn" / name).is_file())
        self.assertEqual(web["environment"], worker["environment"])
        self.assertEqual(web["environment"]["LIEMA_DATABASE_BACKEND"], "mysql")
        self.assertEqual(web["environment"]["LIEMA_ARTIFACT_BACKEND"], "minio")
        self.assertEqual(web["environment"]["LIEMA_TASK_QUEUE_BACKEND"], "redis")
        self.assertEqual(web["environment"]["LIEMA_TASK_EXECUTION_MODE"], "external")
        self.assertIn("LIEMA_REDIS_NAMESPACE", web["environment"])
        self.assertEqual(web["environment"]["LIEMA_REDIS_PASSWORD"], "${LIEMA_REDIS_PASSWORD:-}")
        self.assertIn("${LIEMA_WEB_PORT:-8080}:8080", web["ports"][0])
        self.assertNotIn("depends_on", web)
        self.assertNotIn("depends_on", worker)

    def test_production_env_example_is_secret_free_and_not_server_specific(self):
        text = (DEPLOY_ROOT / "env.production.example").read_text(encoding="utf-8")

        self.assertNotRegex(text, re.compile(r"(?:172\.16|192\.168|10\.\d+)\."))
        values = {}
        for line in text.splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        for key in (
            "LIEMA_MASTER_KEY",
            "LIEMA_MYSQL_PASSWORD",
            "LIEMA_MINIO_ACCESS_KEY",
            "LIEMA_MINIO_SECRET_KEY",
            "LIEMA_REDIS_PASSWORD",
        ):
            self.assertEqual(values[key], "")
        self.assertEqual(values["LIEMA_SESSION_COOKIE_SECURE"], "false")

    def test_offline_image_build_is_network_free_and_keeps_runtime_non_root(self):
        dockerfile = (DOCKER_ROOT / "Dockerfile.offline").read_text(encoding="utf-8")
        dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")

        self.assertIn("--no-index", dockerfile)
        self.assertNotIn("apt-get", dockerfile)
        self.assertIn("COPY config/config.example.yml /app/config/config.example.yml", dockerfile)
        for directory in ("data", "runtime", "artifacts", "instance", "logs"):
            self.assertIn(f"/app/{directory}", dockerfile)
        self.assertIn("USER liema", dockerfile)
        self.assertIn(".env.production", dockerignore)
        self.assertIn("python-3.12-slim-amd64.tar", dockerignore)
        self.assertIn("vendor-offline-20260822.tar", dockerignore)

    def test_offline_runtime_dependencies_are_copied_into_final_image(self):
        dockerfile = (DOCKER_ROOT / "Dockerfile.offline").read_text(encoding="utf-8")
        project = tomllib.loads(
            (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        bundle_script = (
            PROJECT_ROOT / "scripts" / "prepare_offline_bundle.py"
        ).read_text(encoding="utf-8")

        self.assertIn("--ignore-installed", dockerfile)
        self.assertIn("packaging>=20.0", project["project"]["dependencies"])
        self.assertIn('"packaging>=20.0"', bundle_script)


if __name__ == "__main__":
    unittest.main()
