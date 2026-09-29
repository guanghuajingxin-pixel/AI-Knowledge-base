"""Opt-in real PG/MinIO/ES smoke tests, only the model HTTP boundary is mocked.

Run MATERIAL_INTEGRATION=1 uv run python -m unittest discover -s tests -p test_material_integration.py
Creates disposable, uniquely named libraries and removes its own fixtures.
"""
import io
import os
import uuid
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from PIL import Image
from sqlalchemy import select, delete

from app.deps import get_current_user
from app.routes import material_library as routes
from app.services import material_images as p, material_jobs as jobs, material_storage as storage
from kb_common.database import SessionLocal, engine
from kb_common.material_models import ImageComponent, MaterialImage
from kb_common.models import KnowledgeLibrary


@unittest.skipUnless(os.getenv('MATERIAL_INTEGRATION') == '1', 'requires disposable infrastructure fixtures')
class MaterialIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_lifecycle(self):
        app = FastAPI()
        app.include_router(routes.router); app.include_router(routes.components_router)
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role='admin', username='material-test')
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
        component_id = None; libraries = []; images = []
        marker = 'material-test-' + uuid.uuid4().hex[:10]
        raw = p.png(Image.new('RGB', (120, 90), (150, 100, 50)))
        async def call(method, url, expected=200, **kw):
            response = await client.request(method, url, **kw)
            self.assertEqual(response.status_code, expected, response.text[:1000])
            return response.json()
        try:
            cfg = await call('POST', '/api/v1/image-components', json={
                'name': marker, 'kind': 'embedding', 'provider': 'http', 'endpoint': 'http://mock.invalid/embed',
                'model': 'test-fixture-not-a-real-model', 'dimensions': 64, 'api_key': 'never-return-this'})
            component_id = cfg['id']; self.assertNotIn('api_key', cfg)
            list_response = await call('GET', '/api/v1/image-components')
            self.assertNotIn('never-return-this', str(list_response))
            for n in range(2):
                lib = await call('POST', '/api/v1/material-libraries', json={
                    'name': marker+str(n), 'config': {'embedding_id': component_id, 'preprocess': False}})
                libraries.append(lib['id'])
            base = f'/api/v1/material-libraries/{libraries[0]}'
            mat = await call('POST', base+'/materials', json={'code': 'SKU-1', 'name': '很长的中文物料名称用于检查列表不会挤压主要信息', 'category': '测试零件', 'specification': '20mm'})
            await call('POST', base+'/materials', expected=409, json={'code': 'SKU-1', 'name': '重复料号'})
            async def response_for_embedding(component, **kwargs):
                return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1.0]*64}]})
            with patch.object(routes, 'enqueue', AsyncMock()), patch.object(p, 'request_component', side_effect=response_for_embedding) as embedding:
                for _ in range(2):
                    img = await call('POST', base+'/images', data={'material_id': mat['id'], 'preprocess': 'false'}, files={'file': ('fixture.png', raw, 'image/png')})
                    images.append((libraries[0], img['id']))
                    await jobs.process(img['id'], img['run_id'])
                    before = embedding.call_count
                    await jobs.process(img['id'], img['run_id'])
                    self.assertEqual(embedding.call_count, before, 'completed jobs must be idempotent')
                image_list = await call('GET', base+'/images')
                self.assertTrue(all(i['status'] == 'COMPLETED' and i['has_standard'] for i in image_list['items']))
                blob = await client.get(base+f"/images/{img['id']}/standard")
                self.assertEqual(blob.status_code, 200)
                self.assertEqual(Image.open(io.BytesIO(blob.content)).size, (1024, 1024))
                result = await call('POST', base+'/search', files={'file': ('query.png', raw, 'image/png')})
                self.assertEqual(len(result['hits']), 1, 'multiple photos must collapse to one material')
                self.assertEqual(result['hits'][0]['material_id'], mat['id'])
                self.assertGreater(result['hits'][0]['cosine_score'], 0.99)
                self.assertNotIn('vector', result['hits'][0])
                async def disable_during_embedding(*_args):
                    async with SessionLocal() as session:
                        for _, image_id in images:
                            changed = await session.get(MaterialImage, uuid.UUID(image_id))
                            changed.enabled = False
                        await session.commit()
                    return [0.125]*64
                with patch.object(p, 'embed', side_effect=disable_during_embedding):
                    raced = await call('POST', base+'/search', files={'file': ('query.png', raw, 'image/png')})
                    self.assertEqual(raced['hits'], [], 'post-call validation must suppress newly disabled images')
                for _, image_id in images:
                    await call('PATCH', base+f'/images/{image_id}', json={'enabled': True})
                other = f'/api/v1/material-libraries/{libraries[1]}'
                await call('GET', other+f"/images/{img['id']}/standard", expected=404)
                await call('PATCH', other+f"/images/{img['id']}", expected=404, json={'enabled': False})
                none = await call('POST', base+'/search', data={'category': '其他'}, files={'file': ('q.png', raw, 'image/png')})
                self.assertEqual(none['hits'], [])
                for _, image_id in images:
                    await call('PATCH', base+f'/images/{image_id}', json={'enabled': False})
                none = await call('POST', base+'/search', files={'file': ('q.png', raw, 'image/png')})
                self.assertEqual(none['hits'], [])
                await call('PATCH', base+f"/images/{img['id']}", json={'enabled': True})
                await call('PUT', base+f"/materials/{mat['id']}", json={'code': 'SKU-1', 'name': '零件', 'enabled': False})
                none = await call('POST', base+'/search', files={'file': ('q.png', raw, 'image/png')})
                self.assertEqual(none['hits'], [])
                await call('PUT', base, json={'name': marker, 'enabled': False, 'config': {'embedding_id': component_id, 'preprocess': False}})
                await call('POST', base+'/search', expected=409, files={'file': ('q.png', raw, 'image/png')})
                await call('DELETE', '/api/v1/image-components/'+component_id, expected=409)
                # Actual delete cleans vector and object; other test fixtures are handled below.
                await call('DELETE', base+f"/images/{img['id']}")
                await call('GET', base+f"/images/{img['id']}/standard", expected=404)
        finally:
            from kb_common.clients.es_client import es
            for lib_id, image_id in images:
                await storage.remove_prefix(f'materials/{lib_id}/{image_id}/')
            async with SessionLocal() as s:
                if libraries: await s.execute(delete(KnowledgeLibrary).where(KnowledgeLibrary.id.in_(libraries)))
                if component_id: await s.execute(delete(ImageComponent).where(ImageComponent.id == uuid.UUID(component_id)))
                await s.commit()
            if component_id: await es.options(ignore_status=[404]).indices.delete(index=p.index_name(component_id))
            await client.aclose()
            await es.close(); await engine.dispose()


if __name__ == '__main__': unittest.main()
