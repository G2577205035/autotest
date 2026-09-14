import hashlib
import json
import os
import secrets
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from tests import bootstrap  # noqa: F401
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from auto_test.platform.enterprise import OidcService, b64encode, verify_id_token, resolve_secret_reference, secure_url
from auto_test.platform.identity import create_identity_api, install_identity_guard
from auto_test.platform.secrets import encrypt_secret, decrypt_secret, SecretEncryptionError
from auto_test.platform.store import PlatformStore


class EnterpriseIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = cls.key.public_key().public_numbers()
        cls.jwks = {'keys': [{'kid': 'test-key', 'kty': 'RSA', 'alg': 'RS256', 'n': b64encode(numbers.n.to_bytes(256, 'big')), 'e': b64encode(numbers.e.to_bytes(3, 'big'))}]}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'LIEMA_MASTER_KEY': secrets.token_urlsafe(32), 'LIEMA_OIDC_ENABLED': 'true', 'LIEMA_OIDC_ISSUER': 'https://login.example.test', 'LIEMA_OIDC_REDIRECT_URI': 'http://localhost/api/auth/oidc/callback', 'LIEMA_OIDC_CLIENT_ID': 'liema-test', 'LIEMA_OIDC_CLIENT_SECRET': '', 'LIEMA_VAULT_ADDR': 'https://vault.example.test', 'LIEMA_VAULT_TOKEN': secrets.token_urlsafe(24), 'LIEMA_VAULT_ALLOWED_PREFIXES': 'kv/liema'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = PlatformStore(Path(self.temp.name) / 'db.sqlite', recover_jobs=False)
        self.user = self.store.create_user('sso.user', 'SSO 用户', 'unusable-hash')
        self.oidc = OidcService(self.store)
        self.discovery = {'issuer': 'https://login.example.test', 'authorization_endpoint': 'https://login.example.test/auth', 'token_endpoint': 'https://login.example.test/token', 'jwks_uri': 'https://login.example.test/keys'}

    def token(self, **changes):
        now = time.time()
        claims = dict(iss='https://login.example.test', aud='liema-test', sub='subject-1', nonce='test-nonce', iat=now, exp=now + 120)
        claims.update(changes)
        first = b64encode(json.dumps({'alg': 'RS256', 'kid': 'test-key'}).encode()) + '.' + b64encode(json.dumps(claims).encode())
        return first + '.' + b64encode(self.key.sign(first.encode(), padding.PKCS1v15(), hashes.SHA256()))

    def verify(self, token):
        return verify_id_token(token, self.jwks, issuer=self.discovery['issuer'], client_id='liema-test', nonce='test-nonce')

    def test_signed_identity_and_claim_guards(self):
        self.assertEqual(self.verify(self.token())['sub'], 'subject-1')
        for values in ({'iss': 'https://other.example.test'}, {'aud': 'other'}, {'aud': ['liema-test', 'other']}, {'nonce': 'other'}, {'exp': time.time() - 1}, {'iat': time.time() + 100}, {'exp': float('nan')}, {'sub': ''}, {'nbf': time.time() + 120}):
            with self.subTest(values=values), self.assertRaises(Exception):
                self.verify(self.token(**values))
        token = self.token()
        with self.assertRaises(Exception):
            self.verify(token.rsplit('.', 1)[0] + '.' + b64encode(b'bad-signature'))

    def test_state_pkce_browser_binding_and_one_time_login(self):
        self.oidc.bind(self.discovery['issuer'], 'subject-1', self.user['id'])
        with patch('auto_test.platform.enterprise.fetch_json', return_value=self.discovery):
            url, cookie, _ = self.oidc.begin()
        params = parse_qs(urlparse(url).query)
        self.assertEqual(params['code_challenge_method'], ['S256'])
        redeemed = []
        def service(url, **kwargs):
            if url.endswith('/token'):
                redeemed.append(kwargs['data'])
                self.assertEqual(b64encode(hashlib.sha256(kwargs['data']['code_verifier'].encode()).digest()), params['code_challenge'][0])
                return {'id_token': self.token(nonce=params['nonce'][0])}
            return self.jwks if url.endswith('/keys') else self.discovery
        with patch('auto_test.platform.enterprise.fetch_json', side_effect=service):
            with self.assertRaises(ValueError):
                self.oidc.complete(params['state'][0], 'wrong-browser', 'code')
            self.assertEqual(redeemed, [])
            self.assertEqual(self.oidc.complete(params['state'][0], cookie, 'code')['id'], self.user['id'])
            with self.assertRaises(ValueError):
                self.oidc.complete(params['state'][0], cookie, 'code')
        self.assertEqual(len(redeemed), 1)

    def test_no_automatic_email_binding_or_disabled_account_login(self):
        for bind in (False, True):
            if bind:
                self.oidc.bind(self.discovery['issuer'], 'subject-1', self.user['id'])
                self.store.update_user(self.user['id'], is_active=False)
            with patch('auto_test.platform.enterprise.fetch_json', return_value=self.discovery):
                url, cookie, _ = self.oidc.begin()
            params = parse_qs(urlparse(url).query)
            def service(url, **kwargs):
                if url.endswith('/token'):
                    return {'id_token': self.token(nonce=params['nonce'][0], email='sso.user@example.test')}
                return self.jwks if url.endswith('/keys') else self.discovery
            with patch('auto_test.platform.enterprise.fetch_json', side_effect=service), self.assertRaisesRegex(ValueError, '绑定|停用'):
                self.oidc.complete(params['state'][0], cookie, 'code')

    def test_vault_reference_resolves_at_use_without_persisting_value(self):
        value = secrets.token_urlsafe(24)
        encrypted = encrypt_secret('vault://kv/liema/model#api_key')
        with patch('auto_test.platform.enterprise.fetch_json', return_value={'data': {'data': {'api_key': value}}}) as fetch:
            self.assertEqual(decrypt_secret(encrypted), value)
            self.assertEqual(fetch.call_args.args[0], 'https://vault.example.test/v1/kv/data/liema/model')
        self.assertNotIn(value, encrypted)
        with patch('auto_test.platform.enterprise.fetch_json') as fetch:
            for reference in ['vault://kv/other/model#key', 'vault://kv/liema/../model#key', 'vault://kv/liema//model#key']:
                with self.assertRaises(RuntimeError):
                    resolve_secret_reference(reference)
            fetch.assert_not_called()

    def test_vault_failure_never_silently_uses_reference_as_password(self):
        encrypted = encrypt_secret('vault://kv/liema/model#api_key')
        with patch('auto_test.platform.enterprise.fetch_json', side_effect=RuntimeError('private diagnostic')), self.assertRaisesRegex(SecretEncryptionError, 'Vault'):
            decrypt_secret(encrypted)
        with self.assertRaises(ValueError):
            secure_url('http://vault.example.test')

    def test_oidc_http_routes_issue_normal_session_and_hide_login_when_disabled(self):
        router, service = create_identity_api(self.store)
        app = FastAPI()
        app.include_router(router)
        install_identity_guard(app, service)
        self.oidc.bind(self.discovery['issuer'], 'subject-1', self.user['id'])
        with TestClient(app) as client:
            self.assertTrue(client.get('/api/auth/status').json()['sso_enabled'])
            self.assertEqual(client.get('/api/identity/oidc-bindings').status_code, 401)
            with patch('auto_test.platform.enterprise.fetch_json', return_value=self.discovery):
                response = client.get('/api/auth/oidc/start', follow_redirects=False)
            self.assertEqual(response.status_code, 302)
            params = parse_qs(urlparse(response.headers['location']).query)
            def provider(url, **kwargs):
                if url.endswith('/token'):
                    return {'id_token': self.token(nonce=params['nonce'][0])}
                return self.jwks if url.endswith('/keys') else self.discovery
            with patch('auto_test.platform.enterprise.fetch_json', side_effect=provider):
                response = client.get('/api/auth/oidc/callback', params={'state': params['state'][0], 'code': 'code'}, follow_redirects=False)
            self.assertEqual(response.status_code, 303, response.text)
            self.assertIn('liema_session', response.cookies)
            self.assertIn('HttpOnly', response.headers['set-cookie'])
            self.assertEqual(response.headers['cache-control'], 'no-store')
            with patch.dict(os.environ, {'LIEMA_OIDC_ENABLED': 'false'}):
                self.assertEqual(client.get('/api/auth/oidc/start').status_code, 404)
