"""Project-owned material libraries. Images never enter the text RAG pipeline."""
import asyncio
import hashlib
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, require_role
from kb_common.database import get_session
from kb_common.models import KnowledgeLibrary
from kb_common.material_models import ImageComponent, Material, MaterialImage
from app.services import material_images as p, material_storage as storage

router = APIRouter(prefix="/api/v1/material-libraries", tags=["material-libraries"], dependencies=[Depends(get_current_user)])
components_router = APIRouter(prefix="/api/v1/image-components", tags=["image-components"], dependencies=[Depends(get_current_user)])
editor = require_role("super_admin", "admin", "editor")
admin = require_role("super_admin", "admin")


def out(row, exclude=()):
    return jsonable_encoder({c.name: getattr(row, c.name) for c in row.__table__.columns if c.name not in exclude})


async def library(s, library_id, lock=False):
    q = select(KnowledgeLibrary).where(KnowledgeLibrary.id == library_id, KnowledgeLibrary.library_type == "material")
    row = (await s.execute(q.with_for_update() if lock else q)).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "物料库不存在")
    return row


async def material(s, library_id, material_id, lock=False):
    q = select(Material).where(Material.id == material_id, Material.library_id == library_id)
    row = (await s.execute(q.with_for_update() if lock else q)).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "物料不存在或不属于此物料库")
    return row


async def image_row(s, library_id, image_id, lock=False):
    await library(s, library_id)
    q = select(MaterialImage).where(MaterialImage.id == image_id, MaterialImage.library_id == library_id)
    row = (await s.execute(q.with_for_update() if lock else q)).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "图片不存在或不属于此物料库")
    return row


async def component(s, component_id, kind):
    row = await s.get(ImageComponent, component_id) if component_id else None
    if not row or row.kind != kind:
        raise HTTPException(422, "请选择有效的" + ("图像向量组件" if kind == "embedding" else "抠图组件"))
    return row


async def validate_config(s, config: p.LibraryConfig, required=False):
    if config.embedding_id or required:
        await component(s, config.embedding_id, "embedding")
    if config.remover_id or (required and config.preprocess):
        await component(s, config.remover_id, "remover")


async def read_upload(file):
    raw = await file.read(p.MAX_BYTES + 1)
    try:
        await asyncio.to_thread(p.decode_image, raw)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return raw


def read_crop(value):
    try:
        result = json.loads(value) if value else None
        if result is not None and (not isinstance(result, list) or len(result) != 4 or any(type(x) is not int for x in result)):
            raise ValueError()
        return result
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, "主体框格式无效") from exc


async def enqueue(s, image):
    from celery import Celery
    from kb_common.config import get_settings
    queue = Celery("material-producer", broker=get_settings().redis_url)
    try:
        await asyncio.to_thread(queue.send_task, "process_material_image",
            args=[str(image.id), str(image.run_id)], queue="ingestion", retry=False)
    except Exception:
        image.status, image.message = "FAILED", "任务队列不可用，请启动 Redis 和 ingestion worker 后重试"
        await s.commit()
    finally:
        queue.close()


@components_router.get("")
async def list_components(s: AsyncSession = Depends(get_session)):
    rows = (await s.execute(select(ImageComponent).order_by(ImageComponent.created_at.desc()))).scalars().all()
    return [{**out(r, {"api_key"}), "has_api_key": bool(r.api_key)} for r in rows]


@components_router.post("", dependencies=[Depends(admin)])
async def create_component(body: p.ComponentIn, s: AsyncSession = Depends(get_session)):
    row = ImageComponent(**body.model_dump())
    s.add(row)
    await s.commit()
    return {**out(row, {"api_key"}), "has_api_key": bool(row.api_key)}


class CredentialIn(BaseModel):
    api_key: str = Field(max_length=4000)


@components_router.patch("/{component_id}/credential", dependencies=[Depends(admin)])
async def update_credential(component_id: UUID, body: CredentialIn, s: AsyncSession = Depends(get_session)):
    row = await s.get(ImageComponent, component_id)
    if not row:
        raise HTTPException(404, "组件不存在")
    if row.provider == "dashscope" and not body.api_key.strip():
        raise HTTPException(422, "百炼 API Key 不能为空")
    row.api_key = body.api_key.strip()
    await s.commit()
    return {"ok": True}


@components_router.delete("/{component_id}", dependencies=[Depends(admin)])
async def delete_component(component_id: UUID, s: AsyncSession = Depends(get_session)):
    row = await s.get(ImageComponent, component_id)
    if not row:
        raise HTTPException(404, "组件不存在")
    libs = (await s.execute(select(KnowledgeLibrary).where(KnowledgeLibrary.library_type == "material"))).scalars().all()
    if any(str(component_id) in {r.engine_config.get("embedding_id"), r.engine_config.get("remover_id")} for r in libs):
        raise HTTPException(409, "组件已被物料库引用，请先修改库配置")
    try:
        await s.delete(row)
        await s.commit()
    except IntegrityError:
        await s.rollback()
        raise HTTPException(409, "组件已被图片引用，不能删除；更换模型请新增组件版本")
    return {"ok": True}


@components_router.post("/{component_id}/test-connection", dependencies=[Depends(admin)])
async def test_connection(component_id: UUID, s: AsyncSession = Depends(get_session)):
    """Exercise the saved credentials and model with a small, built-in probe."""
    import io
    from PIL import Image, ImageDraw
    row = await s.get(ImageComponent, component_id)
    if not row:
        raise HTTPException(404, "组件不存在")
    await s.commit()
    probe = Image.new("RGB", (256, 256), "white")
    ImageDraw.Draw(probe).ellipse((48, 48, 208, 208), fill="steelblue")
    buffer = io.BytesIO()
    probe.save(buffer, "PNG")
    start = time.monotonic()
    try:
        standard, _ = await p.prepare(buffer.getvalue(), row if row.kind == "remover" else None)
        vector = await p.embed(standard, row) if row.kind == "embedding" else None
        return {"ok": True, "dimensions": len(vector) if vector else None,
                "elapsed_ms": round((time.monotonic()-start)*1000)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@components_router.post("/{component_id}/test", dependencies=[Depends(admin)])
async def test_component(component_id: UUID, file: UploadFile = File(...), crop: str = Form(""), s: AsyncSession = Depends(get_session)):
    row = await s.get(ImageComponent, component_id)
    if not row:
        raise HTTPException(404, "组件不存在")
    raw = await read_upload(file)
    await s.commit()
    start = time.monotonic()
    try:
        standard, foreground = await p.prepare(raw, row if row.kind == "remover" else None, read_crop(crop))
        vector = await p.embed(standard, row) if row.kind == "embedding" else None
        return {"ok": True, "dimensions": len(vector) if vector else None,
                "elapsed_ms": round((time.monotonic()-start)*1000),
                "preview": p.data_uri(foreground, "image/png") if foreground else p.data_uri(standard)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("")
async def list_libraries(s: AsyncSession = Depends(get_session)):
    rows = (await s.execute(select(KnowledgeLibrary).where(KnowledgeLibrary.library_type == "material")
                           .order_by(KnowledgeLibrary.id.desc()))).scalars().all()
    return [out(row) for row in rows]


@router.post("", dependencies=[Depends(editor)])
async def create_library(body: p.LibraryIn, user=Depends(get_current_user), s: AsyncSession = Depends(get_session)):
    await validate_config(s, body.config)
    row = KnowledgeLibrary(name=body.name, description=body.description, enabled=body.enabled,
        platform="material", library_type="material", dataset_id=uuid4().hex,
        creator=user.username, engine_config=body.config.model_dump(mode="json"))
    s.add(row)
    await s.commit()
    return out(row)


@router.put("/{library_id}", dependencies=[Depends(editor)])
async def update_library(library_id: int, body: p.LibraryIn, s: AsyncSession = Depends(get_session)):
    row = await library(s, library_id, lock=True)
    await validate_config(s, body.config)
    row.name, row.description, row.enabled = body.name, body.description, body.enabled
    row.engine_config = body.config.model_dump(mode="json")
    await s.commit()
    await s.refresh(row)
    return out(row)


@router.delete("/{library_id}", dependencies=[Depends(editor)])
async def delete_library(library_id: int, s: AsyncSession = Depends(get_session)):
    row = await library(s, library_id, lock=True)
    count = await s.scalar(select(func.count()).select_from(MaterialImage).where(MaterialImage.library_id == library_id))
    count += await s.scalar(select(func.count()).select_from(Material).where(Material.library_id == library_id))
    if count:
        raise HTTPException(409, "请先删除库内图片和物料，再删除物料库")
    await s.delete(row)
    await s.commit()
    return {"ok": True}


@router.get("/{library_id}/materials")
async def list_materials(library_id: int, q: str = Query("", max_length=200), page: int = Query(1, ge=1),
                        page_size: int = Query(30, ge=1, le=100), s: AsyncSession = Depends(get_session)):
    await library(s, library_id)
    where = [Material.library_id == library_id]
    if q.strip():
        where.append(Material.name.icontains(q.strip(), autoescape=True) | Material.code.icontains(q.strip(), autoescape=True))
    total = await s.scalar(select(func.count()).select_from(Material).where(*where))
    rows = (await s.execute(select(Material).where(*where).order_by(Material.created_at.desc())
        .offset((page-1)*page_size).limit(page_size))).scalars().all()
    counts = dict((await s.execute(select(MaterialImage.material_id, func.count()).where(
        MaterialImage.material_id.in_([r.id for r in rows])).group_by(MaterialImage.material_id))).all())
    return {"items": [{**out(r), "image_count": counts.get(r.id, 0)} for r in rows], "total": total}


@router.get("/{library_id}/materials/{material_id}")
async def get_material(library_id: int, material_id: UUID, s: AsyncSession = Depends(get_session)):
    await library(s, library_id)
    return out(await material(s, library_id, material_id))


@router.post("/{library_id}/materials", dependencies=[Depends(editor)])
async def create_material(library_id: int, body: p.MaterialIn, s: AsyncSession = Depends(get_session)):
    await library(s, library_id, lock=True)
    row = Material(library_id=library_id, **body.model_dump())
    s.add(row)
    try:
        await s.commit()
    except IntegrityError:
        await s.rollback()
        raise HTTPException(409, "此物料库已存在相同料号")
    return out(row)


@router.put("/{library_id}/materials/{material_id}", dependencies=[Depends(editor)])
async def update_material(library_id: int, material_id: UUID, body: p.MaterialIn, s: AsyncSession = Depends(get_session)):
    await library(s, library_id)
    row = await material(s, library_id, material_id, lock=True)
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    try:
        await s.commit()
    except IntegrityError:
        await s.rollback()
        raise HTTPException(409, "此物料库已存在相同料号")
    return out(row)


@router.delete("/{library_id}/materials/{material_id}", dependencies=[Depends(editor)])
async def delete_material(library_id: int, material_id: UUID, s: AsyncSession = Depends(get_session)):
    await library(s, library_id, lock=True)
    row = await material(s, library_id, material_id, lock=True)
    if await s.scalar(select(func.count()).select_from(MaterialImage).where(MaterialImage.material_id == material_id)):
        raise HTTPException(409, "请先删除该物料的图片，再删除物料")
    await s.delete(row)
    await s.commit()
    return {"ok": True}


@router.get("/{library_id}/images")
async def list_images(library_id: int, material_id: UUID | None = None, unassigned: bool = False,
                      page: int = Query(1, ge=1), page_size: int = Query(30, ge=1, le=100), s: AsyncSession = Depends(get_session)):
    await library(s, library_id)
    where = [MaterialImage.library_id == library_id]
    if material_id:
        where.append(MaterialImage.material_id == material_id)
    if unassigned:
        where.append(MaterialImage.material_id.is_(None))
    total = await s.scalar(select(func.count()).select_from(MaterialImage).where(*where))
    rows = (await s.execute(select(MaterialImage).where(*where).order_by(MaterialImage.created_at.desc())
                           .offset((page-1)*page_size).limit(page_size))).scalars().all()
    return {"items": [out(r, {"original_path", "foreground_path", "standard_path"}) | {
        "has_standard": bool(r.standard_path), "has_foreground": bool(r.foreground_path)} for r in rows], "total": total}


async def check_view_limit(s, material_id, exclude=None):
    if material_id:
        where = [MaterialImage.material_id == material_id]
        if exclude:
            where.append(MaterialImage.id != exclude)
        if await s.scalar(select(func.count()).select_from(MaterialImage).where(*where)) >= 20:
            raise HTTPException(422, "每个物料最多 20 张图片，请删除重复视角后再上传")


@router.post("/{library_id}/images", dependencies=[Depends(editor)])
async def upload_image(library_id: int, file: UploadFile = File(...), material_id: UUID | None = Form(None),
                       preprocess: bool | None = Form(None), crop: str = Form(""), s: AsyncSession = Depends(get_session)):
    raw = await read_upload(file)
    box = read_crop(crop)
    try:
        p.crop_image(await asyncio.to_thread(p.decode_image, raw), box)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    lib = await library(s, library_id, lock=True)
    config = p.LibraryConfig(**lib.engine_config)
    if preprocess is not None:
        config.preprocess = preprocess
    await validate_config(s, config, required=True)
    if material_id:
        await material(s, library_id, material_id, lock=True)
    await check_view_limit(s, material_id)
    image_id = uuid4()
    key = f"materials/{library_id}/{image_id}/original"
    await storage.put(key, raw, file.content_type or "application/octet-stream")
    row = MaterialImage(id=image_id, library_id=library_id, material_id=material_id,
        filename=(file.filename or "图片")[:500], original_path=key, content_hash=hashlib.sha256(raw).hexdigest(),
        embedding_id=config.embedding_id, remover_id=config.remover_id if config.preprocess else None,
        preprocess=config.preprocess, crop=box, pipeline_version=p.PIPELINE_VERSION)
    s.add(row)
    await s.commit()
    await enqueue(s, row)
    await s.refresh(row)
    return out(row, {"original_path", "foreground_path", "standard_path"})


@router.get("/{library_id}/images/{image_id}/{variant}")
async def image_content(library_id: int, image_id: UUID, variant: str, s: AsyncSession = Depends(get_session)):
    row = await image_row(s, library_id, image_id)
    if variant not in {"original", "standard", "foreground"}:
        raise HTTPException(404, "图片版本不存在")
    path = getattr(row, variant + "_path")
    if not path:
        raise HTTPException(404, "图片尚未处理完成")
    await s.commit()
    raw = await storage.get(path)
    if variant == "original":
        # Re-encode for display: do not expose file paths, EXIF or active content.
        raw = await asyncio.to_thread(p.png, await asyncio.to_thread(p.decode_image, raw))
    return Response(raw, media_type="image/jpeg" if variant == "standard" else "image/png",
                    headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.patch("/{library_id}/images/{image_id}", dependencies=[Depends(editor)])
async def edit_image(library_id: int, image_id: UUID, body: p.ImageEdit, s: AsyncSession = Depends(get_session)):
    await library(s, library_id, lock=True)
    row = await image_row(s, library_id, image_id, lock=True)
    if "material_id" in body.model_fields_set:
        if body.material_id:
            await material(s, library_id, body.material_id, lock=True)
        await check_view_limit(s, body.material_id, exclude=row.id)
        row.material_id = body.material_id
    if body.enabled is not None:
        row.enabled = body.enabled
    await s.commit()
    return {"ok": True}


@router.post("/{library_id}/images/{image_id}/retry", dependencies=[Depends(editor)])
async def retry_image(library_id: int, image_id: UUID, body: p.RetryIn, s: AsyncSession = Depends(get_session)):
    lib = await library(s, library_id, lock=True)
    row = await image_row(s, library_id, image_id, lock=True)
    if row.status in {"PENDING", "PROCESSING", "INDEXING", "RETRYING"} and row.updated_at > datetime.utcnow()-timedelta(minutes=10):
        raise HTTPException(409, "图片正在处理，请稍后重试；任务中断超过 10 分钟可重新提交")
    config = p.LibraryConfig(**lib.engine_config) if body.use_library_config else p.LibraryConfig(
        embedding_id=row.embedding_id, remover_id=row.remover_id, preprocess=row.preprocess)
    if body.preprocess is not None:
        config.preprocess = body.preprocess
    await validate_config(s, config, required=True)
    crop = body.crop if "crop" in body.model_fields_set else row.crop
    try:
        p.crop_image(await asyncio.to_thread(p.decode_image, await storage.get(row.original_path)), crop)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    await storage.delete_vector(row)
    row.embedding_id, row.remover_id, row.preprocess = config.embedding_id, config.remover_id if config.preprocess else None, config.preprocess
    row.crop, row.run_id, row.status, row.message = crop, uuid4(), "PENDING", ""
    row.standard_path, row.foreground_path = None, None
    row.pipeline_version = p.PIPELINE_VERSION
    await s.commit()
    await enqueue(s, row)
    return {"ok": True, "status": row.status, "message": row.message}


@router.delete("/{library_id}/images/{image_id}", dependencies=[Depends(editor)])
async def delete_image(library_id: int, image_id: UUID, s: AsyncSession = Depends(get_session)):
    row = await image_row(s, library_id, image_id, lock=True)
    # Disable first, invalidate in-flight jobs, then clean external resources.
    row.enabled, row.run_id, row.status = False, uuid4(), "DELETING"
    await s.commit()
    try:
        await storage.delete_vector(row)
        await storage.remove_prefix(f"materials/{library_id}/{image_id}/")
    except Exception:
        raise HTTPException(503, "图片已停用，但存储清理未完成，请检查存储连接后再次删除")
    await s.delete(row)
    await s.commit()
    return {"ok": True}


@router.post("/{library_id}/search")
async def search_images(library_id: int, file: UploadFile = File(...), crop: str = Form(""),
                        category: str = Form("", max_length=120), specification: str = Form("", max_length=500),
                        top_k: int = Form(10, ge=1, le=50), s: AsyncSession = Depends(get_session)):
    start = time.monotonic()
    lib = await library(s, library_id)
    if not lib.enabled:
        raise HTTPException(409, "物料库已停用，无法执行检索")
    raw = await read_upload(file)
    box = read_crop(crop)
    where = [MaterialImage.library_id == library_id, MaterialImage.enabled.is_(True), MaterialImage.status == "COMPLETED",
             (MaterialImage.material_id.is_(None) | Material.enabled.is_(True))]
    if category.strip():
        where.append(Material.category == category.strip())
    if specification.strip():
        where.append(Material.specification.icontains(specification.strip(), autoescape=True))
    rows = (await s.execute(select(MaterialImage, Material).outerjoin(Material, Material.id == MaterialImage.material_id)
                           .where(*where).limit(60001))).all()
    if len(rows) > 60000:
        raise HTTPException(422, "检索范围超过一期上限，请通过分类或规格缩小范围")
    groups = defaultdict(list)
    mapping = {}
    for img, mat in rows:
        groups[p.group_key(img)].append(img)
        mapping[p.entry_id(img)] = (img, mat)
    if len(groups) > 8:
        raise HTTPException(422, "库内模型或处理版本超过 8 组，请统一图片配置并重新处理")
    profiles = {}
    for embedding_id, remover_id, version in groups:
        if version != p.PIPELINE_VERSION:
            raise HTTPException(409, "存在不兼容的图片处理版本，请重新处理后检索")
        profiles[embedding_id] = await component(s, embedding_id, "embedding")
        if remover_id:
            profiles[remover_id] = await component(s, remover_id, "remover")
    await s.commit()
    ranked, previews, cached = [], [], {}
    try:
        for (embedding_id, remover_id, version), images in groups.items():
            group_id = f"{embedding_id}:{remover_id}:{version}"
            if remover_id not in cached:
                cached[remover_id] = await p.prepare(raw, profiles.get(remover_id), box)
            standard, _ = cached[remover_id]
            vector = await p.embed(standard, profiles[embedding_id])
            neighbors = await storage.nearest(embedding_id, library_id, vector, images, top_k)
            hits = []
            for key, score in neighbors:
                if key not in mapping:
                    continue
                img, mat = mapping[key]
                hits.append({"image_id": str(img.id), "run_id": str(img.run_id),
                    "material_id": str(mat.id) if mat else None, "filename": img.filename,
                    "material": out(mat) if mat else None, "cosine_score": round(score, 5),
                    "component_name": profiles[embedding_id].name, "group_id": group_id})
            ranked.append(hits)
            previews.append({"component_name": profiles[embedding_id].name, "group_id": group_id, "preprocess": remover_id is not None,
                             "standard": p.data_uri(standard)})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, "图片检索服务异常，请检查 ES 与模型服务连接") from exc
    # Recheck after slow HTTP calls: disabled/deleted/rebuilt records may not escape.
    s.expire_all()
    lib = await library(s, library_id)
    if not lib.enabled:
        raise HTTPException(409, "物料库已停用")
    valid = (await s.execute(select(MaterialImage, Material).outerjoin(Material, Material.id == MaterialImage.material_id)
                            .where(*where))).all()
    current = {str(img.id): (img, mat) for img, mat in valid}
    checked = []
    for hits in ranked:
        group = []
        for hit in hits:
            now = current.get(hit["image_id"])
            if now and str(now[0].run_id) == hit["run_id"]:
                hit["material_id"] = str(now[1].id) if now[1] else None
                hit["material"] = out(now[1]) if now[1] else None
                group.append(hit)
        checked.append(group)
    return {"hits": p.aggregate(checked, top_k), "previews": previews,
            "elapsed_ms": round((time.monotonic()-start)*1000), "score_type": "rrf",
            "message": "" if rows else "暂无符合条件的已完成图片，请先上传并完成处理"}
