import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, Mock, patch

from django.test import SimpleTestCase, TransactionTestCase
from langchain_core.messages import AIMessage

from .models import Conversation, Run
from .schemas import Analysis, Review
from .services.agent import plan_trip
from .services.schedule import Timetable


def entry(name, start, end, day=1, **fields):
    return {'day': day, 'activity': {'name': name, 'time': start, 'end_time': end, **fields}}


def call(name, args, number):
    return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': f'call-{number}'}])


DETAILS = {'title': '杭州', 'city': '杭州', 'summary': '西湖游览'}


class TimetableTests(SimpleTestCase):
    def test_sorts_time_and_allows_touching_boundaries(self):
        table = Timetable()
        result = table.edit('add', [entry('白堤', '10:00', '11:00'), entry('断桥', '09:00', '10:00')])
        self.assertTrue(result['conflict_free'])
        self.assertEqual([a['name'] for a in result['days'][0]['activities']], ['断桥', '白堤'])

    def test_overlap_has_ids_and_minutes_and_survives_reload(self):
        table = Timetable()
        result = table.edit('add', [entry('断桥', '09:00', '10:00'), entry('白堤', '09:45', '11:00')])
        self.assertFalse(result['conflict_free'])
        issue = result['issues'][0]
        self.assertEqual(issue['code'], 'overlap')
        self.assertEqual(len(set(issue['ids'])), 2)
        self.assertIn('15 分钟', issue['message'])
        self.assertEqual(Timetable(table.data).read(), result)

    def test_reports_nested_overlaps_not_just_previous_end(self):
        table = Timetable()
        result = table.edit('add', [entry('长活动', '09:00', '12:00'), entry('短活动', '09:30', '10:00'), entry('另一个', '10:30', '11:00')])
        self.assertEqual(len([i for i in result['issues'] if i['code'] == 'overlap']), 2)

    def test_update_delete_move_and_partial_patch(self):
        table = Timetable()
        result = table.edit('add', [entry('断桥', '09:00', '10:00'), entry('白堤', '09:45', '11:00')])
        first, second = result['days'][0]['activities']
        result = table.edit('update', [{'id': second['id'], 'activity': {'time': '10:00'}}])
        self.assertTrue(result['conflict_free'])
        result = table.edit('update', [{'id': second['id'], 'day': 2, 'date': '2026-10-17'}])
        self.assertEqual(result['days'][1]['activities'][0]['name'], '白堤')
        result = table.edit('delete', [{'id': first['id']}])
        self.assertEqual([d['day'] for d in result['days']], [2])
        self.assertTrue(result['conflict_free'])

    def test_invalid_batch_rolls_back_all_edits(self):
        table = Timetable()
        table.edit('add', [entry('断桥', '09:00', '10:00')])
        before = deepcopy(table.data)
        result = table.edit('add', [entry('有效', '11:00', '12:00'), entry('无效', '24:30', '25:00')])
        self.assertFalse(result['ok'])
        self.assertEqual(table.data, before)
        result = table.edit('delete', [{'id': 'another-conversation-id'}])
        self.assertFalse(result['ok'])
        self.assertEqual(table.data, before)

    def test_traffic_gap_and_explicit_transport_interval(self):
        table = Timetable()
        result = table.edit('add', [entry('断桥', '09:00', '10:00'), entry('白堤', '10:10', '11:00', travel_minutes=20)])
        self.assertEqual(result['issues'][0]['code'], 'travel_gap')
        self.assertIn('只有 10 分钟', result['issues'][0]['message'])
        destination = result['days'][0]['activities'][1]['id']
        table.edit('update', [{'id': destination, 'activity': {'time': '10:20', 'travel_minutes': 0}}])
        result = table.edit('add', [entry('步行', '10:00', '10:20', kind='交通', travel_minutes=20)])
        self.assertTrue(result['conflict_free'])
        travel = result['days'][0]['activities'][1]['id']
        result = table.edit('update', [{'id': travel, 'activity': {'end_time': '10:10'}}])
        self.assertEqual(result['issues'][0]['code'], 'transport_duration')

    def test_midnight_is_end_only_and_cross_day_is_split(self):
        table = Timetable()
        result = table.edit('add', [entry('夜游', '23:00', '24:00'), entry('休息', '00:00', '06:00', day=2)])
        self.assertTrue(result['conflict_free'])
        for start, end in [('24:00', '24:00'), ('23:00', '01:00'), ('9:00', '10:00')]:
            self.assertFalse(table.edit('add', [entry('错误', start, end)])['ok'])

    def test_dates_match_trip_days_and_invalid_date_is_rejected(self):
        table = Timetable()
        first, second = entry('断桥', '09:00', '10:00'), entry('灵隐', '09:00', '10:00', day=2)
        first['date'], second['date'] = '2026-10-16', '2026-10-16'
        result = table.edit('add', [first, second])
        self.assertIn('duplicate_date', [i['code'] for i in result['issues']])
        second_id = result['days'][1]['activities'][0]['id']
        self.assertTrue(table.edit('update', [{'id': second_id, 'date': '2026-10-17'}])['conflict_free'])
        self.assertFalse(table.edit('update', [{'id': second_id, 'date': '2026-02-30'}])['ok'])


class ScheduleAgentTests(SimpleTestCase):
    def run_agent(self, answers, **kwargs):
        remote = Mock(name='unused', args_schema={})
        remote.name = 'unused'
        with patch('planner.services.agent.amap_tools', new=AsyncMock(return_value=[remote])), \
             patch('planner.services.agent.model') as model, \
             patch('planner.services.agent.structured', new=AsyncMock(side_effect=[Analysis(city='杭州'), Review(reasonable=True)])) as review:
            model.return_value.bind_tools.return_value.ainvoke = AsyncMock(side_effect=answers)
            result = asyncio.run(plan_trip([], {}, {}, lambda *_: None, **kwargs))
            return result, review

    def test_final_plan_uses_table_and_ignores_model_supplied_days(self):
        result, review = self.run_agent([
            call('manage_schedule', {'action': 'add', 'entries': [entry('断桥', '09:00', '10:00')]}, 1),
            call('submit_plan', {'plan': {**DETAILS, 'days': [{'day': 9, 'activities': []}]}, 'message': '已完成'}, 2),
        ])
        self.assertEqual(result['status'], 'done')
        self.assertEqual(result['plan']['days'][0]['activities'][0]['name'], '断桥')
        self.assertEqual(result['plan']['days'][0]['day'], 1)
        self.assertEqual(review.await_count, 2)

    def test_conflict_blocks_submit_before_model_review(self):
        result, review = self.run_agent([
            call('manage_schedule', {'action': 'add', 'entries': [entry('断桥', '09:00', '10:00'), entry('白堤', '09:30', '11:00')]}, 1),
            call('submit_plan', {'plan': DETAILS, 'message': '完成'}, 2),
            call('ask_user', {'question': '要减少一个景点吗？'}, 3),
        ])
        self.assertEqual(result['status'], 'waiting')
        self.assertNotIn('plan', result)
        self.assertEqual(review.await_count, 1)  # Analysis only, no Review call.

    def test_cannot_submit_empty_table(self):
        result, review = self.run_agent([
            call('submit_plan', {'plan': DETAILS, 'message': '完成'}, 1),
            call('ask_user', {'question': '想去哪个景点？'}, 2),
        ])
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(review.await_count, 1)


class SchedulePersistenceTests(TransactionTestCase):
    def test_real_job_saves_draft_before_wait_and_next_run_can_read_it(self):
        from .jobs import execute
        conversation = Conversation.objects.create(owner='schedule-owner')
        other = Conversation.objects.create(owner='another-owner')
        run = Run.objects.create(conversation=conversation)
        remote = Mock(name='unused', args_schema={})
        remote.name = 'unused'
        with patch('planner.services.agent.amap_tools', new=AsyncMock(return_value=[remote])), \
             patch('planner.services.agent.model') as model, \
             patch('planner.services.agent.structured', new=AsyncMock(return_value=Analysis(city='杭州'))):
            model.return_value.bind_tools.return_value.ainvoke = AsyncMock(side_effect=[
                call('manage_schedule', {'action': 'add', 'entries': [entry('断桥', '09:00', '10:00')]}, 1),
                call('ask_user', {'question': '午餐吃什么？'}, 2),
            ])
            execute(run.pk)
            conversation.refresh_from_db()
            run.refresh_from_db()
            other.refresh_from_db()
            self.assertEqual(run.status, 'waiting')
            self.assertEqual(other.schedule, {})
            activity = conversation.schedule['days'][0]['activities'][0]
            self.assertEqual(activity['name'], '断桥')
            self.assertEqual(conversation.plan, {})
            next_run = Run.objects.create(conversation=conversation)
            model.return_value.bind_tools.return_value.ainvoke = AsyncMock(side_effect=[
                call('manage_schedule', {'action': 'update', 'entries': [{'id': activity['id'], 'activity': {'end_time': '10:30'}}]}, 3),
                call('ask_user', {'question': '继续吗？'}, 4),
            ])
            execute(next_run.pk)
            conversation.refresh_from_db()
            self.assertEqual(conversation.schedule['days'][0]['activities'][0]['end_time'], '10:30')
