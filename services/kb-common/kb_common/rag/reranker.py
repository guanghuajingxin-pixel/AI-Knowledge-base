"""重排模型客户端：全部外接（Jina/SiliconFlow 风格 /rerank），不本地部署 bge-reranker。

未配置 RERANK_API_URL 时 rerank() 直接返回原序前 top_n（跳过重排，检索评分排序兜底），
searcher 无需感知配置状态。
"""
import httpx
from kb_common.config import get_settings
from kb_common.rag.scoring import apply_rerank, rerank_term_scores

# 单文档截断：超长分段会超过 rerank 模型上下文（如 bge-reranker-v2-m3 8192 token）
# 被服务以 400 拒绝；二阶段重排取段首即可保持排序信号。
_MAX_DOC_CHARS = 4000

_session: httpx.Client | None = None


def _client() -> httpx.Client:
    global _session
    if _session is None or _session.is_closed:
        _session = httpx.Client(timeout=30)
    return _session


def rerank(query: str, docs: list[dict], top_n: int = 10) -> list[dict]:
    """外接 rerank API 重排；未配置时跳过重排。"""
    if not docs:
        return []
    s = get_settings()
    if not s.rerank_api_url:
        return docs[:top_n]  # 未配置重排：保序截断，检索侧已有相关性分数
    resp = _client().post(
        s.rerank_api_url,
        headers={"Authorization": f"Bearer {s.rerank_api_key}"} if s.rerank_api_key else {},
        json={"model": s.rerank_model, "query": query,
              "documents": [(d.get("text") or "")[:_MAX_DOC_CHARS] for d in docs],
              "top_n": len(docs)},
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    scored = [dict(d) for d in docs]
    lexical = rerank_term_scores(query, scored)
    for d, score in zip(scored, lexical):
        d['retrieval_token_similarity'] = d.get('token_similarity')
        d['token_similarity'] = score
    apply_rerank(scored, results)
    scored.sort(key=lambda d: d['score'], reverse=True)
    return scored[:top_n]




def close() -> None:
    global _session
    if _session is not None and not _session.is_closed:
        _session.close()
    _session = None
