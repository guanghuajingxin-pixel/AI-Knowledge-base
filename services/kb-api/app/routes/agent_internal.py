"""DeerFlow 2.0 QA Sidecar 的内部接口（服务间调用，X-Internal-Token 鉴权）。

- GET  /api/v1/agent/bootstrap           向 sidecar 提供 LLM 引导配置
- POST /api/v1/internal/kb/retrieve       供 sidecar 的 knowledge_search 工具调用统一知识库检索
- POST /api/v1/internal/dingtalk/search   供 sidecar 的 dingtalk_search 工具调用钉钉知识库检索
- POST /api/v1/internal/dingtalk/content  供 sidecar 的 dingtalk_read_doc 工具读取钉钉文档正文
"""
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kb_common.database import get_session

router = APIRouter(prefix="/api/v1", tags=["agent-internal"])

_DEFAULT_TOKEN = "kge-internal-dev-token"

# dws CLI 路径（钉钉 Workspace CLI，用于读取钉钉在线文档正文与下载钉盘文件）
_DWS_BIN = os.getenv("DWS_BIN", str(Path(__file__).resolve().parents[4] / "tools" / "dws" / "dws"))
_DWS_TIMEOUT = 120.0
# dws 并发保护：多会话并行问答时会同时读钉钉文档，钉钉 API 限频下未处理的
# dws 失败会变成 500。按既定约定全局限流（并发≤3 + 0.4s 节流）并对瞬时失败重试 1 次。
_DWS_SEM = asyncio.Semaphore(3)
_DWS_MIN_INTERVAL = 0.4
_last_dws_ts = 0.0


async def _verify_internal_token(x_internal_token: str | None = Header(default=None, alias="X-Internal-Token")):
    expected = os.getenv("KB_INTERNAL_TOKEN", _DEFAULT_TOKEN)
    if x_internal_token != expected:
        raise HTTPException(status_code=401, detail="invalid internal token")


async def _effective_setting(s: AsyncSession, key: str) -> str:
    """DB settings 表优先，回退环境变量/默认 settings。"""
    from kb_common.config import get_settings
    from kb_common.models import Setting

    row = (await s.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
    return (row.value if row else None) or getattr(get_settings(), key, "") or ""


class RetrieveIn(BaseModel):
    query: str
    dataset_ids: list[str] | None = None          # Dify 数据集
    ragflow_dataset_ids: list[str] | None = None   # RAGFlow 数据集
    kb_ids: list[str] | None = None                # 平台本地 ES 知识库
    doc_library_ids: list[int] | None = None       # 平台文档库（knowledge_libraries，本地分段检索）
    top_k: int = 8
    thread_id: str = ""                            # 问答线程 ID：回溯用户身份以执行脱敏策略


async def _resolve_thread_user(s: AsyncSession, thread_id: str):
    """从问答线程 ID 回溯用户身份 (user_id, role)，供脱敏策略联合判断。

    格式（见 search._prepare_qa）：UUID=会话 ID（长期记忆）；
    nomem-{会话UUID}-{rand}；anon-{用户UUID}-{rand}。解析失败按最严格 viewer 处理。
    """
    import uuid as _uuid
    from kb_common.models import ChatSession, User

    tid = (thread_id or "").strip()
    if not tid:
        return None, "viewer"
    if tid.startswith("anon-"):
        try:
            uid = _uuid.UUID(tid[5:41])
        except ValueError:
            return None, "viewer"
        u = (await s.execute(select(User).where(User.id == uid))).scalar_one_or_none()
        return (u.id if u else None), (u.role if u else "viewer")
    sid = None
    if tid.startswith("nomem-"):
        try:
            sid = _uuid.UUID(tid[6:42])
        except ValueError:
            return None, "viewer"
    else:
        try:
            sid = _uuid.UUID(tid)
        except ValueError:
            return None, "viewer"
    sess = (await s.execute(select(ChatSession).where(ChatSession.id == sid))).scalar_one_or_none()
    if not sess or not sess.user_id:
        return None, "viewer"
    u = (await s.execute(select(User).where(User.id == sess.user_id))).scalar_one_or_none()
    return sess.user_id, (u.role if u else "viewer")


async def _mask_pre_llm(s: AsyncSession, hits: list[dict], query: str, *,
                        thread_id: str = "", scope_ids: set[str] | None = None,
                        scene: str = "chat", text_fields: tuple[str, ...] = ("content", "text"),
                        title_fields: tuple[str, ...] = ("document_title", "title")) -> tuple[list[dict], dict | None]:
    """送LLM前脱敏（底线节点）：LLM 不接触未脱敏明文。

    返回 (脱敏后 hits, masking 元信息)。引擎失败按失败策略兜底：
    block=清空阻断 / deny=提示无权限 / non_sensitive=仅按内置正则降级遮蔽。
    """
    from app.services import masking as masking_svc

    user_id, role = await _resolve_thread_user(s, thread_id)
    ctx = await masking_svc.load_mask_context(
        s, scene=scene, user_role=role, user_id=user_id,
        scope_ids=scope_ids or set(), node="pre_llm")
    if ctx is None:
        return hits, None
    gcfg = await masking_svc.get_global_config(s)
    hint = gcfg.get("hint") or "部分内容因权限隐藏"
    try:
        kept, _detail, summary = masking_svc.mask_hits(hits, ctx, text_fields=text_fields)
        for h in kept:
            for tf in title_fields:
                if h.get(tf):
                    h[tf] = masking_svc.mask_title(h[tf], ctx)
        masked_count = sum(h.count for h in summary)
        if masked_count:
            masking_svc.log_masking_async(
                user_id=user_id, username="", scene=scene, node="pre_llm",
                query=masking_svc.mask_text(query or "", ctx)[0],
                policy_ids=ctx.policy_ids, rule_hits=summary, masked_count=masked_count,
                exempted=bool(ctx.exempt_types))
        return kept, {"applied": True, "masked_count": masked_count}
    except Exception:  # noqa: BLE001 - 引擎失败走兜底，绝不让明文带出
        import logging
        logging.getLogger(__name__).exception("pre-LLM masking engine failed")
        strict = 0
        strategy = gcfg.get("failure_strategy", "non_sensitive")
        for p in ctx.policies:
            fs = (p.get("failure_strategy") or "") if isinstance(p, dict) else None
            if fs not in masking_svc.FAILURE_STRATEGIES:
                continue
            rank = {"block": 3, "deny": 2, "non_sensitive": 1}[fs]
            strict = max(strict, rank)
            strategy = {3: "block", 2: "deny", 1: "non_sensitive"}[strict]
        if strategy == "block":
            masking_svc.log_masking_async(
                user_id=user_id, username="", scene=scene, node="pre_llm",
                query="", policy_ids=ctx.policy_ids, rule_hits=[], masked_count=0, blocked=True)
            return [], {"applied": True, "blocked": True}
        if strategy == "deny":
            masking_svc.log_masking_async(
                user_id=user_id, username="", scene=scene, node="pre_llm",
                query="", policy_ids=ctx.policy_ids, rule_hits=[], masked_count=0, blocked=True)
            return [], {"applied": True, "blocked": True, "message": hint}
        fb = masking_svc.MaskContext()
        fb.actions = {et: "partial" for et in ("phone", "id_card", "bank_card", "email")}
        kept, _d, summary = masking_svc.mask_hits(hits, fb, text_fields=text_fields)
        return kept, {"applied": True, "masked_count": sum(h.count for h in summary), "degraded": True}


@router.post("/internal/kb/retrieve", dependencies=[Depends(_verify_internal_token)])
async def internal_kb_retrieve(body: RetrieveIn, s: AsyncSession = Depends(get_session)):
    """企业知识库检索：供 DeerFlow knowledge_search 工具调用。

    检索结果会自动附带知识加工元数据（文档摘要、标签、文档类型），
    帮助智能体理解命中文档的全貌，而不仅仅是片段内容。
    """
    from kb_common.clients import dify_client, ragflow_client
    from kb_common.config import get_settings
    from kb_common.models import ProcessedDocument, KnowledgeLibrary

    settings = get_settings()
    # 运行时写入 lru_cached settings（与问答链路一致）
    settings.dify_base_url = await _effective_setting(s, "dify_base_url")
    settings.dify_api_key = await _effective_setting(s, "dify_api_key")
    settings.ragflow_base_url = await _effective_setting(s, "ragflow_base_url")
    settings.ragflow_api_key = await _effective_setting(s, "ragflow_api_key")

    dataset_ids = list(body.dataset_ids or [])
    ragflow_ids = list(body.ragflow_dataset_ids or [])
    doc_library_ids = [int(i) for i in (body.doc_library_ids or []) if i]
    # 检索目标由问答链路按智能体配置解析后显式传入；全空=不检索
    # （知识库为智能体的资源配置项，未选择=不使用），不设「全部启用库」兜底

    hits: list[dict] = []
    # 检索源级告警：部分库/引擎失败时 fail-soft（不整轮 502），附在响应里供上层感知
    retrieve_warnings: list[str] = []
    if dataset_ids:
        try:
            hits = await dify_client.retrieve(dataset_ids, body.query, top_k=body.top_k)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"dify retrieve failed: {e.__class__.__name__}: {e}") from e
    if ragflow_ids:
        try:
            r = await ragflow_client.retrieve_with_report(ragflow_ids, body.query, top_k=body.top_k)
            hits = hits + r["hits"]
            for sk in r.get("skipped") or []:
                retrieve_warnings.append(f"RAGFlow 库 {sk.get('dataset_id')} 检索失败：{sk.get('error')}")
        except ragflow_client.RagflowNotConfigured:
            pass  # RAGFlow 未配置：跳过该来源，不影响 Dify/本地结果（fail-soft 于多源并集）
        except Exception as e:  # noqa: BLE001
            # RAGFlow 整体异常不再让本轮检索 502：记录告警并返回 Dify/本地结果
            import logging
            logging.getLogger(__name__).warning("ragflow retrieve failed: %s", e)
            retrieve_warnings.append(f"RAGFlow 检索失败：{e.__class__.__name__}: {e}")

    # 本地知识库通道（ES hybrid + rerank）：与 Dify 并列，回查 DB 校验软删除可见性
    kb_ids = [k for k in (body.kb_ids or []) if k]
    local_hits: list[dict] = []
    if kb_ids:
        from uuid import UUID

        from kb_common.database import SessionLocal
        from kb_common.models import Document
        from kb_common.rag import searcher
        try:
            raw = await asyncio.wait_for(
                searcher.hybrid(kb_ids, body.query, body.top_k, rerank=True), 60)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502,
                               detail=f"local kb retrieve failed: {e.__class__.__name__}: {e}") from e
        ids = []
        for h in raw:
            try:
                ids.append(UUID(str(h.get("document_id"))))
            except ValueError:
                continue
        async with SessionLocal() as session:
            docs = (await session.execute(select(Document).where(
                Document.id.in_(ids),
                Document.kb_id.in_([UUID(k) for k in kb_ids]),
                Document.is_deleted.is_(False),
            ))).scalars().all()
        allowed = {str(d.id): d for d in docs}
        for h in raw:
            doc = allowed.get(str(h.get("document_id")))
            if not doc:
                continue
            local_hits.append({**h, "content": h.get("text", "") or h.get("content", ""),
                               "document_title": doc.original_filename, "source": "local"})

    # 平台文档库通道（本地分段 + 向量/词项评分）：与 Dify/RAGFlow 并列；
    # 检索参数缺省时按库级「检索设置」(engine_config.retrieval) 取默认（与检索测试同口径）。
    # 逐库隔离：单库失败记入告警，不影响其他库与外部引擎结果。
    if doc_library_ids:
        from app.services.library_retrieval import search as doc_library_search
        libs = (await s.execute(select(KnowledgeLibrary).where(
            KnowledgeLibrary.id.in_(doc_library_ids),
            KnowledgeLibrary.library_type == "document",
        ))).scalars().all()
        for lib in libs:
            try:
                rcfg = (lib.engine_config or {}).get("retrieval") or {}
                lib_hits = await asyncio.wait_for(doc_library_search(
                    s, lib, body.query,
                    int(rcfg.get("top_k") or body.top_k),
                    rcfg.get("mode") or "hybrid",
                    threshold=float(rcfg.get("score_threshold") or 0.0),
                    vector_weight=float(rcfg.get("vector_weight") or 0.7),
                    rerank=bool(rcfg.get("rerank")),
                    rerank_model_id=str(rcfg["rerank_model_id"]) if rcfg.get("rerank_model_id") else None,
                ), 60)
                for h in lib_hits:
                    local_hits.append({**h, "source": "local",
                                       "library_id": lib.id, "library_name": lib.name})
            except Exception as e:  # noqa: BLE001 - 单库失败 fail-soft，附告警供上层感知
                import logging
                logging.getLogger(__name__).warning(
                    "document library %s retrieve failed: %s", lib.id, e)
                retrieve_warnings.append(f"文档库 {lib.name} 检索失败：{e.__class__.__name__}")

    # 富化：为命中的文档附加加工元数据（摘要/标签/类型）
    doc_ids = list({h.get("document_id") for h in hits if h.get("document_id")})
    enriched_meta: dict[str, dict] = {}
    dt_map: dict = {}
    if doc_ids:
        rows = (await s.execute(
            select(ProcessedDocument).where(ProcessedDocument.document_id.in_(doc_ids))
        )).scalars().all()
        for r in rows:
            enriched_meta[r.document_id] = {
                "summary": r.summary,
                "tags": r.tags,
                "doc_type": r.doc_type,
            }

        # 钉钉来源回填：同步自钉钉知识库的 Dify 文档，附上钉钉原始文档链接与节点 ID，
        # 使智能体引用来源可直接跳转钉钉知识库预览（而非独立预览组件）。
        # 覆盖两个来源：手动同步（dify_dingtalk_doc_mappings）与定时同步引擎（sync_document_mappings）。
        from kb_common.models import DifyDingtalkDocMapping, SyncDocumentMapping
        dt_map = {}
        manual = (await s.execute(
            select(DifyDingtalkDocMapping).where(DifyDingtalkDocMapping.dify_document_id.in_(doc_ids))
        )).scalars().all()
        for r in manual:
            dt_map[r.dify_document_id] = (r.dingtalk_node_id, r.dingtalk_url)
        # 定时同步引擎已记录但手动表缺失的，用 node_id 拼 alidocs 链接补齐
        missing = [d for d in doc_ids if d not in dt_map]
        if missing:
            cron_rows = (await s.execute(
                select(SyncDocumentMapping)
                .where(SyncDocumentMapping.dify_document_id.in_(missing))
                .where(SyncDocumentMapping.node_id.isnot(None))
            )).scalars().all()
            for r in cron_rows:
                if r.dify_document_id and r.node_id:
                    dt_map[r.dify_document_id] = (r.node_id, f"https://alidocs.dingtalk.com/i/nodes/{r.node_id}")

    for h in hits:
        did = h.get("document_id")
        if did and did in enriched_meta:
            h["doc_meta"] = enriched_meta[did]
        mapping = dt_map.get(did) if did else None
        if mapping:
            h["url"] = mapping[1]
            h["node_id"] = mapping[0]
            h["source"] = "dingtalk"

    if local_hits:
        hits = hits + local_hits

    # 送LLM前脱敏（底线节点）：LLM 不得接触未脱敏明文
    scope_ids = {f"kb:{k}" for k in kb_ids if k}
    scope_ids |= {f"library:{i}" for i in doc_library_ids}
    libs = (await s.execute(select(KnowledgeLibrary))).scalars().all()
    ds2lib = {r.dataset_id: r.id for r in libs}
    scope_ids |= {f"library:{ds2lib[d]}" for d in list(dataset_ids) + list(ragflow_ids) if d in ds2lib}
    hits, masking_meta = await _mask_pre_llm(
        s, hits, body.query, thread_id=body.thread_id, scope_ids=scope_ids,
        scene="chat", text_fields=("content", "text"), title_fields=("document_title", "title"))
    resp = {"results": hits, "total": len(hits)}
    if retrieve_warnings:
        resp["warnings"] = retrieve_warnings
    if masking_meta:
        resp["masking"] = masking_meta
    return resp


@router.post("/internal/process/context-expand", dependencies=[Depends(_verify_internal_token)])
async def internal_context_expand(body: dict, s: AsyncSession = Depends(get_session)):
    """上下文扩展：供 DeerFlow 智能体调用，根据命中文档 ID 获取摘要+关联文档+标签。

    body: {"document_ids": [...], "query": "...", "max_related": 3}
    """
    from app.services.processing import expand_context
    document_ids = body.get("document_ids") or []
    query = body.get("query") or ""
    max_related = int(body.get("max_related") or 3)
    return await expand_context(s, document_ids, query, max_related)


class DingTalkSearchIn(BaseModel):
    query: str
    top_k: int = 10
    user_id: str | None = None   # 终端用户 ID（可选）：传入时按其钉钉权限做工作区级隔离


@router.post("/internal/dingtalk/search", dependencies=[Depends(_verify_internal_token)])
async def internal_dingtalk_search(body: DingTalkSearchIn,
                                   s: AsyncSession = Depends(get_session)):
    """钉钉知识库关键词检索：供 DeerFlow dingtalk_search 工具调用。

    基于**持久化快照**（知识加工 →「钉钉知识」页手动刷新写入）做关键词模糊匹配
    （文件名 + 目录路径）。钉钉开放平台无语义检索 API，仅支持按元数据匹配。
    本接口只读快照，不触发钉钉全量遍历（遍历仅由「刷新」按钮触发）。
    携带 user_id 时按该用户钉钉可见的工作库做工作区级隔离。
    """
    from app.routes.knowledge_center import _dingtalk_cache, ensure_dingtalk_files_loaded, _visible_workspace_ids
    from kb_common.clients import dingtalk_client

    # 确保钉钉配置已加载
    try:
        await dingtalk_client.sync_runtime_config()
        if not dingtalk_client.is_configured():
            return {"results": [], "total": 0, "error": "钉钉未配置"}
    except Exception as e:  # noqa: BLE001
        return {"results": [], "total": 0, "error": f"钉钉配置加载失败：{e}"}

    # 只读持久化快照（首次访问时从库载入）
    await ensure_dingtalk_files_loaded(s)
    cached_files = _dingtalk_cache.get("files") or []

    visible, scope_err = (None, None)
    if body.user_id:
        visible, scope_err = await _visible_workspace_ids(body.user_id)

    if not cached_files:
        return {
            "results": [],
            "total": 0,
            "loading": _dingtalk_cache.get("loading", False),
            "error": _dingtalk_cache.get("error", ""),
        }
    if visible is not None:
        cached_files = [f for f in cached_files if f.get("workspace_id") in visible]

    # 关键词分词匹配（所有词都必须出现在文件名或目录路径中）
    keywords = [k.strip().lower() for k in body.query.split() if k.strip()]
    matched = []
    for f in cached_files:
        name = (f.get("name") or "").lower()
        path = (f.get("directory_path") or "").lower()
        combined = f"{name} {path}"
        if all(kw in combined for kw in keywords):
            matched.append(f)

    # 按名称相关度排序（关键词在名称中的位置越前越相关）
    matched.sort(key=lambda f: (f.get("name") or "").lower().find(keywords[0]) if keywords else 999)

    top = matched[: body.top_k]
    results = [
        {
            "title": f.get("name") or "",
            "directory": f.get("directory_path") or "/",
            "workspace": f.get("workspace_name") or "",
            "url": f.get("url") or "",
            "node_id": f.get("node_id") or "",
            "extension": f.get("extension") or "",
            "creator": f.get("creator_name") or f.get("creator_id") or "",
            "created_at": f.get("created_at") or "",
        }
        for f in top
    ]
    return {"results": results, "total": len(results), "matched_total": len(matched),
            **({"error": scope_err} if scope_err else {})}


@router.get("/agent/bootstrap", dependencies=[Depends(_verify_internal_token)])
async def agent_bootstrap(s: AsyncSession = Depends(get_session)):
    """向 DeerFlow sidecar 提供 LLM 引导配置。"""
    base_url = await _effective_setting(s, "llm_base_url")
    api_key = await _effective_setting(s, "llm_api_key")
    model = await _effective_setting(s, "llm_model")
    if not api_key or not model:
        raise HTTPException(status_code=409, detail="LLM not configured in system settings")

    from app.services.agent.config import load_agent_config, resolve_agent_model

    agent_cfg = await load_agent_config(s)
    return {
        "model": resolve_agent_model(agent_cfg, model),
        "base_url": base_url or "https://api.deepseek.com/v1",
        "api_key": api_key,
        "temperature": float(agent_cfg.get("temperature", 0.7)),
        "top_p": float(agent_cfg.get("top_p", 0.9)),
        "max_tokens": int(agent_cfg.get("max_tokens", 4096)),
        "agent_name": agent_cfg.get("agent_name", "杰克百晓生"),
    }


# ---------------------------------------------------------------------------
# 钉钉文档正文读取（dws CLI）
# ---------------------------------------------------------------------------

class DingTalkContentIn(BaseModel):
    node_id: str
    extension: str = ""
    title: str = ""
    thread_id: str = ""   # 问答线程 ID：回溯用户身份以执行脱敏策略


async def _mask_dingtalk_content(s: AsyncSession, data: dict, thread_id: str) -> dict:
    """钉钉文档正文送LLM前脱敏：与知识检索同一底线节点、同一策略引擎。"""
    from app.services import masking as masking_svc

    ctx = None
    try:
        user_id, role = await _resolve_thread_user(s, thread_id)
        ctx = await masking_svc.load_mask_context(
            s, scene="chat", user_role=role, user_id=user_id, scope_ids=set(), node="pre_llm")
    except Exception:  # noqa: BLE001 - 脱敏装载失败不阻断正文返回，引擎层仍兜底
        ctx = None
    if ctx is None:
        return data
    content = data.get("content") or ""
    try:
        masked, summary = masking_svc.mask_text(content, ctx)
        if summary:
            masked_count = sum(h.count for h in summary)
            masking_svc.log_masking_async(
                user_id=user_id, username="", scene="chat", node="pre_llm",
                query=(data.get("title") or "")[:200], policy_ids=ctx.policy_ids,
                rule_hits=summary, masked_count=masked_count, exempted=bool(ctx.exempt_types))
            data["content"] = masked
            data["masking"] = {"applied": True, "masked_count": masked_count}
    except Exception:  # noqa: BLE001 - 引擎失败降级为内置正则遮蔽
        fb = masking_svc.MaskContext()
        fb.actions = {et: "partial" for et in ("phone", "id_card", "bank_card", "email")}
        data["content"] = masking_svc.mask_text(content, fb)[0]
        data["masking"] = {"applied": True, "degraded": True}
    return data


# 可由 dws doc read 直读 Markdown 的在线/文本类文档扩展名
_DT_ONLINE_EXTS = {"adoc", "md", "markdown", "txt", "html", "htm"}
# 可下载后本地解析的办公文档
_DT_OFFICE_EXTS = {"doc", "docx", "pdf", "xlsx", "xls", "ppt", "pptx", "csv", "rtf", "wps"}


async def _dingtalk_cached_files(s: AsyncSession):
    """获取钉钉快照文件列表（首次访问时从数据库载入；不触发遍历）。"""
    from app.routes.knowledge_center import _dingtalk_cache, ensure_dingtalk_files_loaded
    await ensure_dingtalk_files_loaded(s)
    return _dingtalk_cache.get("files") or [], _dingtalk_cache.get("loading", False)


class DingTalkBrowseIn(BaseModel):
    action: str = "map"          # map=知识目录地图；list=列出目录下文件
    directory: str = ""          # list：目录关键词（模糊匹配 directory_path 或 workspace）
    top_k: int = 30


@router.post("/internal/dingtalk/browse", dependencies=[Depends(_verify_internal_token)])
async def internal_dingtalk_browse(body: DingTalkBrowseIn,
                                   s: AsyncSession = Depends(get_session)):
    """钉钉知识库目录浏览：供 DeerFlow dingtalk_browse 工具调用。

    - action=map：返回知识库→目录结构（2 级）及文档数/在线文档数，
      供智能体预判问题答案可能在哪个目录；
    - action=list：列出匹配目录下的文档，在线文档（adoc/md/txt）优先，
      返回 node_id/title/extension，供 dingtalk_read_doc 逐篇读取。
    只读持久化快照，不触发钉钉全量遍历。
    """
    files, loading = await _dingtalk_cached_files(s)
    if not files:
        return {"results": [], "loading": loading,
                "error": "钉钉文件列表加载中" if loading else "钉钉知识库暂无快照，请在「知识加工 → 钉钉知识」点「刷新」同步"}

    if body.action == "map":
        return _browse_map(files)
    return _browse_list(files, body.directory, body.top_k)


def _browse_map(files: list[dict]) -> dict:
    """聚合知识库目录地图：workspace → 一级/二级目录 → 文档计数。"""
    workspaces: dict[str, dict] = {}
    for f in files:
        ext = (f.get("extension") or "").lower()
        if ext not in _DT_ONLINE_EXTS and ext not in _DT_OFFICE_EXTS:
            continue  # 跳过音视频/图片/压缩包等非文档
        ws = f.get("workspace_name") or "未分组"
        path = (f.get("directory_path") or "/").strip("/")
        parts = [p for p in path.split("/") if p][:2]  # 最多 2 级目录
        ws_entry = workspaces.setdefault(ws, {"name": ws, "docs": 0, "online_docs": 0, "dirs": {}})
        online = ext in _DT_ONLINE_EXTS
        ws_entry["docs"] += 1
        ws_entry["online_docs"] += 1 if online else 0
        if parts:
            dkey = "/" + "/".join(parts)
            d = ws_entry["dirs"].setdefault(dkey, {"path": dkey, "docs": 0, "online_docs": 0})
            d["docs"] += 1
            d["online_docs"] += 1 if online else 0

    ws_list = sorted(workspaces.values(), key=lambda w: -w["docs"])[:12]
    for ws in ws_list:
        ws["dirs"] = sorted(ws["dirs"].values(), key=lambda d: -d["docs"])[:15]
    return {"action": "map", "workspaces": ws_list, "total_docs": sum(w["docs"] for w in workspaces.values())}


def _browse_list(files: list[dict], directory: str, top_k: int) -> dict:
    """列出匹配目录下的文档；在线文档优先，办公文档其次。"""
    kw = (directory or "").strip().lower()
    online, office = [], []
    for f in files:
        ext = (f.get("extension") or "").lower()
        if ext not in _DT_ONLINE_EXTS and ext not in _DT_OFFICE_EXTS:
            continue
        path = (f.get("directory_path") or "").lower()
        ws = (f.get("workspace_name") or "").lower()
        if kw and kw not in path and kw not in ws:
            continue
        item = {
            "title": f.get("name") or "",
            "node_id": f.get("node_id") or "",
            "extension": ext,
            "directory": f.get("directory_path") or "/",
            "workspace": f.get("workspace_name") or "",
            "url": f.get("url") or "",
            "online": ext in _DT_ONLINE_EXTS,
        }
        (online if ext in _DT_ONLINE_EXTS else office).append(item)

    # 在线文档优先；同类按修改时间（files 顺序近似）
    results = online + office
    return {"action": "list", "directory": directory, "total": len(results),
            "results": results[:max(1, min(top_k, 50))]}


async def _run_dws(args: list[str]) -> dict:
    """运行 dws CLI 并解析 JSON 输出（全局并发≤3 + 0.4s 节流 + 瞬时失败重试 1 次）。"""
    global _last_dws_ts
    if not Path(_DWS_BIN).exists():
        raise RuntimeError(f"dws CLI 不存在: {_DWS_BIN}")
    async with _DWS_SEM:
        for attempt in (1, 2):
            gap = _last_dws_ts + _DWS_MIN_INTERVAL - time.monotonic()
            if gap > 0:
                await asyncio.sleep(gap)
            _last_dws_ts = time.monotonic()
            proc = await asyncio.create_subprocess_exec(
                _DWS_BIN, *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_DWS_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                if attempt == 2:
                    raise RuntimeError("dws 调用超时")
                continue
            out = stdout.decode("utf-8", errors="ignore").strip()
            err = ""
            if not out:
                err = f"dws 无输出: {stderr.decode('utf-8', errors='ignore')[:200]}"
            else:
                try:
                    return json.loads(out)
                except json.JSONDecodeError:
                    err = f"dws 输出非 JSON: {out[:300]}"
            if attempt == 2:
                raise RuntimeError(err)
            await asyncio.sleep(1.0)  # 限频/瞬时失败退避后重试


def _dingtalk_doc_url(node_id: str) -> str:
    """从钉钉文件缓存按 node_id 查网页链接（无缓存/未命中返回空串）。"""
    from app.routes.knowledge_center import _dingtalk_cache
    for f in _dingtalk_cache.get("files") or []:
        if (f.get("node_id") or "") == node_id:
            return f.get("url") or ""
    return ""


@router.post("/internal/dingtalk/content", dependencies=[Depends(_verify_internal_token)])
async def internal_dingtalk_content(body: DingTalkContentIn, s: AsyncSession = Depends(get_session)):
    """读取钉钉文档正文内容：供 DeerFlow dingtalk_read_doc 工具调用。

    - 在线文档（adoc/markdown/md）：`dws doc read` 直接返回 Markdown
    - 二进制文件（docx/pdf/xlsx 等）：`dws drive download` 下载后用 MinerU/本地解析器转 Markdown
    - 返回携带 `url`（钉钉网页链接），供前端把答案中的《文档名》转为可点击的源文档预览链接
    """
    ext = (body.extension or "").lower()
    node_id = body.node_id.strip()
    if not node_id:
        raise HTTPException(status_code=400, detail="node_id 不能为空")

    doc_url = _dingtalk_doc_url(node_id)

    # 1) 在线文档：直接读 Markdown
    if ext in ("adoc", "markdown", "md", ""):
        try:
            data = await _run_dws(["doc", "read", "--node", node_id, "-f", "json"])
            if data.get("success") is False or "error" in data:
                # 可能是二进制文件（extension 为空时兜底）
                pass
            else:
                return await _mask_dingtalk_content(s, {
                    "title": data.get("title") or body.title,
                    "content": data.get("markdown") or "",
                    "format": "markdown",
                    "node_id": node_id,
                    "url": doc_url,
                }, body.thread_id)
        except Exception:
            # 在线文档读取失败，降级到下载解析
            pass

    # 2) 二进制文件：下载到临时目录，再解析
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            await _run_dws([
                "drive", "download",
                "--node", node_id,
                "--output", tmpdir,
                "--overwrite",
                "-f", "json",
            ])
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"钉钉文档下载失败：{e}") from e

        files = list(Path(tmpdir).iterdir())
        if not files:
            raise HTTPException(status_code=502, detail="钉钉文档下载后未找到文件")
        local_file = files[0]
        file_bytes = local_file.read_bytes()
        filename = body.title or local_file.name

        # 二进制办公文档：MinerU 为首要解析引擎（本地自托管 mineru-api → 云 API），
        # 支持 pptx/ppt/pdf/doc/docx/xls/xlsx 的版面/表格/扫描件 OCR；
        # 引擎不可用或返回空时，降级到本地即时解析（python-pptx/docx/openpyxl/pdfplumber）。
        mineru_error: str | None = None
        try:
            from kb_common.clients import mineru_client
            mineru_api_key = await _effective_setting(s, "mineru_api_key") or None
            parsed = await mineru_client.parse(file_bytes, filename, api_key=mineru_api_key)
            md = (parsed.get("markdown") or "").strip()
            if md:
                return await _mask_dingtalk_content(s, {
                    "title": body.title or local_file.name,
                    "content": md,
                    "format": "markdown",
                    "node_id": node_id,
                    "url": doc_url,
                    "engine": "mineru",
                }, body.thread_id)
            mineru_error = "MinerU 返回空内容"
        except Exception as e:  # noqa: BLE001 - 引擎失败时尝试本地兜底
            mineru_error = f"{e.__class__.__name__}: {e}"

        local_md = _local_parse(file_bytes, filename)
        if local_md and local_md.strip():
            return await _mask_dingtalk_content(s, {
                "title": body.title or local_file.name,
                "content": local_md,
                "format": "markdown",
                "node_id": node_id,
                "url": doc_url,
                "engine": "local",
            }, body.thread_id)

        raise HTTPException(
            status_code=502,
            detail=f"文档解析失败（MinerU 与本地解析均未取得正文）：{mineru_error}",
        )


def _local_parse(file_bytes: bytes, filename: str) -> str | None:
    """本地即时解析常见文本/办公文档为 Markdown（无需外部服务）。

    - txt/md/csv：UTF-8 直读；
    - docx：正文段落 + 表格 + 文本框（w:txbxContent）；
    - ppt/pptx：逐页抽取形状文本、表格、组合形状与备注；
    - xlsx/xls：各工作表表格化；pdf：pdfplumber 抽文本层。
    不支持的格式或解析异常返回 None，由调用方降级 MinerU。
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    from io import BytesIO
    try:
        if ext in ("txt", "md", "markdown", "csv"):
            return file_bytes.decode("utf-8", errors="ignore")

        if ext == "docx":
            import docx as docx_lib  # python-docx
            doc = docx_lib.Document(BytesIO(file_bytes))
            parts: list[str] = [p.text for p in doc.paragraphs if p.text.strip()]
            # 表格
            for tbl in doc.tables:
                rows = []
                for row in tbl.rows:
                    cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                    if any(cells):
                        rows.append("| " + " | ".join(cells) + " |")
                if rows:
                    parts.append("\n".join(rows))
            # 文本框/形状内文字（python-docx 默认不抽 w:txbxContent）
            try:
                ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                for txbx in doc.element.body.iter(
                    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}txbxContent"
                ):
                    txt = "".join(t.text or "" for t in txbx.iter(ns["w"] + "t")).strip()
                    if txt:
                        parts.append(txt)
            except Exception:
                pass
            return "\n".join(parts) if parts else None

        if ext in ("pptx", "ppt"):
            from pptx import Presentation  # python-pptx

            prs = Presentation(BytesIO(file_bytes))

            def _shape_text(shape) -> list[str]:
                out: list[str] = []
                # 组合形状：递归
                if shape.shape_type == 6:  # MSO_SHAPE_TYPE.GROUP
                    for sub in shape.shapes:
                        out.extend(_shape_text(sub))
                    return out
                if getattr(shape, "has_text_frame", False) and shape.text_frame:
                    t = shape.text_frame.text.strip()
                    if t:
                        out.append(t)
                if getattr(shape, "has_table", False):
                    for row in shape.table.rows:
                        cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                        if any(cells):
                            out.append("| " + " | ".join(cells) + " |")
                return out

            slides_md: list[str] = []
            for idx, slide in enumerate(prs.slides, 1):
                lines: list[str] = [f"## 幻灯片 {idx}"]
                for shape in slide.shapes:
                    lines.extend(_shape_text(shape))
                try:
                    notes = slide.notes_slide.notes_text_frame.text.strip()
                    if notes:
                        lines.append(f"> 备注：{notes}")
                except Exception:
                    pass
                body = "\n".join(l for l in lines if l and l != f"## 幻灯片 {idx}")
                if body:
                    slides_md.append(f"## 幻灯片 {idx}\n{body}")
            return "\n\n".join(slides_md) if slides_md else None

        if ext in ("xlsx", "xls"):
            from openpyxl import load_workbook
            wb = load_workbook(BytesIO(file_bytes), read_only=True, data_only=True)
            lines: list[str] = []
            for ws in wb.worksheets:
                lines.append(f"## {ws.title}")
                for row in ws.iter_rows(values_only=True):
                    cells = [str(c) if c is not None else "" for c in row]
                    if any(cells):
                        lines.append("| " + " | ".join(cells) + " |")
            return "\n".join(lines)

        if ext == "pdf":
            import pdfplumber
            text_parts: list[str] = []
            with pdfplumber.open(BytesIO(file_bytes)) as pdf:
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        text_parts.append(t)
            return "\n".join(text_parts) if text_parts else None
    except Exception:
        return None
    return None
