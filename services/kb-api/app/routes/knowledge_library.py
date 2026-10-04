"""知识库（检索抽象层）：统一知识库镜像登记 CRUD。

与「知识源管理」（推送路径定义）相互独立：本路由管理的知识库仅用于智能体检索，
只登记 platform + dataset_id 镜像引用，不支持导入/解析新文档；
检索时按 platform 由抽象层（dify/ragflow 客户端）决定各自检索策略。
"""
from datetime import datetime
from uuid import UUID
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kb_common.database import get_session
from kb_common.models import KnowledgeLibrary
from app.deps import get_current_user, require_role

router = APIRouter(prefix="/api/v1/knowledge-libraries", tags=["knowledge-library"])

PLATFORMS = ("dify", "ragflow")


class KnowledgeLibraryOut(BaseModel):
    id: int
    name: str
    platform: str
    dataset_id: str
    description: str
    enabled: bool
    library_type: str
    updated_at: datetime

    class Config:
        from_attributes = True


class KnowledgeLibraryCreate(BaseModel):
    name: str = Field(max_length=200)
    platform: str
    dataset_id: str = Field(max_length=128)
    description: str = ""
    enabled: bool = True


class KnowledgeLibraryUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    description: str | None = None
    enabled: bool | None = None


@router.get("", response_model=list[KnowledgeLibraryOut])
async def list_knowledge_libraries(
    enabled_only: bool = Query(False, description="只返回已启用的知识库"),
    u=Depends(get_current_user),
    s: AsyncSession = Depends(get_session),
):
    """知识库镜像列表：问答页/统一检索等所有「选择知识库」处从此接口取数。"""
    q = select(KnowledgeLibrary).where(KnowledgeLibrary.library_type != "material")
    if enabled_only:
        q = q.where(KnowledgeLibrary.enabled == True)  # noqa: E712
    q = q.order_by(KnowledgeLibrary.platform, KnowledgeLibrary.id.desc())
    return (await s.execute(q)).scalars().all()


@router.post("", response_model=KnowledgeLibraryOut,
             dependencies=[Depends(require_role("super_admin", "admin"))])
async def create_knowledge_library(
    payload: KnowledgeLibraryCreate,
    u=Depends(get_current_user),
    s: AsyncSession = Depends(get_session),
):
    """登记知识库镜像（仅 dify | ragflow；同平台同 dataset_id 不可重复）。"""
    if payload.platform not in PLATFORMS:
        raise HTTPException(422, f"不支持的平台类型：{payload.platform}（仅支持 dify / ragflow）")
    if not payload.name.strip():
        raise HTTPException(422, "知识库名称不能为空")
    if not payload.dataset_id.strip():
        raise HTTPException(422, "数据集 ID 不能为空")
    dup = (await s.execute(select(KnowledgeLibrary).where(
        KnowledgeLibrary.platform == payload.platform,
        KnowledgeLibrary.dataset_id == payload.dataset_id.strip(),
    ))).scalars().first()
    if dup:
        raise HTTPException(409, f"该平台的此知识库已登记为「{dup.name}」，请勿重复添加")
    lib = KnowledgeLibrary(
        name=payload.name.strip(),
        platform=payload.platform,
        dataset_id=payload.dataset_id.strip(),
        description=(payload.description or "").strip(),
        enabled=payload.enabled,
    )
    s.add(lib)
    await s.commit()
    await s.refresh(lib)
    return lib


@router.put("/{library_id}", response_model=KnowledgeLibraryOut,
            dependencies=[Depends(require_role("super_admin", "admin"))])
async def update_knowledge_library(
    library_id: int,
    payload: KnowledgeLibraryUpdate,
    u=Depends(get_current_user),
    s: AsyncSession = Depends(get_session),
):
    """更新知识库镜像（改名/描述/启停；platform 与 dataset_id 不可改，需删后重加）。"""
    lib = await s.get(KnowledgeLibrary, library_id)
    if lib is None:
        raise HTTPException(404, "知识库不存在")
    data = payload.model_dump(exclude_unset=True)
    if lib.library_type == "material":
        raise HTTPException(409, "物料库请通过物料库管理入口修改")
    if lib.library_type == "document" and set(data) - {"enabled"}:
        raise HTTPException(409, "项目文档库的名称和设置请通过文档库设置接口修改")
    if "name" in data:
        if not (data["name"] or "").strip():
            raise HTTPException(422, "知识库名称不能为空")
        data["name"] = data["name"].strip()
    for key, value in data.items():
        setattr(lib, key, value)
    await s.commit()
    await s.refresh(lib)
    return lib


@router.delete("/{library_id}",
               dependencies=[Depends(require_role("super_admin", "admin"))])
async def delete_knowledge_library(
    library_id: int,
    u=Depends(get_current_user),
    s: AsyncSession = Depends(get_session),
):
    """删除知识库镜像（不影响引擎平台上的原库）。"""
    lib = await s.get(KnowledgeLibrary, library_id)
    if lib is None:
        raise HTTPException(404, "知识库不存在")
    if lib.library_type in {"document", "material"}:
        raise HTTPException(409, "项目文档库请先停用并导出归档，不能通过外部镜像接口移除")
    await s.delete(lib)
    await s.commit()
    return {"ok": True}


class RetrievalTestIn(BaseModel):
    query: str = Field(max_length=2000)
    # 空 = 全部登记的知识库（含停用，便于排查）；也可指定子集
    library_ids: list[int] = Field(default_factory=list)
    # 检索参数均可缺省：缺省时文档库按「知识库设置-检索设置」(engine_config.retrieval)
    # 取库级默认，外部库（Dify/RAGFlow）回落全局默认（hybrid / top_k=8 / 无阈值）
    top_k: int | None = Field(None, ge=1, le=50)
    # 检索模式：hybrid=混合检索（RAGFlow 按权重/rerank 排序，Dify=hybrid_search）、
    # vector=向量检索（weight=1.0 / semantic_search）、fulltext=全文检索（weight=0.0 / full_text_search）
    mode: Literal["hybrid", "vector", "fulltext"] | None = None
    # 混合检索 + Rerank 子策略时的 RAGFlow rerank 模型名（如 bge--reranker-v2–m3）
    rerank_id: str | None = Field(None, max_length=200)
    document_ids: list[str] = Field(default_factory=list, max_length=100)
    rerank: bool | None = None
    rerank_model_id: UUID | None = None
    # 阈值作用于最终分数；向量权重作用于本地和 RAGFlow 的混合检索。
    similarity_threshold: float | None = Field(None, ge=0, le=1)
    vector_similarity_weight: float | None = Field(None, ge=0, le=1)


async def _eff_setting(s: AsyncSession, key: str) -> str:
    """DB settings 表优先，回退环境变量默认值（与智能问答链路口径一致）。"""
    from kb_common.config import get_settings
    from kb_common.models import Setting

    row = (await s.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
    return (row.value if row else None) or getattr(get_settings(), key, "") or ""


async def _search_document_library(s: AsyncSession, lib, query: str, top_k: int,
                                    mode: str = "hybrid", **options) -> list[dict]:
    from app.services.library_retrieval import search
    return await search(s, lib, query, top_k, mode, **options)


@router.post("/retrieval-test", dependencies=[Depends(require_role("super_admin", "admin"))])
async def retrieval_test(body: RetrievalTestIn,
                         u=Depends(get_current_user),
                         s: AsyncSession = Depends(get_session)):
    """检索测试：本地文档库执行 RAGFlow NLP 检索核，外部库调用各自引擎。

    用于排查「引擎自带检索能搜到、智能问答检索不到」的断层：逐库返回
    ok/失败原因（如 RAGFlow code 102 无权数据集）与命中分段，
    检索参数原样回显方便与引擎侧测试对齐。
    """
    return await execute_retrieval(body, s)


async def execute_retrieval(body: RetrievalTestIn, s: AsyncSession, *, public: bool = False):
    """Shared retrieval implementation; public calls must name enabled libraries explicitly."""
    import time as _time

    from kb_common.clients import dify_client, ragflow_client
    from kb_common.config import get_settings

    if not body.query.strip():
        raise HTTPException(422, "检索词不能为空")

    q = select(KnowledgeLibrary).where(KnowledgeLibrary.library_type != "material")
    if public:
        if not body.library_ids:
            raise HTTPException(422, "必须指定知识库")
        q = q.where(KnowledgeLibrary.enabled.is_(True))
    if body.library_ids:
        q = q.where(KnowledgeLibrary.id.in_(body.library_ids))
    libs = (await s.execute(q.order_by(KnowledgeLibrary.platform, KnowledgeLibrary.id))).scalars().all()
    if public and {lib.id for lib in libs} != set(body.library_ids):
        raise HTTPException(403, "指定知识库不存在或未开放检索")
    if not libs:
        raise HTTPException(404, "未找到指定知识库")

    # 运行时配置注入（settings 表优先）：与 /internal/kb/retrieve 同口径
    settings = get_settings()
    settings.dify_base_url = await _eff_setting(s, "dify_base_url")
    settings.dify_api_key = await _eff_setting(s, "dify_api_key")
    settings.ragflow_base_url = await _eff_setting(s, "ragflow_base_url")
    settings.ragflow_api_key = await _eff_setting(s, "ragflow_api_key")

    started = _time.monotonic()
    # 全局默认（外部库/未配置检索设置的库）：与历史行为一致（hybrid / top_k=8 / 无阈值 / 不重排）
    g_mode = body.mode or "hybrid"
    g_top_k = body.top_k or 8
    g_rerank = bool(body.rerank)
    # 模式 → 引擎参数映射：
    # RagFlow：向量检索 weight=1.0、全文检索 weight=0.0、混合检索用默认/显式权重 + 可选 rerank 模型；
    # Dify：hybrid_search / semantic_search / full_text_search（Rerank 仍由知识库自身配置决定）
    ragflow_weight = {"vector": 1.0, "fulltext": 0.0}.get(g_mode)
    # 混合检索-权重设置子策略：透传前端显式向量权重（0~1，语义=向量权重，全文权重=1-该值）
    if g_mode == "hybrid" and body.vector_similarity_weight is not None:
        ragflow_weight = body.vector_similarity_weight
    ragflow_rerank = body.rerank_id if (g_mode == "hybrid" and body.rerank_id) else None
    dify_method = {"hybrid": "hybrid_search", "vector": "semantic_search", "fulltext": "full_text_search"}[g_mode]
    lib_report: list[dict] = []
    hits: list[dict] = []
    max_top_k = g_top_k  # 各库生效 top_k 的最大值：多库合并后按此截取，避免库级配置被全局默认截掉
    for lib in libs:
        entry = {"library_id": lib.id, "name": lib.name, "platform": lib.platform,
                 "dataset_id": lib.dataset_id, "enabled": lib.enabled,
                 "ok": False, "count": 0, "error": ""}
        try:
            if lib.library_type == "document":
                # 项目文档库：真实向量与词项评分，过滤后排序、父段去重。
                # 未显式传参的检索参数按库级「检索设置」(engine_config.retrieval) 取默认。
                rcfg = (lib.engine_config or {}).get("retrieval") or {}
                eff_mode = body.mode or rcfg.get("mode") or "hybrid"
                eff_top_k = body.top_k or rcfg.get("top_k") or 8
                max_top_k = max(max_top_k, eff_top_k)
                eff_threshold = body.similarity_threshold if body.similarity_threshold is not None \
                    else float(rcfg.get("score_threshold") or 0.0)
                eff_weight = body.vector_similarity_weight if body.vector_similarity_weight is not None \
                    else float(rcfg.get("vector_weight") or 0.7)
                eff_rerank = body.rerank if body.rerank is not None else bool(rcfg.get("rerank"))
                eff_rerank_model = str(body.rerank_model_id) if body.rerank_model_id else \
                    (str(rcfg["rerank_model_id"]) if rcfg.get("rerank_model_id") else None)
                entry |= {"mode": eff_mode, "top_k": eff_top_k, "score_threshold": eff_threshold,
                          "vector_weight": eff_weight, "rerank": eff_rerank}
                lib_hits = await _search_document_library(
                    s, lib, body.query, eff_top_k, eff_mode,
                    document_ids=[str(d) for d in body.document_ids],
                    threshold=eff_threshold,
                    vector_weight=eff_weight,
                    rerank=eff_rerank,
                    rerank_model_id=eff_rerank_model)
            elif lib.platform == "ragflow":
                r = await ragflow_client.retrieve_with_report(
                    [lib.dataset_id], body.query, top_k=max(100, g_top_k * 5) if g_rerank else g_top_k,
                    similarity_threshold=0 if g_rerank else (body.similarity_threshold or 0.0),
                    vector_similarity_weight=ragflow_weight,
                    rerank_id=ragflow_rerank or "",
                    document_ids=[str(d) for d in body.document_ids])
                if r.get("skipped"):
                    raise ValueError(r["skipped"][0]["error"])
                lib_hits = r["hits"]
            else:
                lib_hits = await dify_client.retrieve([lib.dataset_id], body.query, top_k=max(100, g_top_k * 5) if g_rerank else g_top_k,
                                                      search_method=dify_method, rerank=False, score_threshold=0)
            if lib.library_type != "document":
                if body.document_ids and lib.platform != "ragflow":
                    raise ValueError("该外部引擎暂不支持文档范围检索")
                if g_rerank and lib_hits:
                    from app.services.library_retrieval import rerank_hits
                    for hit in lib_hits:
                        hit['matched_content'] = hit['content']
                    await rerank_hits(s, body.query, lib_hits,
                                      str(body.rerank_model_id) if body.rerank_model_id else None)
                lib_hits = sorted((h for h in lib_hits if h['score'] >= (body.similarity_threshold or 0)),
                                  key=lambda h: h['score'], reverse=True)[:g_top_k]
            for h in lib_hits:
                hits.append({**h, "library_id": lib.id, "library_name": lib.name})
            entry["ok"] = True
            entry["count"] = len(lib_hits)
        except Exception as e:  # noqa: BLE001 - 逐库隔离：单库失败不影响其他库测试
            entry["error"] = f"{e.__class__.__name__}: {e}"
        lib_report.append(entry)

    hits.sort(key=lambda x: x.get("score") or 0.0, reverse=True)
    hits = hits[:max_top_k]
    return {
        "query": body.query,
        "top_k": max_top_k,
        "elapsed_ms": int((_time.monotonic() - started) * 1000),
        "libraries": lib_report,
        "hits": hits,
        "total": len(hits),
    }
