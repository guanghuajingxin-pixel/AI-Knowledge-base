"""Durable Celery jobs with generation guards and per-image PostgreSQL locks."""
import asyncio
import hashlib
from uuid import UUID

from sqlalchemy import select, text, update
from kb_common.database import SessionLocal, engine
from kb_common.material_models import MaterialImage, ImageComponent
from app.services import material_images as pipeline, material_storage as storage


async def mark(image_id, run_id, **values):
    async with SessionLocal() as s:
        result = await s.execute(update(MaterialImage).where(
            MaterialImage.id == image_id, MaterialImage.run_id == run_id).values(**values))
        await s.commit()
        return result.rowcount > 0


async def process(image_id: str, run_id: str):
    image_id, run_id = UUID(image_id), UUID(run_id)
    lock = int.from_bytes(hashlib.sha256(image_id.bytes).digest()[:8], "big", signed=True)
    # Dedicated connection owns the session advisory lock across state commits.
    async with engine.connect() as connection:
        acquired = (await connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock})).scalar()
        if not acquired:
            raise ValueError("图片正在处理，请稍后重试")
        try:
            async with SessionLocal() as s:
                image = await s.get(MaterialImage, image_id)
                if not image or image.run_id != run_id or image.status == "COMPLETED":
                    return
                component = await s.get(ImageComponent, image.embedding_id)
                remover = await s.get(ImageComponent, image.remover_id) if image.preprocess else None
                if not component or (image.preprocess and not remover):
                    raise ValueError("图片组件缺失，请重新配置后重试")
            if not await mark(image_id, run_id, status="PROCESSING", message="正在预处理图片"):
                return
            prefix = f"materials/{image.library_id}/{image.id}/{run_id}"
            # Reuse valid preprocessing artifacts after a transient embedding failure.
            if image.standard_path:
                standard = await storage.get(image.standard_path)
            else:
                standard, foreground = await pipeline.prepare(await storage.get(image.original_path), remover, image.crop)
                standard_path = prefix + "/standard.jpg"
                foreground_path = prefix + "/foreground.png" if foreground else None
                await storage.put(standard_path, standard, "image/jpeg")
                if foreground:
                    await storage.put(foreground_path, foreground, "image/png")
                if not await mark(image_id, run_id, standard_path=standard_path, foreground_path=foreground_path):
                    await storage.remove_prefix(prefix + "/")
                    return
            await mark(image_id, run_id, status="INDEXING", message="正在生成图片向量")
            vector = await pipeline.embed(standard, component)
            await storage.index_image(image, vector)
            if not await mark(image_id, run_id, status="COMPLETED", message=""):
                # A deleted/restarted image must not leave a searchable old generation.
                from kb_common.clients.es_client import es
                await es.options(ignore_status=[404]).delete(index=pipeline.index_name(image.embedding_id), id=pipeline.entry_id(image))
        finally:
            await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock})


def install(celery_app, run_async):
    @celery_app.task(name="process_material_image", bind=True, max_retries=2,
                     acks_late=True, reject_on_worker_lost=True)
    def process_material_image(self, image_id, run_id):
        try:
            return run_async(process(image_id, run_id))
        except Exception as exc:
            # Only deliberately safe validation errors may reach the UI; never leak URLs/keys.
            message = str(exc)[:500] if isinstance(exc, ValueError) else "图片处理服务异常，请检查对象存储、ES 或模型服务后重试"
            retrying = self.request.retries < self.max_retries
            run_async(mark(UUID(image_id), UUID(run_id), status="RETRYING" if retrying else "FAILED", message=message))
            if retrying:
                raise self.retry(exc=RuntimeError(message), countdown=10*(self.request.retries+1))
            raise RuntimeError(message) from None
    return process_material_image
