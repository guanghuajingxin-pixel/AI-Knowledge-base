import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException

from app.routes import search

TOOLS_ALL = {'knowledge_search': True, 'dingtalk_search': True, 'dingtalk_read_doc': True,
             'ask_clarification': True, 'present_files': True}


class RouteTests(unittest.IsolatedAsyncioTestCase):
    def _session(self):
        # execute() 结果需同时支持 scalar_one_or_none()（单行）与 scalars().all()（多行，
        # 知识源注册表查询走此路径，默认空注册表）
        result = SimpleNamespace(
            scalar_one_or_none=lambda: None,
            scalars=lambda: SimpleNamespace(all=lambda: []),
        )
        return SimpleNamespace(execute=AsyncMock(return_value=result))

    async def test_scope_and_hyperparams_are_applied_per_request(self):
        user = SimpleNamespace(id=uuid4(), role='editor')
        session = self._session()
        cfg = {'top_k': 7, 'long_memory_enabled': False, 'tools_enabled': dict(TOOLS_ALL)}
        with patch('app.services.agent.config.load_agent_config', AsyncMock(return_value=cfg)), \
             patch('app.services.llm_resolver.resolve_llm_config', AsyncMock(return_value={'api_key': 'test'})):
            params = await search._prepare_qa(
                search.ChatIn(query='审批条件', dify_dataset_ids=[]), user, session)
            self.assertEqual(params['dataset_ids'], [])
            self.assertEqual(params['top_k'], 7)
            self.assertEqual(params['disabled_tools'], [])
            self.assertTrue(params['thread_id'].startswith('anon-'))
            with self.assertRaises(HTTPException) as err:
                await search._prepare_qa(
                    search.ChatIn(query='审批条件', dify_dataset_ids=[uuid4()]), user, session)
            self.assertEqual(err.exception.status_code, 403)

    async def test_disabled_knowledge_search_clears_scope(self):
        user = SimpleNamespace(id=uuid4(), role='editor')
        session = self._session()
        tools = dict(TOOLS_ALL, knowledge_search=False)
        cfg = {'tools_enabled': tools}
        with patch('app.services.agent.config.load_agent_config', AsyncMock(return_value=cfg)), \
             patch('app.services.llm_resolver.resolve_llm_config', AsyncMock(return_value={'api_key': 'test'})):
            params = await search._prepare_qa(search.ChatIn(query='审批条件'), user, session)
        self.assertEqual(params['dataset_ids'], [])
        self.assertEqual(params['kb_ids'], [])
        self.assertIn('knowledge_search', params['disabled_tools'])

    async def test_session_thread_reuses_session_id_when_memory_enabled(self):
        user = SimpleNamespace(id=uuid4(), role='editor')
        session = self._session()
        sid = uuid4()
        with patch('app.services.agent.config.load_agent_config', AsyncMock(return_value={'long_memory_enabled': True})), \
             patch('app.services.llm_resolver.resolve_llm_config', AsyncMock(return_value={'api_key': 'test'})), \
             patch('app.routes.chat_session_route._get_owned', AsyncMock(return_value=SimpleNamespace(id=sid))):
            params = await search._prepare_qa(
                search.ChatIn(query='上一条是谁', session_id='s1'), user, session)
            self.assertEqual(params['thread_id'], str(sid))
        with patch('app.services.agent.config.load_agent_config', AsyncMock(return_value={'long_memory_enabled': False})), \
             patch('app.services.llm_resolver.resolve_llm_config', AsyncMock(return_value={'api_key': 'test'})), \
             patch('app.routes.chat_session_route._get_owned', AsyncMock(return_value=SimpleNamespace(id=sid))):
            params = await search._prepare_qa(
                search.ChatIn(query='上一条是谁', session_id='s1'), user, session)
            self.assertTrue(params['thread_id'].startswith(f'nomem-{sid}-'))

    async def test_session_owner_is_verified_before_using_conversation(self):
        user = SimpleNamespace(id=uuid4(), role='editor')
        session = self._session()
        with patch('app.services.agent.config.load_agent_config', AsyncMock(return_value={})), \
             patch('app.services.llm_resolver.resolve_llm_config', AsyncMock(return_value={'api_key': 'test'})), \
             patch('app.routes.chat_session_route._get_owned', AsyncMock(side_effect=HTTPException(404, '会话不存在'))):
            with self.assertRaises(HTTPException) as err:
                await search._prepare_qa(search.ChatIn(query='上一条是谁', session_id='another-session', dify_dataset_ids=[]), user, session)
            self.assertEqual(err.exception.status_code, 404)

    async def test_qa_events_reports_unavailable_sidecar_without_fallback(self):
        from app.services.agent import deerflow_runner

        async def boom(**kwargs):
            raise deerflow_runner.DeerflowUnavailable('connection refused')
            yield  # pragma: no cover

        params = {'query': 'q', 'thread_id': 't', 'llm_config': {'api_key': 'k'}}
        with patch('app.services.agent.deerflow_runner.run_deerflow_stream', boom):
            events = [e async for e in search._qa_events(params)]
        self.assertEqual(events[0]['type'], 'config_error')
        self.assertEqual(events[0]['code'], 'agent_not_ready')

    async def test_json_and_stream_use_same_engine_and_final_contract(self):
        app = FastAPI(); app.include_router(search.router)
        app.dependency_overrides[search.get_principal] = lambda: SimpleNamespace(id='user', role='editor')
        app.dependency_overrides[search.get_session] = lambda: None
        expected = {'answer': '已核实的事实 [1]', 'answer_status': 'answered', 'citations': []}
        async def events(_):
            yield {'type': 'step', 'node': 'retrieve', 'title': '检索', 'detail': '开始'}
            yield {'type': 'final', 'result': expected}
        with patch.object(search, '_prepare_qa', AsyncMock(return_value={'thread_id': 't', 'llm_config': {'api_key': 'k'}, 'query': 'q'})) as prepare, \
             patch.object(search, '_qa_events', events), patch.object(search, '_log_usage'):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                ordinary = await client.post('/api/v1/search/chat', json={'query': '制度问题'})
                stream = await client.post('/api/v1/search/chat/stream', json={'query': '制度问题'})
            self.assertEqual(ordinary.json(), expected)
            chunks = [json.loads(c[6:]) for c in stream.text.strip().split('\n\n') if c.startswith('data:')]
            self.assertEqual(chunks[-1], {'type': 'final', 'result': expected})
            self.assertEqual(prepare.await_count, 2)

    async def test_cancel_cannot_cancel_another_users_session(self):
        sleeper = asyncio.create_task(asyncio.sleep(100))
        search._ACTIVE_QA[('owner', 'session')] = sleeper
        try:
            result = await search.chat_cancel(search.ChatCancelIn(session_id='session'), SimpleNamespace(id='other'))
            self.assertFalse(result['cancelled']); self.assertFalse(sleeper.cancelled())
            result = await search.chat_cancel(search.ChatCancelIn(session_id='session'), SimpleNamespace(id='owner'))
            self.assertTrue(result['cancelled'])
        finally:
            sleeper.cancel(); await asyncio.gather(sleeper, return_exceptions=True)
            search._ACTIVE_QA.clear()


if __name__ == '__main__':
    unittest.main()
