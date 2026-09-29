"""Dify 知识库相关接口：列出/新建数据集、钉钉文档同步。"""
from fastapi import APIRouter, Depends, HTTPException
from typing import Any
from pydantic import BaseModel
import httpx
import asyncio
import hashlib
import time
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from kb_common.database import get_session
from kb_common.models import Setting
from kb_common.clients import dify_client, minio_client
from kb_common.clients.document_upload import max_upload_bytes, prepare_document
from kb_common.config import get_settings
from app.deps import require_role
from app.services.collection_history import track_transfer

router = APIRouter(prefix="/api/v1/dify", tags=["dify"])

async def _effective(s: AsyncSession, key: str) -> str:
    row = (await s.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
    return (row.value if row else None) or getattr(get_settings(), key)


def _apply_runtime_config(base_url: str, api_key: str) -> None:
    """运行时可能在 settings 表中覆盖了 dify 配置，写入 settings 以便 dify_client 读取。"""
    s_obj = get_settings()
    s_obj.dify_base_url = base_url
    s_obj.dify_api_key = api_key


async def _runtime_config(s: AsyncSession) -> tuple[str, str]:
    """读取并应用运行时 Dify 配置，返回 (base_url, api_key)。"""
    base_url = (await _effective(s, "dify_base_url")).strip()
    api_key = (await _effective(s, "dify_api_key")).strip()
    _apply_runtime_config(base_url, api_key)
    get_settings().dify_upload_max_mb = int(await _effective(s, "dify_upload_max_mb"))
    return base_url, api_key


def _require_config(base_url: str, api_key: str) -> None:
    if not base_url:
        raise HTTPException(400, "尚未配置 Dify 服务地址，请先在『Dify 链接配置』中填写并保存")
    if not api_key:
        raise HTTPException(400, "尚未配置 Dify API Key，请先在『Dify 链接配置』中填写并保存")


@router.get("/supported-extensions")
async def supported_extensions(dataset_id: str | None = None,
                               u=Depends(require_role("super_admin", "admin")),
                               s: AsyncSession = Depends(get_session)):
    """返回当前 Dify ETL 类型及其支持的文档扩展名白名单。

    Dify 内置 ETL 仅支持 13 种格式（无 pptx/doc/eml）；配置 ETL_TYPE=Unstructured
    后额外支持 pptx/doc/eml/msg/xml/epub。前端手动上传页据此校验/限制可选格式，
    避免上传后被 Dify 拒收（UnsupportedFileTypeError）。
    """
    from kb_common.clients.document_upload import UPLOAD_EXTENSIONS, supported_dify_extensions
    from kb_common.clients.dify_document import dataset_runtime
    get_settings().dify_upload_max_mb = int(await _effective(s, "dify_upload_max_mb"))
    etl_type = get_settings().dify_etl_type
    extensions = supported_dify_extensions(etl_type)
    if dataset_id:
        base_url, api_key = await _runtime_config(s)
        _require_config(base_url, api_key)
        dataset = await dify_client.get_dataset(dataset_id)
        if not dataset:
            raise HTTPException(502, "无法读取目标知识库支持的文件格式")
        if dataset_runtime(dataset) == "rag_pipeline":
            extensions = UPLOAD_EXTENSIONS
            etl_type = "知识流水线"
    return {
        "etl_type": etl_type,
        "extensions": sorted(extensions),
        "max_upload_bytes": max_upload_bytes(),
    }


# 数据集列表缓存：实时查 Dify 需要 172ms~4.5s，而「本地上传」「知识加工-知识图谱」
# 页每次挂载都会调它，是首屏最慢的一环。按 base_url + api_key 指纹分键，
# 换配置自然失效；过期时先返回旧数据、后台单飞刷新，用户永远不等 Dify。
_DATASETS_TTL = 60.0
_datasets_cache: dict[str, Any] = {"at": 0.0, "key": None, "items": None}
_datasets_refresh: dict[str, bool] = {"running": False}


def _datasets_cache_key(base_url: str, api_key: str) -> str:
    return f"{base_url}|{hashlib.sha256(api_key.encode()).hexdigest()[:12]}"


def invalidate_datasets_cache() -> None:
    """新建/删除知识库后调用：让列表缓存立即失效。"""
    _datasets_cache["at"] = 0.0
    _datasets_cache["key"] = None
    _datasets_cache["items"] = None


async def _fetch_dify_datasets() -> list[dict]:
    """实时拉取 Dify 数据集并标准化字段。"""
    datasets = await dify_client.list_datasets()
    return [{
        "id": d.get("id"),
        "name": d.get("name"),
        "description": d.get("description") or "",
        "document_count": d.get("document_count") or 0,
        "word_count": d.get("word_count") or 0,
    } for d in datasets]


def _schedule_datasets_refresh(key: str) -> None:
    """后台单飞刷新数据集列表：失败保留旧数据，下次访问再试。"""
    if _datasets_refresh["running"]:
        return
    _datasets_refresh["running"] = True

    async def _run() -> None:
        try:
            items = await _fetch_dify_datasets()
            _datasets_cache["at"] = time.monotonic()
            _datasets_cache["key"] = key
            _datasets_cache["items"] = items
        except Exception:  # noqa: BLE001 — 刷新失败不影响旧数据展示
            pass
        finally:
            _datasets_refresh["running"] = False

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        _datasets_refresh["running"] = False


@router.get("/datasets")
async def list_dify_datasets(u=Depends(require_role("super_admin", "admin")),
                              s: AsyncSession = Depends(get_session)):
    """列出当前 Dify API Key 可访问的数据集，供前端选择配置（60s 缓存 + 过期后台刷新）。

    缓存命中时**完全不碰数据库、也不请求 Dify**：早先即使命中缓存也要先跑
    _runtime_config()（3 条 settings 查询 + 一条请求级会话），而连接池需要新建连接时
    这一步实测要 3~28 秒，缓存等于白做。
    代价：Dify 配置变更后必须调用 invalidate_datasets_cache()，否则最多吃 60s 旧列表。
    """
    cached = _datasets_cache["items"]
    if cached is not None and time.monotonic() - _datasets_cache["at"] < _DATASETS_TTL:
        return {"items": cached, "from_cache": True, "stale": False}

    base_url, api_key = await _runtime_config(s)
    if not base_url or not api_key:
        missing = "服务地址" if not base_url else "API Key"
        return {"items": [], "error": f"尚未配置 Dify {missing}，请在『Dify 链接配置』中填写并保存"}

    key = _datasets_cache_key(base_url, api_key)
    if cached is not None and _datasets_cache["key"] == key:
        # 已过 TTL 但配置未变：旧值先顶上，后台单飞刷新
        _schedule_datasets_refresh(key)
        return {"items": cached, "from_cache": True, "stale": True}

    try:
        items = await _fetch_dify_datasets()
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        hint = "API Key 无效或无权限" if status in (401, 403) else "请检查服务地址与 API Key"
        return {"items": [], "error": f"Dify 返回 {status}：{hint}"}
    except Exception as e:
        return {"items": [], "error": f"无法连接 Dify 服务（{e.__class__.__name__}），请检查服务地址是否正确、Dify 是否已启动"}
    _datasets_cache["at"] = time.monotonic()
    _datasets_cache["key"] = key
    _datasets_cache["items"] = items
    return {"items": items, "from_cache": False, "stale": False}


class DatasetIn(BaseModel):
    name: str


@router.post("/datasets")
async def create_dify_dataset(body: DatasetIn,
                              u=Depends(require_role("super_admin", "admin")),
                              s: AsyncSession = Depends(get_session)):
    """在 Dify 中新建知识库（数据集）。"""
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "知识库名称不能为空")
    base_url, api_key = await _runtime_config(s)
    _require_config(base_url, api_key)
    try:
        d = await dify_client.create_dataset(name)
    except Exception as e:
        raise HTTPException(502, f"Dify 新建知识库失败：{e}")
    invalidate_datasets_cache()   # 否则前端刷新列表会命中缓存、看不到刚建的库
    return {"id": d.get("id"), "name": d.get("name") or name}


class SyncDingTalkIn(BaseModel):
    node_id: str
    name: str
    size: int = 0
    pipeline_inputs: dict[str, Any] | None = None


def _dingtalk_doc_url(node_id: str) -> str:
    """钉钉 wiki 节点的在线预览地址（引用来源点击跳转用）。"""
    return f"https://alidocs.dingtalk.com/i/nodes/{node_id}"


async def _save_dingtalk_mapping(s: AsyncSession, dataset_id: str, dify_document_id: str | None,
                                 node_id: str, name: str, method: str) -> None:
    """记录 Dify 文档 ↔ 钉钉节点映射，供检索召回时回填钉钉链接。"""
    if not dify_document_id:
        return
    from kb_common.models import DifyDingtalkDocMapping
    # 同一 Dify 文档可能重复同步（更新），按 dify_document_id upsert
    existing = (await s.execute(
        select(DifyDingtalkDocMapping).where(DifyDingtalkDocMapping.dify_document_id == dify_document_id)
    )).scalar_one_or_none()
    url = _dingtalk_doc_url(node_id)
    if existing:
        existing.dingtalk_node_id = node_id
        existing.dingtalk_url = url
        existing.name = name
        existing.method = method
    else:
        s.add(DifyDingtalkDocMapping(
            dify_dataset_id=dataset_id,
            dify_document_id=dify_document_id,
            dingtalk_node_id=node_id,
            dingtalk_url=url,
            name=name,
            method=method,
        ))
    await s.commit()


@router.post("/datasets/{dataset_id}/sync-dingtalk")
@track_transfer("dingtalk")
async def sync_dingtalk_file(dataset_id: str,
                              body: SyncDingTalkIn,
                              u=Depends(require_role("super_admin", "admin")),
                              s: AsyncSession = Depends(get_session)):
    """钉钉原文件下载或在线文档导出后，备份并按目标知识库类型上传。

    普通库走 create-by-file；流水线库走 file-upload + pipeline/run。
    保留原格式，超限或解析失败直接报错，不降级成 Markdown/纯文本。
    """
    if not dataset_id:
        raise HTTPException(400, "请先选择目标知识库")
    base_url, api_key = await _runtime_config(s)
    _require_config(base_url, api_key)
    if not body.node_id:
        raise HTTPException(400, "钉钉节点 ID 为空")
    from app.services.sync.source_files import download_single_source
    from app.services.sync.dify_pipeline_vars import prepare_inputs_for_dataset
    try:
        inputs = await asyncio.to_thread(prepare_inputs_for_dataset, dataset_id, body.pipeline_inputs)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"读取 Dify 流水线参数失败：{exc}") from exc
    try:
        upload_name, content, source_kind = await asyncio.to_thread(download_single_source, body.node_id)
    except Exception as exc:
        raise HTTPException(502, f"钉钉原文件获取失败：{exc}") from exc
    if not content:
        raise HTTPException(400, "下载的文件内容为空")

    # 源文档先备份到对象存储（失败仅告警，不阻断同步）
    obj_key = f"dingtalk-sync/{body.node_id}/{upload_name}"
    try:
        await asyncio.to_thread(minio_client.upload_bytes, minio_client.RAW, obj_key, content)
    except Exception as e:  # noqa: BLE001
        obj_key = ""

    try:
        upload_name, content = prepare_document(upload_name, content)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    source_sha256 = hashlib.sha256(content).hexdigest()
    try:
        result = await dify_client.upload_document(dataset_id, upload_name, content, inputs=inputs)
    except Exception as e:
        raise HTTPException(502, f"Dify 上传文档失败：{e}")
    doc = result.get("document") or {}
    await _save_dingtalk_mapping(s, dataset_id, doc.get("id"), body.node_id, upload_name, "file")
    return {"document_id": doc.get("id"), "name": doc.get("name") or upload_name,
            "batch": result.get("batch"), "method": "file", "oss_key": obj_key,
            "source_kind": source_kind, "source_sha256": source_sha256, "source_size": len(content)}
