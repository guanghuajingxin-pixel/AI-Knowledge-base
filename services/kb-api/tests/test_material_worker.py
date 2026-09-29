"""Real Redis/Celery/PG/MinIO/ES test against a local synthetic model server.

MATERIAL_INTEGRATION=1 uv run python -m unittest discover -s tests -p test_material_worker.py
The worker consumes a unique queue, never the application's ingestion queue.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from celery import Celery
from PIL import Image, ImageDraw
from sqlalchemy import delete

from kb_common.config import get_settings
from kb_common.database import SessionLocal, engine
from kb_common.models import KnowledgeLibrary
from kb_common.material_models import ImageComponent, MaterialImage
from app.services import material_images as p, material_storage as storage


@unittest.skipUnless(os.getenv('MATERIAL_INTEGRATION') == '1', 'requires live infrastructure')
class MaterialWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_queue_and_removal(self):
        image = Image.new('RGBA', (120, 90), (0, 0, 0, 0))
        ImageDraw.Draw(image).ellipse((20, 10, 100, 80), fill=(160, 100, 50, 255))
        foreground = p.png(image)
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                raw = foreground if self.path == '/remove' else json.dumps({'data': [{'index': 0, 'embedding': [1]*64}]}).encode()
                self.send_response(200); self.send_header('Content-Type', 'image/png' if self.path == '/remove' else 'application/json')
                self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        unique = 'material_smoke_' + uuid.uuid4().hex
        address = f'http://127.0.0.1:{server.server_port}'
        queue = Celery('smoke', broker=get_settings().redis_url, backend=get_settings().redis_url)
        process = None; lib_id = None; embed_id = None; remover_id = None; image_id = None
        log = tempfile.TemporaryFile(mode='w+b')
        try:
            process = subprocess.Popen([sys.executable, '-m', 'celery', '-A', 'app.worker', 'worker',
                '-Q', unique, '--pool=solo', '--concurrency=1', '--loglevel=WARNING',
                '-n', unique+'@localhost', '--without-gossip', '--without-mingle', '--without-heartbeat'],
                cwd=Path(__file__).resolve().parents[1], stdout=log, stderr=log)
            async with SessionLocal() as s:
                component = ImageComponent(name=unique, kind='embedding', provider='http', endpoint=address+'/embed', model='synthetic-test-only', dimensions=64, api_key='')
                remover = ImageComponent(name=unique, kind='remover', provider='http', endpoint=address+'/remove', model='synthetic-test-only', dimensions=64, api_key='')
                s.add_all([component, remover]); await s.flush()
                embed_id, remover_id = component.id, remover.id
                lib = KnowledgeLibrary(name=unique, platform='material', library_type='material', dataset_id=uuid.uuid4().hex, description='', enabled=True, engine_config={})
                s.add(lib); await s.flush(); lib_id = lib.id
                image_id = uuid.uuid4()
                key = f'materials/{lib_id}/{image_id}/original'
                await storage.put(key, p.png(Image.new('RGB', (120, 90), 'white')), 'image/png')
                img = MaterialImage(id=image_id, library_id=lib_id, filename='synthetic.png', original_path=key,
                    content_hash='0'*64, embedding_id=component.id, remover_id=remover.id, preprocess=True,
                    pipeline_version=p.PIPELINE_VERSION)
                s.add(img); await s.commit(); run_id = img.run_id
            task = queue.send_task('process_material_image', args=[str(image_id), str(run_id)], queue=unique)
            for _ in range(60):
                async with SessionLocal() as s:
                    state = await s.get(MaterialImage, image_id)
                    status, message = state.status, state.message
                    if status == 'COMPLETED':
                        self.assertTrue(state.foreground_path)
                        self.assertTrue(state.standard_path)
                        embedding = await p.embed(await storage.get(state.standard_path), component)
                        matches = await storage.nearest(embed_id, lib_id, embedding, [state], 10)
                        self.assertEqual(matches[0][0], p.entry_id(state))
                        break
                    if status == 'FAILED': self.fail(message)
                if process.poll() is not None: self.fail('isolated worker exited unexpectedly')
                await asyncio.sleep(0.5)
            else:
                self.fail('worker did not complete within 30 seconds: '+status+' '+message)
            await asyncio.to_thread(task.forget)
        finally:
            if process:
                process.terminate()
                try: await asyncio.to_thread(process.wait, timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill(); await asyncio.to_thread(process.wait)
            await asyncio.to_thread(server.shutdown); server.server_close(); thread.join(timeout=2)
            if lib_id: await storage.remove_prefix(f'materials/{lib_id}/')
            async with SessionLocal() as s:
                if lib_id: await s.execute(delete(KnowledgeLibrary).where(KnowledgeLibrary.id == lib_id))
                if embed_id: await s.execute(delete(ImageComponent).where(ImageComponent.id.in_([embed_id, remover_id])))
                await s.commit()
            from kb_common.clients.es_client import es
            if embed_id: await es.options(ignore_status=[404]).indices.delete(index=p.index_name(embed_id))
            # Queue name is unique to this test; never purge the application queue.
            with queue.connection() as connection:
                connection.channel().queue_delete(unique)
            queue.close(); log.close(); await es.close(); await engine.dispose()


if __name__ == '__main__': unittest.main()
