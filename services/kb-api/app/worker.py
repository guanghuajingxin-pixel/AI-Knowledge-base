"""Celery 异步采集流水线：PENDING -> PARSING [MinerU] -> INDEXING
[chunk+embed+ES via index_document] -> COMPLETED|FAILED.

`process_document` is a Celery task invoked via `.delay(doc_id)` from
`app.services.ingestion.upload_document` (and the /reprocess route). The
Celery app connects to Redis lazily - importing this module from the api
process is safe (no broker connection on import).

NOTE on the persistent event loop: the brief used `asyncio.run(_run(...))`
per task. That closes the loop after each task, but the module-level async
pools in kb_common (asyncpg engine, AsyncElasticsearch) bind their
connections to the loop that first uses them. Task 2 then fails with
`RuntimeError: ... Future attached to a different loop`. We keep a single
persistent loop per worker process so connections stay valid across tasks.
This is the standard fix for asyncpg/AsyncElasticsearch in a Celery worker."""

import asyncio
from io import BytesIO

from celery import Celery
from sqlalchemy import select, delete

from kb_common.config import get_settings
from kb_common.database import SessionLocal
from kb_common.models import Document, KnowledgeBase, Setting, Segment
from kb_common.clients import minio_client, mineru_client
from kb_common.rag.indexer import index_document

s = get_settings()
celery_app = Celery("kb", broker=s.redis_url, backend=s.redis_url)
# task_track_started/acks_late/default_queue are valid global config keys.
# NOTE: autoretry_for/retry_backoff/retry_kwargs are NOT global config keys
# (setting them via conf.update is a no-op) - they must be passed on the
# @task decorator below, which is the only mechanism Celery reads them from.
celery_app.conf.update(task_track_started=True, task_acks_late=True,
                       task_default_queue="ingestion")

# Persistent event loop for this worker process (see module docstring).
_loop: asyncio.AbstractEventLoop | None = None


def _run_material_async(coro):
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    return _loop.run_until_complete(coro)


from app.services.material_jobs import install as _install_material_jobs
process_material_image = _install_material_jobs(celery_app, _run_material_async)


async def _setting(session, key, default=""):
    row = (await session.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
    return (row.value if row else None) or default or getattr(s, key)


@celery_app.task(name="process_document", bind=True,
                 autoretry_for=(Exception,), retry_backoff=True,
                 retry_kwargs={"max_retries": 3})
def process_document(self, doc_id: str):
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    return _loop.run_until_complete(_run(doc_id))


async def _run(doc_id: str):
    async with SessionLocal() as s:
        doc = await s.get(Document, doc_id)
        if not doc:
            return
        kb = await s.get(KnowledgeBase, doc.kb_id)
        try:
            # 1. PARSING：取原文件 -> MinerU 解析 -> 存 parsed
            doc.status = "PARSING"
            await s.commit()
            # NOTE: minio.get_object returns an HTTP response stream; it does
            # NOT accept a target buffer (passing one would be interpreted as
            # `offset` and error). Read the stream, then close/release conn.
            resp = minio_client.minio.get_object(minio_client.RAW, doc.storage_path)
            try:
                raw = resp.read()
            finally:
                resp.close()
                resp.release_conn()
            api_key = await _setting(s, "mineru_api_key")
            parsed = await mineru_client.parse(raw, doc.original_filename, api_key=api_key)
            markdown = parsed["markdown"]
            pkey = f"{kb.id}/{doc.id}.md"
            minio_client.minio.put_object(minio_client.PARSED, pkey,
                                          BytesIO(markdown.encode()), len(markdown))
            doc.parsed_path = pkey

            # 2-4. CHUNKING/EMBEDDING/INDEXING：index_document 内完成（含 embed）
            doc.status = "INDEXING"
            await s.commit()
            # 重处理时先清除旧 segments，避免 PG 重复累积
            # （ES 由 {doc.id}_{chunk_index} 幂等覆盖，PG segments 每次生成新 UUID 非幂等）
            await s.execute(delete(Segment).where(Segment.document_id == doc.id))
            n = await index_document(s, kb, doc, markdown)

            doc.chunk_count = n
            doc.status = "COMPLETED"
            doc.error_message = None
            await s.commit()
        except Exception as e:
            doc.status = "FAILED"
            doc.error_message = str(e)[:500]
            await s.commit()
            raise  # 触发 Celery 重试（autoretry）
