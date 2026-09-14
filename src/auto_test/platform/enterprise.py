"""Optional OIDC code login and external Vault KV v2 secret references.

References: openid.net/specs/openid-connect-core-1_0.html#IDTokenValidation;
rfc-editor.org/rfc/rfc7636; developer.hashicorp.com/vault/api-docs/secret/kv/kv-v2.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import time
from urllib.parse import urlencode, urlparse

import requests
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from auto_test.common.env import get_env


class OidcBindingInput(BaseModel):
    issuer: str = Field(min_length=1, max_length=500)
    subject: str = Field(min_length=1, max_length=255)
    user_id: str = Field(min_length=1, max_length=64)


def secure_url(url):
    parsed = urlparse(str(url))
    if parsed.username or parsed.password or parsed.fragment or not parsed.hostname:
        raise ValueError('企业服务地址无效')
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1', '::1'}):
        raise ValueError('企业服务必须使用 HTTPS（本机回环测试除外）')
    return str(url)


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('重复 JSON 字段')
        value[key] = item
    return value


def fetch_json(url, *, method='GET', **kwargs):
    secure_url(url)
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.request(method, url, timeout=(5, 15), allow_redirects=False, stream=True,
                                 verify=str(get_env('ENTERPRISE_CA_BUNDLE', '')).strip() or True, **kwargs) as response:
                if response.status_code != 200:
                    raise RuntimeError('企业服务返回异常状态')
                chunks, size = [], 0
                for chunk in response.iter_content(16384):
                    size += len(chunk)
                    if size > 1_000_000:
                        raise ValueError('企业服务响应过大')
                    chunks.append(chunk)
                value = json.loads(b''.join(chunks), object_pairs_hook=unique_object)
                if not isinstance(value, dict):
                    raise ValueError('企业服务响应无效')
                return value
    except Exception as exc:
        raise RuntimeError('企业服务访问或响应校验失败，请检查配置、证书与授权') from exc


def resolve_secret_reference(value):
    value = str(value or '')
    if not value.startswith('vault://'):
        return value
    match = re.fullmatch(r'vault://([A-Za-z0-9_-]+)/([A-Za-z0-9_/-]+)#([A-Za-z0-9_.-]+)', value)
    if not match or any(part in {'', '.', '..'} for part in match.group(2).split('/')):
        raise RuntimeError('Vault 引用格式应为 vault://挂载名/路径#字段')
    mount, path, field = match.groups()
    allowed = [item.strip().strip('/') for item in str(get_env('VAULT_ALLOWED_PREFIXES', '')).split(',') if item.strip()]
    full_path = mount + '/' + path
    if not any(full_path == prefix or full_path.startswith(prefix + '/') for prefix in allowed):
        raise RuntimeError('Vault 密钥路径不在允许范围内')
    address = secure_url(str(get_env('VAULT_ADDR', '')).strip().rstrip('/'))
    token = str(get_env('VAULT_TOKEN', '')).strip()
    if not token:
        raise RuntimeError('未配置 Vault 运行身份')
    headers = {'X-Vault-Token': token}
    namespace = str(get_env('VAULT_NAMESPACE', '')).strip()
    if namespace:
        headers['X-Vault-Namespace'] = namespace
    result = fetch_json(address + '/v1/' + mount + '/data/' + path, headers=headers)
    data = (result.get('data') or {}).get('data') or {}
    secret = data.get(field)
    if not isinstance(secret, str) or not secret:
        raise RuntimeError('Vault 密钥字段不存在或为空')
    return secret


def initialize_enterprise(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS oidc_login_states (
        state_hash VARCHAR(64) PRIMARY KEY, browser_hash VARCHAR(64) NOT NULL,
        payload_enc LONGTEXT NOT NULL, expires_at DOUBLE NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS oidc_identity_bindings (
        id VARCHAR(64) PRIMARY KEY, issuer VARCHAR(500) NOT NULL,
        subject VARCHAR(255) NOT NULL, user_id VARCHAR(64) NOT NULL,
        created_at DOUBLE NOT NULL)""")


def b64encode(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def b64decode(value):
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9_-]+', value):
        raise ValueError('Base64URL 格式无效')
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def verify_id_token(token, jwks, *, issuer, client_id, nonce, now=None):
    if not isinstance(token, str) or len(token) > 64000:
        raise ValueError('身份令牌无效')
    parts = token.split('.')
    if len(parts) != 3:
        raise ValueError('身份令牌结构无效')
    header, claims = [json.loads(b64decode(part), object_pairs_hook=unique_object) for part in parts[:2]]
    if not isinstance(header, dict) or not isinstance(claims, dict) or header.get('alg') != 'RS256' or header.get('crit'):
        raise ValueError('仅支持 RS256 签名的身份令牌')
    keys = [key for key in jwks.get('keys', []) if key.get('kid') == header.get('kid') and key.get('kty') == 'RSA' and key.get('use', 'sig') == 'sig' and key.get('alg', 'RS256') == 'RS256' and 'verify' in key.get('key_ops', ['verify'])]
    if len(keys) != 1:
        raise ValueError('身份签名密钥不存在或不唯一')
    key = keys[0]
    public = rsa.RSAPublicNumbers(int.from_bytes(b64decode(key['e']), 'big'), int.from_bytes(b64decode(key['n']), 'big')).public_key()
    if not 2048 <= public.key_size <= 8192:
        raise ValueError('身份签名密钥长度不支持')
    public.verify(b64decode(parts[2]), (parts[0] + '.' + parts[1]).encode('ascii'), padding.PKCS1v15(), hashes.SHA256())
    now = time.time() if now is None else now
    audience = claims.get('aud')
    audience = [audience] if isinstance(audience, str) else audience
    if claims.get('iss') != issuer or not isinstance(audience, list) or client_id not in audience:
        raise ValueError('身份令牌签发方或接收方不匹配')
    if (len(audience) > 1 or 'azp' in claims) and claims.get('azp') != client_id:
        raise ValueError('身份令牌授权方不匹配')
    for field in ('exp', 'iat'):
        if type(claims.get(field)) not in (int, float) or not math.isfinite(claims[field]):
            raise ValueError('身份令牌时间字段无效')
    if claims['exp'] <= now or claims['iat'] > now + 60 or claims['iat'] < now - 600:
        raise ValueError('身份令牌已过期或签发时间无效')
    if 'nbf' in claims and (type(claims['nbf']) not in (int, float) or not math.isfinite(claims['nbf']) or claims['nbf'] > now + 60):
        raise ValueError('身份令牌尚未生效')
    if not isinstance(claims.get('nonce'), str) or not hmac.compare_digest(claims['nonce'], nonce):
        raise ValueError('身份令牌随机数不匹配')
    if not isinstance(claims.get('sub'), str) or not 1 <= len(claims['sub']) <= 255:
        raise ValueError('身份主体无效')
    return claims


class OidcService:
    def __init__(self, store):
        self.store = store

    @property
    def enabled(self):
        return str(get_env('OIDC_ENABLED', 'false')).lower() in {'true', '1', 'yes'}

    def config(self):
        if not self.enabled:
            raise ValueError('单点登录未启用')
        issuer = secure_url(str(get_env('OIDC_ISSUER', '')).strip().rstrip('/'))
        redirect = secure_url(str(get_env('OIDC_REDIRECT_URI', '')).strip())
        if urlparse(issuer).query or urlparse(redirect).query or urlparse(redirect).path != '/api/auth/oidc/callback':
            raise ValueError('OIDC 地址配置无效')
        client_id = str(get_env('OIDC_CLIENT_ID', '')).strip()
        if not client_id:
            raise ValueError('未配置 OIDC 客户端')
        discovery = fetch_json(issuer + '/.well-known/openid-configuration')
        if discovery.get('issuer') != issuer:
            raise ValueError('OIDC 发现文档的签发方不匹配')
        for name in ('authorization_endpoint', 'token_endpoint', 'jwks_uri'):
            secure_url(discovery.get(name, ''))
            if urlparse(issuer).scheme == 'https' and urlparse(discovery[name]).scheme != 'https':
                raise ValueError('OIDC 端点不能降级为 HTTP')
        return issuer, redirect, client_id, discovery

    def begin(self):
        from auto_test.platform.secrets import encrypt_secret
        issuer, redirect, client_id, discovery = self.config()
        state, browser, verifier, nonce = [secrets.token_urlsafe(32) for _ in range(4)]
        now = time.time()
        with self.store._connection() as connection:
            self.store.lock_interface_dispatch(connection)
            connection.execute('DELETE FROM oidc_login_states WHERE expires_at<?', (now,))
            if connection.execute('SELECT COUNT(*) AS total FROM oidc_login_states').fetchone()['total'] >= 1000:
                raise ValueError('单点登录请求过多，请稍后重试')
            connection.execute('INSERT INTO oidc_login_states(state_hash,browser_hash,payload_enc,expires_at) VALUES(?,?,?,?)', (hashlib.sha256(state.encode()).hexdigest(), hashlib.sha256(browser.encode()).hexdigest(), encrypt_secret(json.dumps({'issuer': issuer, 'client_id': client_id, 'redirect': redirect, 'verifier': verifier, 'nonce': nonce})), now + 300))
        params = dict(response_type='code', scope='openid', client_id=client_id, redirect_uri=redirect, state=state, nonce=nonce, code_challenge=b64encode(hashlib.sha256(verifier.encode()).digest()), code_challenge_method='S256')
        separator = '&' if '?' in discovery['authorization_endpoint'] else '?'
        return discovery['authorization_endpoint'] + separator + urlencode(params), browser, urlparse(redirect).scheme == 'https'

    def complete(self, state, browser, code):
        from auto_test.platform.secrets import decrypt_secret
        if not all(isinstance(value, str) and 1 <= len(value) <= 4096 for value in (state, browser, code)):
            raise ValueError('单点登录回调参数无效')
        state_hash, browser_hash = [hashlib.sha256(value.encode()).hexdigest() for value in (state, browser)]
        with self.store._connection() as connection:
            row = connection.execute('SELECT payload_enc FROM oidc_login_states WHERE state_hash=? AND browser_hash=? AND expires_at>?', (state_hash, browser_hash, time.time())).fetchone()
            if not row or not connection.execute('DELETE FROM oidc_login_states WHERE state_hash=? AND browser_hash=?', (state_hash, browser_hash)).rowcount:
                raise ValueError('单点登录请求已失效，请重新发起')
        pending = json.loads(decrypt_secret(row['payload_enc']))
        issuer, redirect, client_id, discovery = self.config()
        if (issuer, redirect, client_id) != (pending['issuer'], pending['redirect'], pending['client_id']):
            raise ValueError('单点登录配置已变化，请重新发起')
        client_secret = resolve_secret_reference(str(get_env('OIDC_CLIENT_SECRET', '')).strip())
        auth = (client_id, client_secret) if client_secret else None
        response = fetch_json(discovery['token_endpoint'], method='POST', auth=auth, data=dict(grant_type='authorization_code', client_id=client_id, code=code, redirect_uri=redirect, code_verifier=pending['verifier']))
        claims = verify_id_token(response.get('id_token'), fetch_json(discovery['jwks_uri']), issuer=issuer, client_id=client_id, nonce=pending['nonce'])
        identifier = hashlib.sha256((issuer + '\0' + claims['sub']).encode()).hexdigest()
        with self.store._connection() as connection:
            binding = connection.execute('SELECT user_id FROM oidc_identity_bindings WHERE id=?', (identifier,)).fetchone()
        user = self.store.get_user(binding['user_id']) if binding else None
        if not user or not user.get('is_active'):
            raise ValueError('企业身份尚未绑定平台账号，或账号已停用')
        return user

    def bind(self, issuer, subject, user_id):
        issuer = secure_url(issuer.rstrip('/'))
        if not 1 <= len(subject) <= 255 or len(issuer) > 500 or not self.store.get_user(user_id):
            raise ValueError('身份绑定信息无效')
        identifier = hashlib.sha256((issuer + '\0' + subject).encode()).hexdigest()
        with self.store._connection() as connection:
            # Rebinding requires explicit removal; never silently link another account.
            existing = connection.execute('SELECT user_id FROM oidc_identity_bindings WHERE id=?', (identifier,)).fetchone()
            if existing:
                raise ValueError('该企业身份已绑定，请先删除原绑定')
            connection.execute('INSERT INTO oidc_identity_bindings(id,issuer,subject,user_id,created_at) VALUES(?,?,?,?,?)', (identifier, issuer, subject, user_id, time.time()))
        return {'id': identifier, 'issuer': issuer, 'subject': subject, 'user_id': user_id}


def register_oidc_routes(router, service):
    from fastapi.responses import RedirectResponse
    from auto_test.platform.identity import _require_superuser, _set_session_cookie
    oidc = OidcService(service.store)

    @router.get('/auth/oidc/start')
    def start():
        if service.setup_required or not oidc.enabled:
            raise HTTPException(404, '单点登录尚不可用')
        try:
            url, cookie, secure = oidc.begin()
        except Exception as exc:
            raise HTTPException(503, '单点登录服务暂不可用，请联系管理员检查配置') from exc
        response = RedirectResponse(url, 302, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
        response.set_cookie('liema_oidc_state', cookie, max_age=300, httponly=True, secure=secure, samesite='lax', path='/api/auth/oidc')
        return response

    @router.get('/auth/oidc/callback')
    def callback(request: Request, state: str = '', code: str = ''):
        try:
            user = oidc.complete(state, request.cookies.get('liema_oidc_state', ''), code)
            token, _ = service.create_session(user['id'])
        except Exception as exc:
            service.audit(request, actor_user_id=None, project_id=None, action='identity.oidc.login', outcome='denied', detail={'reason': 'verification_failed'})
            raise HTTPException(401, '单点登录验证失败或账号未绑定，请重新登录或联系管理员', headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'}) from exc
        response = RedirectResponse('/', 303, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
        _set_session_cookie(response, token, secure=True if urlparse(str(get_env('OIDC_REDIRECT_URI', ''))).scheme == 'https' else None)
        response.delete_cookie('liema_oidc_state', path='/api/auth/oidc')
        service.audit(request, actor_user_id=user['id'], project_id=None, action='identity.oidc.login', outcome='success', detail={})
        return response

    @router.get('/identity/oidc-bindings')
    def bindings(request: Request):
        _require_superuser(request)
        with service.store._connection() as connection:
            return {'issuer': str(get_env('OIDC_ISSUER', '')), 'bindings': [dict(row) for row in connection.execute('SELECT b.*,u.username FROM oidc_identity_bindings b JOIN users u ON u.id=b.user_id ORDER BY b.created_at DESC LIMIT 500').fetchall()]}

    @router.post('/identity/oidc-bindings', status_code=201)
    def bind(payload: OidcBindingInput, request: Request):
        _require_superuser(request)
        try:
            result = oidc.bind(payload.issuer, payload.subject, payload.user_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        service.audit(request, actor_user_id=request.state.identity['user']['id'], project_id=None, action='identity.oidc.bind', target_type='user', target_id=payload.user_id, detail={})
        return result

    @router.delete('/identity/oidc-bindings/{identifier}')
    def unbind(identifier: str, request: Request):
        _require_superuser(request)
        with service.store._connection() as connection:
            row = connection.execute('SELECT user_id FROM oidc_identity_bindings WHERE id=?', (identifier,)).fetchone()
            deleted = connection.execute('DELETE FROM oidc_identity_bindings WHERE id=?', (identifier,)).rowcount
            if row:
                connection.execute('DELETE FROM auth_sessions WHERE user_id=?', (row['user_id'],))
        if not deleted:
            raise HTTPException(404, '绑定不存在')
        service.audit(request, actor_user_id=request.state.identity['user']['id'], project_id=None, action='identity.oidc.unbind', target_type='oidc_binding', target_id=identifier, detail={})
        return {'deleted': True}
