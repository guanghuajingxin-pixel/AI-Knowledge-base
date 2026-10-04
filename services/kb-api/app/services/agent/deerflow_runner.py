"""DeerFlow 2.0 sidecar 运行器。

把 QA Sidecar（services/deerflow，DeerFlow 2.0 super agent harness）的 SSE 事件
转译为问答页既有的流式协议（step / answer_delta / final / config_error）。

- Lead Agent 自主决定检索次数、检索词、是否澄清、是否派发子智能体；
- knowledge_search 工具的返回内容在此汇总为引用来源（按文档去重）；
- 只接受当前运行的主回答最终事件，过程单独实时下发；
- sidecar 不可达时抛 DeerflowUnavailable，由上层直接报错（fail-fast，无旧链回退）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, AsyncGenerator

import httpx
from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger(__name__)

DEERFLOW_URL = os.getenv("DEERFLOW_SERVICE_URL", "http://127.0.0.1:2027").rstrip("/")

# 健康检查缓存（避免每次问答都探测）
_health_cache: dict[str, Any] = {"ok": False, "ts": 0.0}
_HEALTH_TTL = 5.0


class DeerflowUnavailable(Exception):
    """sidecar 未启动或未就绪；调用方直接报错（fail-fast），不回退旧链。"""


def _filter_cited_citations(answer: str, citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只保留答案中真正引用到的文档。

    判断依据：文档标题（去掉 [钉钉] 前缀、扩展名、杰克_前缀后的核心名）
    与答案的归一化匹配。答案常把长文件名简写为《短标题》（如
    《北斗2026年半年度战略报告外宣版》 ← 杰克_北斗…_V5.0.pptx），因此除
    完整包含外，还接受「标题首段（≥6 字）出现在答案中」的简写形式。
    这样不依赖 ref 编号方案，也不会把仅检索过但答案未提及的文档列出来。
    """
    if not answer or not citations:
        return citations
    ans_lower = answer.lower()

    def _norm(t: str) -> str:
        for ch in " \t\r\n_-.，。:：;；()（）【】[]《》「」·—'’“”\"!?！？":
            t = t.replace(ch, "")
        return t.lower()

    def _core_title(title: str) -> str:
        # 去掉 [钉钉] 前缀、扩展名、通用前缀，提取可用于匹配的核心名
        t = title
        if t.startswith("[钉钉]"):
            t = t[len("[钉钉]"):]
        # 去掉扩展名
        for ext in (".docx", ".doc", ".pdf", ".xlsx", ".xls", ".ppt", ".pptx", ".txt", ".md", ".adoc"):
            if t.lower().endswith(ext):
                t = t[: -len(ext)]
                break
        # 去掉开头的 "杰克_" 前缀（答案常引用时会省略）
        if t.startswith("杰克_"):
            t = t[len("杰克_"):]
        return t.strip()

    keep: list[dict[str, Any]] = []
    for c in citations:
        title = c.get("document_title") or ""
        core = _core_title(title)
        if len(core) < 4:
            if title.lower() in ans_lower:
                keep.append(c)
            continue
        # 1) 完整核心名（归一化后）出现在答案中
        if _norm(core) in _norm(answer):
            keep.append(c)
            continue
        # 2) 简写形式：标题首段（按 _ - （） 分隔的最长语义段，≥6 字）出现在答案中
        first_seg = next(
            (s for s in [_norm(x) for x in re.split(r"[_\-（）()]+", core)] if len(s) >= 6),
            "",
        )
        if first_seg and first_seg in _norm(answer):
            keep.append(c)
    return keep


# 跨流累积的线程级引用：choice_pause → continue/stop 续跑是新的 SSE 流，
# 已读文档的引用需要按 thread 保留，否则续跑后 final citations 为空、
# 前端无法把答案中的《文档名》转成可点击的源文档链接。
_THREAD_CITES: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_THREAD_CITES_TTL = 6 * 3600  # 秒；会话通常几分钟内续跑，超时清理


def _load_thread_cites(thread_id: str) -> list[dict[str, Any]]:
    import copy

    entry = _THREAD_CITES.get(thread_id)
    if not entry:
        return []
    ts, cites = entry
    if time.time() - ts > _THREAD_CITES_TTL:
        _THREAD_CITES.pop(thread_id, None)
        return []
    return copy.deepcopy(cites)


def _store_thread_cites(thread_id: str, citations: list[dict[str, Any]]) -> None:
    # 过量清理：仅保留最近使用的线程
    if len(_THREAD_CITES) > 200:
        for k in sorted(_THREAD_CITES, key=lambda k: _THREAD_CITES[k][0])[:100]:
            _THREAD_CITES.pop(k, None)
    _THREAD_CITES[thread_id] = (time.time(), citations[-60:])


async def is_deerflow_alive() -> bool:
    now = time.time()
    if now - _health_cache["ts"] < _HEALTH_TTL:
        return _health_cache["ok"]
    ok = False
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{DEERFLOW_URL}/health")
            resp.raise_for_status()
            data = resp.json()
            ok = bool(data.get("config_ready"))
    except Exception:
        ok = False
    _health_cache["ok"] = ok
    _health_cache["ts"] = now
    return ok


async def cancel_deerflow_stream(thread_id: str) -> None:
    """通知 sidecar 中断指定 thread 的 agent 运行（用户点停止/断开 SSE 时调用）。

    fire-and-forget：sidecar 在下一个事件边界停止拉取图事件，未开始的模型
    调用由限时中间件短路，不再消耗 token。正常结束时调用为 no-op。
    """
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=2.0, read=2.0, write=2.0, pool=2.0)
        ) as client:
            resp = await client.post(
                f"{DEERFLOW_URL}/v1/chat/cancel", json={"thread_id": thread_id}
            )
            logger.info("sidecar cancel thread=%s -> %s", thread_id, resp.status_code)
    except Exception as e:  # noqa: BLE001
        logger.warning("sidecar cancel request failed: %s", e)


def _sse(obj: dict[str, Any]) -> dict[str, Any]:
    return obj


async def _generate_follow_ups(query: str, answer: str,
                               llm_config: dict[str, str] | None,
                               agent_config: dict[str, Any] | None) -> list[str]:
    """复用既有追问提示词，在 sidecar 回答完成后生成 3 个追问建议。"""
    cfg = agent_config or {}
    if not cfg.get("follow_up_enabled", True) or not answer:
        return []
    try:
        from .llm import FOLLOW_UP_SYSTEM, build_llm

        llm = build_llm(llm_config or {}, cfg, temperature=0.7)
        resp = await llm.ainvoke([
            SystemMessage(content=FOLLOW_UP_SYSTEM),
            HumanMessage(content=(
                f"用户问题：{query}\n"
                f"本次回答：\n{answer[:1500]}"
            )),
        ])
        questions = re.findall(r"(?:^|\n)\s*(?:\d+[.、)]\s*)?(.{6,60}[？?])", resp.content or "")
        return [q.strip() for q in questions[:3]]
    except Exception as e:  # noqa: BLE001
        logger.warning("deerflow follow-up generation failed: %s", e)
        return []


async def run_deerflow_stream(
    query: str,
    *,
    thread_id: str,
    dataset_ids: list[str] | None = None,
    ragflow_dataset_ids: list[str] | None = None,
    kb_ids: list[str] | None = None,
    doc_library_ids: list[int] | None = None,
    top_k: int = 8,
    plan_mode: bool = False,
    subagent_enabled: bool = False,
    disabled_tools: list[str] | None = None,
    llm_config: dict[str, str] | None = None,
    agent_config: dict[str, Any] | None = None,
    action: str = "",
) -> AsyncGenerator[dict[str, Any], None]:
    """调用 DeerFlow sidecar 并转译事件。

     yield 协议与 run_agent_stream 兼容，额外增加：
      {"type": "answer_delta", "delta": str}  回答文本实时增量
      {"type": "choice", "question": str, "options": list[str]}  限时探索确认按钮
      {"type": "choice_pause", "question": str, "options": list[str], "usage": dict}
          智能体暂停等待用户选择（不下发 final；用户点选后以 action=continue/stop 续跑）
    """
    payload = {
        "thread_id": thread_id,
        "message": query,
        "thinking_enabled": False,
        "plan_mode": bool(plan_mode),
        "subagent_enabled": bool(subagent_enabled),
        "recursion_limit": 80,
        "dataset_ids": dataset_ids or [],
        "ragflow_dataset_ids": ragflow_dataset_ids or [],
        "kb_ids": kb_ids or [],
        "doc_library_ids": doc_library_ids or [],
        "top_k": max(int(top_k or 8), 5),
        "disabled_tools": disabled_tools or [],
        "retrieval_mode": str((agent_config or {}).get("retrieval_mode", "smart") or "smart"),
        "max_retrieval_rounds": int((agent_config or {}).get("max_retrieval_rounds", 2) or 2),
        "action": (action or "").strip().lower(),
    }

    answer_parts: list[str] = []
    run_id: str | None = None
    saw_end = False
    tool_calls: dict[str, str] = {}
    citations: list[dict[str, Any]] = _load_thread_cites(thread_id)
    # 每次 knowledge_search 调用内 ref 序号 → 引用文档（收尾把答案 [n] 重映射为文档级编号）
    ref_maps: list[dict[int, dict[str, Any]]] = []
    # 已见文档去重签名：钉钉条目带 [钉钉] 前缀，需还原为去重时的原始签名
    seen_docs: set[str] = {
        ("dt:" + c["document_title"][len("[钉钉] "):]) if (c.get("document_title") or "").startswith("[钉钉] ") else c.get("document_title", "")
        for c in citations
    }
    search_count = 0
    ding_calls = 0
    ding_hits = 0
    warnings: list[str] = []
    # 限时探索：智能体调用 ask_clarification 请求用户确认时，本轮以 choice_pause 结束
    choice_pending: dict[str, Any] | None = None
    usage: dict[str, Any] = {}

    yield _sse({
        "type": "step",
        "node": "df_start", "step_id": "request", "status": "running",
        "title": "处理问题",
        "detail": "正在准备本轮问答",
        "data": {},
    })

    def _absorb_retrieval(content: str) -> int:
        """解析 knowledge_search 工具返回，汇总引用，返回本次召回数。"""
        nonlocal citations
        try:
            data = json.loads(content)
        except Exception:
            return 0
        hits = data.get("results") or []
        ref_map: dict[int, dict[str, Any]] = {}
        for idx, h in enumerate(hits, 1):
            title = h.get("document_title") or "未知文档"
            sig = title
            existing = next((c for c in citations if c.get("document_title") == title), None)
            if sig in seen_docs:
                ref_map[idx] = existing or {}
                continue
            seen_docs.add(sig)
            cite: dict[str, Any] = {
                "document_title": title,
                "document_id": h.get("document_id") or "",
                "segment_id": h.get("segment_id") or "",
                "dataset_id": h.get("dataset_id") or "",
                "page_number": h.get("page_number"),
                "score": h.get("score"),
                "content": h.get("content") or "",
                "evidence_read": bool(h.get("content")),
                "source": h.get("source") or "dify",
            }
            # 同步自钉钉知识库的 Dify 文档：携带钉钉原始链接，引用来源点击跳钉钉预览
            if h.get("url"):
                cite["url"] = h.get("url")
                cite["node_id"] = h.get("node_id") or ""
                cite["source"] = h.get("source") or "dingtalk"
            citations.append(cite)
            cite["citation_id"] = len(citations)
            ref_map[idx] = cite
        ref_maps.append(ref_map)
        return len(hits)

    def _absorb_dingtalk(content: str) -> int:
        """解析 dingtalk_search 工具返回，汇总引用，返回命中数。"""
        nonlocal citations
        try:
            data = json.loads(content)
        except Exception:
            return 0
        hits = data.get("results") or []
        for h in hits:
            title = h.get("title") or "未知文档"
            sig = f"dt:{title}"
            if sig in seen_docs:
                continue
            seen_docs.add(sig)
            cite = {
                "document_title": f"[钉钉] {title}",
                "page_number": None,
                "score": None,
                "content": h.get("url") or "",
                "url": h.get("url") or "",
                "node_id": h.get("node_id") or "",
                "extension": h.get("extension") or "",
                "source": "dingtalk",
            }
            citations.append(cite)
            cite["citation_id"] = len(citations)
        return len(hits)

    def _absorb_read_doc(content: str) -> None:
        """解析 dingtalk_read_doc 工具返回，把被读取的源文档纳入引用（携带 url 供前端跳转预览）。"""
        nonlocal citations
        try:
            data = json.loads(content)
        except Exception:
            return
        title = data.get("title") or ""
        if not title:
            return
        sig = f"dt:{title}"
        existing = next((c for c in citations if c.get("document_title") == f"[钉钉] {title}"), None)
        if existing is not None:
            existing.update(content=data.get("content") or "", evidence_read=bool(data.get("content")))
            return
        seen_docs.add(sig)
        citations.append({"document_title": f"[钉钉] {title}", "page_number": None,
                          "score": None, "content": data.get("content") or "",
                          "evidence_read": bool(data.get("content")), "url": data.get("url") or "",
                          "node_id": data.get("node_id") or "", "extension": "", "source": "dingtalk",
                          "citation_id": len(citations) + 1})

    titles = {"knowledge_search": "知识库检索", "dingtalk_search": "钉钉文档定位",
              "dingtalk_read_doc": "读取文档正文", "dingtalk_browse": "浏览钉钉目录",
              "knowledge_context_expand": "展开相关上下文", "task": "子任务处理",
              "ask_clarification": "等待确认"}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=660.0, write=30.0, pool=10.0)) as client:
            async with client.stream("POST", f"{DEERFLOW_URL}/v1/chat/stream", json=payload) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise DeerflowUnavailable(f"sidecar HTTP {resp.status_code}: {body[:200]!r}")
                event_buf = ""
                async for chunk in resp.aiter_text():
                    event_buf += chunk
                    frames = event_buf.split("\n\n")
                    event_buf = frames.pop()
                    for frame in frames:
                        line = frame.strip()
                        if not line.startswith("data:"):
                            continue
                        try:
                            evt = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        etype = evt.get("type")
                        if etype == "ready":
                            if run_id is None and evt.get("thread_id") == thread_id:
                                run_id = evt.get("run_id")
                                yield _sse({"type": "step", "step_id": "request", "status": "completed",
                                            "title": "处理问题", "detail": "已接收本轮问题"})
                            continue
                        if etype not in {"error", "cancelled"} and (not run_id or evt.get("run_id") != run_id
                                                  or evt.get("thread_id") != thread_id):
                            continue
                        if etype == "tool_start":
                            name = evt.get("name") or ""
                            args = evt.get("args") or {}
                            call_id = evt.get("call_id")
                            if not call_id or call_id in tool_calls:
                                continue
                            tool_calls[call_id] = name
                            detail = str(args.get("query") or args.get("title") or args.get("directory") or "处理中")[:100]
                            if name == "knowledge_search":
                                search_count += 1
                            elif name in {"dingtalk_search", "dingtalk_browse"}:
                                ding_calls += 1
                            yield _sse({"type": "step", "step_id": call_id, "status": "running",
                                        "title": titles.get(name, "处理资料"), "detail": detail})
                            if name == "ask_clarification":
                                q = str(args.get("question") or args.get("clarification") or "需要确认是否继续")
                                opts = [str(o) for o in (args.get("options") or []) if str(o).strip()]
                                choice_pending = {"question": q, "options": opts,
                                    "choice_kind": args.get("choice_kind", "clarification"),
                                    "evidence_summary": args.get("evidence_summary", ""),
                                    "confidence": args.get("confidence"),
                                    "confidence_reason": args.get("confidence_reason", "")}
                                yield _sse({"type": "choice", **choice_pending})
                        elif etype == "tool_end":
                            name = evt.get("name") or ""
                            call_id = evt.get("call_id")
                            if not call_id:
                                continue
                            try:
                                content = json.loads(evt.get("content") or "{}")
                            except (ValueError, TypeError):
                                content = {}
                            if not isinstance(content, dict):
                                content = {}
                            failed = evt.get("status") == "error" or bool(content.get("error"))
                            detail = "处理失败" if failed else "处理完成"
                            if failed:
                                warnings.append(f"{titles.get(name, '资料处理')}失败")
                            elif name == "knowledge_search":
                                n = _absorb_retrieval(evt.get("content") or "")
                                detail = f"找到 {n} 段参考内容"
                            elif name == "dingtalk_search":
                                n = _absorb_dingtalk(evt.get("content") or "")
                                ding_hits += n
                                detail = f"找到 {n} 个候选文档"
                            elif name == "dingtalk_read_doc":
                                _absorb_read_doc(evt.get("content") or "")
                                ding_hits += bool(content.get("content"))
                                detail = "已获取正文" if content.get("content") else "未获取到正文"
                            elif name == "dingtalk_browse":
                                detail = "目录读取完成"
                            elif name == "knowledge_context_expand":
                                detail = "相关上下文已展开"
                            yield _sse({"type": "step", "step_id": call_id,
                                        "status": "failed" if failed else "completed",
                                        "title": titles.get(name, "处理资料"), "detail": detail})
                            if name in {"knowledge_search", "dingtalk_search", "dingtalk_read_doc"}:
                                yield _sse({"type": "citations", "citations": [dict(c) for c in citations]})
                        elif etype == "phase":
                            phase = evt.get("phase")
                            if isinstance(phase, str) and phase.startswith("model:"):
                                completed = evt.get("status") == "completed"
                                yield _sse({"type": "step", "step_id": phase, "status": "completed" if completed else "running",
                                            "title": "分析与整理", "detail": ("已确定下一步检索" if evt.get("has_tools") else "已整理本轮结论") if completed else "正在处理当前问题及可用资料"})
                            elif phase == "context":
                                yield _sse({"type": "step", "step_id": "context", "status": "completed",
                                            "title": "整理会话上下文", "detail": "历史背景已整理"})
                        elif etype == "ai_text":
                            text = str(evt.get("text") or "")
                            if text:
                                yield _sse({"type": "answer_delta", "delta": text})
                        elif etype == "ai_discard":
                            # 答案轮被判定为工具过渡轮：清空前端已流出的临时文本
                            yield _sse({"type": "answer_delta", "delta": "", "reset": True})
                        elif etype == "answer_final":
                            if evt.get("source_node") == "model" and evt.get("message_id"):
                                # 权威正文替换暂存结果；不拼接来自不同模型调用的内容。
                                answer_parts = [evt.get("content") or ""]
                        elif etype == "cancelled":
                            # 用户手动中断（停止按钮/断连）：立即结束，不再生成追问/最终结果
                            logger.info("deerflow stream cancelled by user: %s", thread_id)
                            return
                        elif etype == "error":
                            warnings.append(f"DeerFlow 智能体执行失败：{evt.get('message', '未知错误')}")
                            yield _sse({"type": "config_error", "code": "agent_failed",
                                        "message": f"🤖 DeerFlow 智能体执行失败：{evt.get('message', '未知错误')}"})
                            return
                        elif etype == "end":
                            saw_end = True
                            u = evt.get("usage") or {}
                            if isinstance(u, dict):
                                usage = {
                                    "input_tokens": int(u.get("input_tokens") or 0),
                                    "output_tokens": int(u.get("output_tokens") or 0),
                                    "total_tokens": int(u.get("total_tokens") or 0),
                                }
                            break
    except DeerflowUnavailable:
        raise
    except httpx.HTTPError as e:
        logger.warning("deerflow sidecar request failed: %s", e)
        raise DeerflowUnavailable(str(e)) from e

    answer = "".join(answer_parts).strip()

    # 限时探索暂停：智能体通过 ask_clarification 询问用户是否继续。
    # 本轮不下发 final（无答案），前端渲染选择按钮；用户点选后发起新请求（action=continue/stop）续跑。
    if choice_pending is not None and not answer and saw_end:
        # 暂停前落盘本流已累积的引用，供续跑流 seed（否则续跑后 final citations 为空）
        _store_thread_cites(thread_id, citations)
        yield _sse({
            "type": "choice_pause",
            **choice_pending,
            "citations": citations,
            "usage": usage,
        })
        return

    if not saw_end or not answer:
        yield _sse({"type": "config_error", "code": "answer_incomplete",
                    "message": "本轮回答未完整生成，请重试。"})
        return

    follow_ups = await _generate_follow_ups(query, answer, llm_config, agent_config)

    # 仅保留答案中真正引用到的文档：按文档标题/显著子串是否出现在答案中过滤
    # 先落盘（未过滤的全量列表，供续跑周期累积），再重建文档级引用：
    # 答案中的 [n] 是 knowledge_search 单次调用内序号，按出现顺序重映射为文档级编号；
    # 同一文档的多个 ref 合并为同一编号；无法解析的序号标记直接删除；
    # 仅以标题提及（如钉钉"可参阅"）而无序号的文档追加在后。
    _store_thread_cites(thread_id, citations)
    # 全量召回快照（含未被答案引用的分段）：供「问答明细」记录过程中召回了哪些，
    # 与最终 citations（仅被引用文档）互补；同一对象引用，过滤后回填 cited 标记
    retrieval_all = list(citations)
    ordered: list[dict[str, Any]] = []

    def _resolve_ref(n: int) -> dict[str, Any] | None:
        for cm in reversed(ref_maps):
            if n in cm:
                return cm[n] or None
        return None

    def _repl(m: "re.Match") -> str:
        cite = _resolve_ref(int(m.group(1)))
        if not cite or not cite.get("document_title"):
            return ""
        if cite not in ordered:
            ordered.append(cite)
        return f"[{ordered.index(cite) + 1}]"

    # 无条件重映射：本轮有检索时把单次调用内 ref 序号重映射为文档级编号；
    # 本轮无检索（答案复用历史结论或模型自有知识）时 [n] 全部无法解析、直接删除，
    # 避免前端出现没有引用区对应的悬空 [1]。
    answer = re.sub(r"\[(\d+)\]", _repl, answer)
    for c in _filter_cited_citations(answer, citations):
        if c not in ordered:
            ordered.append(c)
    citations = ordered
    for i, c in enumerate(citations, 1):
        c["citation_id"] = i
        c.setdefault("text", c.get("content", ""))
        # 无服务端硬核验（提示词约束口径）：引用摘录取召回原文片段本身
        c.setdefault("quote", c.get("content", ""))
    # 全量召集中标记哪些被答案实际引用（问答明细据此区分「已引用/未引用」）
    cited_ids = {id(c) for c in citations}
    retrieval_all = [
        {**c, "cited": id(c) in cited_ids,
         "content": (c.get("content") or "")[:500], "text": (c.get("content") or "")[:500]}
        for c in retrieval_all
    ]

    searched = search_count > 0 or ding_calls > 0
    if citations:
        status = "answered"
    elif searched:
        status = "insufficient"
    else:
        status = "answered"  # 未触发检索的会话性回复（问候/闲聊）
    reused = not searched and any(c.get("evidence_read") for c in citations)
    if reused:
        yield _sse({"type": "step", "step_id": "reuse", "status": "completed",
                    "title": "复用本会话资料", "detail": "引用本会话此前已读取的正文，本轮未重新检索"})
    dws_state = "hit" if ding_hits else ("searched" if ding_calls else "not_needed")

    yield _sse({
        "type": "final",
        "result": {
            "answer": answer or "（未生成回答）",
            "citations": citations,
            # 全量召回快照（含未引用分段，cited 标记是否被答案引用）：问答明细用
            "retrieval_all": retrieval_all,
            "follow_ups": follow_ups,
            "classification": "deerflow2",
            "engine": "deerflow",
            "answer_status": status,
            "sufficiency": {
                "sufficient": status == "answered",
                "missing": "" if citations else ("未检索到足以回答的原文" if searched else ""),
            },
            "quality": {"verification": "unverified", "dws": dws_state, "warnings": warnings},
            "rewritten_query": query,
            "last_query": query,
            "last_answer": answer if status == "answered" else "",
            "usage": usage,
        },
    })
