import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from app.qa_events import AnswerProjection


def test_summary_and_nested_nodes_cannot_publish_answers():
    p = AnswerProjection('thread')
    old = AIMessage(id='old', content='旧答案')
    p.feed('values', {'messages': [old, HumanMessage(content='新问题', id='q')]})
    summary = AIMessage(id='s', content='用户当前问题：旧问题\n已完成动作：检索')
    p.feed('updates', {'SummarizationMiddleware.before_model': {'messages': [summary]}})
    p.feed('updates', {'subagent': {'messages': [AIMessage(id='sub', content='内部调查')]}})
    p.feed('values', {'messages': [summary]})
    with pytest.raises(ValueError, match='最终答案'):
        p.finish(str)
    answer = AIMessage(id='a', content='本轮结论 [1]')
    assert not p.feed('updates', {'model': {'messages': [answer]}})
    p.feed('values', {'messages': [summary, answer]})
    assert p.finish(str)['content'] == '本轮结论 [1]'


def test_tool_call_does_not_fall_back_to_previous_answer():
    p = AnswerProjection('thread')
    old = AIMessage(id='old', content='旧答案')
    p.feed('values', {'messages': [old]})
    call = AIMessage(id='new', content='我先查找', tool_calls=[{'name': 'knowledge_search', 'args': {}, 'id': 'c'}])
    starts = p.feed('updates', {'model': {'messages': [call]}})
    assert starts[0]['call_id'] == 'c'
    p.feed('values', {'messages': [old, call]})
    with pytest.raises(ValueError, match='尚未完成'):
        p.finish(str)
    result = ToolMessage(id='t', tool_call_id='c', name='knowledge_search', content='错误', status='error')
    ends = p.feed('values', {'messages': [old, call, result]})
    assert ends[0]['status'] == 'error'
    assert not p.feed('values', {'messages': [old, call, result]})


def test_generated_notes_are_rejected_instead_of_silently_stripped():
    p = AnswerProjection('thread')
    p.feed('values', {'messages': []})
    msg = AIMessage(id='a', content='用户当前问题：旧问题\n\n正式回答')
    p.feed('updates', {'model': {'messages': [msg]}})
    p.feed('values', {'messages': [msg]})
    with pytest.raises(ValueError, match='内部记录'):
        p.finish(str)


def test_real_langgraph_summary_two_turns():
    """使用真实 LangGraph/Middleware/检查点，复现摘要与主回答同时生成。"""
    from langchain.agents import create_agent
    from langchain.agents.middleware import SummarizationMiddleware
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel, FakeListChatModel
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import InMemorySaver

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    @tool
    def knowledge_search(query: str) -> str:
        """获取测试正文。"""
        return '正文依据'

    model = Model(responses=[
        AIMessage(content='', tool_calls=[{'id': 'c1', 'name': 'knowledge_search', 'args': {'query': '报告时限'}}]),
        AIMessage(content='上一轮回答'), AIMessage(content='本轮只回答分级标准 [1]'),
    ])
    summary = FakeListChatModel(responses=['用户当前问题：报告提交时限\n已完成动作：已检索'])
    graph = create_agent(model, tools=[knowledge_search], checkpointer=InMemorySaver(), middleware=[
        SummarizationMiddleware(model=summary, trigger=('messages', 4), keep=('messages', 2)),
    ])
    config = {'configurable': {'thread_id': 'regression'}}
    def run(question):
        p = AnswerProjection('regression')
        events = []
        for mode, chunk in graph.stream({'messages': [HumanMessage(content=question)]}, config=config,
                                         stream_mode=['values', 'updates', 'messages']):
            events.extend(p.feed(mode, chunk))
        return p.finish(str), events
    first, _ = run('报告提交时限？')
    second, events = run('分级标准？')
    assert first['content'] == '上一轮回答'
    assert second['content'] == '本轮只回答分级标准 [1]'
    assert first['run_id'] != second['run_id']
    assert any(e.get('phase') == 'context' for e in events)
    assert all('用户当前问题' not in str(e) for e in events)


def test_token_text_never_leaks_even_from_main_model():
    from langchain_core.messages import AIMessageChunk
    p = AnswerProjection('thread')
    msg = AIMessageChunk(id='a', content='用户当前问题：旧问题')
    events = p.feed('messages', (msg, {'langgraph_node': 'model', 'langgraph_checkpoint_ns': 'model:1'}))
    assert events[0]['type'] == 'phase'
    assert '用户当前问题' not in str(events)
    assert not p.feed('messages', (msg, {'langgraph_node': 'SummarizationMiddleware.before_model'}))
    assert not p.feed('messages', (msg, {'langgraph_node': 'model', 'langgraph_checkpoint_ns': 'task:1|model:1'}))


def test_stream_tokens_forwarded_for_answer_turn():
    """答案轮 token 以 ai_text 外发：首段达阈值后整体流出，后续增量逐段转发。"""
    from langchain_core.messages import AIMessageChunk
    p = AnswerProjection('thread')
    ns = {'langgraph_node': 'model', 'langgraph_checkpoint_ns': 'model:1'}
    c1 = AIMessageChunk(id='a', content='根据现行管理办法，检测机构应当在规定时限内提交检验报告。')
    c2 = AIMessageChunk(id='a', content='逾期未报的应当说明原因。')
    ev1 = p.feed('messages', (c1, dict(ns)))
    ev2 = p.feed('messages', (c2, dict(ns)))
    texts = [e for e in ev1 + ev2 if e.get('type') == 'ai_text']
    assert ''.join(e['text'] for e in texts) == c1.content + c2.content
    assert texts[0]['text'] == c1.content  # 首段缓冲后一次性外发


def test_tool_turn_tokens_are_suppressed():
    """工具过渡轮：短过渡语被阈值挡住；出现工具调用块后整轮抑制、不再外发。"""
    from langchain_core.messages import AIMessageChunk
    p = AnswerProjection('thread')
    ns = {'langgraph_node': 'model', 'langgraph_checkpoint_ns': 'model:1'}
    ev1 = p.feed('messages', (AIMessageChunk(id='t', content='让我先检索。'), dict(ns)))
    assert not any(e.get('type') == 'ai_text' for e in ev1)
    tool = AIMessageChunk(id='t', content='', tool_call_chunks=[
        {'name': 'knowledge_search', 'args': '{}', 'id': 'c', 'index': 0}])
    ev2 = p.feed('messages', (tool, dict(ns)))
    assert not any(e.get('type') in ('ai_text', 'ai_discard') for e in ev2)
    ev3 = p.feed('messages', (AIMessageChunk(id='t', content='继续输出'), dict(ns)))
    assert not any(e.get('type') == 'ai_text' for e in ev3)


def test_forwarded_text_discarded_when_tool_call_follows():
    """先外发了文本、随后出现工具调用块 → 补发 ai_discard 让前端清空临时文本。"""
    from langchain_core.messages import AIMessageChunk
    p = AnswerProjection('thread')
    ns = {'langgraph_node': 'model', 'langgraph_checkpoint_ns': 'model:1'}
    long_text = '根据现行管理办法，检测机构应当在规定时限内提交检验报告。'
    ev1 = p.feed('messages', (AIMessageChunk(id='x', content=long_text), dict(ns)))
    assert any(e.get('type') == 'ai_text' for e in ev1)
    tool = AIMessageChunk(id='x', content='', tool_call_chunks=[
        {'name': 'knowledge_search', 'args': '{}', 'id': 'c', 'index': 0}])
    ev2 = p.feed('messages', (tool, dict(ns)))
    assert any(e.get('type') == 'ai_discard' for e in ev2)


def test_pause_and_forced_wrapup_keep_current_message_identity():
    p = AnswerProjection('thread')
    p.feed('values', {'messages': [AIMessage(id='old', content='历史答案')]})
    msg = AIMessage(id='pause', content='', tool_calls=[{'name': 'ask_clarification', 'id': 'ask', 'args': {'question': '继续？'}}])
    p.feed('updates', {'model': {'messages': [msg]}})
    p.feed('values', {'messages': [msg]})
    assert p.finish(str) is None
    forced = msg.model_copy(update={'tool_calls': [], 'content': '已有资料尚不足以回答。'})
    p.feed('values', {'messages': [forced]})
    assert p.finish(str)['content'] == '已有资料尚不足以回答。'
