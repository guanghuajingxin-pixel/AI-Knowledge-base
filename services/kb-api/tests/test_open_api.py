import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from kb_common import platform_auth
from app.routes import open_api


class PlatformAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_keycloak_exchange_and_no_cache(self):
        cfg = SimpleNamespace(oidc_api_clients=['integration'], oidc_issuer='https://sso/realms/jack', oidc_token_url='')
        response = httpx.Response(200, json={'access_token': 'signed-token'}, request=httpx.Request('POST', 'https://sso'))
        client = AsyncMock()
        client.post.return_value = response
        client.__aenter__.return_value = client
        with patch.object(platform_auth, 'enabled', return_value=True), patch.object(platform_auth, 'oidc_config'), patch.object(platform_auth, 'get_settings', return_value=cfg), patch.object(platform_auth.httpx, 'AsyncClient', return_value=client), patch.object(platform_auth, 'verify_access_token', new_callable=AsyncMock) as verify:
            for _ in range(2):
                await platform_auth.authenticate_api_key('integration:secret')
            self.assertEqual(client.post.await_count, 2)
            verify.assert_awaited_with('signed-token', allowed_clients=('integration',))
            for key in (None, 'kb_old', 'unknown:secret', 'integration:'):
                with self.assertRaises(HTTPException) as error:
                    await platform_auth.authenticate_api_key(key)
                self.assertEqual(error.exception.status_code, 401)
            client.post.return_value = httpx.Response(401, request=response.request)
            with self.assertRaises(HTTPException) as error:
                await platform_auth.authenticate_api_key('integration:revoked')
            self.assertEqual(error.exception.status_code, 401)
            client.post.side_effect = httpx.ConnectError('offline')
            with self.assertRaises(HTTPException) as error:
                await platform_auth.authenticate_api_key('integration:secret')
            self.assertEqual(error.exception.status_code, 503)

    async def test_roles_are_resource_specific(self):
        with patch.object(platform_auth, 'get_settings', return_value=SimpleNamespace(oidc_audience='kb-api')):
            for claims in ({}, {'realm_access': {'roles': ['knowledge:retrieve']}}, {'resource_access': {'other': {'roles': ['knowledge:retrieve']}}}):
                with self.assertRaises(HTTPException):
                    platform_auth.require_api_role(claims, 'knowledge:retrieve')
            platform_auth.require_api_role({'resource_access': {'kb-api': {'roles': ['knowledge:retrieve']}}}, 'knowledge:retrieve')


class RetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_selection_must_be_exact_and_enabled(self):
        from app.routes.knowledge_library import execute_retrieval
        session = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        session.execute.return_value = result
        with self.assertRaises(HTTPException) as error:
            await execute_retrieval(open_api.RetrieveIn(query='test', library_ids=[1]), session, public=True)
        self.assertEqual(error.exception.status_code, 403)
        self.assertIn('knowledge_libraries.enabled IS true', str(session.execute.call_args.args[0]))

    async def test_response_allowlist_and_masking_failure(self):
        result = {'hits': [{'library_id': 1, 'library_name': 'lib', 'content': 'secret', 'score': .9, 'internal_url': 'hidden'}], 'libraries': [{'library_id': 1, 'ok': True}], 'elapsed_ms': 10}
        body = open_api.RetrieveIn(query='test', library_ids=[1])
        with patch.object(open_api, 'execute_retrieval', new_callable=AsyncMock, return_value=result), patch.object(open_api.masking, 'load_mask_context', new_callable=AsyncMock, return_value=object()), patch.object(open_api.masking, 'mask_text', return_value=('masked', [])):
            out = await open_api.retrieve(body, {}, AsyncMock())
            self.assertEqual(out.hits[0].content, 'masked')
            self.assertNotIn('internal_url', out.hits[0].model_dump())
        with patch.object(open_api, 'execute_retrieval', new_callable=AsyncMock, return_value=result), patch.object(open_api.masking, 'load_mask_context', new_callable=AsyncMock, side_effect=RuntimeError):
            with self.assertRaises(HTTPException) as error:
                await open_api.retrieve(body, {}, AsyncMock())
            self.assertEqual(error.exception.status_code, 503)

    async def test_partial_and_total_failure(self):
        result = {'hits': [], 'libraries': [{'library_id': 1, 'ok': False}, {'library_id': 2, 'ok': True}], 'elapsed_ms': 1}
        body = open_api.RetrieveIn(query='test', library_ids=[1, 2])
        with patch.object(open_api, 'execute_retrieval', new_callable=AsyncMock, return_value=result), patch.object(open_api.masking, 'load_mask_context', new_callable=AsyncMock, return_value=None):
            out = await open_api.retrieve(body, {}, AsyncMock())
            self.assertTrue(out.partial)
            self.assertEqual(out.failed_library_ids, [1])
            result['libraries'][1]['ok'] = False
            with self.assertRaises(HTTPException) as error:
                await open_api.retrieve(body, {}, AsyncMock())
            self.assertEqual(error.exception.status_code, 502)

    def test_openapi_and_request_validation(self):
        app = FastAPI()
        app.include_router(open_api.router)
        schema = app.openapi()
        operation = schema['paths']['/api/openapi/v1/knowledge/retrieve']['post']
        self.assertEqual(operation['security'], [{'PlatformAPIKey': []}])
        app.dependency_overrides[open_api.retrieval_principal] = lambda: {}
        app.dependency_overrides[open_api.get_session] = lambda: None
        with TestClient(app) as client:
            for body in ({'query': ' ', 'library_ids': [1]}, {'query': 'test', 'library_ids': []}, {'query': 'test', 'library_ids': [1], 'top_k': 51}):
                self.assertEqual(client.post('/api/openapi/v1/knowledge/retrieve', json=body).status_code, 422)
