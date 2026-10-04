import math
import unittest
from types import SimpleNamespace as NS
from uuid import uuid4
from unittest.mock import AsyncMock, Mock, patch

from app.services import library_retrieval as retrieval
from kb_common.rag import searcher, reranker
from kb_common.rag.scoring import cosine, term_similarity


def chunk(text, parent=None, ident=None):
    return NS(id=ident or uuid4(), parent_id=parent, document_id=uuid4(),
              content=text, important_keywords=[])


def session_for(rows):
    session = AsyncMock()
    session.execute.return_value.all = Mock(return_value=rows)
    return session


class ScoringTests(unittest.TestCase):
    def test_chinese_without_spaces(self):
        self.assertGreater(term_similarity('合同审批流程', '合同审批需要主管审核'),
                           term_similarity('合同审批流程', '天气晴朗'))
        self.assertEqual(term_similarity('合同审批流程', '合同审批流程'), 1)
        self.assertEqual(term_similarity('!!!', '文本'), 0)

    def test_real_cosine(self):
        self.assertAlmostEqual(cosine([1, 0], [3, 4]), .6)
        self.assertEqual(cosine([1, 0], [-1, 0]), -1)
        for vector in ([0, 0], [1], [math.nan, 0]):
            with self.assertRaises(ValueError): cosine([1, 0], vector)

    def test_reranker_sorts_and_replaces_score(self):
        response = Mock()
        response.json.return_value = {'results': [
            {'index': 0, 'relevance_score': .1}, {'index': 1, 'relevance_score': .9}]}
        client = Mock()
        client.post.return_value = response
        with patch.object(reranker, 'get_settings', return_value=NS(rerank_api_url='url', rerank_api_key='', rerank_model='model')), patch.object(reranker, '_client', return_value=client):
            hits = reranker.rerank('query', [{'text': 'a', 'score': .8}, {'text': 'b', 'score': .2}])
        self.assertAlmostEqual(hits[0]['score'], .7 * .9)
        self.assertAlmostEqual(hits[1]['score'], .7 * .1)
        self.assertEqual(hits[0]['rerank_score'], .9)
        self.assertEqual(hits[0]['retrieval_score'], .2)


class RetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def test_vector_retrieves_without_literal_overlap(self):
        rows = [(chunk('带薪休假如何申请'), '员工手册'), (chunk('服务器重启'), '运维手册')]
        with patch.object(retrieval, 'config', AsyncMock(return_value={})), patch.object(retrieval, 'embeddings', AsyncMock(return_value=[[1,0], [.8,.6], [0,1]])):
            hits = await retrieval.search(session_for(rows), NS(id=1), '年假办理', 1, 'vector')
        self.assertEqual(hits[0]['content'], '带薪休假如何申请')
        self.assertAlmostEqual(hits[0]['score'], .8)
        self.assertEqual(hits[0]['token_similarity'], 0)

    async def test_fulltext_is_model_free_and_not_constant(self):
        rows = [(chunk('合同审批流程'), ''), (chunk('合同审批'), ''), (chunk('天气'), '')]
        with patch.object(retrieval, 'embeddings', AsyncMock()) as embed:
            hits = await retrieval.search(session_for(rows), NS(id=1), '合同审批流程', 5, 'fulltext')
        embed.assert_not_called()
        self.assertEqual(hits[0]['score'], 1)
        self.assertAlmostEqual(hits[1]['score'], 0.4060327034971552)

    async def test_hybrid_formula_and_threshold(self):
        rows = [(chunk('合同审批'), '')]
        with patch.object(retrieval, 'config', AsyncMock(return_value={})), patch.object(retrieval, 'embeddings', AsyncMock(return_value=[[1,0], [.8,.6]])):
            hits = await retrieval.search(session_for(rows), NS(id=1), '合同审批流程', 5, 'hybrid', threshold=.65)
        self.assertAlmostEqual(hits[0]['score'], .3 * 0.4060327034971552 + .7 * .8)

    async def test_parent_dedup_after_ranking_and_disabled_parent(self):
        parent = chunk('完整父段')
        a, b = chunk('合同', parent.id), chunk('合同审批', parent.id)
        hidden = chunk('合同审批流程', uuid4())
        hits = await retrieval.search(session_for([(parent,''),(a,''),(b,''),(hidden,'')]), NS(id=1), '合同审批', 2, 'fulltext')
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['segment_id'], str(b.id))
        self.assertEqual(hits[0]['content'], '完整父段')
        self.assertEqual(hits[0]['matched_content'], '合同审批')

    async def test_document_scope_in_sql_before_ranking(self):
        session = session_for([])
        doc = str(uuid4())
        await retrieval.search(session, NS(id=1), 'x', 2, document_ids=[doc])
        stmt = session.execute.call_args.args[0]
        self.assertIn('library_documents.id IN', str(stmt))
        self.assertIn([__import__('uuid').UUID(doc)], stmt.compile().params.values())

    async def test_threshold_applied_after_rerank(self):
        async def rank(session, query, hits, profile):
            hits[0].update(score=.95, score_type='rerank')
        with patch.object(retrieval, 'rerank_hits', rank):
            hits = await retrieval.search(session_for([(chunk('合同'),'')]), NS(id=1), '合同审批流程', 2,
                                          'fulltext', threshold=.9, rerank=True)
        self.assertEqual(hits[0]['score'], .95)

    async def test_unconfigured_vector_does_not_fake_scores(self):
        with self.assertRaisesRegex(ValueError, '向量模型'):
            await retrieval.embeddings(['a'], {'base_url':'', 'model':'', 'api_key':''})

    async def test_es_uses_cosine_and_expands_candidates(self):
        es = NS(indices=NS(exists=AsyncMock(return_value=True)))
        with patch.object(searcher.es_client, 'es', es), patch.object(searcher.embedder, 'embed', return_value=[[1,0]]), patch.object(searcher, '_knn', AsyncMock(return_value=[('a', .8, {'text':'x'})])) as knn:
            hits = await searcher.semantic(['kb'], 'x', 1, rerank=False)
        self.assertAlmostEqual(hits[0]['score'], .6)
        self.assertGreater(knn.call_args.args[3], 1)

    async def test_embedding_response_order_and_cache(self):
        import httpx
        retrieval._vectors.clear()
        cfg = {'base_url': 'http://embedding/v1', 'api_key': '', 'model': 'test'}
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = httpx.Response(200, json={'data': [
            {'index': 1, 'embedding': [0,1]}, {'index': 0, 'embedding': [1,0]}]},
            request=httpx.Request('POST', 'http://embedding/v1/embeddings'))
        with patch.object(retrieval.httpx, 'AsyncClient', return_value=client):
            vectors = await retrieval.embeddings(['first', 'second'], cfg)
            cached = await retrieval.embeddings(['second', 'first'], cfg)
        self.assertEqual(vectors, [[1,0],[0,1]])
        self.assertEqual(cached, [[0,1],[1,0]])
        self.assertEqual(client.post.await_count, 1)
        retrieval._vectors.clear()

    async def test_ragflow_zero_score_and_document_filter(self):
        import httpx
        from kb_common.clients import ragflow_client as rf
        cfg = NS(ragflow_base_url='http://ragflow/api/v1', ragflow_api_key='key',
                 ragflow_retrieval_top_k=8, ragflow_similarity_threshold=0,
                 ragflow_vector_similarity_weight=.7, ragflow_rerank_id='')
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = httpx.Response(200, json={'code':0, 'data':{'chunks':[
            {'similarity':0, 'vector_similarity':.9, 'content':'unrelated'}]}},
            request=httpx.Request('POST', 'http://ragflow/api/v1/retrieval'))
        with patch.object(rf, 'get_settings', return_value=cfg), patch.object(rf, '_binding_errors', AsyncMock(return_value=[])), patch.object(rf.httpx, 'AsyncClient', return_value=client):
            result = await rf.retrieve_with_report(['dataset'], 'query', document_ids=['doc'])
        self.assertEqual(result['hits'][0]['score'], 0)
        self.assertEqual(client.post.call_args.kwargs['json']['document_ids'], ['doc'])

    async def test_equal_similarity_preserves_bm25_recall_rank(self):
        # Same title covers the query in both chunks. Prefer the body match,
        # not an arbitrary chunk UUID, when the final coverage scores tie.
        unrelated = chunk('天气晴朗')
        matching = chunk('合同审批流程')
        session = session_for([(unrelated,'合同审批流程'), (matching,'合同审批流程')])
        hits = await retrieval.search(session, NS(id=1), '合同审批流程', 2, 'fulltext')
        self.assertEqual(hits[0]['segment_id'], str(matching.id))
        self.assertEqual(hits[0]['score'], hits[1]['score'])


class RerankProfileResolutionTests(unittest.IsolatedAsyncioTestCase):
    """rerank_hits：仅已生效模型可用；历史引用停用模型时回退；大批量分批合并。"""

    def _session(self, specified=None, enabled=None):
        session = AsyncMock()
        session.get = AsyncMock(return_value=specified)
        result = Mock()
        result.scalar_one_or_none = Mock(return_value=enabled)
        session.execute = AsyncMock(return_value=result)
        return session

    def _client(self, payloads):
        import httpx
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post = AsyncMock(side_effect=[
            httpx.Response(200, json=p, request=httpx.Request('POST', 'http://x/v1/rerank'))
            for p in payloads])
        return client

    def _hits(self, n):
        return [{'matched_content': f'合同审批流程第{i}条相关规定', 'score': .5,
                 'token_similarity': .1, 'document_title': '', 'important_keywords': []}
                for i in range(n)]

    async def test_disabled_profile_falls_back_to_enabled(self):
        disabled = NS(id=uuid4(), enabled=False, api_url='http://disabled/rerank', api_key='', model='old')
        enabled = NS(id=uuid4(), name='enabled-profile', enabled=True,
                     api_url='http://enabled/rerank', api_key='k', model='new')
        session = self._session(disabled, enabled)
        client = self._client([{'results': [
            {'index': 0, 'relevance_score': .9}, {'index': 1, 'relevance_score': .1}]}])
        with patch.object(retrieval, 'config', AsyncMock(return_value={'api_url': '', 'api_key': '', 'model': ''})), \
             patch.object(retrieval.httpx, 'AsyncClient', return_value=client):
            await retrieval.rerank_hits(session, '合同审批', self._hits(2), str(disabled.id))
        self.assertEqual(client.post.call_args.args[0], 'http://enabled/rerank')
        self.assertEqual(client.post.call_args.kwargs['json']['model'], 'new')
        self.assertEqual(client.post.call_args.kwargs['headers']['Authorization'], 'Bearer k')

    async def test_rerank_batches_and_remaps_indices(self):
        session = self._session(None, None)
        p1 = {'results': [{'index': i, 'relevance_score': .5} for i in range(32)]}
        p2 = {'results': [{'index': 0, 'relevance_score': .8}]}
        client = self._client([p1, p2])
        hits = self._hits(33)
        with patch.object(retrieval, 'config', AsyncMock(
                return_value={'api_url': 'http://e/rerank', 'api_key': '', 'model': 'm'})), \
             patch.object(retrieval.httpx, 'AsyncClient', return_value=client):
            await retrieval.rerank_hits(session, '合同审批', hits, None)
        self.assertEqual(client.post.await_count, 2)
        self.assertEqual(len(client.post.call_args.kwargs['json']['documents']), 1)
        self.assertEqual(client.post.call_args.kwargs['json']['top_n'], 1)
        self.assertAlmostEqual(hits[-1]['rerank_score'], .8)

    async def test_specified_missing_without_enabled_raises(self):
        session = self._session(None, None)
        with patch.object(retrieval, 'config', AsyncMock(
                return_value={'api_url': '', 'api_key': '', 'model': ''})):
            with self.assertRaisesRegex(ValueError, '未生效'):
                await retrieval.rerank_hits(session, '合同审批', self._hits(2), str(uuid4()))

    async def test_long_document_truncated_for_rerank(self):
        """超长分段（超过模型上下文）发送前截断，原始 hit 内容不变。"""
        session = self._session(None, None)
        client = self._client([{'results': [
            {'index': 0, 'relevance_score': .9}, {'index': 1, 'relevance_score': .1}]}])
        hits = self._hits(2)
        hits[0]['matched_content'] = '超长制度条款' * 3000  # 18000 字符，超过 8192 token
        with patch.object(retrieval, 'config', AsyncMock(
                return_value={'api_url': 'http://e/rerank', 'api_key': '', 'model': 'm'})), \
             patch.object(retrieval.httpx, 'AsyncClient', return_value=client):
            await retrieval.rerank_hits(session, '合同审批', hits, None)
        docs = client.post.call_args.kwargs['json']['documents']
        self.assertEqual(len(docs[0]), retrieval._RERANK_MAX_DOC_CHARS)
        self.assertEqual(len(hits[0]['matched_content']), 18000)


if __name__ == '__main__':
    unittest.main()
