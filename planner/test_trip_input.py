import asyncio
import json
from unittest.mock import AsyncMock, Mock, patch

from django.test import SimpleTestCase, TestCase
from langchain_core.messages import AIMessage

from .models import Conversation, Message, Run
from .schemas import Analysis
from .services.agent import plan_trip
from .services.schedule import Timetable


TRIP = {'start_location': '北京站', 'start_date': '2026-11-01',
        'end_date': '2026-11-03', 'requirements': '早上六点到，参观北京高校'}


class TripInputAPITests(TestCase):
    def setUp(self):
        self.client.get('/')
        self.id = self.client.post('/api/conversations/', '{}', content_type='application/json').json()['id']

    def send(self, **data):
        return self.client.post('/api/chat/', json.dumps({'conversation_id': self.id, **data}),
                                content_type='application/json')

    @patch('planner.views.enqueue')
    def test_trip_saved_and_restored_before_dispatch_and_preserved_on_followup(self, enqueue):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.send(trip=TRIP)
        self.assertEqual(response.status_code, 202)
        enqueue.assert_called_once()
        self.assertEqual(Conversation.objects.get(pk=self.id).trip_input, TRIP)
        self.assertIn('开始地点：北京站', Message.objects.get().content)
        self.assertEqual(self.client.get('/api/state/').json()['conversation']['trip_input'], TRIP)
        Run.objects.update(status='waiting')
        response = self.send(message='主要乘坐地铁')
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['trip_input'], TRIP)

    def test_invalid_trip_never_creates_message_or_run(self):
        for change in ({'start_location': ''}, {'start_date': '2026-02-30'},
                       {'end_date': '2026-10-31'}, {'requirements': 'a' * 6001}):
            with self.subTest(change=change):
                self.assertEqual(self.send(trip={**TRIP, **change}).status_code, 400)
        self.assertEqual(self.send(trip=[]).status_code, 400)
        self.assertFalse(Message.objects.exists())
        self.assertFalse(Run.objects.exists())


class TripTimetableTests(SimpleTestCase):
    def test_dates_are_assigned_and_out_of_range_edits_roll_back(self):
        table = Timetable(trip_input=TRIP)
        entry = {'day': 3, 'activity': {'name': '清华', 'time': '09:00', 'end_time': '10:00'}}
        result = table.edit('add', [entry])
        self.assertTrue(result['ok'])
        self.assertEqual(result['days'][0]['date'], '2026-11-03')
        activity_id = result['days'][0]['activities'][0]['id']
        self.assertFalse(table.edit('update', [{'id': activity_id, 'day': 4}])['ok'])
        self.assertFalse(table.edit('add', [{**entry, 'date': '2026-11-02'}])['ok'])
        self.assertEqual(len(table.read()['days'][0]['activities']), 1)

    def test_changed_trip_dates_flag_existing_draft_until_repaired(self):
        table = Timetable({'days': [{'day': 1, 'title': '北京', 'date': '2026-10-01',
                                    'activities': []}]}, trip_input=TRIP)
        self.assertIn('trip_date', [i['code'] for i in table.read()['issues']])

    def test_tool_observation_drives_next_action_and_form_survives_model_analysis(self):
        from langchain_core.tools import StructuredTool
        remote = StructuredTool(name='maps_text_search', description='查询地点',
                                args_schema={'type': 'object', 'properties': {'keywords': {'type': 'string'}}},
                                coroutine=AsyncMock(return_value='{"pois":[{"id":"hotel-1","name":"站前酒店"}]}'))
        calls = []

        async def decide(messages):
            calls.append(list(messages))
            if len(calls) == 1:
                return AIMessage(content='', tool_calls=[{'name': 'maps_text_search',
                                 'args': {'keywords': '北京站酒店'}, 'id': 'search'}])
            observation = messages[-1]
            self.assertEqual(observation.type, 'tool')
            self.assertIn('hotel-1', observation.content)
            return AIMessage(content='', tool_calls=[{'name': 'ask_user',
                             'args': {'question': '住宿预算是多少？'}, 'id': 'ask'}])

        with patch('planner.services.agent.amap_tools', new=AsyncMock(return_value=[remote])), \
             patch('planner.services.agent.model') as model, \
             patch('planner.services.agent.structured', new=AsyncMock(return_value=Analysis(city='北京'))) as analyze:
            model.return_value.bind_tools.return_value.ainvoke = decide
            result = asyncio.run(plan_trip([], {}, {}, lambda *_: None, trip_input=TRIP))
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(json.loads(analyze.call_args.args[2])['trip_input'], TRIP)
        self.assertEqual(json.loads(calls[0][1].content)['trip_input'], TRIP)
