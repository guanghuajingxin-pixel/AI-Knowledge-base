import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from app.deps import get_current_user
from app.routes import platform_keys as routes
from app.services import platform_keys as service
from kb_common import platform_auth


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.remote = AsyncMock()
        @asynccontextmanager
        async def session():
            yield self.remote
        p = patch.object(service, 'admin_session', session)
        p.start()
        self.addCleanup(p.stop)
        self.id = uuid4()
        self.row = {'id': str(self.id), 'clientId': service.PREFIX + uuid4().hex,
            'name': 'test', 'enabled': True, 'secret': 'hidden',
            'attributes': {'platform.api.managed': service.MARKER}}

    async def test_create_authorizes_before_enabling(self):
        self.remote.call.side_effect = [[{'id': 'resource'}], {'id': 'role'}, None,
            [{'id': str(self.id)}], {'id': 'user'}, None, None, {'value': 'secret'}, None]
        with patch.object(service, 'configuration', return_value=SimpleNamespace(oidc_audience='kb-api')):
            result = await service.create_key('test', 'actor')
        calls = self.remote.call.call_args_list
        self.assertFalse(calls[2].kwargs['json']['enabled'])
        self.assertFalse(calls[2].kwargs['json']['fullScopeAllowed'])
        self.assertFalse(calls[2].kwargs['json']['standardFlowEnabled'])
        self.assertEqual(calls[5].args, ('POST', 'users/user/role-mappings/clients/resource'))
        self.assertEqual(calls[-1].kwargs['json'], {'enabled': True})
        self.assertTrue(result['raw_key'].endswith(':secret'))
        self.assertNotIn('secret', service.public(self.row))

    async def test_failed_creation_cleanup(self):
        self.remote.call.side_effect = [[{'id': 'resource'}], {'id': 'role'}, None,
            [{'id': str(self.id)}], HTTPException(503), None]
        with patch.object(service, 'configuration', return_value=SimpleNamespace(oidc_audience='kb-api')):
            with self.assertRaises(HTTPException):
                await service.create_key('test', 'actor')
        self.remote.call.assert_awaited_with('DELETE', f'clients/{self.id}')

    async def test_foreign_clients_protected(self):
        self.remote.call.return_value = {'clientId': 'knowledge-web', 'attributes': {}}
        for action in (service.update_key(self.id, enabled=False), service.delete_key(self.id)):
            with self.assertRaises(HTTPException) as error:
                await action
            self.assertEqual(error.exception.status_code, 404)
        self.assertTrue(all(c.args[0] == 'GET' for c in self.remote.call.call_args_list))

    async def test_disable_delete(self):
        self.remote.call.side_effect = [self.row, None, self.row, None]
        self.assertFalse((await service.update_key(self.id, enabled=False))['enabled'])
        await service.delete_key(self.id)
        self.remote.call.assert_awaited_with('DELETE', f'clients/{self.id}')

    async def test_list_paginates_without_secret(self):
        self.remote.call.side_effect = [[self.row] + [{'clientId': 'other'}] * 99, []]
        rows = await service.list_keys()
        self.assertEqual(len(rows), 1)
        self.assertNotIn('secret', rows[0])
        self.assertEqual(self.remote.call.call_args.kwargs['params']['first'], 100)

    async def test_managed_key_requires_signed_marker(self):
        import httpx
        cfg = SimpleNamespace(oidc_api_clients=[], oidc_issuer='https://sso/realms/jack', oidc_token_url='')
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = httpx.Response(200, json={'access_token': 'token'}, request=httpx.Request('POST', 'https://sso'))
        with patch.object(platform_auth, 'enabled', return_value=True), patch.object(platform_auth, 'oidc_config'), patch.object(platform_auth, 'get_settings', return_value=cfg), patch.object(platform_auth.httpx, 'AsyncClient', return_value=client), patch.object(platform_auth, 'verify_access_token', new_callable=AsyncMock) as verify:
            for claims in ({}, {'platform_api': 'true'}, {'platform_api': False}):
                verify.return_value = claims
                with self.assertRaises(HTTPException):
                    await platform_auth.authenticate_api_key(self.row['clientId'] + ':secret')
            verify.return_value = {'platform_api': True}
            self.assertEqual(await platform_auth.authenticate_api_key(self.row['clientId'] + ':secret'), {'platform_api': True})


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(routes.router)
        self.app.dependency_overrides[routes.get_session] = lambda: None
        self.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role='viewer', id=uuid4())
        self.client = TestClient(self.app)

    def test_all_mutations_admin_only(self):
        base = '/api/v1/platform/api-keys'
        for method, url, kwargs in [('GET', base, {}), ('POST', base, {'json': {'name': 'test'}}), ('PATCH', base + '/' + str(uuid4()), {'json': {'enabled': False}}), ('DELETE', base + '/' + str(uuid4()), {})]:
            self.assertEqual(self.client.request(method, url, **kwargs).status_code, 403)

    def test_create_no_cache_no_secret_in_audit(self):
        self.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role='admin', id=uuid4())
        row = {'id': str(uuid4()), 'client_id': 'test', 'name': 'test', 'enabled': True,
            'created_at': None, 'permissions': ['knowledge:retrieve'], 'raw_key': 'secret'}
        with patch.object(service, 'create_key', new_callable=AsyncMock, return_value=row), patch.object(routes, 'write_audit', new_callable=AsyncMock) as audit:
            r = self.client.post('/api/v1/platform/api-keys', json={'name': 'test'})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.headers['cache-control'], 'no-store')
        self.assertNotIn('secret', str(audit.call_args))
        self.assertEqual(r.json()['raw_key'], 'secret')

    def test_empty_and_long_names_rejected(self):
        self.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role='admin', id=uuid4())
        for name in ('', ' ', 'x' * 101):
            self.assertEqual(self.client.post('/api/v1/platform/api-keys', json={'name': name}).status_code, 422)
