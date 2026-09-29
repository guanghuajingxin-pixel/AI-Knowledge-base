import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from PIL import Image, ImageDraw
from pydantic import ValidationError

from app.services import material_images as p


def fixture_image():
    img = Image.new("RGBA", (240, 180), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle((40, 30, 200, 150), fill=(100, 120, 140, 255))
    draw.ellipse((100, 60, 140, 100), fill=(0, 0, 0, 0))
    return img


class ImageRulesTests(unittest.TestCase):
    def test_opaque_removal_and_empty_mask_rejected(self):
        for image in [Image.new("RGBA", (100, 100), "white"), Image.new("RGBA", (100, 100), (0, 0, 0, 0))]:
            with self.assertRaises(ValueError):
                p.standardize(image, removed=True)

    def test_holes_and_aspect_ratio_preserved_on_neutral_canvas(self):
        standard = Image.open(io.BytesIO(p.standardize(fixture_image(), removed=True)))
        self.assertEqual(standard.size, (1024, 1024))
        self.assertTrue(all(abs(c-245) < 3 for c in standard.getpixel((0, 0))))
        # Interior transparent hole stays background, never filled with material color.
        self.assertTrue(all(abs(c-245) < 3 for c in standard.getpixel((512, 502))))
        self.assertTrue(standard.getpixel((180, 250))[0] < 150)

    def test_crop_bounds_and_exif_rotation(self):
        img = Image.new("RGB", (80, 40), "red")
        exif = img.getexif(); exif[274] = 6
        stream = io.BytesIO(); img.save(stream, "JPEG", exif=exif)
        decoded = p.decode_image(stream.getvalue())
        self.assertEqual(decoded.size, (40, 80))
        self.assertEqual(p.crop_image(decoded, [0, 0, 20, 60]).size, (20, 60))
        for crop in [[0, 0, 100, 100], [0, 0, 1, 1], [1, 2, 3], [0, 0, 16.0, 20]]:
            with self.assertRaises(ValueError): p.crop_image(decoded, crop)

    def test_invalid_and_oversized_upload(self):
        with self.assertRaises(ValueError): p.decode_image(b"<svg />")
        with self.assertRaises(ValueError): p.decode_image(b"x"*(p.MAX_BYTES+1))

    def test_nonfinite_or_zero_embedding_rejected(self):
        for vector in [[0]*64, [float('nan')]*64, [float('inf')]*64, [True]*64, [1]*63]:
            with self.assertRaises(ValueError): p.validate_vector(vector, 64)
        self.assertAlmostEqual(sum(x*x for x in p.validate_vector([2]*64, 64)), 1)

    def test_many_views_have_one_vote(self):
        hit = lambda image, material: {"image_id": image, "material_id": material}
        output = p.aggregate([[hit('a1', 'a'), hit('a2', 'a'), hit('b1', 'b')], [hit('b2', 'b'), hit('a3', 'a')]], 10)
        self.assertEqual(len(output), 2)
        self.assertAlmostEqual(output[0]['rank_score'], output[1]['rank_score'])

    def test_different_preprocessing_is_separate_group(self):
        x = SimpleNamespace(embedding_id='e', remover_id='r', preprocess=True, pipeline_version='image-v1')
        y = SimpleNamespace(embedding_id='e', remover_id='r', preprocess=False, pipeline_version='image-v1')
        self.assertNotEqual(p.group_key(x), p.group_key(y))

    def test_configuration_validation(self):
        valid = dict(name='百炼', kind='embedding', provider='dashscope', endpoint=p.DASHSCOPE_ENDPOINT,
                     model='multimodal-embedding-v1', dimensions=1024, api_key='test-secret')
        self.assertEqual(p.ComponentIn(**valid).dimensions, 1024)
        for patch_ in [dict(dimensions=768), dict(api_key=''), dict(endpoint='https://other.example/api'), dict(model='bge-m3')]:
            with self.assertRaises(ValidationError): p.ComponentIn(**(valid | patch_))
        with self.assertRaises(ValidationError): p.MaterialIn(code=' ', name='物料')


class ModelContractsTests(unittest.IsolatedAsyncioTestCase):
    async def test_component_writes_and_upload_require_privilege(self):
        from app.routes import material_library as routes
        from app.deps import get_current_user
        app = FastAPI(); app.include_router(routes.router); app.include_router(routes.components_router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/api/v1/material-libraries')
            self.assertEqual(response.status_code, 401)
            app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role='viewer')
            response = await client.post('/api/v1/material-libraries', json={'name': 'unauthorized'})
            self.assertEqual(response.status_code, 403)
            response = await client.post('/api/v1/image-components', json={})
            self.assertEqual(response.status_code, 403)

    async def test_gzip_model_response_is_decoded_once(self):
        import gzip
        import json
        component = SimpleNamespace(api_key="secret", endpoint="https://model.example/embed")
        payload = {"output": {"embeddings": [{"embedding": [1.0] * 64}]}}
        transport = httpx.MockTransport(lambda request: httpx.Response(200,
            headers={"content-encoding": "gzip", "content-type": "application/json"},
            content=gzip.compress(json.dumps(payload).encode())))
        original = httpx.AsyncClient
        with patch.object(p.httpx, "AsyncClient", side_effect=lambda **kw: original(transport=transport, **kw)):
            result = await p.request_component(component, json={})
        self.assertEqual(result.json(), payload)
        self.assertNotIn("content-encoding", result.headers)

    async def test_connection_uses_saved_component_and_safe_errors(self):
        from app.routes import material_library as routes
        from fastapi import HTTPException
        component = SimpleNamespace(kind="embedding")
        session = AsyncMock(); session.get.return_value = component
        with patch.object(p, "embed", new=AsyncMock(return_value=[1.0]*1024)) as embed:
            result = await routes.test_connection("id", session)
            self.assertEqual(result["dimensions"], 1024)
            self.assertTrue(result["ok"])
            self.assertIs(embed.call_args.args[1], component)
        with patch.object(p, "embed", new=AsyncMock(side_effect=ValueError("模型服务鉴权失败"))):
            with self.assertRaises(HTTPException) as error:
                await routes.test_connection("id", session)
            self.assertEqual(error.exception.status_code, 422)
        session.get.return_value = None
        with self.assertRaises(HTTPException) as error:
            await routes.test_connection("missing", session)
        self.assertEqual(error.exception.status_code, 404)

    async def test_queue_failure_is_visible_and_retryable(self):
        from app.routes import material_library as routes
        from unittest.mock import Mock
        queue = Mock(); queue.send_task.side_effect = RuntimeError('broker unreachable')
        row = SimpleNamespace(id='image', run_id='run', status='PENDING', message='')
        session = AsyncMock()
        with patch('celery.Celery', return_value=queue):
            await routes.enqueue(session, row)
        self.assertEqual(row.status, 'FAILED')
        self.assertIn('任务队列不可用', row.message)
        session.commit.assert_awaited_once()

    async def test_dashscope_wire_contract(self):
        cfg = SimpleNamespace(provider='dashscope', model='multimodal-embedding-v1', dimensions=1024)
        response = httpx.Response(200, json={'output': {'embeddings': [{'index': 0, 'embedding': [1]*1024}]}})
        with patch.object(p, 'request_component', AsyncMock(return_value=response)) as request:
            vector = await p.embed(b'jpeg', cfg)
            body = request.call_args.kwargs['json']
            self.assertTrue(body['input']['contents'][0]['image'].startswith('data:image/jpeg;base64,'))
            self.assertNotIn('parameters', body)
            self.assertEqual(len(vector), 1024)

    async def test_qwen_dimensions_are_provider_parameters(self):
        cfg = SimpleNamespace(provider='dashscope', model='qwen3-vl-embedding', dimensions=512)
        with patch.object(p, 'request_component', AsyncMock(return_value=httpx.Response(200, json={'output': {'embeddings': [{'embedding': [1]*512}]}}))) as request:
            await p.embed(b'jpeg', cfg)
            self.assertEqual(request.call_args.kwargs['json']['parameters'], {'dimension': 512})

    async def test_remover_requires_same_size_and_alpha(self):
        cfg = SimpleNamespace(model='BiRefNet')
        with patch.object(p, 'request_component', AsyncMock(return_value=httpx.Response(200, content=p.png(Image.new('RGBA', (20, 20), 'red'))))):
            with self.assertRaisesRegex(ValueError, '同尺寸'):
                await p.remove_background(fixture_image(), cfg)

    async def test_no_silent_fallback_on_remover_error(self):
        with patch.object(p, 'remove_background', AsyncMock(side_effect=ValueError('抠图失败'))):
            with self.assertRaisesRegex(ValueError, '抠图失败'):
                await p.prepare(p.png(fixture_image()), SimpleNamespace())

    async def test_auth_errors_redacted_and_redirects_not_followed(self):
        original = httpx.AsyncClient
        for status in (401, 403, 302, 500):
            transport = httpx.MockTransport(lambda req: httpx.Response(status, text='secret-token; internal endpoint'))
            with patch.object(p.httpx, 'AsyncClient', side_effect=lambda **kw: original(transport=transport, **kw)):
                with self.assertRaises(ValueError) as error:
                    await p.request_component(SimpleNamespace(api_key='secret-token', endpoint='http://component'), json={})
                self.assertNotIn('secret-token', str(error.exception))


if __name__ == '__main__': unittest.main()
