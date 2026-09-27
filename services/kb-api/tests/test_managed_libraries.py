import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import httpx
from fastapi import HTTPException
from pydantic import ValidationError
from app.services.knowledge_engines.ragflow import RagflowEngine, EngineError, document_state
from app.services.knowledge_engines.mineru import job_state
from app.services.knowledge_engines.local_chunker import chunk_markdown, clean_markdown, est_tokens
from app.routes import managed_library as routes


class EngineContractTests(unittest.IsolatedAsyncioTestCase):
    async def request_with(self, method, path, responses, **kwargs):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.request.side_effect = responses
        with patch('app.services.knowledge_engines.ragflow.httpx.AsyncClient', return_value=client):
            result = await RagflowEngine('http://engine/api/v1', 'test').request(method, path, **kwargs)
        return result, client

    def response(self, code=200, body=None):
        return httpx.Response(code, json=body or {'code': 0, 'data': {}}, request=httpx.Request('GET', 'http://engine'))

    async def test_legacy_patch_fallback_only_on_405(self):
        _, client = await self.request_with('PATCH', '/datasets/a/documents/b', [self.response(405), self.response()], json={'chunk_method':'naive'})
        self.assertEqual([c.args[0] for c in client.request.call_args_list], ['PATCH', 'PUT'])
        self.assertEqual(client.request.call_args_list[1].kwargs['json'], {'chunk_method':'naive'})

    async def test_ambiguous_mutation_not_retried(self):
        with self.assertRaises(EngineError):
            await self.request_with('PATCH', '/datasets/a/documents/b', [self.response(500)])

    async def test_business_failure_rejected(self):
        with self.assertRaisesRegex(EngineError, 'No access'):
            await self.request_with('POST', '/datasets', [self.response(body={'code': 102, 'message':'No access'})])

    async def test_wire_contracts(self):
        adapter = RagflowEngine('http://engine', 'test')
        adapter.request = AsyncMock(return_value={'chunks': [], 'total': 0})
        await adapter.parse('ds', 'doc')
        adapter.request.assert_awaited_with('POST', '/datasets/ds/chunks', json={'document_ids':['doc']})
        await adapter.parse('ds', 'doc', stop=True)
        adapter.request.assert_awaited_with('DELETE', '/datasets/ds/chunks', json={'document_ids':['doc']})
        await adapter.delete_chunk('ds', 'doc', 'chunk')
        adapter.request.assert_awaited_with('DELETE', '/datasets/ds/documents/doc/chunks', json={'chunk_ids':['chunk']})
        await adapter.chunks('ds', 'doc', 3, 20)
        adapter.request.assert_awaited_with('GET', '/datasets/ds/documents/doc/chunks', params={'page':3, 'page_size':20})

    def test_state_mapping(self):
        for run in ['3', 'DONE']:
            self.assertEqual(document_state({'run':run,'progress':1}), ('COMPLETED', 1))
        self.assertEqual(document_state({'run':'RUNNING','progress':-1}), ('FAILED',0))
        self.assertEqual(document_state({'run':'2','progress':0}), ('CANCELLED',0))
        self.assertEqual(document_state({'run':'future_state'}), ('UNKNOWN',0))

    def test_validation(self):
        for payload in [{'name':'   '}, {'name':'x','chunk_token_num':0}, {'name':'x','chunk_token_num':2049}, {'name':'x','delimiter':''}]:
            with self.assertRaises(ValidationError): routes.LibraryIn(**payload)
        lib = routes.LibraryIn(name='知识库')
        self.assertEqual(lib.chunk_method, 'naive')
        self.assertEqual(lib.delimiter, '\n。！？；')
        self.assertEqual(lib.chunk_token_num, 512)
        self.assertEqual(lib.strategy, 'auto')  # 策略视图缺省 auto（旧扁平载荷兼容）

    def test_library_in_strategy_view(self):
        """库级索引设置：strategy/enhancements/type_rules 与 processing 分开存储到 engine_config。"""
        lib = routes.LibraryIn(name='知识库', chunk_method='naive', strategy='by_file_type',
                               enhancements={'include_filename': True},
                               type_rules={'pdf': {'strategy': 'custom', 'method': 'paper', 'chunk_token_num': 300,
                                                   'delimiter': '。', 'children_delimiter': '\n'}})
        cfg = routes._library_engine_config(lib)
        self.assertEqual(cfg['strategy'], 'by_file_type')
        self.assertEqual(cfg['enhancements'], {'include_filename': True})
        self.assertEqual(cfg['type_rules']['pdf']['method'], 'paper')
        self.assertNotIn('strategy', cfg['processing'])
        self.assertNotIn('name', cfg['processing'])
        self.assertEqual(cfg['processing']['chunk_method'], 'naive')
        with self.assertRaises(ValidationError):
            routes.LibraryIn(name='知识库', strategy='unknown')

    def test_lib_out_full_config_and_legacy_derivation(self):
        """lib_out 返回与文档级对齐的全量配置；旧库未存 strategy 时按 processing 推导。"""
        lib = SimpleNamespace(id=1, name='库', description='', enabled=True, creator='u', created_at=None,
                              engine_config={'processing': {'chunk_method': 'naive'}, 'strategy': 'by_file_type',
                                             'enhancements': {'auto_summary': True}, 'type_rules': {'pdf': {}}})
        cfg = routes.lib_out(lib)['config']
        self.assertEqual(cfg['strategy'], 'by_file_type')
        self.assertEqual(cfg['enhancements'], {'auto_summary': True})
        self.assertEqual(cfg['type_rules'], {'pdf': {}})
        self.assertEqual(cfg['processing']['chunk_method'], 'naive')
        legacy = lambda p: routes.lib_out(SimpleNamespace(id=1, name='库', description='', enabled=True,
                                                          creator='u', created_at=None,
                                                          engine_config={'processing': p}))['config']['strategy']
        self.assertEqual(legacy({'chunk_method': 'naive', 'enable_children': True}), 'parent_child')
        self.assertEqual(legacy({'chunk_method': 'auto'}), 'auto')
        self.assertEqual(legacy({'chunk_method': 'naive'}), 'custom')

    async def test_no_edit_while_parsing(self):
        lib = SimpleNamespace(dataset_id='ds')
        doc = SimpleNamespace(engine_document_id='doc', status='PARSING')
        with patch.object(routes, 'bound_document', AsyncMock(return_value=(lib, doc))):
            with self.assertRaises(HTTPException) as raised:
                await routes.chunk_context(None, 1, 'doc', writing=True)
            self.assertEqual(raised.exception.status_code, 409)

    async def test_reparse_running_is_rejected(self):
        lib = SimpleNamespace(dataset_id='ds')
        doc = SimpleNamespace(engine_document_id='doc', status='PARSING')
        with patch.object(routes, 'bound_document', AsyncMock(return_value=(lib, doc))):
            with self.assertRaises(HTTPException): await routes.parse(1, 'doc', None)

    async def test_wrong_library_document_is_not_found(self):
        session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)))
        with patch.object(routes, 'library', AsyncMock(return_value=object())):
            with self.assertRaises(HTTPException) as raised:
                await routes.bound_document(session, 1, 'doc')
            self.assertEqual(raised.exception.status_code, 404)

    async def test_engine_unconfigured_is_503(self):
        with patch.object(routes, 'get_settings', return_value=SimpleNamespace(structured_kit_base_url='')):
            with self.assertRaises(HTTPException) as raised:
                routes.engine()
            self.assertEqual(raised.exception.status_code, 503)

    async def test_legacy_parsing_doc_marked_failed_on_refresh(self):
        lib = SimpleNamespace(engine_config={'processing': {}})
        doc = SimpleNamespace(engine_document_id='old-ragflow-id', engine_job_id=None,
                              status='PARSING', progress=0.5, message='')
        await routes.refresh_state(SimpleNamespace(), None, lib, doc)
        self.assertEqual(doc.status, 'FAILED')
        self.assertIn('MinerU', doc.message)



class UpgradeAndIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_models_normalize_source_response(self):
        adapter = RagflowEngine('http://engine', 'test')
        adapter.request = AsyncMock(return_value=[{'model_id':'id','name':'bge-m3','instance_name':'local','provider_name':'Xinference'}])
        self.assertEqual(await adapter.embedding_models(), [{'id':'id','name':'bge-m3@local@Xinference'}])

    async def test_new_disabled_chunk_updates_returned_id(self):
        adapter = RagflowEngine('http://engine','test')
        adapter.request = AsyncMock(side_effect=[{'chunk':{'id':'new'}}, None])
        await adapter.write_chunk('ds','doc',{'content':'text','available':False})
        adapter.request.assert_awaited_with('PATCH','/datasets/ds/documents/doc/chunks/new',json={'available':False})

    def test_parent_child_config(self):
        cfg = routes.ProcessingConfig(enable_children=True, children_delimiter='。')
        self.assertTrue(cfg.enable_children)
        with self.assertRaises(ValidationError): routes.ProcessingConfig(enable_children=True, chunk_method='table')

    async def test_query_does_not_leave_project_for_wrong_engine(self):
        from kb_common.clients import ragflow_client
        settings = SimpleNamespace(ragflow_base_url='http://other/api/v1',ragflow_api_key='key',ragflow_retrieval_top_k=8,
            ragflow_similarity_threshold=0.2,ragflow_vector_similarity_weight=0.3,ragflow_rerank_id='')
        with patch.object(ragflow_client,'get_settings',return_value=settings), patch.object(ragflow_client,'_binding_errors',AsyncMock(return_value=[{'dataset_id':'ds','error':'changed'}])), patch.object(ragflow_client.httpx,'AsyncClient') as client:
            result = await ragflow_client.retrieve_with_report(['ds'],'private query')
        self.assertEqual(result['hits'],[])
        self.assertEqual(result['skipped'][0]['dataset_id'],'ds')
        client.assert_not_called()



class MinerULocalTests(unittest.TestCase):
    def test_job_state_mapping(self):
        self.assertEqual(job_state({'status': 'queued'}), ('PARSING', 0.05))
        self.assertEqual(job_state({'status': 'running'}), ('PARSING', 0.5))
        self.assertEqual(job_state({'status': 'completed'}), ('COMPLETED', 1.0))
        self.assertEqual(job_state({'status': 'partial'}), ('COMPLETED', 1.0))
        self.assertEqual(job_state({'status': 'failed'}), ('FAILED', 0.0))
        self.assertEqual(job_state({'status': 'canceled'}), ('CANCELLED', 0.0))
        self.assertEqual(job_state({'status': 'weird'}), ('UNKNOWN', 0.0))

    def test_one_method_single_chunk(self):
        pieces = chunk_markdown('# 标题\n\n第一段。第二段！', {'chunk_method': 'one'})
        self.assertEqual(len(pieces), 1)
        self.assertIsNone(pieces[0]['children'])

    def test_naive_window_aggregation(self):
        text = '。'.join(f'句子{i}内容' for i in range(20)) + '。'
        pieces = chunk_markdown(text, {'chunk_method': 'naive', 'chunk_token_num': 20, 'delimiter': '。'})
        self.assertGreater(len(pieces), 1)
        for piece in pieces:
            self.assertLessEqual(est_tokens(piece['content']), 20 + 8)  # 单句不硬拆

    def test_children_materialized(self):
        text = '第一行内容\n第二行内容\n第三行内容'
        pieces = chunk_markdown(text, {'chunk_method': 'naive', 'chunk_token_num': 500,
                                       'delimiter': '。', 'enable_children': True, 'children_delimiter': '\n'})
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0]['children'], ['第一行内容', '第二行内容', '第三行内容'])

    def test_preprocess_replace_whitespace(self):
        md = '第一段。  \n\n\n\n第二段。\t\t第三段。'
        pieces = chunk_markdown(md, {'chunk_method': 'one', 'replace_whitespace': True})
        content = pieces[0]['content']
        self.assertNotRegex(content, r'\n{3,}')      # 3+ 连续换行折叠为 2 个
        self.assertNotRegex(content, r' {2,}|\t{2,}')  # 2+ 连续空格/制表符折叠为单空格
        self.assertIn('\n\n', content)               # 换行结构保留（不影响标题识别）

    def test_preprocess_replace_whitespace_off_keeps_raw(self):
        md = '第一段。  \n\n\n\n第二段。'
        pieces = chunk_markdown(md, {'chunk_method': 'one'})
        self.assertIn('\n\n\n\n', pieces[0]['content'])

    def test_preprocess_remove_urls_emails(self):
        md = ('联系 admin@example.com 或访问 https://example.com/doc?a=1 获取详情。\n\n'
              '![图片](https://cdn.example.com/a.png) 与 [文档](https://docs.example.com/x) 保留。')
        pieces = chunk_markdown(md, {'chunk_method': 'one', 'remove_urls_emails': True})
        content = pieces[0]['content']
        self.assertNotIn('admin@example.com', content)             # 邮箱删除
        self.assertNotIn('https://example.com/doc', content)       # 裸 URL 删除
        self.assertIn('![图片](https://cdn.example.com/a.png)', content)  # markdown 图片受保护
        self.assertIn('[文档](https://docs.example.com/x)', content)      # markdown 链接受保护

    def test_overlap_carries_tail_sentences(self):
        text = ''.join(f'第{i}句话讲的是内容{"甲" if i % 2 else "乙"}。' for i in range(40))
        base = {'chunk_method': 'naive', 'chunk_token_num': 100, 'delimiter': '。'}
        plain = chunk_markdown(text, {**base, 'overlap': 0})
        overlapped = chunk_markdown(text, {**base, 'overlap': 20})
        self.assertGreater(len(plain), 1)
        self.assertGreater(len(overlapped), 1)
        # 无重叠：相邻分段内容不重复；有重叠：后一分段以分段间重复的尾部句子开头
        self.assertNotIn(plain[1]['content'][:8], plain[0]['content'])
        self.assertIn(overlapped[1]['content'][:8], overlapped[0]['content'])
        for piece in overlapped:  # 重叠不改变长度上限（单句不硬拆容忍度一致）
            self.assertLessEqual(est_tokens(piece['content']), 100 + 10)

    def test_overlap_clamped_to_limit(self):
        """overlap 夹取到 limit-1：limit=1 时重叠为 0，退化为逐句成块且无重复。"""
        text = '。'.join(f'句子{i}内容' for i in range(20)) + '。'
        pieces = chunk_markdown(text, {'chunk_method': 'naive', 'chunk_token_num': 1, 'delimiter': '。'})
        for piece in pieces:
            self.assertLessEqual(est_tokens(piece['content']), 1 + 8)

    def test_data_uri_images_stripped(self):
        md = '# 标题\n\n![img](data:image/png;base64,AAAA)\n\n正文内容。'
        self.assertNotIn('data:image', clean_markdown(md))
        pieces = chunk_markdown(md, {'chunk_method': 'one'})
        self.assertIn('正文内容', pieces[0]['content'])
        self.assertNotIn('AAAA', pieces[0]['content'])

    def test_image_refs_preserved_in_chunks(self):
        # MinerU zip 产物的相对图片引用必须完整保留在分段中（渲染层改写为代理 URL）
        md = '## 装配图\n\n零件关系如下图所示。\n\n![](images/page_0_image_body_3.jpg)\n\n按序号装配。'
        pieces = chunk_markdown(md, {'chunk_method': 'auto', 'chunk_token_num': 512, 'delimiter': '\n。'})
        joined = '\n'.join(p['content'] for p in pieces)
        self.assertIn('![](images/page_0_image_body_3.jpg)', joined)

    def test_extract_zip_bundle(self):
        import io, zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('markdown.md', '# 手册\n\n![](images/a.jpg)\n\n说明。')
            zf.writestr('images/a.jpg', b'\xff\xd8\xff')  # JPEG 头
            zf.writestr('images/b.png', b'\x89PNG')
            zf.writestr('model_output.json', '{}')  # 非图片/md，忽略
        markdown, images = routes._extract_zip_bundle(buf.getvalue())
        self.assertIn('![](images/a.jpg)', markdown)
        self.assertEqual(set(images), {'a.jpg', 'b.png'})
        self.assertEqual(images['a.jpg'], b'\xff\xd8\xff')

    def test_image_name_validation(self):
        # 图片代理的文件名白名单：拒绝路径遍历
        for bad in ('../x.jpg', 'a/b.jpg', 'a\\b.jpg', ''):
            self.assertIsNone(routes._IMAGE_NAME_RE.match(bad))
        self.assertTrue(routes._IMAGE_NAME_RE.match('page_0_image_body_3.jpg'))

    def test_empty_markdown(self):
        self.assertEqual(chunk_markdown('   \n\n', {'chunk_method': 'naive'}), [])

    def test_auto_splits_at_headings(self):
        md = '# 第一章 概述\n\n这是概述内容。\n\n## 背景\n\n背景内容。\n\n# 第二章 实现\n\n实现内容。'
        pieces = chunk_markdown(md, {'chunk_method': 'auto', 'chunk_token_num': 512, 'delimiter': '\n。'})
        self.assertEqual(len(pieces), 3)
        self.assertTrue(pieces[0]['content'].startswith('# 第一章 概述'))
        self.assertTrue(pieces[1]['content'].startswith('## 背景'))
        self.assertTrue(pieces[2]['content'].startswith('# 第二章 实现'))

    def test_auto_oversized_section_splits_with_path_prefix(self):
        body = '。'.join(f'第{i}句内容较长一些' for i in range(60)) + '。'
        md = f'## 3.2 接口设计\n\n{body}'
        pieces = chunk_markdown(md, {'chunk_method': 'auto', 'chunk_token_num': 60, 'delimiter': '\n。'})
        self.assertGreater(len(pieces), 1)
        self.assertTrue(pieces[0]['content'].startswith('## 3.2 接口设计'))
        for piece in pieces[1:]:
            self.assertTrue(piece['content'].startswith('3.2 接口设计\n'))

    def test_auto_fence_heading_ignored(self):
        md = '# 真标题\n\n```python\n# 这是注释不是标题\nprint(1)\n```\n\n正文内容。'
        pieces = chunk_markdown(md, {'chunk_method': 'auto', 'chunk_token_num': 512, 'delimiter': '\n。'})
        self.assertEqual(len(pieces), 1)
        self.assertIn('# 这是注释不是标题', pieces[0]['content'])

    def test_auto_table_stays_atomic(self):
        intro = ''.join(f'前文说明第{i}句。' for i in range(10))
        outro = ''.join(f'后续补充第{i}句。' for i in range(10))
        rows = '\n'.join(f'| 参数名称{i} | 详细说明文字{i} |' for i in range(6))
        md = f'## 参数表\n\n{intro}\n\n| 参数 | 说明 |\n| --- | --- |\n{rows}\n\n{outro}'
        pieces = chunk_markdown(md, {'chunk_method': 'auto', 'chunk_token_num': 100, 'delimiter': '。'})
        self.assertGreater(len(pieces), 1)
        table_chunks = [p for p in pieces if '| 参数 | 说明 |' in p['content']]
        self.assertEqual(len(table_chunks), 1)
        self.assertIn('| 参数名称5 | 详细说明文字5 |', table_chunks[0]['content'])

    def test_html_table_kept_atomic(self):
        """MinerU HTML 表格是原子单元：绝不从中间切断（宁可整块超出）。"""
        rows = ''.join(f'<tr><td>151381300{i}</td><td>左线钩Thread guide</td>'
                       f'<td>螺钉SM11/64"×40 L=6</td></tr>' for i in range(30))
        md = f'# 装配图\n\n<table><tbody>{rows}</tbody></table>\n\n按序号装配。'
        pieces = chunk_markdown(md, {'chunk_method': 'auto', 'chunk_token_num': 64, 'delimiter': '。'})
        joined = ''.join(p['content'] for p in pieces)
        self.assertEqual(joined.count('<table>'), 1)
        self.assertEqual(joined.count('</table>'), 1)
        self.assertEqual(joined.count('<tr>'), 30)
        self.assertNotIn('\x00tbl', joined)  # 占位符全部还原，无切断残留
        containing = [p for p in pieces if '<table>' in p['content']]
        self.assertEqual(len(containing), 1)  # 完整表格恰好落在同一个分段内
        self.assertIn('</table>', containing[0]['content'])

    def test_html_table_not_duplicated_in_overlap(self):
        """表格占位不进重叠尾：相邻分段不重复整表。"""
        md = '前文说明。' * 10 + '\n\n<table><tr><td>零件</td></tr></table>\n\n' + '后续补充。' * 10
        pieces = chunk_markdown(md, {'chunk_method': 'auto', 'chunk_token_num': 40,
                                     'delimiter': '。', 'overlap': 20})
        self.assertEqual(sum('<table>' in p['content'] for p in pieces), 1)

    def test_pipe_table_atomic_in_naive(self):
        """naive 切句路径同样不切断管道表格（单元格内含句末标点）。"""
        md = '前言说明。\n\n| 零件 | 说明 |\n| --- | --- |\n| 左线钩。单价 | 5 元 |\n\n结尾。'
        pieces = chunk_markdown(md, {'chunk_method': 'naive', 'chunk_token_num': 8, 'delimiter': '。'})
        joined = ''.join(p['content'] for p in pieces)
        self.assertIn('| 左线钩。单价 | 5 元 |', joined)  # 表格行完整，未按句号切断
        self.assertEqual(joined.count('| 零件 | 说明 |'), 1)

    def test_table_forces_segment_break(self):
        """表格结束后强制分段：表格分段不含后续正文，下一分段即使有 overlap 也不回带表格。"""
        rows = '\n'.join(f'| 参数{i} | 说明{i} |' for i in range(6))
        md = (f'前文说明第一句。前文说明第二句。\n\n| 参数 | 说明 |\n| --- | --- |\n{rows}\n\n'
              f'后续正文第一句。后续正文第二句。')
        pieces = chunk_markdown(md, {'chunk_method': 'auto', 'chunk_token_num': 200,
                                     'delimiter': '。', 'overlap': 30})
        containing = [p for p in pieces if '| 参数 | 说明 |' in p['content']]
        self.assertEqual(len(containing), 1)
        self.assertNotIn('后续正文第一句', containing[0]['content'])  # 表格分段到此为止
        after = [p for p in pieces if '后续正文第一句' in p['content']]
        self.assertTrue(after)
        self.assertNotIn('| 参数', after[0]['content'])  # overlap 不回带表格内容

    def test_table_note_attached_to_table_segment(self):
        """表格下标（注：…）随表格同分段，且表格段（含下标）结束后才分段。"""
        rows = '\n'.join(f'| 零件{i} | 数量{i} |' for i in range(5))
        md = (f'| 零件 | 数量 |\n| --- | --- |\n{rows}\n\n注：以上数量为装配用量。\n\n'
              f'后续正文内容。')
        pieces = chunk_markdown(md, {'chunk_method': 'auto', 'chunk_token_num': 200, 'delimiter': '。'})
        containing = [p for p in pieces if '| 零件 | 数量 |' in p['content']]
        self.assertEqual(len(containing), 1)
        self.assertIn('注：以上数量为装配用量。', containing[0]['content'])  # 下标属于表格分段
        self.assertNotIn('后续正文内容', containing[0]['content'])          # 下标之后强制分段

    def test_naive_table_forces_segment_break(self):
        """naive 聚合路径：表格结束后同样强制分段（占位与邻文同句时先拆开再聚合）。"""
        md = '前言说明。\n\n| a | b |\n| --- | --- |\n| 甲。乙 | 丙 |\n\n结尾说明。'
        pieces = chunk_markdown(md, {'chunk_method': 'naive', 'chunk_token_num': 200, 'delimiter': '。'})
        containing = [p for p in pieces if '| a | b |' in p['content']]
        self.assertEqual(len(containing), 1)
        self.assertNotIn('结尾说明', containing[0]['content'])
        self.assertTrue(any('结尾说明' in p['content'] for p in pieces))

    def test_auto_texttile_fallback_structureless(self):
        topic_a = ''.join(f'数据库索引结构影响查询性能和事务吞吐{i}。' for i in range(24))
        topic_b = ''.join(f'烹饪火候时长决定食材口感与调味层次{i}。' for i in range(24))
        pieces = chunk_markdown(topic_a + topic_b, {'chunk_method': 'auto', 'chunk_token_num': 512, 'delimiter': '。'})
        self.assertEqual(len(pieces), 2)
        self.assertIn('数据库', pieces[0]['content'])
        self.assertNotIn('烹饪', pieces[0]['content'])
        self.assertIn('烹饪', pieces[1]['content'])

    def test_auto_short_text_single_chunk(self):
        pieces = chunk_markdown('简短内容。', {'chunk_method': 'auto'})
        self.assertEqual(len(pieces), 1)

    def test_auto_texttile_noisy_within_topic(self):
        # 主题内用词多样（主题内相似度低）时，仍应只在真实主题切换处切分
        db = ''.join(s + '。' for s in (
            '数据库索引结构直接影响查询性能和事务吞吐能力', '合理的索引设计可以显著降低磁盘读取次数',
            '查询优化器根据统计信息选择执行计划', '事务隔离级别决定了并发读写的可见性规则',
            '缓冲池命中率是衡量存储引擎效率的重要指标', '慢查询日志帮助定位性能瓶颈的SQL语句',
            '主从复制通过二进制日志实现数据同步', '分库分表策略解决单表数据量过大的问题',
            '数据库连接池需要合理配置最大连接数', '定期执行表分析可以保证统计信息准确'))
        cooking = ''.join(s + '。' for s in (
            '烹饪火候的时长决定食材口感与调味层次', '大火快炒能够锁住蔬菜的水分和色泽',
            '炖汤需要小火慢煨才能释放食材鲜味', '调味讲究先后顺序与比例搭配',
            '翻炒的锅气来源于高温与美拉德反应', '食材的腌制时间影响入味程度',
            '蒸制能够最大程度保留营养成分', '刀工的粗细均匀影响受热一致性',
            '勾芡的浓稠度决定菜肴的挂汁效果', '油温的判断可以通过竹筷气泡观察'))
        pieces = chunk_markdown(db + '\n' + cooking,
                                {'chunk_method': 'auto', 'chunk_token_num': 512, 'delimiter': '\n。'})
        self.assertEqual(len(pieces), 2)
        self.assertIn('数据库', pieces[0]['content'])
        self.assertNotIn('烹饪', pieces[0]['content'])
        self.assertIn('烹饪', pieces[1]['content'])
        self.assertNotIn('数据库', pieces[1]['content'])

    def test_auto_texttile_uniform_text_not_split(self):
        # 高相似均匀文本：段落间相对波动不应触发误切
        uniform = '\n'.join(''.join(f'数据库索引影响查询性能{i}{j}。' for j in range(5)) for i in range(4))
        pieces = chunk_markdown(uniform, {'chunk_method': 'auto', 'chunk_token_num': 512, 'delimiter': '\n。'})
        self.assertEqual(len(pieces), 1)

    def test_auto_empty_markdown(self):
        self.assertEqual(chunk_markdown('   \n\n', {'chunk_method': 'auto'}), [])

    def test_auto_ignores_enable_children(self):
        # 策略隔离：auto 分段不产生子块，即使配置中残留 enable_children
        md = '# 第一章\n\n第一段。\n\n第二段。\n\n# 第二章\n\n第三段。'
        pieces = chunk_markdown(md, {'chunk_method': 'auto', 'chunk_token_num': 512,
                                     'enable_children': True, 'children_delimiter': '\n'})
        self.assertGreater(len(pieces), 1)
        for piece in pieces:
            self.assertIsNone(piece['children'])

    def test_non_naive_methods_ignore_enable_children(self):
        # 策略隔离：book/paper 等自定义方式同样不产生子块
        pieces = chunk_markdown('第一段。\n\n第二段。', {'chunk_method': 'book', 'chunk_token_num': 512,
                                                    'delimiter': '。', 'enable_children': True})
        self.assertEqual(len(pieces), 1)
        self.assertIsNone(pieces[0]['children'])

    def test_parsed_markdown_key_sibling_of_original(self):
        doc = SimpleNamespace(storage_path='document-libraries/7/abc/doc.pdf')
        self.assertEqual(routes._parsed_markdown_key(doc), 'document-libraries/7/abc/parsed.md')

    def test_sniff_format(self):
        # 合法 JSON 对象/数组 → json 视图；标量/非法 JSON/普通文本 → markdown 渲染
        self.assertEqual(routes._sniff_format('{"a": 1}'), 'json')
        self.assertEqual(routes._sniff_format('  \n[1, 2, 3]'), 'json')
        self.assertEqual(routes._sniff_format('42'), 'markdown')
        self.assertEqual(routes._sniff_format('{"a": '), 'markdown')
        self.assertEqual(routes._sniff_format('# 标题\n\n正文'), 'markdown')


class ParsedContentTests(unittest.IsolatedAsyncioTestCase):
    """解析原文持久化（MinIO parsed.md）与读取端点；假模块注入避免依赖真实 MinIO。"""

    def _fake_minio_module(self, get_object=None, upload_bytes=None):
        import sys, types
        fake = types.ModuleType('kb_common.clients.minio_client')
        fake.RAW = 'raw-docs'
        fake.upload_bytes = upload_bytes or Mock()
        fake.minio = SimpleNamespace(get_object=get_object or Mock())
        return fake

    async def test_store_parsed_markdown_uploads_sibling_object(self):
        import sys
        doc = SimpleNamespace(storage_path='document-libraries/7/abc/doc.pdf')
        fake = self._fake_minio_module()
        with patch.dict(sys.modules, {'kb_common.clients.minio_client': fake}):
            await routes._store_parsed_markdown(doc, '# 手册')
        fake.upload_bytes.assert_called_once_with('raw-docs', 'document-libraries/7/abc/parsed.md',
                                                  '# 手册'.encode('utf-8'), 'text/markdown')

    async def test_store_parsed_markdown_skips_empty(self):
        import sys
        fake = self._fake_minio_module()
        with patch.dict(sys.modules, {'kb_common.clients.minio_client': fake}):
            await routes._store_parsed_markdown(SimpleNamespace(storage_path='a/b/c.md'), '')
        fake.upload_bytes.assert_not_called()

    async def test_parsed_content_endpoint_404_when_missing(self):
        import sys, uuid as _uuid
        doc = SimpleNamespace(storage_path='document-libraries/7/abc/doc.pdf')
        fake = self._fake_minio_module(get_object=Mock(side_effect=RuntimeError('NoSuchKey')))
        with patch.object(routes, 'bound_document', AsyncMock(return_value=(object(), doc))), \
             patch.dict(sys.modules, {'kb_common.clients.minio_client': fake}):
            with self.assertRaises(HTTPException) as ctx:
                await routes.parsed_content(7, _uuid.uuid4(), s=None)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn('重新解析', str(ctx.exception.detail))


class ProcessingMergeTests(unittest.TestCase):
    """_processing_for 合并语义：文档级逐键覆盖库级（空字符串/None 不覆盖，False/0 覆盖）。"""

    def merge(self, lib_cfg, doc_cfg):
        lib = SimpleNamespace(engine_config={'processing': lib_cfg})
        doc = SimpleNamespace(engine_config={'processing': doc_cfg} if doc_cfg is not None else None)
        return routes._processing_for(lib, doc)

    def test_doc_auto_overrides_lib_naive(self):
        merged = self.merge({'chunk_method': 'naive', 'enable_children': True},
                            {'chunk_method': 'auto', 'enable_children': False})
        self.assertEqual(merged['chunk_method'], 'auto')
        self.assertFalse(merged['enable_children'])  # False 必须覆盖，否则父子残留进 auto

    def test_doc_without_config_uses_library(self):
        merged = self.merge({'chunk_method': 'auto', 'chunk_token_num': 256}, None)
        self.assertEqual(merged['chunk_method'], 'auto')
        self.assertEqual(merged['chunk_token_num'], 256)

    def test_empty_values_do_not_override(self):
        merged = self.merge({'chunk_method': 'naive', 'delimiter': '。'},
                            {'delimiter': '', 'embedding_model': None})
        self.assertEqual(merged['delimiter'], '。')
        self.assertIsNone(merged.get('embedding_model'))

    def test_zero_overlap_overrides(self):
        merged = self.merge({'overlap': 25}, {'overlap': 0})
        self.assertEqual(merged['overlap'], 0)  # 0 是有效配置（不重叠），不能被过滤

if __name__ == '__main__': unittest.main()
