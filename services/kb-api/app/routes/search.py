import asyncio
import json
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Literal
from uuid import UUID, uuid4
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from kb_common.database import get_session, SessionLocal
from kb_common.models import Document, UsageLog
from kb_common.rag import searcher, tracer
from kb_common.rag.embedder import EmbeddingError
from app.deps import get_current_user, get_principal

router = APIRouter(prefix="/api/v1/search", tags=["search"])
# Keep only the current question thread when conversation memory is disabled.
# Choices are part of that same question, not a new independent question.
_TURN_THREADS: dict[tuple[str, str], tuple[float, str]] = {}



class SearchIn(BaseModel):
    query: str
    kb_ids: list[str]
    top_k: int = Field(default=10, ge=1, le=100)
    search_type: str = "hybrid"   # hybrid | semantic | keyword
    filters: dict | None = None
    score_threshold: float = Field(default=0, ge=0, le=1)
    rerank_model_id: UUID | None = None
    rerank: bool | None = None     # None=按接口默认（正式检索开启、测试关闭）；显式指定则覆盖


def _log_usage(user_id, scene: str, query: str,
               hit_count: int, doc_ids: list[str], doc_titles: list[str]):
    """后台记录调用日志（fire-and-forget，使用独立 session，异常不影响主流程）。"""
    async def _run():
        try:
            async with SessionLocal() as s:
                s.add(UsageLog(
                    user_id=user_id,
                    scene=scene,
                    query=query,
                    hit_count=hit_count,
                    hit_document_ids=doc_ids[:50],
                    hit_document_titles=doc_titles[:50],
                ))
                await s.commit()
        except Exception:
            pass
    asyncio.create_task(_run())


@router.post("")
async def search(body: SearchIn, u=Depends(get_principal), s: AsyncSession = Depends(get_session)):
    t0 = time.perf_counter()
    hits = await _dispatch(body, rerank=body.rerank if body.rerank is not None else True)
    doc_ids = {h.get("document_id") for h in hits if h.get("document_id")}
    docs = (await s.execute(select(Document).where(Document.id.in_(doc_ids)))).scalars().all()
    dmap = {str(d.id): (d.original_filename, d.storage_path, d.file_type) for d in docs}
    results = [tracer.trace(h, dmap) for h in hits]
    masking_meta = await _mask_search_results(s, u, body, results)
    took_ms = int((time.perf_counter() - t0) * 1000)
    # 埋点
    uid = getattr(u, "id", None)
    titles = [d.original_filename for d in docs]
    _log_usage(uid, "search", body.query, len(results), [str(d.id) for d in docs], titles)
    resp = {"results": results, "total": len(results), "took_ms": took_ms}
    if masking_meta:
        resp["masking"] = masking_meta
    return resp


@router.post("/test")
async def search_test(body: SearchIn, u=Depends(get_current_user), s: AsyncSession = Depends(get_session)):
    """检索测试：返回召回内容、K 值、Score，便于调参。rerank 默认关闭，可显式开启。"""
    rerank = body.rerank if body.rerank is not None else False
    # A selected profile is request-local; never mutate global model settings.
    candidate_body = body.model_copy(update={"top_k": 100}) if rerank else body
    try:
        hits = await _dispatch(candidate_body, rerank=False)
    except EmbeddingError as e:
        raise HTTPException(502, str(e))
    if rerank and hits:
        from app.services.library_retrieval import rerank_hits
        for hit in hits:
            hit['matched_content'] = hit.get('text', '')
        try:
            await rerank_hits(s, body.query, hits, str(body.rerank_model_id) if body.rerank_model_id else None)
        except ValueError as e:
            raise HTTPException(502, str(e))
        hits.sort(key=lambda h: h['score'], reverse=True)
    hits = [h for h in hits if h['score'] >= body.score_threshold][:body.top_k]
    results = [{"text": h.get("text"), "score": h.get("score"),
                "document_title": h.get("document_title"), "chunk_index": h.get("chunk_index"),
                "document_id": h.get("document_id"),
                "score_type": h.get("score_type"), "token_similarity": h.get("token_similarity"),
                "vector_similarity": h.get("vector_similarity"),
                "rerank_score": h.get("rerank_score"), "semantic_weight": h.get("semantic_weight")} for h in hits]
    masking_meta = await _mask_search_results(s, u, body, results)
    resp = {"k": body.top_k, "search_type": body.search_type, "rerank": rerank, "results": results}
    if masking_meta:
        resp["masking"] = masking_meta
    return resp


async def _dispatch(body: SearchIn, rerank: bool) -> list[dict]:
    """按 search_type 分发到 semantic/keyword/hybrid；faq 与未知值回退 hybrid。"""
    st = (body.search_type or "hybrid").lower()
    if st == "semantic":
        return await searcher.semantic(body.kb_ids, body.query, body.top_k, body.filters, rerank=rerank)
    if st == "keyword":
        return await searcher.keyword(body.kb_ids, body.query, body.top_k, body.filters, rerank=rerank)
    return await searcher.hybrid(body.kb_ids, body.query, body.top_k, body.filters, rerank=rerank)


async def _mask_search_results(s: AsyncSession, u, body: SearchIn, results: list[dict]) -> dict | None:
    """统一检索输出后脱敏（文本片段+文档标题），返回 masking 元信息。"""
    from app.services import masking as masking_svc

    role = getattr(u, "role", "") or "viewer"
    scope_ids = {f"kb:{k}" for k in (body.kb_ids or []) if k}
    ctx = await masking_svc.load_mask_context(
        s, scene="search", user_role=role, user_id=getattr(u, "id", None),
        scope_ids=scope_ids, node="post_output")
    if ctx is None:
        return None
    gcfg = await masking_svc.get_global_config(s)
    hint = gcfg.get("hint") or "部分内容因权限隐藏"
    try:
        total = 0
        merged = []
        for r in results:
            for f in ("text", "faq_answer", "document_title"):
                v = r.get(f)
                if not v:
                    continue
                m, hh = masking_svc.mask_text(v, ctx)
                if hh:
                    r[f] = m
                    total += sum(x.count for x in hh)
                    merged.extend(hh)
        if total:
            masking_svc.log_masking_async(
                user_id=getattr(u, "id", None), username=getattr(u, "username", "") or "",
                scene="search", node="post_output",
                query=masking_svc.mask_text(body.query or "", ctx)[0],
                policy_ids=ctx.policy_ids, rule_hits=merged, masked_count=total,
                exempted=bool(ctx.exempt_types))
            return {"applied": True, "masked_count": total, "hint": hint}
        return None
    except Exception:  # noqa: BLE001 - 引擎失败降级为内置正则遮蔽，不让明文带出
        import logging
        logging.getLogger(__name__).exception("search output masking failed")
        fb = masking_svc.MaskContext()
        fb.actions = {et: "partial" for et in ("phone", "id_card", "bank_card", "email")}
        total = 0
        for r in results:
            for f in ("text", "faq_answer"):
                v = r.get(f)
                if v:
                    m, _ = masking_svc.mask_text(v, fb)
                    r[f] = m
                    total += 1
        return {"applied": True, "masked_count": total, "hint": hint, "degraded": True} if total else None


class HistoryTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class ChatIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    kb_ids: list[UUID] = Field(default_factory=list, max_length=12)
    # 首选：知识库（检索抽象层）ID，后端按 platform 解析为 dify/ragflow 两组 dataset
    library_ids: list[int] | None = Field(default=None, max_length=24)
    # 兼容旧字段：知识源注册表 ID（推送路径登记，非管理员校验沿用）
    knowledge_source_ids: list[int] | None = Field(default=None, max_length=24)
    # 兼容旧前端：直接传 Dify 数据集 ID（缺省时按注册表兜底）
    dify_dataset_ids: list[UUID] | None = Field(default=None, max_length=12)
    top_k: int | None = Field(default=None, ge=1, le=20)
    last_query: str = Field(default="", max_length=2000)
    last_answer: str = Field(default="", max_length=8000)
    history: list[HistoryTurn] = Field(default_factory=list, max_length=16)
    model: str = Field(default="", max_length=200)
    llm_profile_id: str = Field(default="", max_length=100)
    session_id: str = Field(default="", max_length=100)
    action: Literal["", "continue", "stop"] = ""


async def _resolve_retrieval_targets(body: "ChatIn", user, session) -> tuple[list[str], list[str]]:
    """把选中的知识库解析为 (dify_dataset_ids, ragflow_dataset_ids)。

    真相源=知识库抽象层（knowledge_libraries，enabled 的 dify / ragflow 镜像）：
    - 传 library_ids：按知识库逐条解析（platform 决定通道），校验存在且启用；
    - 兼容旧 knowledge_source_ids / dify_dataset_ids：走知识源注册表解析（过渡保护）；
    - 都不传：默认全部启用的知识库镜像。
    检索策略（rerank、检索方式等）由抽象层按 platform 内部决定，与本解析无关。
    非管理员只能检索启用的库（越权 403）。
    """
    from kb_common.models import KnowledgeLibrary, KnowledgeSource

    is_admin = user.role in {"admin", "super_admin"}
    dify_ids: list[str] = []
    ragflow_ids: list[str] = []

    if body.library_ids is not None:
        rows = (await session.execute(select(KnowledgeLibrary))).scalars().all()
        by_id = {r.id: r for r in rows}
        if not is_admin:
            bad = [i for i in body.library_ids
                   if i not in by_id or not by_id[i].enabled]
            if bad:
                raise HTTPException(403, "无权检索未向企业问答开放的知识库")
        for lid in body.library_ids:
            r = by_id.get(lid)
            if not r:
                # 选到已删除的库时忽略，避免整轮失败
                continue
            (dify_ids if r.platform == "dify" else ragflow_ids).append(r.dataset_id)
    elif body.knowledge_source_ids is not None or body.dify_dataset_ids is not None:
        rows = (await session.execute(select(KnowledgeSource).where(
            KnowledgeSource.enabled == True,  # noqa: E712
            KnowledgeSource.source_type.in_(["dify_dataset", "ragflow_dataset"]),
        ))).scalars().all()
        by_id = {r.id: r for r in rows}
        allowed_dify = {r.external_id for r in rows if r.source_type == "dify_dataset"}

        if body.knowledge_source_ids is not None:
            if not is_admin:
                missing = [i for i in body.knowledge_source_ids if i not in by_id]
                if missing:
                    raise HTTPException(403, "无权检索未向企业问答开放的知识库")
            for sid in body.knowledge_source_ids:
                r = by_id.get(sid)
                if not r:
                    # 管理员可选到未启用/不存在的库时忽略，避免整轮失败
                    continue
                if r.source_type == "dify_dataset":
                    dify_ids.append(r.external_id)
                else:
                    ragflow_ids.append(r.external_id)
        else:
            # 兼容旧前端：直接传 Dify 数据集 ID。非管理员仍受注册表约束（越权即 403）。
            dify_ids = [str(d) for d in body.dify_dataset_ids]
            if not is_admin and set(dify_ids) - allowed_dify:
                raise HTTPException(403, "无权检索未向企业问答开放的数据集")
    else:
        # 缺省：知识库抽象层里全部启用的库
        rows = (await session.execute(select(KnowledgeLibrary).where(
            KnowledgeLibrary.enabled == True,  # noqa: E712
        ))).scalars().all()
        for r in rows:
            (dify_ids if r.platform == "dify" else ragflow_ids).append(r.dataset_id)

    dify_ids = list(dict.fromkeys(dify_ids))
    ragflow_ids = list(dict.fromkeys(ragflow_ids))
    if len(dify_ids) + len(ragflow_ids) > 24:
        raise HTTPException(422, "单次检索的知识库过多，请缩小范围")
    return dify_ids, ragflow_ids


async def _prepare_qa(body: ChatIn, user, session) -> dict:
    """组装 DeerFlow sidecar 调用参数（旧内置问答链已废弃，无回退）。"""
    from kb_common.models import KnowledgeBase
    from app.services.agent.config import load_agent_config
    from app.services.llm_resolver import resolve_llm_config

    if not body.query.strip():
        raise HTTPException(422, "问题不能为空")

    cfg = await load_agent_config(session)
    llm = await resolve_llm_config(session, body.llm_profile_id, body.model)
    # 检索目标：从知识源注册表解析为 dify / ragflow 两组 dataset（取代旧的 dify_dataset_ids 设置项）
    datasets, ragflow_datasets = await _resolve_retrieval_targets(body, user, session)
    local_ids = [str(k) for k in body.kb_ids]
    if local_ids:
        query = select(KnowledgeBase).where(KnowledgeBase.id.in_(body.kb_ids))
        if user.role not in {"admin", "super_admin"}:
            query = query.where(KnowledgeBase.owner_id == user.id)
        rows = (await session.execute(query)).scalars().all()
        if {str(r.id) for r in rows} != set(local_ids):
            raise HTTPException(403, "无权检索指定知识库")
    # 多轮记忆由 sidecar checkpointer 按 thread 承载：会话 ID 即 thread_id；
    # 关闭长期记忆或无会话时每次新 thread，不沿用上一轮上下文
    if body.session_id:
        from app.routes.chat_session_route import _get_owned
        owned = await _get_owned(body.session_id, user, session)
        if cfg.get("long_memory_enabled", True):
            thread_id = str(owned.id)
        else:
            key = (str(user.id), str(owned.id))
            now = time.monotonic()
            for old_key, (created, _) in list(_TURN_THREADS.items()):
                if now - created > 86400:
                    _TURN_THREADS.pop(old_key, None)
            if body.action:
                if key not in _TURN_THREADS:
                    raise HTTPException(409, "本次选择已过期，请重新提问")
                thread_id = _TURN_THREADS[key][1]
            else:
                thread_id = f"nomem-{owned.id}-{uuid4()}"
                _TURN_THREADS[key] = (now, thread_id)
                while len(_TURN_THREADS) > 1000:
                    _TURN_THREADS.pop(next(iter(_TURN_THREADS)))
    else:
        thread_id = f"anon-{user.id}-{uuid4()}"
    tools = cfg.get("tools_enabled") or {}
    if not tools.get("knowledge_search", True):
        datasets, ragflow_datasets, local_ids = [], [], []
    top_k = body.top_k if body.top_k is not None else int(cfg.get("top_k", 8))
    return {
        "query": body.query,
        "thread_id": thread_id,
        "dataset_ids": datasets,
        "ragflow_dataset_ids": ragflow_datasets,
        "kb_ids": local_ids,
        "top_k": top_k,
        "plan_mode": bool(cfg.get("planning_enabled", True)),
        "subagent_enabled": bool(cfg.get("subagent_enabled", False)),
        "disabled_tools": [k for k, v in tools.items() if not v],
        "llm_config": llm,
        "agent_config": cfg,
        "action": body.action,
    }


# Cancellation is scoped by authenticated user and session. Disconnect also cancels the generator.
_ACTIVE_QA: dict[tuple[str, str], asyncio.Task] = {}


async def _qa_events(params: dict):
    from app.services.agent.deerflow_runner import (DeerflowUnavailable,
                                                     cancel_deerflow_stream,
                                                     run_deerflow_stream)
    if not params["llm_config"].get("api_key"):
        yield {"type": "config_error", "code": "llm_not_configured", "message": "请先在系统配置中配置问答模型。"}
        return
    try:
        async with asyncio.timeout(660):
            async for event in run_deerflow_stream(**params):
                yield event
    except asyncio.TimeoutError:
        await cancel_deerflow_stream(params["thread_id"])
        yield {"type": "final", "result": {
            "answer": "检索或回答超时，尚未形成可靠答案，请稍后重试或缩小问题范围。",
            "citations": [], "follow_ups": [], "engine": "deerflow",
            "answer_status": "insufficient",
            "sufficiency": {"sufficient": False, "missing": "问答超时"},
            "quality": {"verification": "prompt", "dws": "not_needed", "warnings": ["问答超时"]},
            "rewritten_query": params["query"], "last_query": params["query"],
            "last_answer": "", "usage": {}}}
    except DeerflowUnavailable as exc:
        # fail-fast：sidecar 未就绪直接报错，不回退旧链
        yield {"type": "config_error", "code": "agent_not_ready",
               "message": f"DeerFlow 问答服务未就绪：{exc}。请确认 sidecar 已启动。"}
    except Exception:
        import logging
        logging.getLogger(__name__).exception("DeerFlow QA failed")
        yield {"type": "config_error", "code": "agent_failed", "message": "问答服务暂不可用，请稍后重试。"}


async def _mask_qa_events(params: dict, u, s: AsyncSession):
    """问答输出后二次过滤：流式 delta 增量脱敏 + final 答案/引用整体脱敏。

    送LLM前已在 /internal/kb/retrieve 出口脱敏（底线）；本节点为输出侧第二道防线，
    捕获未脱敏内容（如模型复述、历史会话回显）。确定性令牌保证流式与最终答案一致。
    """
    from app.services import masking as masking_svc
    from kb_common.models import KnowledgeLibrary

    try:
        scope_ids = {f"kb:{k}" for k in (params.get("kb_ids") or []) if k}
        ds = list(params.get("dataset_ids") or []) + list(params.get("ragflow_dataset_ids") or [])
        if ds:
            libs = (await s.execute(select(KnowledgeLibrary))).scalars().all()
            ds2lib = {r.dataset_id: r.id for r in libs}
            scope_ids |= {f"library:{ds2lib[d]}" for d in ds if d in ds2lib}
        ctx = await masking_svc.load_mask_context(
            s, scene="chat", user_role=getattr(u, "role", "") or "viewer",
            user_id=getattr(u, "id", None), scope_ids=scope_ids, node="post_output")
    except Exception:  # noqa: BLE001
        ctx = None
    if ctx is None:
        async for event in _qa_events(params):
            yield event
        return

    gcfg = await masking_svc.get_global_config(s)
    hint = gcfg.get("hint") or "部分内容因权限隐藏"
    dm = masking_svc.DeltaMasker(ctx)

    def _mask_cites(cites: list) -> int:
        n = 0
        for c in cites or []:
            if not isinstance(c, dict):
                continue
            for f in ("content", "quote", "text"):
                v = c.get(f)
                if v:
                    m, hh = masking_svc.mask_text(v, ctx)
                    if hh:
                        c[f] = m
                        n += sum(x.count for x in hh)
        return n

    async for event in _qa_events(params):
        etype = event.get("type")
        if etype == "answer_delta":
            if event.get("reset"):
                dm.reset()
                yield event
                continue
            event["delta"] = dm.feed(event.get("delta") or "")
        elif etype == "citations":
            _mask_cites(event.get("citations") or [])
        elif etype == "choice_pause":
            _mask_cites(event.get("citations") or [])
        elif etype == "final":
            result = event.get("result") or {}
            total = 0
            merged = list(dm.summary())
            ans = result.get("answer") or ""
            if ans:
                masked_ans, hh = masking_svc.mask_text(ans, ctx)
                if hh:
                    result["answer"] = f"{masked_ans}\n\n> {hint}"
                    total += sum(x.count for x in hh)
                    merged.extend(hh)
            total += _mask_cites(result.get("citations") or [])
            if total:
                result["masking"] = {"applied": True, "masked_count": total, "hint": hint}
                masking_svc.log_masking_async(
                    user_id=getattr(u, "id", None), username=getattr(u, "username", "") or "",
                    scene="chat", node="post_output",
                    query=masking_svc.mask_text(params.get("query") or "", ctx)[0],
                    policy_ids=ctx.policy_ids, rule_hits=merged, masked_count=total,
                    exempted=bool(ctx.exempt_types))
        yield event


@router.post("/chat")
async def chat(body: ChatIn, u=Depends(get_principal), s: AsyncSession = Depends(get_session)):
    qa = await _prepare_qa(body, u, s)
    async for event in _mask_qa_events(qa, u, s):
        if event["type"] == "final":
            result = event["result"]
            cites = result.get("citations") or []
            _log_usage(u.id, "chat", body.query, len(cites),
                       [c.get("document_id", "") for c in cites], [c.get("document_title", "") for c in cites])
            return result
        if event["type"] == "choice_pause":
            return {"answer": event.get("evidence_summary", ""), "answer_status": "awaiting_choice",
                    "choice": event, "citations": event.get("citations", []), "engine": "deerflow"}
        if event["type"] == "config_error":
            return {"answer": event["message"], "citations": [], "config_error": event["code"]}


@router.post("/chat/stream")
async def chat_stream(body: ChatIn, u=Depends(get_principal), s: AsyncSession = Depends(get_session)):
    qa = await _prepare_qa(body, u, s)
    key = (str(u.id), body.session_id)
    async def generate():
        task = asyncio.current_task()
        if body.session_id:
            previous = _ACTIVE_QA.get(key)
            if previous and previous is not task:
                previous.cancel()
            _ACTIVE_QA[key] = task
        queue = asyncio.Queue()
        async def produce():
            try:
                async for event in _mask_qa_events(qa, u, s):
                    await queue.put(event)
            finally:
                await queue.put(None)
        worker = asyncio.create_task(produce())
        terminal_received = False
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if event is None:
                    break
                if event["type"] in {"final", "choice_pause"}:
                    terminal_received = True
                if event["type"] == "final":
                    cites = event["result"].get("citations") or []
                    _log_usage(u.id, "chat", body.query, len(cites),
                               [c.get("document_id", "") for c in cites], [c.get("document_title", "") for c in cites])
                yield _sse(event)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            if _ACTIVE_QA.get(key) is task:
                _ACTIVE_QA.pop(key, None)
            from app.services.agent.deerflow_runner import cancel_deerflow_stream
            if not terminal_received:
                await cancel_deerflow_stream(qa["thread_id"])
    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class ChatCancelIn(BaseModel):
    session_id: str = Field(default="", max_length=100)


@router.post("/chat/cancel")
async def chat_cancel(body: ChatCancelIn, u=Depends(get_principal)):
    task = _ACTIVE_QA.get((str(u.id), body.session_id))
    if task:
        task.cancel()
    return {"cancelled": bool(task)}


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"
