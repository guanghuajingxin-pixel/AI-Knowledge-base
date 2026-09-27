"""Real retrieval over local MinerU chunks; never simulate vector matches.

Chunks currently have no persistent vector index. Batch embeddings use a bounded
content/model cache; all eligible leaf chunks participate before top-k selection.
"""
import asyncio
import hashlib
from collections import OrderedDict
from uuid import UUID

import httpx
from sqlalchemy import select
from kb_common.config import get_settings
from kb_common.models import LibraryChunk, LibraryDocument, RerankProfile, Setting
from kb_common.rag.scoring import (cosine, prepare_query, document_tokens,
                                   term_scores, lexical_candidates, blend, apply_rerank, rerank_term_scores)

_vectors: OrderedDict = OrderedDict()


async def config(session, prefix):
    keys = [f'{prefix}_{k}' for k in ('base_url', 'api_url', 'api_key', 'model')]
    rows = (await session.execute(select(Setting).where(Setting.key.in_(keys)))).scalars().all()
    overrides = {r.key: r.value for r in rows}
    defaults = get_settings()
    return {k: overrides.get(f'{prefix}_{k}', getattr(defaults, f'{prefix}_{k}', '')) or ''
            for k in ('base_url', 'api_url', 'api_key', 'model')}


async def embeddings(texts, cfg):
    if not cfg['base_url'] or not cfg['model']:
        raise ValueError('请先配置并启用向量模型，向量检索不能使用关键词匹配代替')
    identity = (cfg['base_url'], cfg['model'], hashlib.sha256(cfg['api_key'].encode()).hexdigest())
    keys = [(*identity, hashlib.sha256(t.encode()).hexdigest()) for t in texts]
    result = [_vectors.get(key) for key in keys]
    missing = [i for i, value in enumerate(result) if value is None]
    async with httpx.AsyncClient(timeout=120) as client:
        for offset in range(0, len(missing), 32):
            indices = missing[offset:offset + 32]
            try:
                response = await client.post(cfg['base_url'].rstrip('/') + '/embeddings',
                    headers={'Authorization': f"Bearer {cfg['api_key']}"} if cfg['api_key'] else {},
                    json={'model': cfg['model'], 'input': [texts[i] for i in indices]})
                response.raise_for_status()
                data = response.json()['data']
            except httpx.HTTPError as e:
                raise ValueError('向量模型调用失败，请修改后重试') from e
            except (KeyError, ValueError) as e:
                raise ValueError('向量模型调用失败，请修改后重试') from e
            if len(data) != len(indices) or {d['index'] for d in data} != set(range(len(indices))):
                raise ValueError('向量模型返回的索引或数量不正确')
            for item in data:
                i = indices[item['index']]
                vector = item['embedding']
                cosine(vector, vector)  # validate before caching
                result[i] = vector
                _vectors[keys[i]] = vector
                _vectors.move_to_end(keys[i])
                while len(_vectors) > 8192:
                    _vectors.popitem(last=False)
    return result


async def rerank_hits(session, query, hits, profile_id):
    cfg = await config(session, 'rerank')
    if profile_id:
        profile = await session.get(RerankProfile, UUID(profile_id))
        if profile is None:
            raise ValueError('所选重排模型不存在')
        cfg.update(api_url=profile.api_url, api_key=profile.api_key, model=profile.model)
    if not cfg['api_url'] or not cfg['model']:
        raise ValueError('已开启 Rerank，请先选择或配置重排模型')
    async with httpx.AsyncClient(timeout=120) as client:
        try:
            response = await client.post(cfg['api_url'],
                headers={'Authorization': f"Bearer {cfg['api_key']}"} if cfg['api_key'] else {},
                json={'model': cfg['model'], 'query': query,
                      'documents': [h['matched_content'] for h in hits], 'top_n': len(hits)})
            response.raise_for_status()
            rows = response.json()['results']
        except httpx.HTTPError as e:
            raise ValueError('重排模型调用失败，请修改后重试') from e
        except (KeyError, ValueError) as e:
            raise ValueError('重排模型调用失败，请修改后重试') from e
    lexical = await asyncio.to_thread(rerank_term_scores, query, hits)
    for hit, score in zip(hits, lexical):
        hit['retrieval_token_similarity'] = hit.get('token_similarity')
        hit['token_similarity'] = score
    apply_rerank(hits, rows)



async def search(session, lib, query, top_k, mode='hybrid', *, document_ids=None,
                 threshold=0.0, vector_weight=0.7, rerank=False, rerank_model_id=None):
    conditions = [LibraryDocument.library_id == lib.id, LibraryDocument.enabled.is_(True),
                  LibraryChunk.available.is_(True)]
    if document_ids:
        conditions.append(LibraryDocument.id.in_([UUID(d) for d in document_ids]))
    rows = (await session.execute(select(LibraryChunk, LibraryDocument.name)
        .join(LibraryDocument, LibraryDocument.id == LibraryChunk.document_id)
        .where(*conditions).order_by(LibraryChunk.id))).all()
    by_id = {c.id: c for c, _ in rows}
    parents = {c.parent_id for c, _ in rows if c.parent_id}
    # Disabled/missing parents must not leak through enabled children.
    leaves = [(c, name) for c, name in rows if c.id not in parents
              and (not c.parent_id or c.parent_id in by_id)]
    if not leaves:
        return []
    if mode not in {'hybrid', 'vector', 'fulltext'}:
        raise ValueError('未知的检索模式')
    if not 1 <= top_k <= 100 or not 0 <= threshold <= 1 or not 0 <= vector_weight <= 1:
        raise ValueError('检索参数超出有效范围')
    prepared = await asyncio.to_thread(prepare_query, query)
    if not prepared.keywords:
        return []
    weight = {'vector': 1.0, 'fulltext': 0.0}.get(mode, vector_weight)
    texts = [{'text': c.content, 'title': name, 'keywords': c.important_keywords or []}
             for c, name in leaves]
    recall_count = max(1024, top_k * 5)
    lexical_ids = await asyncio.to_thread(lexical_candidates, prepared, texts, recall_count) if weight < 1 else []
    similarities = [None] * len(leaves)
    dense_ids = []
    if weight > 0:
        vectors = await embeddings([query] + [c.content for c, _ in leaves], await config(session, 'embedding'))
        similarities = [cosine(vectors[0], vector) for vector in vectors[1:]]
        dense_ids = sorted(range(len(leaves)), key=lambda i: (-similarities[i], i))[:recall_count]
    # RAGFlow stable sort preserves recall ranking when similarity scores tie.
    candidates = list(dict.fromkeys(lexical_ids + dense_ids))
    if not candidates:
        return []
    def lexical_scores():
        fields = [document_tokens(texts[i]['text'], texts[i]['title'], texts[i]['keywords']) for i in candidates]
        return term_scores(prepared, fields)
    lexical = await asyncio.to_thread(lexical_scores)
    hits = []
    for i, token_score in zip(candidates, lexical):
        chunk, name = leaves[i]
        semantic = similarities[i]
        score = blend(token_score, semantic or 0.0, weight)
        parent = by_id.get(chunk.parent_id)
        hits.append({'content': parent.content if parent else chunk.content,
            'matched_content': chunk.content, 'score': score,
            'score_type': 'ragflow_token' if weight == 0 else ('cosine' if weight == 1 else 'ragflow_hybrid'),
            # In fulltext+rerank, the model is explicitly enabled as a second stage.
            'semantic_weight': weight if weight > 0 else vector_weight,
            'token_similarity': token_score, 'vector_similarity': semantic,
            'important_keywords': chunk.important_keywords or [],
            'document_title': name, 'document_id': str(chunk.document_id),
            'segment_id': str(chunk.id), 'parent_segment_id': str(chunk.parent_id or chunk.id)})
    hits.sort(key=lambda h: -h['score'])
    if rerank and hits:
        hits = hits[:max(64, top_k * 5)]
        await rerank_hits(session, query, hits, rerank_model_id)
        hits.sort(key=lambda h: -h['score'])
    output, seen = [], set()
    for hit in hits:
        if hit['score'] <= 0 or hit['score'] < threshold or hit['parent_segment_id'] in seen:
            continue
        seen.add(hit['parent_segment_id'])
        output.append(hit)
        if len(output) == top_k:
            break
    return output
