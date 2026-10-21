import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from django.test import Client, TestCase, TransactionTestCase, override_settings
from langchain_core.messages import AIMessage, HumanMessage

from .models import Conversation, Run
from .services.debug import RunTrace, log_path, read_entries, record, sanitize, trace_context


class DebugTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        override = override_settings(RUNTIME_DIR=Path(self.directory.name),
            DEEPSEEK_API_KEY='sk-test-only-key', AMAP_API_KEY='amap-test-secret', SECRET_KEY='django-test-secret')
        override.enable()
        self.addCleanup(override.disable)
        self.client.get('/')
        self.conversation = self.client.post('/api/conversations/', '{}', content_type='application/json').json()
        self.run = Run.objects.create(conversation_id=self.conversation['id'])

    def test_disabled_by_default_does_not_write_files(self):
        with trace_context(RunTrace(self.run.pk)):
            record('test', content='not recorded')
        self.assertFalse(log_path(self.run.pk).exists())
        self.assertFalse(self.client.get('/api/state/').json()['debug_enabled'])

    def test_enable_disable_during_active_run_and_persist_preference(self):
        response = self.client.post('/api/debug/', json.dumps({'enabled': True}), content_type='application/json')
        self.assertTrue(response.json()['debug_enabled'])
        with trace_context(RunTrace(self.run.pk)):
            record('enabled.marker', value='record this')
            self.client.post('/api/debug/', json.dumps({'enabled': False}), content_type='application/json')
            record('disabled.marker', value='do not record')
        entries = read_entries(self.run.pk)['entries']
        self.assertIn('enabled.marker', [e['kind'] for e in entries])
        self.assertNotIn('disabled.marker', [e['kind'] for e in entries])
        self.assertFalse(self.client.get('/api/state/').json()['debug_enabled'])

    def test_sensitive_values_are_redacted_without_hiding_token_counts(self):
        data = sanitize({'api_key': 'key', 'Cookie': 'a=secret', 'input_tokens': 123,
            'nested': {'authorization': 'Bearer foo', 'reasoning_content': 'private'},
            'url': 'https://example.com/?key=amap-test-secret&xsec_token=signed-note-token&foo=ok',
            'body': '{"token":"nested-secret"} sk-test-only-key django-test-secret'})
        text = json.dumps(data)
        for secret in ('amap-test-secret', 'signed-note-token', 'nested-secret', 'sk-test-only-key', 'django-test-secret', 'private'):
            self.assertNotIn(secret, text)
        self.assertEqual(data['input_tokens'], 123)
        self.assertIn('foo=ok', data['url'])

    def test_read_cursor_download_and_owner_isolation(self):
        self.run.debug_enabled = True
        self.run.save()
        trace = RunTrace(self.run.pk)
        trace.write('first', {'message': '中文日志', 'key': 'amap-test-secret'})
        url = f'/api/runs/{self.run.pk}/debug/'
        first = self.client.get(url).json()
        self.assertEqual(len(first['entries']), 1)
        self.assertNotIn('amap-test-secret', json.dumps(first))
        trace.write('second', {'value': 2})
        second = self.client.get(url, {'after': first['cursor']}).json()
        self.assertEqual([e['kind'] for e in second['entries']], ['second'])
        self.assertEqual(Client().get(url).status_code, 404)
        self.assertEqual(Client().get(url + '?download=1').status_code, 404)
        response = self.client.get(url + '?download=1')
        self.assertEqual(response.status_code, 200)
        content = b''.join(response.streaming_content).decode()
        self.assertIn('中文日志', content)
        self.assertNotIn('amap-test-secret', content)
        response.close()

    def test_invalid_toggle_and_cursor_rejected(self):
        for enabled in ('yes', 1, None):
            self.assertEqual(self.client.post('/api/debug/', json.dumps({'enabled': enabled}), content_type='application/json').status_code, 400)
        self.assertEqual(self.client.get(f'/api/runs/{self.run.pk}/debug/?after=bad').status_code, 400)

    @patch('planner.views.enqueue')
    def test_new_request_inherits_debug_preference(self, enqueue):
        self.run.status = 'done'
        self.run.save()
        self.client.post('/api/debug/', '{"enabled":true}', content_type='application/json')
        response = self.client.post('/api/chat/', json.dumps({'conversation_id': self.conversation['id'], 'message': '杭州三天'}), content_type='application/json')
        self.assertEqual(response.status_code, 202)
        self.assertTrue(Run.objects.get(pk=response.json()['run_id']).debug_enabled)


class DebugAsyncTests(TransactionTestCase):
    @patch('planner.services.knowledge.crawler.crawl', return_value=[])
    @patch('planner.services.agent.amap_tools', new_callable=AsyncMock)
    @patch('planner.services.agent.model')
    @patch('planner.services.agent.structured', new_callable=AsyncMock)
    def test_rag_trace_is_recorded_from_langchain_tool_thread(self, structured, llm, maps, crawl):
        from unittest.mock import Mock
        from .schemas import Analysis
        from .services.agent import plan_trip
        structured.return_value = Analysis(city='杭州', requirements={'天数': 2})
        maps.return_value = [Mock(name='unused', args_schema={})]
        maps.return_value[0].name = 'unused'
        llm.return_value.bind_tools.return_value.ainvoke = AsyncMock(side_effect=[
            AIMessage(content='', tool_calls=[{'name': 'get_travel_info', 'args': {'query': '西湖路线', 'city': '杭州'}, 'id': 'rag-1'}]),
            AIMessage(content='', tool_calls=[{'name': 'ask_user', 'args': {'question': '要换一个景点吗？'}, 'id': 'ask-1'}])])
        with tempfile.TemporaryDirectory() as directory, override_settings(RUNTIME_DIR=Path(directory)):
            conversation = Conversation.objects.create(owner='rag-thread-test')
            run = Run.objects.create(conversation=conversation, debug_enabled=True)
            with trace_context(RunTrace(run.pk)):
                result = asyncio.run(plan_trip([], {}, {}, lambda *_: None))
            self.assertEqual(result['status'], 'waiting')
            kinds = [entry['kind'] for entry in read_entries(run.pk)['entries']]
            self.assertIn('rag.request', kinds)
            self.assertIn('rag.supplement', kinds)
            self.assertIn('rag.result', kinds)
            self.assertIn('tool.response', kinds)

    def test_llm_and_tool_threads_share_trace_and_exclude_private_reasoning(self):
        from .services.llm import invoke
        with tempfile.TemporaryDirectory() as directory, override_settings(RUNTIME_DIR=Path(directory)):
            conversation = Conversation.objects.create(owner='async-test')
            run = Run.objects.create(conversation=conversation, debug_enabled=True)
            llm = AsyncMock()
            llm.ainvoke.return_value = AIMessage(content='模型输出', additional_kwargs={'reasoning_content': 'PRIVATE_THOUGHT'})

            async def execute():
                await invoke(llm, [HumanMessage(content='旅行需求')], 'test')
                await asyncio.to_thread(record, 'thread.event', value='工具线程输出')

            with trace_context(RunTrace(run.pk)):
                asyncio.run(execute())
            entries = read_entries(run.pk)['entries']
            self.assertEqual([e['kind'] for e in entries], ['llm.request', 'llm.response', 'thread.event'])
            self.assertNotIn('PRIVATE_THOUGHT', json.dumps(entries))

    @patch('planner.services.agent.plan_trip', new_callable=AsyncMock)
    def test_background_failure_preserves_redacted_traceback(self, plan):
        from .jobs import execute
        with tempfile.TemporaryDirectory() as directory, override_settings(RUNTIME_DIR=Path(directory), DEEPSEEK_API_KEY='sk-failure-test'):
            conversation = Conversation.objects.create(owner='failure-test')
            run = Run.objects.create(conversation=conversation, debug_enabled=True)
            plan.side_effect = RuntimeError('API rejected sk-failure-test')
            execute(run.pk)
            run.refresh_from_db()
            self.assertEqual(run.status, 'error')
            entries = read_entries(run.pk)['entries']
            self.assertEqual(entries[-1]['kind'], 'run.error')
            self.assertIn('Traceback', entries[-1]['data']['traceback'])
            self.assertNotIn('sk-failure-test', json.dumps(entries))
