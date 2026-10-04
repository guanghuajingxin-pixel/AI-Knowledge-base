import asyncio
import time
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from jose import jwt, jwk

from kb_common import oidc
from kb_common.models import User, UserIdentity
from app.deps import get_current_user, get_principal
from app.routes import auth, users


class OidcTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(auth_provider='keycloak', oidc_issuer='https://sso.test/realms/jack',
            oidc_jwks_url='', oidc_audience='kb-api', oidc_portal_client_id='jack-portal', oidc_web_client_id='knowledge-web')
        self.settings = patch.object(oidc, 'get_settings', return_value=self.cfg)
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.pem = self.private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        public = self.private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.key = jwk.construct(public, algorithm='RS256').to_dict()
        self.key.update(kid='test-key', alg='RS256', use='sig')
        self.url = self.cfg.oidc_issuer + '/protocol/openid-connect/certs'
        oidc._keys[self.url] = (time.monotonic(), [self.key])
        self.addCleanup(oidc._keys.clear)
        self.claims = dict(iss=self.cfg.oidc_issuer, aud='kb-api', azp='jack-portal', sub=str(uuid4()),
            typ='Bearer', iat=int(time.time()), exp=int(time.time()) + 120, preferred_username='test',
            resource_access={'kb-api': {'roles': ['viewer']}})

    def token(self, **changes):
        return jwt.encode({**self.claims, **changes}, self.pem, algorithm='RS256', headers={'kid': 'test-key'})

    async def test_both_clients_validate_and_map_exact_audience_role(self):
        for client in ('jack-portal', 'knowledge-web'):
            claims = await oidc.verify_access_token(self.token(azp=client))
            self.assertEqual(oidc.role_from_claims(claims), 'viewer')

    async def test_rejects_wrong_issuer_audience_client_expiration_and_id_token(self):
        for changes in ({'iss': 'https://attacker/realms/jack'}, {'aud': 'other'}, {'azp': 'other'}, {'exp': 1}, {'typ': 'ID'}):
            with self.subTest(changes=changes), self.assertRaises(HTTPException) as error:
                await oidc.verify_access_token(self.token(**changes))
            self.assertEqual(error.exception.status_code, 401)

    async def test_local_token_cannot_bypass_sso(self):
        local = jwt.encode(self.claims, 'local-secret', algorithm='HS256')
        with self.assertRaises(HTTPException):
            await oidc.verify_access_token(local)

    async def test_machine_client_is_only_accepted_with_explicit_binding(self):
        token = self.token(azp='partner-search')
        claims = await oidc.verify_access_token(token, allowed_clients=('partner-search',))
        self.assertEqual(claims['azp'], 'partner-search')
        for allowed in (None, (), ('other-client',)):
            with self.assertRaises(HTTPException):
                await oidc.verify_access_token(token, allowed_clients=allowed)

    async def test_missing_expiration_rejected(self):
        claims = {**self.claims}
        del claims['exp']
        token = jwt.encode(claims, self.pem, algorithm='RS256', headers={'kid': 'test-key'})
        with self.assertRaises(HTTPException):
            await oidc.verify_access_token(token)

    async def test_realm_or_other_client_admin_does_not_grant_application_access(self):
        for resources in (None, 'invalid', {}, {'other': {'roles': ['super_admin']}}, {'kb-api': {'roles': 'super_admin'}}):
            with self.assertRaises(HTTPException) as error:
                oidc.role_from_claims({'realm_access': {'roles': ['super_admin']}, 'resource_access': resources})
            self.assertEqual(error.exception.status_code, 403)

    async def test_no_password_or_local_user_admin_bypass(self):
        session = SimpleNamespace(execute=AsyncMock(), get=AsyncMock())
        with self.assertRaises(HTTPException) as error:
            await auth.login(auth.LoginIn(username='test', password='test'), session)
        self.assertEqual(error.exception.status_code, 409)
        session.execute.assert_not_called()
        with self.assertRaises(HTTPException):
            await users.create_user(users.UserCreate(username='test', password='test'), None, None, session)
        with self.assertRaises(HTTPException):
            await auth.dingtalk_login(auth.DingtalkLoginIn(auth_code='test'), session)
        with self.assertRaises(HTTPException):
            await users.create_key(users.ApiKeyIn(name='test'), None, session)

    async def test_legacy_api_key_cannot_bypass_central_user_disable(self):
        with self.assertRaises(HTTPException):
            await get_principal(None, 'kb_test')

    async def test_no_credential_returns_401(self):
        with self.assertRaises(HTTPException) as error:
            await get_current_user(None)
        self.assertEqual(error.exception.status_code, 401)

    async def test_same_name_never_links_existing_account(self):
        result = lambda value: SimpleNamespace(scalar_one_or_none=lambda: value)
        session = SimpleNamespace(execute=AsyncMock(side_effect=[result(None), result(uuid4()), result(None)]), add=unittest.mock.Mock())
        @asynccontextmanager
        async def fake_session():
            yield session
        with patch.object(oidc, 'short_session', fake_session), self.assertRaises(HTTPException) as error:
            await oidc.resolve_oidc_user(self.claims)
        self.assertEqual(error.exception.status_code, 409)
        session.add.assert_not_called()

    async def test_explicit_binding_retains_user_id_and_uses_token_role(self):
        user_id = uuid4()
        user = User(id=user_id, username='existing', role='super_admin', is_active=True)
        result = SimpleNamespace(scalar_one_or_none=lambda: UserIdentity(user_id=user_id))
        session = SimpleNamespace(execute=AsyncMock(return_value=result), get=AsyncMock(return_value=user),
            commit=AsyncMock(), refresh=AsyncMock(), expunge=unittest.mock.Mock())
        @asynccontextmanager
        async def fake_session():
            yield session
        with patch.object(oidc, 'short_session', fake_session):
            resolved = await oidc.resolve_oidc_user(self.claims)
        self.assertEqual(resolved.id, user_id)
        self.assertEqual(resolved.role, 'viewer')
        self.assertEqual(resolved.username, 'existing')

    async def test_disabled_binding_stays_disabled(self):
        user = User(id=uuid4(), username='test', is_active=False)
        result = SimpleNamespace(scalar_one_or_none=lambda: UserIdentity(user_id=user.id))
        session = SimpleNamespace(execute=AsyncMock(return_value=result), get=AsyncMock(return_value=user))
        @asynccontextmanager
        async def fake_session():
            yield session
        with patch.object(oidc, 'short_session', fake_session), self.assertRaises(HTTPException):
            await oidc.resolve_oidc_user(self.claims)

    async def test_parallel_first_login_reuses_exact_identity(self):
        user = User(id=uuid4(), username='test', role='viewer', is_active=True)
        identity = UserIdentity(user_id=user.id)
        result = lambda value: SimpleNamespace(scalar_one_or_none=lambda: value)
        session = SimpleNamespace(execute=AsyncMock(side_effect=[result(None), result(user.id), result(identity)]),
            get=AsyncMock(return_value=user), commit=AsyncMock(), refresh=AsyncMock(), expunge=unittest.mock.Mock())
        @asynccontextmanager
        async def fake_session():
            yield session
        with patch.object(oidc, 'short_session', fake_session):
            resolved = await oidc.resolve_oidc_user(self.claims)
        self.assertEqual(resolved.id, user.id)

    async def test_unknown_signing_key_rejected_without_unbounded_fetch(self):
        token = jwt.encode(self.claims, self.pem, algorithm='RS256', headers={'kid': 'unknown'})
        with patch.object(oidc.httpx, 'AsyncClient') as client, self.assertRaises(HTTPException):
            await oidc.verify_access_token(token)
        client.assert_not_called()

    async def test_invalid_signature_rejected(self):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private = other.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        token = jwt.encode(self.claims, private, algorithm='RS256', headers={'kid': 'test-key'})
        with self.assertRaises(HTTPException):
            await oidc.verify_access_token(token)
