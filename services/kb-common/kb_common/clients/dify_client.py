"""Dify 知识库客户端。

通过 Dify Service API 实现知识库检索与数据集管理。
- 检索端点：POST {base_url}/datasets/{dataset_id}/retrieve
- 数据集列表：GET {base_url}/datasets
- 文档上传：
  * 普通数据集（runtime_mode=general）：POST /datasets/{id}/document/create-by-file
  * 流水线数据集（runtime_mode=rag_pipeline）：三步接口
      1. GET  /datasets/{id}/pipeline/datasource-plugins?is_published=true 拿 local_file 节点 ID
      2. POST /datasets/pipeline/file-upload 上传文件到暂存区拿 reference
      3. POST /datasets/{id}/pipeline/run 阻塞运行流水线
    流水线数据集若误用 create-by-file 会报 "No subchunk segmentation found in rules"。
鉴权使用 Dataset API Key（单个数据集）或拥有多数据集权限的 App/Console Key。
"""
import json
import logging
import mimetypes
import time
from typing import Any

import httpx
from kb_common.config import get_settings
from .dify_document import dataset_runtime, file_payload, pipeline_payload, pipeline_result

logger = logging.getLogger(__name__)

# 流水线数据集的 local_file 起始节点 ID 缓存：dataset_id → (monotonic_ts, node_id)
# 手动上传 100 个文件到同一流水线库时避免重复查询节点接口。
_PIPELINE_NODE_CACHE: dict[str, tuple[float, str]] = {}
_PIPELINE_NODE_TTL = 300.0  # 5 分钟


def _base_url() -> str:
    return get_settings().dify_base_url.rstrip("/")


def _api_key() -> str:
    return get_settings().dify_api_key


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"}


async def _dataset_retrieval_model(client: httpx.AsyncClient, did: str, top_k: int) -> dict[str, Any] | None:
    """拉取知识库自身在 Dify 控制台保存的检索配置（含 Rerank 模型），仅放大 top_k 后复用。

    返回 None 表示拉取失败或知识库未保存配置，调用方回退到环境变量配置。
    """
    try:
        resp = await client.get(f"{_base_url()}/datasets/{did}", headers=_headers())
        resp.raise_for_status()
        raw = (resp.json() or {}).get("retrieval_model_dict")
        if not (isinstance(raw, dict) and raw.get("search_method")):
            return None
        cfg = {k: v for k, v in raw.items() if v not in (None, [], {})}
        rerank = raw.get("reranking_model") or {}
        # reranking_model 为空对象时必须剔除，否则 Dify 校验会以空模型名报 400
        if not (cfg.get("reranking_enable") and rerank.get("reranking_model_name")):
            cfg.pop("reranking_model", None)
        cfg["top_k"] = max(int(cfg.get("top_k") or 0), top_k)
        return cfg
    except Exception as e:
        logger.warning("Fetch dataset %s retrieval config failed: %s", did, e)
        return None


async def retrieve(
    dataset_ids: list[str],
    query: str,
    top_k: int | None = None,
    score_threshold: float | None = None,
    search_method: str | None = None,
    rerank: bool | None = None,
) -> list[dict[str, Any]]:
    """跨多个 Dify 数据集检索，合并并按 score 降序返回。

    优先使用各知识库自身保存的检索配置（含其 Rerank 模型）——单一知识库时
    返回顺序即 Dify Rerank 后的顺序；拉取配置失败时回退到环境变量配置。
    显式传入 search_method（hybrid_search/semantic_search/full_text_search）
    时置于回退链最前，优先于知识库自身配置的检索方式（检索测试指定模式用）。

    返回的每个 hit 统一为：
        {"score": float, "content": str, "document_title": str,
         "document_id": str, "dataset_id": str, "segment_id": str,
         "page_number": int | None}
    """
    s = get_settings()
    top_k = top_k or s.dify_retrieval_top_k
    threshold_override = score_threshold is not None
    score_threshold = score_threshold if score_threshold is not None else s.dify_score_threshold
    method = (s.dify_search_method or "semantic_search").strip()

    def _payload(retrieval_model: dict[str, Any] | None = None,
                 search_method: str | None = None) -> dict[str, Any]:
        # Dify HitTestingPayload 要求 score_threshold_enabled 必填；
        # reranking 需在 Dify 控制台配置 Rerank 模型，未配置时必须关闭（否则 400）。
        model: dict[str, Any]
        if retrieval_model is not None:
            model = retrieval_model
        else:
            model = {
                "search_method": search_method or method,
                "top_k": top_k,
                "score_threshold": score_threshold,
                "score_threshold_enabled": False,
                "reranking_enable": bool(s.dify_reranking_enable),
            }
            if model["reranking_enable"]:
                model["reranking_mode"] = "reranking_model"
                model["reranking_model"] = {
                    "reranking_provider_name": "langgenius/openai_api_compatible/openai_api_compatible",
                    "reranking_model_name": "BAAI/bge-reranker-v2-m3",
                }
        model = dict(model)
        if threshold_override:
            model.update(score_threshold=score_threshold, score_threshold_enabled=score_threshold > 0)
        if rerank is not None:
            model['reranking_enable'] = rerank
            if not rerank:
                model.pop('reranking_model', None)
        return {"query": query, "retrieval_model": model}

    merged: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        for did in dataset_ids:
            if not did:
                continue
            url = f"{_base_url()}/datasets/{did}/retrieve"
            # 知识库自身配置优先（检索方式、Rerank 模型、分数阈值均以控制台配置为准）
            cfg = await _dataset_retrieval_model(client, did, top_k)
            attempts: list[dict[str, Any]] = []
            # 检索测试显式指定模式时置于最前：保留知识库配置的 Rerank 等，仅覆盖检索方式
            if search_method:
                if cfg:
                    attempts.append(_payload({**cfg, "search_method": search_method}))
                attempts.append(_payload(search_method=search_method))
            attempts.extend([_payload(cfg)] if cfg else [])
            # 不传 retrieval_model 时 Dify 同样使用知识库自身默认配置
            attempts.append({"query": query})
            # 知识库配置请求均失败时，最终回退到环境变量配置
            fallbacks = [method, "full_text_search"] if method != "full_text_search" else [method]
            attempts.extend(_payload(search_method=m) for m in fallbacks)
            if search_method:
                # Explicit test modes must never silently fall back to another mode.
                attempts = [p for p in attempts if p.get('retrieval_model', {}).get('search_method') == search_method]
            data = None
            last_err = ""
            for payload in attempts:
                try:
                    resp = await client.post(url, json=payload, headers=_headers())
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except httpx.HTTPStatusError as e:
                    body = (e.response.text or "")[:200]
                    last_err = f"HTTP {e.response.status_code}"
                    logger.warning("Dify retrieve failed for dataset %s: HTTP %s %s",
                                   did, e.response.status_code, body)
                except Exception as e:
                    last_err = e.__class__.__name__
                    logger.warning("Dify retrieve failed for dataset %s: %s", did, e)
            if not data:
                if search_method:
                    raise RuntimeError(
                        f"Dify 服务调用失败，请检查网络或配置（{last_err or '详见服务日志'}）")
                continue
            for record in data.get("records", []) or []:
                seg = record.get("segment") or {}
                doc = seg.get("document") or {}
                merged.append({
                    "score": record.get("score") or 0.0,
                    "content": seg.get("content") or "",
                    "document_title": doc.get("name") or "",
                    "document_id": seg.get("document_id") or "",
                    "dataset_id": did,
                    "segment_id": seg.get("id") or "",
                    "page_number": None,
                })

    merged.sort(key=lambda x: x["score"], reverse=True)
    return merged[:top_k]


async def list_datasets() -> list[dict[str, Any]]:
    """列出当前 API Key 可访问的 Dify 数据集，失败时抛出异常由调用方处理。"""
    url = f"{_base_url()}/datasets"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(url, headers=_headers())
        resp.raise_for_status()
        data = resp.json()
        return data.get("data") or []


async def create_dataset(name: str) -> dict[str, Any]:
    """在 Dify 中新建知识库（数据集），失败抛异常。"""
    url = f"{_base_url()}/datasets"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json={"name": name}, headers=_headers())
        resp.raise_for_status()
        return resp.json()


def _upload_headers() -> dict[str, str]:
    """multipart 上传请求头：只保留 Authorization，不能带 Content-Type: application/json。"""
    return {"Authorization": f"Bearer {_api_key()}"}


def _format_dify_http_error(e: httpx.HTTPStatusError, action: str) -> str:
    """把 Dify HTTP 错误格式化为带中文提示的字符串。"""
    body = e.response.text
    try:
        err_json = e.response.json()
        code = err_json.get("code", "")
        msg = err_json.get("message", "") or body
        detail = f"{code}: {msg}" if code else msg
    except Exception:
        detail = body
    hints = {
        401: "请检查系统配置中的 Dataset API Key。",
        403: "当前 API Key 无权写入此知识库。",
        404: "知识库或流水线节点不存在，请确认 dataset_id 与流水线已发布。",
        413: "文件超过 Dify 或网关限制，请压缩或拆分文件。",
        415: "此 Dify 实例不支持该格式，请另存为 DOCX、PDF 或 TXT。",
        429: "Dify 请求频率或配额受限，请稍后检查配额再重试。",
    }
    hint = hints.get(e.response.status_code, "")
    return f"{action}失败（HTTP {e.response.status_code}）：{detail[:800]}。{hint}".strip()


async def _local_file_node_id(client: httpx.AsyncClient, dataset_id: str) -> str:
    """取流水线数据集的本地文件数据源节点 ID（start_node_id），5 分钟内存缓存。"""
    now = time.monotonic()
    cached = _PIPELINE_NODE_CACHE.get(dataset_id)
    if cached and now - cached[0] < _PIPELINE_NODE_TTL:
        return cached[1]
    resp = await client.get(
        f"{_base_url()}/datasets/{dataset_id}/pipeline/datasource-plugins",
        params={"is_published": "true"},
        headers=_headers(),
    )
    resp.raise_for_status()
    data = resp.json()
    nodes = data if isinstance(data, list) else (data.get("data") or [])
    for node in nodes:
        if node.get("datasource_type") == "local_file" and node.get("node_id"):
            node_id = str(node["node_id"])
            _PIPELINE_NODE_CACHE[dataset_id] = (now, node_id)
            return node_id
    raise RuntimeError(
        "Dify 知识流水线未配置本地文件数据源节点，无法通过 API 同步文档。"
        "请在 Dify 流水线编辑器中添加『本地文件』数据源节点并点击『发布』。"
    )


async def _upload_via_pipeline(
    client: httpx.AsyncClient, dataset_id: str, filename: str, content: bytes,
    inputs: dict | None = None,
) -> dict[str, Any]:
    """流水线数据集上传：Step2 file-upload → Step3 pipeline/run（阻塞模式）。

    ⚠️ 真实响应结构（2026-09-11 本地 Dify 1.16.1 实测）与官方文档/源码不一致：
    实际返回 `{batch, dataset, documents: [{id, name, indexing_status, ...}]}` 顶层结构，
    而不是 WorkflowAppBlockingResponse 的 `{task_id, workflow_run_id, data: {outputs}}`。
    本实现按真实响应解析，同时保留 data.outputs 兜底路径以兼容未来版本。

    ⚠️ `inputs` 必须携带流水线定义的必填变量（如 max_chunk_length / parent_mode /
    child_length），缺失会报 500 "xxx is required in input form"。不同流水线的变量
    完全不同，值由调用方传入；未传时由已发布流水线使用自己的默认值。

    返回结构与 create-by-file 对齐：{"document": {...}, "batch": "..."}，
    额外带 runtime_mode="rag_pipeline" 便于上层日志/审计。
    """
    start_node_id = await _local_file_node_id(client, dataset_id)

    # Step 2：上传到流水线暂存区，拿 reference（文件 ID）
    # 必须用三元组 (filename, content, mimetype) 显式传 MIME 类型：
    # Dify FileService.upload_file 靠 filename 的扩展名 + mimetype 识别文件类型，
    # httpx 二元组对中文文件名 + .pptx/.docx 等扩展名可能猜错或退化成
    # application/octet-stream，导致 Dify 侧存储时扩展名/类型丢失。
    mime, _ = mimetypes.guess_type(filename)
    mime = mime or "application/octet-stream"
    logger.info(
        "Dify pipeline file-upload: dataset=%s filename=%r mimetype=%s size=%d",
        dataset_id, filename, mime, len(content),
    )
    upload_resp = await client.post(
        f"{_base_url()}/datasets/pipeline/file-upload",
        files={"file": (filename, content, mime)},
        headers=_upload_headers(),
    )
    upload_resp.raise_for_status()
    uploaded = upload_resp.json()
    reference = uploaded.get("id")
    if not reference:
        raise RuntimeError(f"Dify 流水线文件上传未返回文件 ID：{uploaded}")
    logger.info(
        "Dify pipeline file-upload ok: reference=%s name=%r extension=%r mime_type=%r",
        reference, uploaded.get("name"), uploaded.get("extension"), uploaded.get("mime_type"),
    )

    # Step 3：阻塞运行流水线（含解析+分段+索引，可能持续数分钟）
    # inputs 必须包含流水线定义的输入变量；不同流水线变量不同（max_chunk_length /
    # parent_mode / child_length 等），值由调用方传入；缺失必填变量 Dify 报 500。
    run_payload = pipeline_payload(start_node_id, str(reference), filename, inputs)
    run_resp = await client.post(
        f"{_base_url()}/datasets/{dataset_id}/pipeline/run",
        json=run_payload,
        headers=_headers(),
    )
    run_resp.raise_for_status()
    result = run_resp.json()

    try:
        return pipeline_result(result, filename)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


async def _upload_via_create_by_file(
    client: httpx.AsyncClient, dataset: dict[str, Any], dataset_id: str,
    filename: str, content: bytes,
) -> dict[str, Any]:
    """普通数据集：走 create-by-file 端点，复用数据集自身的分段规则。"""
    data = file_payload(dataset)
    url = f"{_base_url()}/datasets/{dataset_id}/document/create-by-file"
    # 与 pipeline 分支保持一致：三元组显式传 mimetype，避免中文文件名 + 特殊扩展名
    # 场景下 httpx 二元组猜错类型导致 Dify 侧识别异常。
    mime, _ = mimetypes.guess_type(filename)
    mime = mime or "application/octet-stream"
    logger.info(
        "Dify create-by-file: dataset=%s filename=%r mimetype=%s size=%d",
        dataset_id, filename, mime, len(content),
    )
    resp = await client.post(
        url,
        files={"file": (filename, content, mime)},
        data={"data": json.dumps(data)},
        headers=_upload_headers(),
    )
    resp.raise_for_status()
    result = resp.json()
    doc = result.get("document") or {}
    logger.info(
        "Dify create-by-file ok: document_id=%s document_name=%r batch=%s (original filename=%r)",
        doc.get("id"), doc.get("name"), result.get("batch"), filename,
    )
    return result


async def upload_document(dataset_id: str, filename: str, content: bytes,
                          inputs: dict | None = None) -> dict[str, Any]:
    """上传文档到指定 Dify 知识库，按 dataset.runtime_mode 自动分流。

    - `rag_pipeline`（流水线数据集）：三步接口 datasource-plugins → file-upload → pipeline/run
      （阻塞模式，含解析+分段+索引，耗时数十秒到数分钟）。
      流水线数据集若误用 create-by-file 会报 "No subchunk segmentation found in rules"，
      或控制台预览报 PublishedWorkflowRunPayload 校验错误。
      `inputs` 为流水线 input form 变量值（分段参数），缺失必填变量 Dify 报 500。
    - `general`（普通数据集）：create-by-file 端点，Dify 按数据集自身分段规则处理，
      忽略 inputs。

    返回：{"document": {...}, "batch": "..."}；流水线分支额外带 runtime_mode 字段。
    """
    # 流水线阻塞模式含解析+分段+索引，可能持续数分钟；create-by-file 通常几秒完成。
    # 统一把 client timeout 拉到 600s，覆盖两条路径的最坏情况。
    async with httpx.AsyncClient(timeout=600.0) as client:
        try:
            dataset_response = await client.get(
                f"{_base_url()}/datasets/{dataset_id}", headers=_headers()
            )
            dataset_response.raise_for_status()
            dataset = dataset_response.json()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(_format_dify_http_error(e, "获取 Dify 知识库信息")) from e
        except httpx.TimeoutException as e:
            raise RuntimeError("获取 Dify 知识库信息超时，请检查 Dify 服务是否可达。") from e

        runtime_mode = dataset_runtime(dataset)
        action = "Dify 流水线上传" if runtime_mode == "rag_pipeline" else "Dify 上传"
        try:
            if runtime_mode == "rag_pipeline":
                return await _upload_via_pipeline(client, dataset_id, filename, content,
                                                  inputs=inputs)
            return await _upload_via_create_by_file(client, dataset, dataset_id, filename, content)
        except httpx.HTTPStatusError as e:
            raise RuntimeError(_format_dify_http_error(e, action)) from e
        except httpx.TimeoutException as e:
            raise RuntimeError(
                "Dify 响应超时，请先到目标知识库确认文档是否已创建，再决定是否重试，避免重复上传。"
            ) from e


async def list_documents(dataset_id: str) -> list[dict[str, Any]]:
    """列出指定 Dify 数据集下的所有文档。"""
    url = f"{_base_url()}/datasets/{dataset_id}/documents"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(url, headers=_headers())
        resp.raise_for_status()
        data = resp.json()
        return data.get("data") or []


async def get_document_segments(dataset_id: str, document_id: str) -> list[dict[str, Any]]:
    """获取指定文档的所有分块（segment）内容。"""
    url = f"{_base_url()}/datasets/{dataset_id}/documents/{document_id}/segments"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(url, headers=_headers())
        resp.raise_for_status()
        data = resp.json()
        return data.get("data") or []


async def get_dataset(dataset_id: str) -> dict[str, Any] | None:
    """获取单个数据集信息。"""
    url = f"{_base_url()}/datasets/{dataset_id}"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(url, headers=_headers())
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.warning("Dify get_dataset failed for %s: %s", dataset_id, e)
        return None
