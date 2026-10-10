"""Real retrieval over local MinerU chunks; never simulate vector matches.

Chunks currently have no persistent vector index. Batch embeddings use a bounded
content/model cache; all eligible leaf chunks participate before top-k selection.
"""
import asyncio
import hashlib
import logging
from collections import OrderedDict
from uuid import UUID

import httpx
from sqlalchemy import select
from kb_common.config import get_settings
from kb_common.models import LibraryChunk, LibraryDocument, RerankProfile, Setting
from app.services.knowledge_engines.chunk_enhancer import retrieval_text
from kb_common.rag.scoring import (cosine, prepare_query, document_tokens,
                                   term_scores, lexical_candidates, blend, apply_rerank, rerank_term_scores)

_vectors: OrderedDict = OrderedDict()

# Embedding 与 rerank 均分批、限时：外部服务周期性降级时批量请求会长时间无响应
# （实测 32 条批量 embedding / 多文档 rerank 挂死 120s+），超时越短降级越早。
_EMBEDDING_BATCH = 32
_EMBEDDING_TIMEOUT = 30.0  # 远小于外层 /internal/kb/retrieve 的 60s 超时，失败可及时降级全文检索
# Rerank 每文档独立打分，可安全分批；批量过大外部服务易挂死（实测 32+ 条会长时间无响应）。
_RERANK_BATCH = 32
_RERANK_TIMEOUT = 45.0
# 单文档截断：bge-reranker-v2-m3 等模型上下文上限 8192 token，超长分段（实测 1.2 万字符
# 的混合文本）会被服务以 400 拒绝，导致「连通性测试通过、实际检索失败」。
# 4000 字符（中文约 4k token）留足余量；二阶段重排取段首内容即可保持排序信号。
_RERANK_MAX_DOC_CHARS = 4000

logger = logging.getLogger(__name__)


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
    async with httpx.AsyncClient(timeout=_EMBEDDING_TIMEOUT) as client:
        for offset in range(0, len(missing), _EMBEDDING_BATCH):
            indices = missing[offset:offset + _EMBEDDING_BATCH]
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


async def _enabled_rerank_profile(session):
    """当前生效（默认）的重排模型配置；多条配置中只有一条可 enabled。"""
    return (await session.execute(
        select(RerankProfile).where(RerankProfile.enabled.is_(True))
        .order_by(RerankProfile.created_at).limit(1))).scalar_one_or_none()


async def _resolve_rerank_profile(session, profile_id):
    """解析实际可用的重排模型：指定 ID 必须是已生效配置；

    历史库可能仍引用已停用/已删除的配置（此前界面允许选择未生效模型），
    此时自动回退到当前生效模型，保证检索链路与「模型配置」的开关一致。
    返回 None 表示库级未指定模型（由调用方回落全局生效配置）。
    """
    if not profile_id:
        return None
    try:
        profile = await session.get(RerankProfile, UUID(str(profile_id)))
    except ValueError as exc:
        raise ValueError('所选重排模型无效，请重新选择已生效模型') from exc
    if profile is not None and profile.enabled:
        return profile
    fallback = await _enabled_rerank_profile(session)
    if fallback is not None:
        logger.warning('rerank profile %s 不存在或未生效，回退到生效模型 %s', profile_id, fallback.name)
    return fallback


async def rerank_hits(session, query, hits, profile_id):
    cfg = await config(session, 'rerank')
    profile = await _resolve_rerank_profile(session, profile_id)
    if profile is not None:
        cfg.update(api_url=profile.api_url, api_key=profile.api_key, model=profile.model)
    elif profile_id:
        # 指定了模型但既无有效配置也无生效模型：明确报错，避免静默跳过重排
        raise ValueError('所选重排模型未生效，请先在「模型配置」中启用')
    if not cfg['api_url'] or not cfg['model']:
        raise ValueError('已开启 Rerank，请先在「模型配置」中启用重排模型')
    rows = []
    documents_all = [(h.get('matched_content') or '')[:_RERANK_MAX_DOC_CHARS] for h in hits]
    async with httpx.AsyncClient(timeout=_RERANK_TIMEOUT) as client:
        for start in range(0, len(hits), _RERANK_BATCH):
            batch_docs = documents_all[start:start + _RERANK_BATCH]
            try:
                response = await client.post(cfg['api_url'],
                    headers={'Authorization': f"Bearer {cfg['api_key']}"} if cfg['api_key'] else {},
                    json={'model': cfg['model'], 'query': query,
                          'documents': batch_docs, 'top_n': len(batch_docs)})
                response.raise_for_status()
                batch_rows = response.json()['results']
            except httpx.HTTPError as e:
                raise ValueError('重排模型调用失败，请在「模型配置」检查服务地址、密钥与网络') from e
            except (KeyError, ValueError) as e:
                raise ValueError('重排模型返回格式异常，请确认使用标准 /rerank 服务') from e
            # 各批评分相互独立，把批内 index 重映射为全量候选 index 后合并
            for row in batch_rows:
                idx = row.get('index')
                if isinstance(idx, int) and not isinstance(idx, bool):
                    rows.append({**row, 'index': idx + start})
                else:
                    rows.append(row)
    lexical = await asyncio.to_thread(rerank_term_scores, query, hits)
    for hit, score in zip(hits, lexical):
        hit['retrieval_token_similarity'] = hit.get('token_similarity')
        hit['token_similarity'] = score
    try:
        apply_rerank(hits, rows)
    except ValueError as e:
        raise ValueError(f'重排模型返回结果异常：{e}') from e



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
    texts = [{'text': retrieval_text(c.content, c.retrieval_enhancements),
              'title': name, 'keywords': c.important_keywords or []}
             for c, name in leaves]
    recall_count = max(1024, top_k * 5)
    lexical_ids = await asyncio.to_thread(lexical_candidates, prepared, texts, recall_count) if weight < 1 else []
    similarities = [None] * len(leaves)
    dense_ids = []
    if weight > 0:
        try:
            vectors = await embeddings([query] + [c.content for c, _ in leaves], await config(session, 'embedding'))
            similarities = [cosine(vectors[0], vector) for vector in vectors[1:]]
            dense_ids = sorted(range(len(leaves)), key=lambda i: (-similarities[i], i))[:recall_count]
        except ValueError as e:  # 向量模型未配置/不可用/超时：降级纯全文，不阻断检索
            logger.warning("document library %s vector embedding failed, fallback to fulltext: %s", lib.id, e)
            weight = 0.0
            if not lexical_ids:
                lexical_ids = await asyncio.to_thread(lexical_candidates, prepared, texts, recall_count)
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
            'retrieval_enhancements': chunk.retrieval_enhancements or {},
            'document_title': name, 'document_id': str(chunk.document_id),
            'segment_id': str(chunk.id), 'parent_segment_id': str(chunk.parent_id or chunk.id)})
    hits.sort(key=lambda h: -h['score'])
    if rerank and hits:
        hits = hits[:max(64, top_k * 5)]
        try:
            await rerank_hits(session, query, hits, rerank_model_id)
            hits.sort(key=lambda h: -h['score'])
        except ValueError as e:  # 重排模型不可用/超时：跳过重排，保留召回原始排序
            logger.warning("document library %s rerank failed, keep original order: %s", lib.id, e)
    output, seen = [], set()
    for hit in hits:
        if hit['score'] <= 0 or hit['score'] < threshold or hit['parent_segment_id'] in seen:
            continue
        seen.add(hit['parent_segment_id'])
        output.append(hit)
        if len(output) == top_k:
            break
    return output
