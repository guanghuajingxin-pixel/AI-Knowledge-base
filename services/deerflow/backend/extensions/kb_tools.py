"""企业知识库检索工具（DeerFlow 扩展工具）。

DeerFlow Lead Agent / Sub-Agent 通过本工具访问知识治理平台 kb-api
背后的统一知识库层（覆盖企业已接入的各平台知识库），
实现基于企业内部资料的可信问答与引用溯源。

运行时上下文（数据集选择等）由 qa_server 按 thread_id 注入 RUNTIME_CTX，
工具通过 LangChain 注入的 RunnableConfig 获取 thread_id。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
from langchain.tools import tool
from langchain_core.runnables import RunnableConfig

logger = logging.getLogger(__name__)

#: thread_id -> {"dataset_ids": list[str], "top_k": int}
RUNTIME_CTX: dict[str, dict[str, Any]] = {}


def _kb_api_url() -> str:
    return os.getenv("KB_API_URL", "http://127.0.0.1:8000").rstrip("/")


def _headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Internal-Token": os.getenv("KB_INTERNAL_TOKEN", ""),
    }


def _dingtalk_budget(config) -> float:
    from deerflow.agents.middlewares.exploration_timeout_middleware import ExplorationTimeoutMiddleware
    tid = ((config or {}).get("configurable") or {}).get("thread_id", "default")
    return ExplorationTimeoutMiddleware.dingtalk_budget(tid)


def _blocked() -> str:
    return json.dumps({"error": "钉钉探索尚未获用户同意、已停止或到达本阶段时限。请先总结知识库内容并等待用户选择。", "results": []}, ensure_ascii=False)


@tool("knowledge_search", parse_docstring=True)
def knowledge_search_tool(query: str, top_k: int = 8, config: RunnableConfig = None) -> str:
    """检索企业内部知识库，涵盖管理制度、流程规范、产品技术文档、审批规则、操作手册等公司内部资料。

    使用规则：
    - 凡涉及公司内部信息的问题，必须先调用本工具检索，再严格依据召回内容回答，
      不得编造知识库中不存在的信息；
    - 回答中引用召回内容时，用 [1][2] 形式标注对应的ref 序号。最后ref如果是同一个文档，则只标注文档序号.例如[1][2]来自同一个文档，则统一成[1]；
    - 一次调用只传一个检索词；复杂/多方面问题应拆分成多个检索词，
      分多次调用（可并行），最后综合各次结果作答；
    - 若召回结果与问题无关，可换一种表述再次检索；仍无结果时明确告知
      “知识库中暂未检索到相关内容”，不要强行作答。

    Args:
        query: 检索词，使用简洁、能命中文档的关键词或问句。
        top_k: 召回段落数量，默认 8，最多 15。
    """
    cfg = (config or {}).get("configurable", {}) or {}
    thread_id = cfg.get("thread_id", "default")
    ctx = RUNTIME_CTX.get(thread_id, {})
    dataset_ids = ctx.get("dataset_ids") or []
    ragflow_dataset_ids = ctx.get("ragflow_dataset_ids") or []
    kb_ids = ctx.get("kb_ids") or []
    doc_library_ids = ctx.get("doc_library_ids") or []
    effective_top_k = min(max(int(top_k or 8), 1), 15)

    from deerflow.agents.middlewares.exploration_timeout_middleware import ExplorationTimeoutMiddleware
    ExplorationTimeoutMiddleware.record_evidence(thread_id, [])

    # 单轮检索轮数硬限（配置页 max_retrieval_rounds）：上限=轮数×3 次调用
    rounds = max(1, int(ctx.get("max_retrieval_rounds", 2)))
    cap = rounds * 3
    calls = int(ctx.get("search_calls", 0))
    if calls >= cap:
        return json.dumps(
            {"error": f"本轮 knowledge_search 调用已达上限（{cap} 次，max_retrieval_rounds={rounds}）。"
                      f"请总结现有证据；不足时询问用户是否同意钉钉探索，禁止自动调用钉钉工具。",
             "results": [], "cap_reached": True},
            ensure_ascii=False,
        )
    ctx["search_calls"] = calls + 1

    payload: dict[str, Any] = {
        "query": query,
        "dataset_ids": dataset_ids,
        "ragflow_dataset_ids": ragflow_dataset_ids,
        "kb_ids": kb_ids,
        "doc_library_ids": doc_library_ids,
        "top_k": effective_top_k,
        # 问答线程 ID：kb-api 据此回溯用户身份，执行检索返回脱敏策略
        "thread_id": thread_id,
    }
    data: dict[str, Any] | None = None
    last_err: Exception | None = None
    # 冷启动/并行召回叠加时首调可能超过单次超时（实测冷启动首调 30s+），
    # 一次 ReadTimeout 不应让本轮丢失引用证据：单次 40s、失败自动重试一次。
    for _attempt in range(2):
        try:
            with httpx.Client(timeout=40.0) as client:
                resp = client.post(
                    f"{_kb_api_url()}/api/v1/internal/kb/retrieve",
                    json=payload,
                    headers=_headers(),
                )
                resp.raise_for_status()
                data = resp.json()
            break
        except Exception as e:  # noqa: BLE001 - 工具异常需回传给 Agent 处理
            last_err = e
            logger.warning("knowledge_search attempt %d failed: %s", _attempt + 1, e)
    if data is None:
        return json.dumps(
            {"error": f"知识库检索失败：{last_err.__class__.__name__}，请稍后重试或换个检索词", "results": []},
            ensure_ascii=False,
        )

    hits = data.get("results") or []
    from deerflow.agents.middlewares.exploration_timeout_middleware import ExplorationTimeoutMiddleware
    ExplorationTimeoutMiddleware.record_evidence(thread_id, hits)
    slim = [
        {
            "ref": i + 1,
            "document_title": h.get("document_title") or "未知文档",
            "page_number": h.get("page_number"),
            "score": round(float(h.get("score") or 0.0), 3),
            "content": (h.get("content") or "")[:1200],
            # 同步自钉钉知识库的文档带原始链接，引用来源可跳回钉钉预览
            "url": h.get("url") or "",
            "node_id": h.get("node_id") or "",
            "source": h.get("source") or "",
        }
        for i, h in enumerate(hits)
    ]
    return json.dumps(
        {"count": len(slim), "results": slim},
        ensure_ascii=False,
        indent=1,
    )


@tool("dingtalk_search", parse_docstring=True)
def dingtalk_search_tool(query: str, top_k: int = 10, config: RunnableConfig = None) -> str:
    """检索钉钉知识库中的文档，按文件名和目录路径做关键词匹配。

    钉钉知识库与 knowledge_search 配合，作为企业知识的检索来源之一。
    钉钉知识库支持文档、表格、演示文稿等文件类型，按文件名/目录路径关键词匹配
    （非语义检索），返回文件元数据（名称、所属知识库、目录、链接）。

    使用规则：
    - 与 knowledge_search 配合使用：同一问题先检索知识库获取正文内容，
      再检索钉钉发现知识库未收录的相关文档；
    - 检索词使用能命中文档名的关键词，多个词空格分隔（全部需命中）；
      结果不理想时换同义词/上位词再次检索；
    - 返回的是文件元数据而非正文：在答案末尾以"可参阅"形式列出文档名和链接，
      答案主体仍基于 knowledge_search 的正文内容组织。

    Args:
        query: 检索关键词，多个词以空格分隔。
        top_k: 返回结果数，默认 10。
    """
    cfg = (config or {}).get("configurable", {}) or {}
    thread_id = cfg.get("thread_id", "default")

    budget = _dingtalk_budget(config)
    if budget <= 0:
        return _blocked()

    payload: dict[str, Any] = {
        "query": query,
        "top_k": min(max(int(top_k or 10), 1), 30),
    }
    try:
        with httpx.Client(timeout=min(60.0, budget)) as client:
            resp = client.post(
                f"{_kb_api_url()}/api/v1/internal/dingtalk/search",
                json=payload,
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("dingtalk_search failed: %s", e)
        return json.dumps(
            {"error": f"钉钉知识库检索失败：{e.__class__.__name__}", "results": []},
            ensure_ascii=False,
        )

    if data.get("error"):
        err = data["error"]
        # 钉钉未配置/配置失败：告知智能体该来源不可用，直接基于知识库结果作答，无需重试
        if "未配置" in err or "配置" in err:
            return json.dumps(
                {"error": f"钉钉知识库暂不可用（{err}），请直接基于 knowledge_search 的结果作答，不要再调用本工具。",
                 "results": []},
                ensure_ascii=False,
            )
        return json.dumps(
            {"error": err, "results": [], "loading": data.get("loading", False)},
            ensure_ascii=False,
        )

    # 钉钉文件列表首次加载中（后台拉取）：提示稍后可换关键词重试
    if data.get("loading"):
        return json.dumps(
            {"note": "钉钉知识库文件列表正在后台同步中，本次暂无结果；可换关键词稍后再试一次。",
             "results": [], "loading": True},
            ensure_ascii=False,
        )

    hits = data.get("results") or []
    slim = [
        {
            "ref": i + 1,
            "title": h.get("title") or "",
            "directory": h.get("directory") or "",
            "workspace": h.get("workspace") or "",
            "url": h.get("url") or "",
            "extension": h.get("extension") or "",
            "creator": h.get("creator") or "",
        }
        for i, h in enumerate(hits)
    ]
    return json.dumps(
        {"count": len(slim), "results": slim},
        ensure_ascii=False,
        indent=1,
    )


@tool("dingtalk_browse", parse_docstring=True)
def dingtalk_browse_tool(action: str, directory: str = "", top_k: int = 30, config: RunnableConfig = None) -> str:
    """浏览钉钉知识库的目录结构与目录下文档（元数据），用于"先定位目录、再逐篇精读"的检索策略。

    两个动作：
    - action="map"：返回各知识库（workspace）及其 2 级目录的文档数量、在线文档数量。
      回答复杂/不确定归属的问题时，**先调用 map 预判**答案最可能落在哪个知识库/目录，
      再用 list 列该目录文档，避免盲目关键词检索。
    - action="list"：按 directory 关键词（目录路径或知识库名的一部分）列出该目录下文档，
      **在线文档（adoc/md/txt，可秒读正文）排在最前**，办公文档（docx/pdf/xlsx，需下载解析）其次。

    使用规则：
    - 问题主题明确但 dingtalk_search 关键词命中率低时，优先 map → list → read_doc；
    - list 结果含 node_id/extension/title，配合 dingtalk_read_doc 逐篇读取；
    - 每读完一篇文档，立即判断其内容能否回答用户问题：能答就停止扩展并作答，不能答再读下一篇，
      不要无差别顺序读取全部文档。

    Args:
        action: "map" 返回目录地图；"list" 列出目录下文档。
        directory: action="list" 时的目录关键词（目录路径或知识库名片段），如 "差旅"、"财务"。
        top_k: list 时最多返回文档数，默认 30。
    """
    budget = _dingtalk_budget(config)
    if budget <= 0:
        return _blocked()

    payload: dict[str, Any] = {
        "action": action if action in ("map", "list") else "map",
        "directory": directory or "",
        "top_k": min(max(int(top_k or 30), 1), 50),
    }
    try:
        with httpx.Client(timeout=min(60.0, budget)) as client:
            resp = client.post(
                f"{_kb_api_url()}/api/v1/internal/dingtalk/browse",
                json=payload,
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("dingtalk_browse failed: %s", e)
        return json.dumps(
            {"error": f"钉钉目录浏览失败：{e.__class__.__name__}", "results": []},
            ensure_ascii=False,
        )

    if data.get("error"):
        return json.dumps({"error": data["error"], "results": [], "loading": data.get("loading", False)},
                          ensure_ascii=False)
    return json.dumps(data, ensure_ascii=False, indent=1)


@tool("dingtalk_read_doc", parse_docstring=True)
def dingtalk_read_doc_tool(node_id: str, title: str = "", extension: str = "", config: RunnableConfig = None) -> str:
    """读取钉钉知识库中某篇文档的正文内容（Markdown 格式）。

    dingtalk_search 只返回文件元数据（文件名/链接），要获取文档正文必须调用本工具。
    在线文档直接读取 Markdown；二进制文件（docx/pdf/xlsx 等）下载后由平台解析为 Markdown。

    使用规则：
    - 调用本工具前应先通过 dingtalk_search 找到目标文档的 node_id、extension；
    - 回答涉及钉钉文档内容时，**必须**先调用本工具读取正文，再基于正文作答，
      不能仅根据文件名猜测内容或只给文档链接；
    - 同一问题可读取多篇钉钉文档，逐篇读取后综合；
    - 文档较长时，内容会被截断，可聚焦与问题相关的部分。

    Args:
        node_id: 钉钉文档节点 ID（来自 dingtalk_search 的 results[].node_id，必填）。
        title: 文档标题（来自 dingtalk_search 的 results[].title），可选。
        extension: 文档扩展名（来自 dingtalk_search 的 results[].extension），如 docx/pdf/adoc，可选。
    """
    budget = _dingtalk_budget(config)
    if budget <= 0:
        return _blocked()

    cfg = (config or {}).get("configurable", {}) or {}
    thread_id = cfg.get("thread_id", "default")

    payload: dict[str, Any] = {
        "node_id": node_id,
        "title": title or "",
        "extension": extension or "",
        # 问答线程 ID：kb-api 据此回溯用户身份，执行正文送LLM前脱敏
        "thread_id": thread_id,
    }
    try:
        with httpx.Client(timeout=min(60.0, budget)) as client:
            resp = client.post(
                f"{_kb_api_url()}/api/v1/internal/dingtalk/content",
                json=payload,
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("dingtalk_read_doc failed: %s", e)
        return json.dumps(
            {"error": f"钉钉文档读取失败：{e.__class__.__name__}，可能无权限或文档类型不支持", "content": ""},
            ensure_ascii=False,
        )

    content = data.get("content") or ""
    # 截断过长内容，避免超出上下文（保留首尾，删除中间）
    max_chars = 8000
    if len(content) > max_chars:
        head = content[: max_chars // 2]
        tail = content[-max_chars // 2 :]
        content = f"{head}\n\n...[内容过长已截断]...\n\n{tail}"

    return json.dumps(
        {
            "title": data.get("title") or title,
            "node_id": node_id,
            "content": content,
            "format": data.get("format", "markdown"),
            "url": data.get("url") or "",
        },
        ensure_ascii=False,
        indent=1,
    )


@tool("knowledge_context_expand", parse_docstring=True)
def knowledge_context_expand_tool(document_ids: str, query: str = "", max_related: int = 3,
                                   config: RunnableConfig = None) -> str:
    """扩展知识检索上下文：根据已命中的文档 ID，获取这些文档的摘要、关联文档和标签聚合。

    当 knowledge_search 返回的片段不足以回答问题，或需要更全面理解文档全貌时，
    调用本工具获取：① 命中文档的摘要（理解文档核心内容）；② 语义关联文档（深度探索）；
    ③ 标签聚合（辅助判断主题范围）。

    使用规则：
    - document_ids 来自 knowledge_search 返回的 results[].document_id，多个用逗号分隔；
    - 优先传入 2-5 个最相关的文档 ID，避免上下文过长；
    - 返回的 related 文档可进一步用 knowledge_search 检索其内容；
    - 若命中文档尚未加工（无摘要/关联），返回为空，属正常情况。

    Args:
        document_ids: 命中文档 ID，多个用英文逗号分隔。
        query: 用户原始问题，用于辅助关联排序，可选。
        max_related: 最多返回的关联文档数，默认 3。
    """
    payload: dict[str, Any] = {
        "document_ids": [d.strip() for d in (document_ids or "").split(",") if d.strip()],
        "query": query or "",
        "max_related": min(max(int(max_related or 3), 1), 10),
    }
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                f"{_kb_api_url()}/api/v1/internal/process/context-expand",
                json=payload,
                headers=_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("knowledge_context_expand failed: %s", e)
        return json.dumps(
            {"error": f"上下文扩展失败：{e.__class__.__name__}", "summaries": [], "related": [], "tags": []},
            ensure_ascii=False,
        )
    return json.dumps(data, ensure_ascii=False, indent=1)
