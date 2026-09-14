import os
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import bootstrap  # noqa: F401
from auto_test.platform.store import PlatformStore
from auto_test.platform.model_access import ProjectModelStore, set_model_access
from auto_test.platform.models import get_active_model_config
from auto_test.platform.secrets import encrypt_secret
from auto_test.evaluation.manager import _resolve_request_secrets


class ModelProjectAccessTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {'LIEMA_MASTER_KEY': secrets.token_urlsafe(32)})
        env.start()
        self.addCleanup(env.stop)
        self.store = PlatformStore(Path(temp.name) / 'db.sqlite', recover_jobs=False)
        self.a = self.store.create_project('A1', '项目甲')['id']
        self.b = self.store.create_project('B1', '项目乙')['id']
        self.key = secrets.token_urlsafe(24)
        self.model = self.store.save_model_profile({'name': '模型', 'model_name': 'test', 'base_url': 'https://model.example.test', 'is_active': True}, encrypt_secret(self.key))
        self.allowed = ProjectModelStore(self.store, self.a)
        self.denied = ProjectModelStore(self.store, self.b)

    def test_default_compatibility_and_project_filtering(self):
        self.assertEqual(len(self.denied.list_model_profiles()), 1)
        set_model_access(self.store, self.model['id'], True, [self.a])
        self.assertEqual(self.denied.list_model_profiles(), [])
        self.assertIsNone(self.denied.active_model_profile())
        self.assertIsNone(self.denied.get_model_profile(self.model['id']))
        self.assertEqual(self.allowed.get_model_profile(self.model['id'])['id'], self.model['id'])
        self.assertEqual(get_active_model_config(self.store), {})
        self.assertEqual(get_active_model_config(self.store, project_id=self.a)['api_key'], self.key)

    def test_queue_rechecks_model_and_judge_even_with_environment_credentials(self):
        set_model_access(self.store, self.model['id'], True, [self.a])
        for config, model_id in [({}, self.model['id']), ({'api_key_env': 'IGNORED'}, self.model['id']), ({'judge': {'profile_id': self.model['id']}}, '')]:
            with self.subTest(config=config), self.assertRaises(RuntimeError):
                _resolve_request_secrets(config, platform_store=self.denied, model_profile_id=model_id)
        resolved = _resolve_request_secrets({}, platform_store=self.allowed, model_profile_id=self.model['id'])
        self.assertEqual(resolved['LIEMA_EVAL_MODEL_API_KEY'], self.key)

    def test_invocation_audits_success_failure_without_content_or_secrets(self):
        prompt, response = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        with patch('auto_test.platform.models.call_model', return_value=response):
            self.assertEqual(self.allowed.call_model(self.model, prompt), response)
        with patch('auto_test.platform.models.call_model', side_effect=RuntimeError(self.key)), self.assertRaises(RuntimeError):
            self.allowed.call_model(self.model, prompt)
        events = self.store.list_audit_events()
        self.assertEqual({event['outcome'] for event in events}, {'success', 'failed'})
        self.assertTrue(all(event['project_id'] == self.a for event in events))
        for private in (prompt, response, self.key):
            self.assertNotIn(private, str(events))

    def test_empty_restricted_grant_denies_cached_profile_and_unknown_projects(self):
        with self.assertRaises(ValueError):
            set_model_access(self.store, self.model['id'], True, ['missing'])
        set_model_access(self.store, self.model['id'], True, [])
        with patch('auto_test.platform.models.call_model') as call, self.assertRaises(PermissionError):
            self.allowed.call_model(self.model, 'prompt')
        call.assert_not_called()

    def test_business_log_analysis_uses_audited_model_call(self):
        from auto_test.monitoring.log_analyzer import analyze_errors
        root = self.store.db_path.parent
        errors, stress = root / 'errors_test.log', root / 'stress.txt'
        errors.write_text('Synthetic failure event', encoding='utf-8')
        stress.write_text('Synthetic CPU measurement', encoding='utf-8')
        with patch('auto_test.platform.models.call_model', return_value='合成分析结论'), patch('auto_test.monitoring.log_analyzer._call_llm') as legacy:
            answer = analyze_errors([str(errors)], self.key, self.model['base_url'], 'test', str(root), str(stress), model_call=lambda prompt: self.allowed.call_model(self.model, prompt))
        self.assertIn('合成分析结论', answer)
        legacy.assert_not_called()
        self.assertEqual(len(self.store.list_audit_events()), 2)
