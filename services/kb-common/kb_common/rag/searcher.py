import asyncio
import logging
from kb_common.clients import es_client
from kb_common.rag import embedder, reranker
from elasticsearch import AsyncElasticsearch

from kb_common.rag.scoring import cosine, prepare_query, document_tokens, term_scores, blend

logger = logging.getLogger(__name__)


async def _knn(es: AsyncElasticsearch, index: str, vector, top_k: int, filters: dict):
    q = {"field": "vector", "query_vector": vector, "k": top_k, "num_candidates": top_k * 5}
    knn_filter = []
    if filters.get("directory_ids"):
        knn_filter.append({"terms": {"directory_id": filters["directory_ids"]}})
    if filters.get("document_ids"):
        knn_filter.append({"terms": {"document_id": filters["document_ids"]}})
    if knn_filter:
        q["filter"] = {"bool": {"must": knn_filter}} if len(knn_filter) > 1 else knn_filter[0]
    r = await es.search(
        index=index,
        knn=[q],
        size=top_k,
        source=["text", "document_id", "document_title", "chunk_index",
                "page_number", "source_path", "file_type", "content_hash",
                "directory_id", "kb_type", "faq_entry_id", "faq_answer", "vector"],
    )
    return [(h["_id"], h["_score"], h["_source"]) for h in r["hits"]["hits"]]


async def _bm25(es: AsyncElasticsearch, index: str, query: str, top_k: int, filters: dict):
    prepared = await asyncio.to_thread(prepare_query, query)
    if not prepared.expression:
        return []
    must = [{"query_string": {"query": prepared.expression, "fields": ["text^2", "document_title^10"],
                               "type": "best_fields", "minimum_should_match": "30%"}}]
    if filters.get("directory_ids"):
        must.append({"terms": {"directory_id": filters["directory_ids"]}})
    if filters.get("document_ids"):
        must.append({"terms": {"document_id": filters["document_ids"]}})
    r = await es.search(index=index, query={"bool": {"must": must}}, size=top_k,
                        source=True)
    return [(h["_id"], h["_score"], h["_source"]) for h in r["hits"]["hits"]]


async def _search(kb_ids, query, top_k, filters, rerank, mode):
    if not query.strip() or not kb_ids:
        return []
    if not 1 <= top_k <= 100:
        raise ValueError("top_k 必须介于 1 和 100 之间")
    filters = filters or {}
    count = max(100, top_k * 5)
    qvec = None
    if mode != 'keyword':
        try:
            qvec = (await asyncio.to_thread(embedder.embed, [query]))[0]
        except embedder.EmbeddingError as e:
            if mode == 'semantic':
                raise  # 纯向量检索依赖向量，缺向量无法降级
            logger.warning("ES kb %s embedding failed, fallback to keyword: %s", kb_ids, e)
    es = es_client.es
    candidates = {}
    for kb_id in dict.fromkeys(kb_ids):
        index = f"kb_{kb_id.replace('-', '')}"
        if not await es.indices.exists(index=index):
            continue
        knn = await _knn(es, index, qvec, count, filters) if qvec is not None else []
        lexical = await _bm25(es, index, query, count, filters) if mode != 'semantic' else []
        knn_scores = {hid: score for hid, score, _ in knn}
        for hid, raw, src in knn + lexical:
            key = (index, hid)
            if key not in candidates:
                candidates[key] = {**src, 'id': hid}
            if hid in knn_scores:
                candidates[key]['es_vector_score'] = knn_scores[hid]
    docs = []
    prepared = await asyncio.to_thread(prepare_query, query)
    fields = [document_tokens(h.get('text', ''), h.get('document_title', '')) for h in candidates.values()]
    lexical_scores = await asyncio.to_thread(term_scores, prepared, fields)
    for hit, token in zip(candidates.values(), lexical_scores):
        vector = hit.pop('vector', None)
        similarity = None
        if qvec is not None:
            # ES cosine scores are (1 + cosine) / 2, not raw cosine.
            if vector is not None:
                similarity = cosine(qvec, vector)
            elif 'es_vector_score' in hit:
                similarity = max(-1.0, min(1.0, 2 * hit['es_vector_score'] - 1))
            else:
                raise ValueError('候选分段缺少向量，无法计算混合分数，请重建索引')
        hit['token_similarity'] = token
        hit['vector_similarity'] = similarity
        if mode == 'keyword' or similarity is None:
            hit['score_type'] = 'ragflow_token'
            hit['semantic_weight'] = 0.0
            hit['score'] = token
        elif mode == 'semantic':
            hit['score_type'] = 'cosine'
            hit['semantic_weight'] = 1.0
            hit['score'] = similarity
        else:
            hit['score_type'] = 'ragflow_hybrid'
            hit['semantic_weight'] = 0.7
            hit['score'] = blend(token, similarity, 0.7)
        if hit['score'] > 0:
            docs.append(hit)
    docs.sort(key=lambda h: -h['score'])
    if rerank and docs:
        docs = await asyncio.to_thread(reranker.rerank, query, docs[:count], top_n=top_k)
    return docs[:top_k]


async def hybrid(kb_ids: list[str], query: str, top_k: int = 10,
                 filters: dict | None = None, rerank: bool = True) -> list[dict]:
    return await _search(kb_ids, query, top_k, filters, rerank, 'hybrid')


async def semantic(kb_ids: list[str], query: str, top_k: int = 10,
                   filters: dict | None = None, rerank: bool = True) -> list[dict]:
    return await _search(kb_ids, query, top_k, filters, rerank, 'semantic')


async def keyword(kb_ids: list[str], query: str, top_k: int = 10,
                  filters: dict | None = None, rerank: bool = False) -> list[dict]:
    return await _search(kb_ids, query, top_k, filters, rerank, 'keyword')
