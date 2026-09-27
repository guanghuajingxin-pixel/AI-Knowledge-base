"""RAGFlow 知识库客户端（异步）。

第二个外部检索引擎，与 Dify 并列（同为 dataset/document/chunk/retrieval 结构）。
- 数据集列表：GET  {base_url}/datasets
- 文档列表  ：GET  {base_url}/datasets/{id}/documents
- 上传文档  ：POST {base_url}/datasets/{id}/documents   （multipart，上传后不自动解析）
- 触发解析  ：POST {base_url}/datasets/{id}/chunks       body {"document_ids": [...]}
- 删除文档  ：DELETE {base_url}/datasets/{id}/documents   body {"ids": [...]}
- 检索      ：POST {base_url}/retrieval                   body {"question", "dataset_ids", ...}

鉴权：Authorization: Bearer <API_KEY>；base_url 需含 /api/v1 后缀。
RAGFlow 统一响应信封 {"code": 0, "data": ...}，code!=0 视为业务错误（含 message）。

fail-fast 约定：ragflow_api_key 未配置时抛 RagflowNotConfigured，由路由层转成
可读提示（引导到「系统配置」填写连接），绝不返回假数据。
"""
from __future__ import annotations

import logging
import mimetypes
from typing import Any

import httpx

from kb_common.config import get_settings

logger = logging.getLogger(__name__)

# RAGFlow 文档解析状态（document.run 字段）
RUN_DONE = "DONE"
RUN_FAIL = "FAIL"
RUN_RUNNING = "RUNNING"
RUN_UNSTART = "UNSTART"
RUN_CANCEL = "CANCEL"


class RagflowError(Exception):
    """RAGFlow 业务/HTTP 错误。"""

    def __init__(self, message: str, status: int = 0, code: Any = ""):
        super().__init__(message)
        self.status = status
        self.code = code


class RagflowNotConfigured(RagflowError):
    """未配置连接（base_url/api_key 为空）；调用方应引导用户去系统配置。"""


def _base_url() -> str:
    return get_settings().ragflow_base_url.rstrip("/")


def _api_key() -> str:
    return get_settings().ragflow_api_key


def _headers(json_ct: bool = True) -> dict[str, str]:
    """请求头：Authorization 必带；multipart 上传时不能带 Content-Type: application/json。"""
    h = {"Authorization": f"Bearer {_api_key()}"}
    if json_ct:
        h["Content-Type"] = "application/json"
    return h


def _ensure_configured() -> None:
    if not _base_url():
        raise RagflowNotConfigured("尚未配置 RAGFlow 服务地址，请先在『RAGFlow 链接配置』中填写并保存")
    if not _api_key():
        raise RagflowNotConfigured("尚未配置 RAGFlow API Key，请先在『RAGFlow 链接配置』中填写并保存")


def _unwrap(resp: httpx.Response, action: str) -> Any:
    """校验 HTTP 状态与 RAGFlow 信封 code，返回 data 字段。"""
    if resp.status_code == 401:
        raise RagflowError(f"{action}：API Key 无效或已过期", status=401)
    if resp.status_code == 403:
        raise RagflowError(f"{action}：API Key 无访问权限", status=403)
    if resp.status_code == 404:
        raise RagflowError(f"{action}：端点或资源不存在，请检查 base_url（需含 /api/v1）与 ID", status=404)
    if resp.status_code >= 400:
        raise RagflowError(f"{action}：HTTP {resp.status_code} {resp.text[:200]}", status=resp.status_code)
    try:
        body = resp.json()
    except ValueError:
        raise RagflowError(f"{action}：响应非 JSON（{resp.text[:200]}）", status=resp.status_code)
    if isinstance(body, dict) and body.get("code") not in (0, "0", None):
        msg = body.get("message") or str(body)
        # Rerank 等模型未在租户授权（LookupError('Model(xxx@yyy) not authorized')）：给出可行动提示
        if "not authorized" in msg and "Model(" in msg:
            msg += "。该模型未在此 RAGFlow 租户授权，请在 RAGFlow 控制台『模型供应商』添加后重试，或改用『权重设置』子策略"
        raise RagflowError(f"{action}：{msg}", code=body.get("code"))
    return body.get("data") if isinstance(body, dict) else body


async def test_connection(base_url: str = "", api_key: str = "") -> dict[str, Any]:
    """探活：拉一页数据集，验证地址与 Key。供系统配置页「测试连接」调用。

    允许传入临时 base_url/api_key（编辑弹窗未保存时测试）；留空则用运行时配置。
    """
    base = (base_url or _base_url()).rstrip("/")
    key = (api_key or _api_key()).strip()
    if not base:
        return {"ok": False, "message": "请先填写服务地址"}
    if not key:
        return {"ok": False, "message": "请先填写 API Key"}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{base}/datasets",
                            headers={"Authorization": f"Bearer {key}"},
                            params={"page": 1, "page_size": 1})
    except Exception:
        return {"ok": False, "message": "无法连接到服务地址（网络不通/超时/DNS 失败），请检查地址"}
    if r.status_code == 401:
        return {"ok": False, "message": "API Key 无效或已过期，请检查后重试"}
    if r.status_code == 403:
        return {"ok": False, "message": "API Key 无访问权限"}
    if r.status_code == 404:
        return {"ok": False, "message": "端点不存在，请检查地址（需含端口与 /api/v1，如 http://127.0.0.1:9380/api/v1）"}
    if r.status_code >= 500:
        return {"ok": False, "message": f"RAGFlow 服务端错误（{r.status_code}），请检查服务状态"}
    try:
        body = r.json()
    except Exception:
        return {"ok": False, "message": f"响应异常（{r.status_code}）：{r.text[:120]}"}
    if r.status_code != 200 or body.get("code") not in (0, "0", None):
        return {"ok": False, "message": f"连接失败（{r.status_code}）：{body.get('message') or r.text[:120]}"}
    data = body.get("data") or []
    total = len(data) if isinstance(data, list) else "?"
    return {"ok": True, "message": f"连接成功，可见 {total} 个知识库（首页）"}


async def list_datasets(page: int = 1, page_size: int = 100, name: str = "") -> list[dict[str, Any]]:
    """列出 API Key 可访问的 RAGFlow 数据集（供知识源『从 RAGFlow 选择』picker）。"""
    _ensure_configured()
    params: dict[str, Any] = {"page": page, "page_size": page_size}
    if name:
        params["name"] = name
    async with httpx.AsyncClient(timeout=60.0) as c:
        resp = await c.get(f"{_base_url()}/datasets", headers=_headers(), params=params)
        data = _unwrap(resp, "列出 RAGFlow 数据集")
    return data if isinstance(data, list) else []


async def get_dataset(dataset_id: str) -> dict[str, Any] | None:
    _ensure_configured()
    async with httpx.AsyncClient(timeout=60.0) as c:
        resp = await c.get(f"{_base_url()}/datasets", headers=_headers(),
                           params={"id": dataset_id, "page": 1, "page_size": 1})
        data = _unwrap(resp, "获取 RAGFlow 数据集")
    items = data if isinstance(data, list) else []
    return items[0] if items else None


async def create_dataset(name: str, chunk_method: str = "naive",
                         embedding_model: str = "", parser_config: dict | None = None) -> dict[str, Any]:
    """在 RAGFlow 中新建数据集。

    字段名按实测版本（192.168.1.81）：chunk_method（分块方法，旧版叫 parser_id）、
    embedding_model（向量模型标识，形如 bge-m3@Local@Xinference）；留空则用租户默认。
    """
    _ensure_configured()
    payload: dict[str, Any] = {"name": name, "chunk_method": chunk_method or "naive"}
    if embedding_model:
        payload["embedding_model"] = embedding_model
    if parser_config:
        payload["parser_config"] = parser_config
    async with httpx.AsyncClient(timeout=60.0) as c:
        resp = await c.post(f"{_base_url()}/datasets", json=payload, headers=_headers())
        data = _unwrap(resp, "新建 RAGFlow 数据集")
    return data if isinstance(data, dict) else {}


async def list_documents(dataset_id: str, page: int = 1, page_size: int = 100,
                         keywords: str = "") -> list[dict[str, Any]]:
    """列出数据集下文档（含解析状态 run/progress、chunk_count）。"""
    _ensure_configured()
    params: dict[str, Any] = {"page": page, "page_size": page_size}
    if keywords:
        params["keywords"] = keywords
    async with httpx.AsyncClient(timeout=60.0) as c:
        resp = await c.get(f"{_base_url()}/datasets/{dataset_id}/documents",
                           headers=_headers(), params=params)
        data = _unwrap(resp, "列出 RAGFlow 文档")
    if isinstance(data, dict):
        return data.get("docs") or []
    return data if isinstance(data, list) else []


async def upload_document(dataset_id: str, filename: str, content: bytes) -> dict[str, Any]:
    """上传文档到数据集（仅入库，不解析）。返回 {id, name, ...}。

    解析需再调 parse_documents。三元组显式传 mimetype，避免中文文件名/特殊扩展名被猜错。
    """
    _ensure_configured()
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    async with httpx.AsyncClient(timeout=600.0) as c:
        resp = await c.post(f"{_base_url()}/datasets/{dataset_id}/documents",
                            files={"file": (filename, content, mime)},
                            headers=_headers(json_ct=False))
        data = _unwrap(resp, "上传 RAGFlow 文档")
    items = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    if not items:
        raise RagflowError("上传 RAGFlow 文档未返回文档对象")
    return items[0]


async def parse_documents(dataset_id: str, document_ids: list[str]) -> None:
    """触发文档解析（chunking）。RAGFlow 上传后必须显式调用本接口才开始解析。"""
    _ensure_configured()
    if not document_ids:
        return
    async with httpx.AsyncClient(timeout=60.0) as c:
        resp = await c.post(f"{_base_url()}/datasets/{dataset_id}/chunks",
                            json={"document_ids": document_ids}, headers=_headers())
        _unwrap(resp, "触发 RAGFlow 解析")


async def delete_documents(dataset_id: str, document_ids: list[str]) -> None:
    _ensure_configured()
    if not document_ids:
        return
    async with httpx.AsyncClient(timeout=60.0) as c:
        resp = await c.request("DELETE", f"{_base_url()}/datasets/{dataset_id}/documents",
                               json={"ids": document_ids}, headers=_headers())
        _unwrap(resp, "删除 RAGFlow 文档")


async def retrieve(dataset_ids: list[str], query: str, top_k: int | None = None,
                   similarity_threshold: float | None = None,
                   vector_similarity_weight: float | None = None,
                   rerank_id: str | None = None) -> list[dict[str, Any]]:
    """跨多个 RAGFlow 数据集检索（多库合并请求，无权库降级见 retrieve_with_report）。"""
    r = await retrieve_with_report(dataset_ids, query, top_k=top_k,
                                   similarity_threshold=similarity_threshold,
                                   vector_similarity_weight=vector_similarity_weight,
                                   rerank_id=rerank_id)
    return r["hits"]


async def _binding_errors(dataset_ids: list[str], endpoint: str) -> list[dict[str, str]]:
    """Do not send project library queries to a different configured engine."""
    from sqlalchemy import select
    from kb_common.database import short_session
    from kb_common.models import KnowledgeLibrary

    def normalized(url):
        return url.rstrip("/").removesuffix("/api/v1")

    async with short_session() as session:
        rows = (await session.execute(select(KnowledgeLibrary).where(
            KnowledgeLibrary.library_type == "document",
            KnowledgeLibrary.dataset_id.in_(dataset_ids),
        ))).scalars().all()
    return [{"dataset_id": row.dataset_id, "error": "引擎连接已切换，请恢复建库连接或完成迁移"}
            for row in rows if normalized(row.engine_config.get("endpoint", "")) != normalized(endpoint)]


async def retrieve_with_report(dataset_ids: list[str], query: str, top_k: int | None = None,
                               similarity_threshold: float | None = None,
                               vector_similarity_weight: float | None = None,
                               rerank_id: str | None = None,
                               document_ids: list[str] | None = None) -> dict[str, Any]:
    """跨多个 RAGFlow 数据集检索，归一化为与 dify_client.retrieve 一致的 hit 结构。

    与 retrieve 的差异：多库合并请求遇到「无权数据集」（code 102）时，降级为
    逐库检索——有权库正常返回，无权库记入 skipped（API Key 与库归属租户不匹配
    是常见配置错误，不能让一个坏库拖垮整轮检索）。

    返回：
        {"hits": [...], "skipped": [{"dataset_id", "error"}],
         "params": {实际生效的检索参数}}
    每个 hit：
        {"score", "content", "document_title", "document_id", "dataset_id",
         "segment_id", "page_number", "source": "ragflow"}
    """
    _ensure_configured()
    s = get_settings()
    ids = [d for d in dataset_ids if d]
    if not ids:
        return {"hits": [], "skipped": [], "params": {}}
    top_k = top_k or s.ragflow_retrieval_top_k
    params: dict[str, Any] = {
        "similarity_threshold": s.ragflow_similarity_threshold if similarity_threshold is None else similarity_threshold,
        "vector_similarity_weight": s.ragflow_vector_similarity_weight if vector_similarity_weight is None else vector_similarity_weight,
        "top_k": top_k,
    }
    rr = rerank_id if rerank_id is not None else s.ragflow_rerank_id
    if rr:
        params["rerank_id"] = rr

    # Snapshot credentials before awaits; concurrent settings changes must not retarget a request.
    endpoint, headers = _base_url(), _headers()
    skipped = await _binding_errors(ids, endpoint)
    blocked = {item["dataset_id"] for item in skipped}
    ids = [item for item in ids if item not in blocked]
    if not ids:
        return {"hits": [], "skipped": skipped, "params": params}

    async def _call(ds: list[str]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "question": query,
            "dataset_ids": ds,
            "page": 1,
            "page_size": max(int(top_k), 1),
            "similarity_threshold": params["similarity_threshold"],
            "vector_similarity_weight": params["vector_similarity_weight"],
            "top_k": top_k,
        }
        if document_ids:
            payload["document_ids"] = document_ids
        if "rerank_id" in params:
            payload["rerank_id"] = params["rerank_id"]
        async with httpx.AsyncClient(timeout=60.0) as c:
            try:
                resp = await c.post(f"{endpoint}/retrieval", json=payload, headers=headers)
            except httpx.HTTPError as e:
                raise RagflowError(f"RAGFlow 服务连接失败，请检查网络或配置（{e.__class__.__name__}）") from e
            return _unwrap(resp, "RAGFlow 检索") or {}

    data: dict[str, Any] = {}
    try:
        data = await _call(ids)
    except RagflowError as e:
        if len(ids) == 1 or e.code != 102:
            raise
        # 多库合并失败且含无权库：逐库降级，坏库跳过、好库照常返回
        logger.warning("RAGFlow 多库检索失败（%s），降级为逐库检索", e)
        data = {"chunks": []}
        for d in ids:
            try:
                one = await _call([d])
                data["chunks"] = (data.get("chunks") or []) + (one.get("chunks") or [])
            except RagflowError as de:
                skipped.append({"dataset_id": d, "error": str(de)})

    chunks = (data or {}).get("chunks") if isinstance(data, dict) else None
    hits: list[dict[str, Any]] = []
    for ch in chunks or []:
        hits.append({
            "score": ch["similarity"] if ch.get("similarity") is not None else ch.get("vector_similarity", 0.0),
            "score_type": "ragflow",
            "token_similarity": ch.get("term_similarity"),
            "vector_similarity": ch.get("vector_similarity"),
            "content": ch.get("content") or "",
            "document_title": ch.get("document_keyword") or ch.get("document_name") or "",
            "document_id": ch.get("document_id") or "",
            "dataset_id": ch.get("dataset_id") or (ids[0] if len(ids) == 1 else ""),
            "segment_id": ch.get("id") or ch.get("chunk_id") or "",
            "page_number": ch.get("page_num") or None,
            "source": "ragflow",
        })
    hits.sort(key=lambda x: x["score"] or 0.0, reverse=True)
    return {"hits": hits[:top_k], "skipped": skipped, "params": params}
