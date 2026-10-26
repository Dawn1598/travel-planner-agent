import asyncio
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import AsyncMock, Mock, patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from .models import Chunk, Note
from .schemas import NoteQualityBatch
from .services import crawler
from .services.knowledge import judge_notes, save_notes, retrieve_window
from .services.xhs_extract import extract_note, parse_date, parse_published, validate_note
from .services.xhs_pipeline import Budget, Limits, collect_candidates, run_collection

NOW = datetime(2026, 10, 25, 12, 30, tzinfo=dt_timezone(timedelta(hours=8)))


def ident(n):
    return f'{n:024x}'


def card(n, **extra):
    return {'noteId': ident(n), 'title': f'旅行资料 {n}', 'desc': '西湖从断桥步行到白堤需要约二十分钟，建议清晨前往。' * 3,
            'type': 'video', 'time': f'2025-10-{n % 28 + 1:02}', **extra}


def candidate(n, data=None):
    return {'note_id': ident(n), 'url': f'https://www.xiaohongshu.com/explore/{ident(n)}?xsec_token=test-secret',
            'sources': [('search_response', data)] if data else []}


def decision(note_id, accept=True):
    return {'note_id': note_id, 'accept': accept, 'cities': ['杭州'], 'places': ['西湖'],
            'topics': ['步行路线'], 'reason': '文字包含路线与时间'}


def valid_note(n=1):
    note = extract_note(ident(n), candidate(n)['url'], [('detail_response', card(n))], NOW)
    note['quality'] = decision(note['note_id'])
    return note


class FakeBrowser:
    def __init__(self, pages, details=None, clock=None, seconds_per_visit=0):
        self.pages = pages
        self.details = details or {}
        self.position = 0
        self.visited = []
        self.clock, self.seconds_per_visit = clock, seconds_per_visit
        self.report = {}

    def open_search(self, query, budget):
        budget.check()

    def sort_latest(self, budget):
        return {'confirmed': True, 'reason': 'fake_selected_control'}

    def search_snapshot(self, budget):
        return self.pages[min(self.position, len(self.pages) - 1)]

    def scroll(self, budget):
        self.position += 1

    def read_detail(self, row, budget):
        self.visited.append(row['note_id'])
        if self.clock:
            self.clock.value += self.seconds_per_visit
        values = self.details.get(row['note_id'], [card(int(row['note_id'], 16))])
        value = values.pop(0) if len(values) > 1 else values[0]
        if isinstance(value, Exception):
            raise value
        return [('detail_response', value)]


class Clock:
    value = 0

    def __call__(self):
        return self.value


class DateExtractionTests(SimpleTestCase):
    def test_seconds_milliseconds_and_timezone_are_equivalent(self):
        self.assertEqual(parse_published(1750000000, NOW), parse_published(1750000000000, NOW))
        self.assertEqual(parse_published('1750000000000', NOW), parse_published(1750000000, NOW))
        self.assertEqual(parse_published('2025-10-01T12:34:56+08:00', NOW),
                         parse_published('2025-10-01T04:34:56Z', NOW))
        self.assertEqual(parse_published('2025年10月1日 12:34:56', NOW).hour, 12)

    def test_relative_dates_preserve_raw_anchor_and_precision(self):
        for raw, expected in [('3分钟前', NOW - timedelta(minutes=3)), ('2小时前', NOW - timedelta(hours=2)),
                              ('昨天 18:20 上海', (NOW - timedelta(days=1)).replace(hour=18, minute=20)),
                              ('昨天', (NOW - timedelta(days=1)).replace(hour=0, minute=0)), ('刚刚', NOW)]:
            with self.subTest(raw=raw):
                value, proof = parse_date(raw, NOW)
                self.assertEqual(value, expected)
                self.assertEqual(proof['raw'], raw)
                self.assertEqual(proof['observed_at'], NOW.isoformat())
                self.assertIn('relative', proof['basis'])

    def test_no_guessed_year_edit_time_or_future(self):
        for raw in ('10-12', '编辑于2025-10-12', '更新于昨天', None, True, '2025-02-30', '2099-01-01'):
            self.assertIsNone(parse_published(raw, NOW), raw)
        self.assertEqual(parse_date('10-12', NOW)[1]['failure'], 'unknown_year')
        self.assertEqual(parse_date('编辑于2025-10-12', NOW)[1]['failure'], 'edited_time')

    def test_body_success_does_not_skip_nested_state_or_visible_date(self):
        body = card(1, time=None)
        sources = [('detail_response', body), ('initial_state', {'noteDetailMap': {ident(1): {
            'note': {'metadata': {'publication': {'publish_time': '2025-01-02T03:04:05+08:00'}}}}}})]
        note = extract_note(ident(1), candidate(1)['url'], sources, NOW)
        self.assertTrue(note['content'])
        self.assertEqual(note['date_provenance']['source'], 'initial_state')
        self.assertIn('metadata.publication.publish_time', note['date_provenance']['path'])
        sources = [('detail_response', body), ('visible_page', {'noteId': ident(1), 'publishTime': '昨天 10:30'})]
        self.assertEqual(extract_note(ident(1), candidate(1)['url'], sources, NOW)['date_provenance']['source'], 'visible_page')

    def test_response_has_priority_over_state_and_visible_date(self):
        note = extract_note(ident(1), candidate(1)['url'], [
            ('visible_page', card(1, time='2025-10-03')), ('initial_state', card(1, time='2025-10-02')),
            ('detail_response', {'data': {'items': [{'id': ident(1), 'note_card': card(1, time='2025-10-01')}]}})], NOW)
        self.assertEqual(note['published_at'].day, 1)
        self.assertEqual(note['date_provenance']['source'], 'detail_response')

    def test_nested_publication_list_timestamp_is_scoped(self):
        data = card(1, time=None, metadata={'publishInfo': [{'timestamp': 1750000000000}]})
        note = extract_note(ident(1), candidate(1)['url'], [('detail_response', data)], NOW)
        self.assertEqual(note['published_at'], parse_published(1750000000, NOW))
        self.assertIn('publishInfo[0].timestamp', note['date_provenance']['path'])

    def test_other_note_recommendation_comment_and_edit_fields_not_used(self):
        body = card(1, time=None, lastUpdateTime=1750000000000,
                    recommendations=[card(2)], comments=[{'time': 1750000000000}])
        payload = {'data': {'items': [body, card(2)]}}
        note = extract_note(ident(1), candidate(1)['url'], [('detail_response', payload)], NOW)
        self.assertIsNone(note['published_at'])
        wrong = extract_note(ident(3), candidate(3)['url'], [('detail_response', payload)], NOW)
        self.assertEqual(wrong['date_failure'], 'note_id_mismatch')

    def test_explicit_other_id_inside_matching_map_cannot_leak(self):
        payload = {'noteDetailMap': {ident(1): {'note': card(2)}}}
        note = extract_note(ident(1), candidate(1)['url'], [('initial_state', payload)], NOW)
        self.assertIsNone(note['published_at'])
        self.assertFalse(note['content'])

    def test_images_and_image_dependent_text_are_excluded(self):
        for data in (card(1, type='normal'), card(1, type='unknown', imageList=[{}]),
                     card(1, desc='这次详细路线见图，保存截图攻略即可。')):
            note = extract_note(ident(1), candidate(1)['url'], [('detail_response', data)], NOW)
            self.assertIn(note['exclusion'], ('image_note', 'image_dependent'))
        self.assertFalse(valid_note()['exclusion'])

    def test_unknown_type_missing_title_and_fake_proof_fail_gate(self):
        note = valid_note()
        self.assertEqual(validate_note(note), '')
        note['title'] = ''
        self.assertEqual(validate_note(note), 'missing_title')
        note = valid_note()
        note['published_at'] = NOW
        self.assertEqual(validate_note(note), 'invalid_date_provenance')


class PipelineTests(SimpleTestCase):
    def run_pipeline(self, browser, judge=None, save=None, **kwargs):
        self.batches = []

        def quality(notes, remaining):
            self.batches.append([n['note_id'] for n in notes])
            return {n['note_id']: decision(n['note_id']) for n in notes}

        return run_collection(browser, '西湖', existing=kwargs.pop('existing', lambda _: {}),
                              judge=judge or quality, save=save or (lambda notes, *_a, **_kw: {n['note_id']: 'new' for n in notes}),
                              **kwargs)

    def test_scroll_id_dedup_and_candidate_cap(self):
        first = [candidate(n) for n in range(1, 151)]
        second = [candidate(n) for n in range(100, 230)]
        second[0]['url'] += '&source=another'
        browser = FakeBrowser([(first, False), (second, False)])
        report = {}
        rows = collect_candidates(browser, Budget(Limits()), report, lambda *_: None)
        self.assertEqual(len(rows), 200)
        self.assertEqual(len({r['note_id'] for r in rows}), 200)
        self.assertEqual(report['collection_stop'], 'candidate_limit')

    def test_scroll_stops_on_no_more_and_stagnation(self):
        for no_more, reason, position in [(True, 'no_more_results', 0), (False, 'no_new_candidates', 3)]:
            browser = FakeBrowser([([candidate(1)], no_more)])
            report = {}
            collect_candidates(browser, Budget(Limits()), report, lambda *_: None)
            self.assertEqual(report['collection_stop'], reason)
            self.assertEqual(browser.position, position)

    def test_date_failures_images_duplicates_quality_rejects_do_not_fill_new_target(self):
        rows = [candidate(n) for n in range(1, 10)]
        rows[0] = candidate(1, card(1, type='normal'))
        browser = FakeBrowser([(rows, True)], {ident(2): [card(2, time=None)]})
        old = {ident(3): {'verified': True, 'fetched_at': timezone.now()}}

        def quality(notes, _):
            return {n['note_id']: decision(n['note_id'], n['note_id'] != ident(9)) for n in notes}

        def save(notes, *_, **__):
            return {n['note_id']: 'unchanged' if n['note_id'] == ident(8) else 'new' for n in notes}

        result = self.run_pipeline(browser, judge=quality, save=save, existing=lambda _: old,
                                   limits=Limits(target=4))
        self.assertEqual(result['new'], 4)
        self.assertEqual(result['unchanged'], 1)
        self.assertNotIn(ident(1), browser.visited)
        self.assertNotIn(ident(3), browser.visited)
        self.assertEqual(browser.visited.count(ident(2)), 2)
        self.assertEqual(result['items'][ident(2)]['status'], 'failed')
        self.assertEqual(result['items'][ident(9)]['status'], 'quality_rejected')
        self.assertFalse(result['verification_complete'])
        self.assertTrue(result['verification_attempts_complete'])

    def test_real_date_sort_and_cached_body_no_second_navigation(self):
        browser = FakeBrowser([([candidate(3), candidate(1), candidate(2)], True)])
        result = self.run_pipeline(browser)
        self.assertEqual(self.batches, [[ident(3), ident(2), ident(1)]])
        self.assertEqual(len(browser.visited), 3)
        self.assertTrue(result['verification_complete'])

    def test_retries_only_failed_detail_and_recovers_date(self):
        browser = FakeBrowser([([candidate(1), candidate(2)], True)],
                              {ident(1): [card(1, time=None), card(1)], ident(2): [TimeoutError(), card(2)]})
        result = self.run_pipeline(browser)
        self.assertEqual(result['new'], 2)
        self.assertEqual(len(browser.visited), 4)

    def test_visit_budget_reports_partial_verification_and_saves_verified_subset(self):
        browser = FakeBrowser([([candidate(n) for n in range(1, 5)], True)])
        result = self.run_pipeline(browser, limits=Limits(visits=2))
        self.assertEqual(result['visits'], 2)
        self.assertEqual(result['new'], 2)
        self.assertFalse(result['verification_complete'])
        self.assertEqual(result['stop_reason'], 'visit_budget')
        self.assertEqual(result['date_verified'], 2)

    def test_time_budget_stops_work_and_never_claims_full_latest_set(self):
        clock = Clock()
        browser = FakeBrowser([([candidate(n) for n in range(1, 5)], True)], clock=clock, seconds_per_visit=6)
        saver = Mock()
        result = self.run_pipeline(browser, save=saver, limits=Limits(seconds=5), clock=clock)
        self.assertEqual(result['stop_reason'], 'time_budget')
        self.assertEqual(result['new'], 0)
        self.assertFalse(result['verification_complete'])
        self.assertEqual(len(browser.visited), 1)
        saver.assert_not_called()

    def test_quality_budget_expiration_prevents_ingestion(self):
        clock = Clock()
        browser = FakeBrowser([([candidate(1)], True)])
        saver = Mock()

        def slow(notes, remaining):
            clock.value += 11
            return {n['note_id']: decision(n['note_id']) for n in notes}

        report = self.run_pipeline(browser, judge=slow, save=saver, limits=Limits(seconds=10), clock=clock)
        self.assertEqual(report['stop_reason'], 'time_budget')
        saver.assert_not_called()

    def test_target_30_with_batches_of_five(self):
        browser = FakeBrowser([([candidate(n) for n in range(1, 41)], True)])
        report = self.run_pipeline(browser)
        self.assertEqual(report['new'], 30)
        self.assertEqual(len(self.batches), 6)
        self.assertTrue(all(len(batch) == 5 for batch in self.batches))

    def test_quality_failure_skips_batch_and_continues(self):
        browser = FakeBrowser([([candidate(n) for n in range(1, 8)], True)])
        calls = []

        def quality(notes, _):
            calls.append(1)
            if len(calls) == 1:
                raise ValueError('bad JSON')
            return {n['note_id']: decision(n['note_id']) for n in notes}

        result = self.run_pipeline(browser, judge=quality)
        self.assertEqual(result['new'], 2)
        self.assertEqual(result['counts']['failed'], 5)

    def test_login_challenge_stops_without_retry(self):
        browser = FakeBrowser([([candidate(1), candidate(2)], True)], {ident(1): [crawler.LoginRequired('验证')]})
        with self.assertRaises(crawler.LoginRequired):
            self.run_pipeline(browser)
        self.assertEqual(browser.visited, [ident(1)])
        self.assertEqual(browser.report['stop_reason'], 'login_required')

    def test_report_has_no_signed_links_and_keeps_over_100_items(self):
        browser = FakeBrowser([([candidate(n, card(n, type='normal')) for n in range(1, 151)], True)])
        report = self.run_pipeline(browser)
        self.assertEqual(len(report['items']), 150)
        self.assertNotIn('test-secret', str(report))


class IngestionTests(TestCase):
    @patch('planner.services.knowledge.LocalEmbeddings')
    def test_program_and_quality_gates_precede_embedding(self, embeddings):
        for change in ({'published_at': None}, {'quality': {}}, {'quality': decision(ident(2))},
                       {'quality': decision(ident(1), False)}, {'content_type': 'image'}, {'title': ''}):
            result = save_notes([{**valid_note(), **change}])
            self.assertEqual(result[ident(1)], 'rejected')
        embeddings.return_value.embed_documents.assert_not_called()
        self.assertFalse(Note.objects.exists())

    @patch('planner.services.knowledge.LocalEmbeddings')
    def test_metadata_update_without_reembedding_and_counts_only_actual_new(self, embeddings):
        embeddings.return_value.embed_documents.side_effect = lambda texts: [[1., 0.] for _ in texts]
        note = valid_note()
        self.assertEqual(save_notes([note]), {ident(1): 'new'})
        self.assertEqual(save_notes([note]), {ident(1): 'unchanged'})
        corrected = extract_note(ident(1), candidate(1)['url'], [('detail_response', card(1, time='2025-10-05'))], NOW)
        corrected['quality'] = {**decision(ident(1)), 'cities': ['杭州', '绍兴']}
        self.assertEqual(save_notes([corrected]), {ident(1): 'updated'})
        corrected['title'] = '修正后的西湖路线标题'
        self.assertEqual(save_notes([corrected]), {ident(1): 'updated'})
        self.assertEqual(embeddings.return_value.embed_documents.call_count, 1)
        stored = Note.objects.get()
        self.assertEqual(stored.published_at.day, 4)  # UTC storage of Shanghai midnight.
        self.assertEqual(stored.cities, ['杭州', '绍兴'])
        self.assertNotIn('test-secret', stored.url)

    @patch('planner.services.knowledge.LocalEmbeddings')
    def test_query_city_not_substituted_for_empty_extracted_city(self, embeddings):
        embeddings.return_value.embed_documents.side_effect = lambda texts: [[1., 0.] for _ in texts]
        note = valid_note()
        note['city'] = '北京'
        note['quality']['cities'] = []
        save_notes([note])
        self.assertEqual(Note.objects.get().city, '')

    @patch('planner.services.knowledge.LocalEmbeddings')
    def test_refresh_cache_requires_current_vectors_and_verified_metadata(self, embeddings):
        from .services.knowledge import existing_notes
        embeddings.return_value.embed_documents.side_effect = lambda texts: [[1., 0.] for _ in texts]
        save_notes([valid_note()])
        self.assertTrue(existing_notes([ident(1)])[ident(1)]['verified'])
        Chunk.objects.all().delete()
        self.assertFalse(existing_notes([ident(1)])[ident(1)]['verified'])

    @patch('planner.services.knowledge.structured', new_callable=AsyncMock)
    def test_quality_sees_full_text_and_duplicates_or_unknown_ids_are_not_accepted(self, structured):
        note = valid_note()
        note['content'] = '杭州步行路线。' * 1000
        structured.return_value = NoteQualityBatch(notes=[decision(ident(1)), decision(ident(1)), decision(ident(99))])
        self.assertEqual(judge_notes([note]), {})
        self.assertIn(note['content'], structured.call_args.args[2])
        self.assertNotIn('query', structured.call_args.args[2])

    @patch('planner.services.knowledge.structured', new_callable=AsyncMock)
    def test_quality_timeout_is_bounded(self, structured):
        async def slow(*args):
            await asyncio.sleep(1)
        structured.side_effect = slow
        with self.assertRaises(TimeoutError):
            judge_notes([valid_note()], timeout_seconds=.01)

    def test_unverified_legacy_note_is_not_retrieved(self):
        from django.conf import settings
        old = Note.objects.create(note_id=ident(1), title='杭州西湖', content='西湖路线',
                                  url=candidate(1)['url'], content_hash='old', published_at=NOW)
        Chunk.objects.create(note=old, position=0, content=old.content, embedding=[1., 0.], embedding_model=settings.EMBEDDING_MODEL)
        self.assertEqual(retrieve_window('西湖', '', [1., 0.], 0, None), [])


class BrowserAdapterTests(SimpleTestCase):
    def test_body_ready_but_date_late_still_waits_and_reads_all_sources_again(self):
        adapter = crawler.XhsBrowser.__new__(crawler.XhsBrowser)
        adapter.detail_data = []
        adapter.detail = Mock()
        adapter.check_auth = Mock()
        adapter.detail_snapshot = Mock(side_effect=[
            [('detail_response', card(1, time=None))],
            [('detail_response', card(1, time=None)), ('visible_page', {'noteId': ident(1), 'publishTime': '2025-10-01'})],
        ])
        sources = adapter.read_detail(candidate(1), Budget(Limits()))
        self.assertEqual(adapter.detail_snapshot.call_count, 2)
        adapter.detail.wait_for_function.assert_called_once()
        self.assertTrue(extract_note(ident(1), candidate(1)['url'], sources)['published_at'])

    def test_already_complete_response_does_not_wait_for_fixed_delay(self):
        adapter = crawler.XhsBrowser.__new__(crawler.XhsBrowser)
        adapter.detail_data = []
        adapter.detail = Mock()
        adapter.check_auth = Mock()
        adapter.detail_snapshot = Mock(return_value=[('detail_response', card(1))])
        adapter.read_detail(candidate(1), Budget(Limits()))
        adapter.detail.wait_for_function.assert_not_called()

    def test_api_auth_failure_and_visible_challenge_stop(self):
        adapter = crawler.XhsBrowser.__new__(crawler.XhsBrowser)
        adapter.auth_error = False
        response = Mock(url='https://www.xiaohongshu.com/api/sns/web/v1/feed', status=403)
        response.json.return_value = {'msg': '登录失效'}
        adapter.response_data(response)
        page = Mock()
        page.locator.return_value.inner_text.return_value = '笔记正文'
        with self.assertRaises(crawler.LoginRequired):
            adapter.check_auth(page)
        adapter.auth_error = False
        page.locator.return_value.inner_text.return_value = '请完成验证'
        with self.assertRaises(crawler.LoginRequired):
            adapter.check_auth(page)

    @patch('planner.services.crawler.login_status', return_value={'logged_in': True})
    @patch('planner.services.crawler.XhsBrowser')
    @patch('planner.services.crawler.run_collection')
    @patch('playwright.sync_api.sync_playwright')
    def test_browser_context_and_lock_released_even_when_close_raises(self, playwright, run, adapter, login):
        browser = playwright.return_value.__enter__.return_value.chromium.launch.return_value
        context = browser.new_context.return_value
        context.close.side_effect = RuntimeError('close failed')
        run.side_effect = crawler.LoginRequired('需要验证')
        adapter.return_value.report = {'stop_reason': 'login_required'}
        with self.assertRaises(crawler.LoginRequired) as caught:
            crawler.crawl('杭州')
        self.assertEqual(caught.exception.crawl_report['stop_reason'], 'login_required')
        context.close.assert_called_once()
        browser.close.assert_called_once()
        self.assertFalse(crawler.CRAWL_LOCK.locked())
        handler = context.route.call_args.args[1]
        for kind in ('image', 'media'):
            route = Mock()
            route.request.resource_type = kind
            handler(route)
            route.abort.assert_called_once()
            route.continue_.assert_not_called()

    def test_sort_failure_still_uses_verified_date_order(self):
        browser = FakeBrowser([([candidate(1), candidate(2)], True)])
        browser.sort_latest = lambda _: {'confirmed': False, 'reason': 'control_missing'}
        judged = []

        def judge(notes, _):
            judged.extend(n['note_id'] for n in notes)
            return {n['note_id']: decision(n['note_id']) for n in notes}

        report = run_collection(browser, '西湖', existing=lambda _: {}, judge=judge,
                                save=lambda ns, *_, **__: {n['note_id']: 'new' for n in ns})
        self.assertFalse(report['sort_latest']['confirmed'])
        self.assertEqual(judged, [ident(2), ident(1)])

    def test_scrolling_time_budget_preserves_partial_inventory(self):
        clock = Clock()
        browser = FakeBrowser([([candidate(1)], False)])
        browser.scroll = lambda _: setattr(clock, 'value', 11)
        report = run_collection(browser, '西湖', existing=lambda _: {}, judge=Mock(), save=Mock(),
                                limits=Limits(seconds=10), clock=clock)
        self.assertEqual(report['candidate_count'], 1)
        self.assertEqual(report['collection_stop'], 'time_budget')
        self.assertFalse(report['verification_complete'])


class PipelineIntegrationTests(TestCase):
    @patch('planner.services.knowledge.LocalEmbeddings')
    def test_failed_date_does_not_enter_db_and_later_good_notes_fill_target(self, embeddings):
        embeddings.return_value.embed_documents.side_effect = lambda texts: [[1., 0.] for _ in texts]
        browser = FakeBrowser([([candidate(n) for n in range(1, 7)], True)],
                              {ident(1): [card(1, time=None)], ident(2): [card(2, type='normal')]})
        report = run_collection(browser, '西湖', existing=lambda _: {},
                    judge=lambda notes, _: {n['note_id']: decision(n['note_id']) for n in notes},
                    save=save_notes, limits=Limits(target=3))
        self.assertEqual(report['new'], 3)
        self.assertEqual(Note.objects.count(), 3)
        self.assertFalse(Note.objects.filter(note_id__in=[ident(1), ident(2)]).exists())
        self.assertFalse(Note.objects.filter(published_at__isnull=True).exists())

    @patch('planner.services.knowledge.crawler.crawl', side_effect=crawler.LoginRequired('需要登录'))
    def test_login_resume_is_not_suppressed_by_cooldown(self, crawl):
        from .services.knowledge import get_travel_info
        for _ in range(2):
            with self.assertRaises(crawler.LoginRequired):
                get_travel_info('西湖')
        self.assertEqual(crawl.call_count, 2)
