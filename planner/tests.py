import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

from django.test import Client, TestCase, SimpleTestCase, override_settings
from django.utils import timezone
from langchain_core.messages import AIMessage

from .models import Chunk, Conversation, Message, Note, Run
from .schemas import Analysis, EvidenceCheck, Plan, Review
from .services.agent import plan_trip, route_succeeded, validate_plan
from .services.crawler import LoginRequired, parse_published
from .services.knowledge import get_travel_info, retrieve_window, save_notes


class ChatAPITests(TestCase):
    def setUp(self):
        self.client.get('/')
        self.conversation = self.client.post('/api/conversations/', '{}', content_type='application/json').json()

    def test_page_and_state_do_not_expose_keys(self):
        from django.conf import settings
        for response in [self.client.get('/'), self.client.get('/api/state/')]:
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(settings.DEEPSEEK_API_KEY, response.content.decode())
            self.assertNotIn(settings.AMAP_API_KEY, response.content.decode())

    def test_session_cannot_read_or_write_another_session(self):
        other = Client()
        response = other.get('/api/state/', {'conversation': self.conversation['id']})
        self.assertIsNone(response.json()['conversation'])
        response = other.post('/api/chat/', json.dumps({'conversation_id': self.conversation['id'], 'message': '继续'}), content_type='application/json')
        self.assertEqual(response.status_code, 404)
        run = Run.objects.create(conversation_id=self.conversation['id'])
        self.assertEqual(other.get(f'/api/runs/{run.pk}/').status_code, 404)

    @patch('planner.views.enqueue')
    def test_message_is_persisted_before_background_dispatch(self, enqueue):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post('/api/chat/', json.dumps({'conversation_id': self.conversation['id'], 'message': '杭州三天'}), content_type='application/json')
        self.assertEqual(response.status_code, 202)
        self.assertEqual(Message.objects.get().content, '杭州三天')
        enqueue.assert_called_once()
        self.assertEqual(self.client.post('/api/chat/', json.dumps({'conversation_id': self.conversation['id'], 'message': '继续'}), content_type='application/json').status_code, 409)

    def test_invalid_inputs_and_csrf(self):
        self.assertEqual(self.client.post('/api/chat/', '[]', content_type='application/json').status_code, 400)
        self.assertEqual(self.client.post('/api/chat/', '{}', content_type='application/json').status_code, 400)
        self.assertEqual(self.client.get('/api/state/?conversation=invalid').status_code, 400)
        protected = Client(enforce_csrf_checks=True)
        self.assertEqual(protected.post('/api/conversations/', '{}', content_type='application/json').status_code, 403)

    def test_project_uses_separate_cookies_and_accepts_its_own_csrf_token(self):
        protected = Client(enforce_csrf_checks=True)
        response = protected.get('/')
        self.assertIn('travel_sessionid', response.cookies)
        self.assertIn('travel_csrftoken', response.cookies)
        self.assertNotIn('sessionid', response.cookies)
        self.assertNotIn('csrftoken', response.cookies)
        token = protected.cookies['travel_csrftoken'].value
        response = protected.post('/api/conversations/', '{}', content_type='application/json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 201)

    @override_settings(XHS_LOGIN_ENABLED=False)
    @patch('planner.views.subprocess.Popen')
    def test_server_does_not_open_a_desktop_login_browser(self, launch):
        response = self.client.post('/api/xhs/login/', '{}', content_type='application/json')
        self.assertEqual(response.status_code, 403)
        launch.assert_not_called()


class KnowledgeTests(TestCase):
    def add(self, name, days, vector=None, content='西湖步行路线和游览建议'):
        from django.conf import settings
        note = Note.objects.create(note_id=name, title=name, content=content,
            url='https://www.xiaohongshu.com/explore/' + name, city='杭州',
            published_at=None if days is None else timezone.now() - timedelta(days=days), content_hash=name)
        return Chunk.objects.create(note=note, position=0, content=content,
            embedding=vector or [1., 0.], embedding_model=settings.EMBEDDING_MODEL)

    def test_date_filter_precedes_similarity_and_unknown_not_recent(self):
        recent = self.add('recent', 2)
        self.add('old', 70)
        self.add('unknown', None)
        self.add('irrelevant', 1, [0., 1.])
        results = retrieve_window('西湖路线', '杭州', [1., 0.], 0, 30)
        self.assertEqual([r['id'] for r in results], [recent.pk])

    @patch('planner.services.knowledge.crawler.crawl')
    @patch('planner.services.knowledge.LocalEmbeddings')
    @patch('planner.services.knowledge.judge_evidence')
    def test_expands_window_and_preserves_useful_recent_evidence(self, judge, embedding, crawl):
        recent = self.add('recent', 3)
        old = self.add('old', 60)
        embedding.return_value.embed_query.return_value = [1., 0.]
        judge.side_effect = [EvidenceCheck(relevant_ids=[recent.pk], sufficient=False, missing='还缺返程'),
                             EvidenceCheck(relevant_ids=[recent.pk, old.pk], sufficient=True)]
        result = get_travel_info('西湖路线和返程', '杭州')
        self.assertTrue(result['sufficient'])
        self.assertEqual([i['note_id'] for i in result['evidence']], ['recent', 'old'])
        crawl.assert_not_called()

    @patch('planner.services.knowledge.crawler.crawl')
    @patch('planner.services.knowledge.LocalEmbeddings')
    def test_dynamic_information_does_not_fall_back_to_old_notes(self, embedding, crawl):
        self.add('old-ticket', 80)
        embedding.return_value.embed_query.return_value = [1., 0.]
        crawl.return_value = []
        result = get_travel_info('最近预约规则', '杭州', freshness_days=7)
        self.assertFalse(result['sufficient'])
        self.assertEqual(result['evidence'], [])
        crawl.assert_called_once()

    @patch('planner.services.knowledge.crawler.crawl', return_value=[])
    def test_failed_crawl_is_bounded_and_cached(self, crawl):
        get_travel_info('缺少的资料', '杭州')
        get_travel_info('缺少的资料', '杭州')
        crawl.assert_called_once()

    @patch('planner.services.knowledge.crawler.crawl', side_effect=LoginRequired('需要登录'))
    def test_login_request_is_propagated_to_agent(self, crawl):
        with self.assertRaises(LoginRequired):
            get_travel_info('西湖路线', '杭州')

    @patch('planner.services.knowledge.LocalEmbeddings')
    def test_note_updates_replace_chunks_without_duplicate_note(self, embedding):
        embedding.return_value.embed_documents.side_effect = lambda texts: [[1., 0.] for _ in texts]
        data = {'note_id': 'same', 'title': '西湖', 'content': '西湖路线介绍。' * 30,
                'url': 'https://www.xiaohongshu.com/explore/same', 'city': '杭州',
                'published_at': timezone.now() - timedelta(days=3)}
        save_notes([data])
        save_notes([data])
        self.assertEqual(Note.objects.count(), 1)
        self.assertEqual(embedding.return_value.embed_documents.call_count, 1)
        data['content'] = '新的路线和入口介绍。' * 20
        save_notes([data])
        self.assertEqual(Note.objects.count(), 1)
        self.assertEqual(embedding.return_value.embed_documents.call_count, 2)
        self.assertTrue(all('新的路线' in c.content for c in Chunk.objects.all()))


class AgentTests(SimpleTestCase):
    def test_failed_or_empty_route_is_not_counted_as_confirmed(self):
        self.assertFalse(route_succeeded('{"paths": []}'))
        self.assertFalse(route_succeeded('{"status":"0","duration":50}'))
        self.assertFalse(route_succeeded('{"error":"Unavailable"}'))
        self.assertTrue(route_succeeded('{"paths":[{"duration":"300","distance":"1000"}]}'))

    def plan(self):
        return Plan.model_validate({'title': '杭州一日', 'city': '杭州', 'summary': '轻松游览', 'days': [
            {'day': 1, 'title': '西湖', 'activities': [
                {'time': '09:00', 'end_time': '10:00', 'name': '断桥'},
                {'time': '10:20', 'end_time': '11:00', 'name': '白堤', 'travel_minutes': 15}]}]})

    def test_time_overlap_and_missing_route_detected(self):
        plan = self.plan()
        self.assertEqual(validate_plan(plan, 1), [])
        self.assertTrue(validate_plan(plan, 0))
        plan.days[0].activities[1].time = '10:05'
        self.assertTrue(any('交通时间' in s for s in validate_plan(plan, 1)))

    def test_explicit_transport_interval_is_not_counted_twice(self):
        plan = self.plan()
        activity = plan.days[0].activities[1]
        activity.kind = '交通'
        activity.time = '10:00'
        activity.end_time = '10:30'
        activity.travel_minutes = 30
        self.assertEqual(validate_plan(plan, 1), [])
        activity.end_time = '10:10'
        self.assertTrue(any('短于' in s for s in validate_plan(plan, 1)))

    @patch('planner.services.agent.amap_tools', new_callable=AsyncMock)
    @patch('planner.services.agent.structured', new_callable=AsyncMock)
    def test_missing_user_info_pauses_without_map_calls(self, structured, maps):
        structured.return_value = Analysis(title='新行程', question='想去哪里，玩几天？')
        result = asyncio.run(plan_trip([{'role': 'user', 'content': '想出去玩'}], {}, {}, lambda *_: None))
        self.assertEqual(result['status'], 'waiting')
        maps.assert_not_called()

    @patch('planner.services.agent.amap_tools', new_callable=AsyncMock)
    @patch('planner.services.agent.model')
    @patch('planner.services.agent.structured', new_callable=AsyncMock)
    def test_agent_can_ask_user_during_planning(self, structured, llm, maps):
        structured.return_value = Analysis(title='杭州', city='杭州', requirements={'天数': 2})
        maps.return_value = [Mock(name='maps_text_search')]
        maps.return_value[0].name = 'unused'
        llm.return_value.bind_tools.return_value.ainvoke = AsyncMock(return_value=AIMessage(content='', tool_calls=[{'name': 'ask_user', 'args': {'question': '想自己选景点吗？'}, 'id': 'ask-1'}]))
        result = asyncio.run(plan_trip([], {}, {}, lambda *_: None))
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(result['message'], '想自己选景点吗？')

    def test_publish_date_never_uses_fetch_time_or_future(self):
        self.assertIsNone(parse_published(None))
        self.assertIsNone(parse_published('10-12'))
        self.assertIsNone(parse_published('2099-01-01'))
        self.assertIsNone(parse_published('编辑于2026-10-12'))
        self.assertIsNotNone(parse_published('2025-03-12'))
        self.assertIsNotNone(parse_published(1750000000000))
