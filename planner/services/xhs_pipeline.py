"""Bounded collection orchestration, independent of Playwright and the model."""
import time
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .debug import record, sanitize
from .xhs_extract import extract_note, id_from_url, validate_note


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Limits:
    candidates: int = 200
    target: int = 30
    visits: int = 240
    seconds: float = 600
    retries: int = 1
    stagnant_rounds: int = 3
    refresh_hours: int = 24

    @classmethod
    def configured(cls):
        return cls(candidates=max(1, min(200, settings.XHS_CANDIDATE_LIMIT)),
                   target=max(1, settings.XHS_TARGET_NEW), visits=max(1, settings.XHS_MAX_VISITS),
                   seconds=max(1, settings.XHS_TIME_BUDGET_SECONDS), retries=max(0, settings.XHS_RETRIES),
                   stagnant_rounds=max(1, settings.XHS_STAGNANT_ROUNDS), refresh_hours=max(0, settings.XHS_REFRESH_HOURS))


class Budget:
    def __init__(self, limits, clock=time.monotonic):
        self.limits, self.clock = limits, clock
        self.started, self.visits = clock(), 0

    @property
    def remaining(self):
        return max(0, self.limits.seconds - (self.clock() - self.started))

    def check(self):
        if self.remaining <= 0:
            raise BudgetExceeded('time_budget')

    def visit(self):
        self.check()
        if self.visits >= self.limits.visits:
            raise BudgetExceeded('visit_budget')
        self.visits += 1

    def timeout(self, maximum=12000):
        self.check()
        return max(1, min(maximum, int(self.remaining * 1000)))


def collect_candidates(browser, budget, report, emit):
    candidates, stagnant = {}, 0
    report['sort_latest'] = browser.sort_latest(budget)
    record('crawl.sort', **report['sort_latest'])
    while True:
        budget.check()
        rows, no_more = browser.search_snapshot(budget)
        before = len(candidates)
        for row in rows:
            note_id = id_from_url(row.get('url', ''))
            if not note_id:
                continue
            if note_id in candidates:
                # New response metadata can enrich the same ID; signed URL variants are not new notes.
                previous = candidates[note_id]
                previous['sources'].extend(row.get('sources', []))
                continue
            candidates[note_id] = {**row, 'note_id': note_id, 'sources': list(row.get('sources', []))}
            if len(candidates) >= budget.limits.candidates:
                break
        stagnant = stagnant + 1 if len(candidates) == before else 0
        emit('crawl', f'已收集 {len(candidates)} / {budget.limits.candidates} 个唯一候选')
        record('crawl.scroll', count=len(candidates), new=len(candidates) - before, stagnant=stagnant)
        reason = ('candidate_limit' if len(candidates) >= budget.limits.candidates else
                  'no_more_results' if no_more else 'no_new_candidates' if stagnant >= budget.limits.stagnant_rounds else '')
        # Preserve partial candidates even when the next scroll exhausts the budget.
        report['_candidates'] = list(candidates.values())
        if reason:
            report['collection_stop'] = reason
            return list(candidates.values())
        browser.scroll(budget)


def run_collection(browser, query, *, existing, judge, save, emit=lambda *_: None, limits=None, clock=time.monotonic, budget=None):
    """Callbacks make all limits, failures and count semantics testable offline.

    save returns actual DB dispositions keyed by ID; only 'new' meets the target.
    """
    limits = limits or Limits.configured()
    budget = budget or Budget(limits, clock)
    report = {'target': limits.target, 'candidate_count': 0, 'date_verified': 0,
              'verification_attempted': 0, 'verification_complete': False, 'new': 0,
              'updated': 0, 'unchanged': 0, 'items': {}, 'stop_reason': '', 'collection_stop': ''}
    audit = report['items']

    def outcome(note_id, status, **details):
        audit.setdefault(note_id, {}).update(status=status, **details)
        record('crawl.candidate', note_id=note_id, **audit[note_id])

    def read(candidate):
        note_id = candidate['note_id']
        sources = list(candidate.get('sources', []))
        note = extract_note(note_id, candidate['url'], sources)
        last_failure = ''
        for attempt in range(limits.retries + 1):
            budget.visit()
            emit('crawl', f'核实笔记 {note_id}（第 {attempt + 1} 次读取）')
            try:
                sources.extend(browser.read_detail(candidate, budget))
                note = extract_note(note_id, candidate['url'], sources)
                record('crawl.date', note_id=note_id, attempts=note['date_attempts'], failure=note['date_failure'])
                if note['exclusion'] not in ('', 'content_type_unknown'):
                    outcome(note_id, 'excluded', reason=note['exclusion'])
                    return None
                failure = validate_note(note)
                if not failure:
                    candidate['full_note'] = note
                    return note
                last_failure = note['date_failure'] or failure
                outcome(note_id, 'retry', reason=last_failure, attempt=attempt + 1,
                        date_attempts=note['date_attempts'], inspected_fields=note['inspected_fields'])
            except BudgetExceeded:
                raise
            except Exception as exc:
                from .crawler import LoginRequired
                if isinstance(exc, LoginRequired):
                    raise
                # Exception text may contain authenticated URLs; record only category/type here.
                last_failure = 'detail_read_failed'
                outcome(note_id, 'retry', reason=last_failure, error_type=type(exc).__name__, attempt=attempt + 1)
        outcome(note_id, 'failed', reason=last_failure,
                date_attempts=note['date_attempts'], inspected_fields=note['inspected_fields'])
        return None

    candidates, verified = [], []
    try:
        browser.open_search(query, budget)
        try:
            candidates = collect_candidates(browser, budget, report, emit)
        except BudgetExceeded as exc:
            candidates = report.get('_candidates', [])
            report['collection_stop'] = str(exc)
        report['candidate_count'] = len(candidates)
        old_notes = existing([c['note_id'] for c in candidates])
        remaining = []
        for candidate in candidates:
            note_id = candidate['note_id']
            note = extract_note(note_id, candidate['url'], candidate.get('sources', []))
            audit[note_id] = {'title': note['title'] or candidate.get('title', ''),
                             'summary': candidate.get('summary', ''), 'content_type': note['content_type'],
                             'url': f'https://www.xiaohongshu.com/explore/{note_id}',
                             'published_at': note['published_at'].isoformat() if note['published_at'] else None}
            if note['exclusion'] not in ('', 'content_type_unknown'):
                outcome(note_id, 'excluded', reason=note['exclusion'])
                continue
            old = old_notes.get(note_id)
            if old and old.get('verified') and old['fetched_at'] > timezone.now() - timedelta(hours=limits.refresh_hours):
                outcome(note_id, 'refresh_skipped', reason='within_refresh_period')
                continue
            remaining.append(candidate)
        report['verification_eligible'] = len(remaining)
        # Reserve up to 60 s for quality judgment/indexing, rather than spend the entire run on metadata.
        verification_deadline = budget.clock() + max(0, budget.remaining - min(60, limits.seconds * .2))
        for index, candidate in enumerate(remaining):
            if budget.clock() >= verification_deadline:
                report['verification_stop'] = 'time_reserved_for_ingestion'
                break
            note_id = candidate['note_id']
            report['verification_attempted'] += 1
            try:
                # Detail verification establishes both the final date and the complete text.
                # Keep the result for the sorted ingestion phase; never reopen an already read note.
                note = read(candidate)
                if note and note['published_at']:
                    verified.append((note, candidate))
                    report['date_verified'] = len(verified)
                    outcome(note_id, 'date_verified', published_at=note['published_at'].isoformat(), date_provenance=note['date_provenance'])
            except BudgetExceeded as exc:
                report['verification_stop'] = str(exc)
                break
            emit('crawl', f'日期已核实 {len(verified)} 篇，已检查 {index + 1} / {len(remaining)} 个待核实候选')
        report['verification_attempts_complete'] = report['verification_attempted'] == len(remaining) and not report.get('verification_stop')
        report['verification_complete'] = bool(report['verification_attempts_complete'] and
            all(audit[c['note_id']].get('status') in ('date_verified', 'excluded') for c in remaining))
        report['date_verified'] = len(verified)
        for candidate in remaining:
            if candidate['note_id'] not in audit or not audit[candidate['note_id']].get('status'):
                outcome(candidate['note_id'], 'unverified', reason=report.get('verification_stop', 'not_processed'))
        verified.sort(key=lambda pair: (pair[0]['published_at'], pair[0]['note_id']), reverse=True)
        record('crawl.sorted', note_ids=[n['note_id'] for n, _ in verified],
               verification_complete=report['verification_complete'], verified=len(verified), total=len(candidates))
        pending = []

        def flush():
            if not pending:
                return
            budget.check()
            emit('quality', f'判断 {len(pending)} 篇文字资料的旅行价值')
            try:
                decisions = judge(pending, budget.remaining)
            except Exception as exc:
                for note in pending:
                    outcome(note['note_id'], 'failed', reason='quality_failed', error_type=type(exc).__name__)
                pending.clear()
                return
            accepted = []
            for note in pending:
                decision = decisions.get(note['note_id'])
                if not decision or not decision.get('accept'):
                    outcome(note['note_id'], 'quality_rejected', reason=(decision or {}).get('reason', 'missing_quality_result'))
                else:
                    accepted.append({**note, 'quality': decision})
            if accepted:
                budget.check()
                results = save(accepted, emit, budget=budget)
                for note in accepted:
                    status = results.get(note['note_id'], 'failed')
                    if status in ('new', 'updated', 'unchanged'):
                        report[status] += 1
                    outcome(note['note_id'], status, reason=note['quality']['reason'])
            pending.clear()
            emit('crawl', f'已新增 {report["new"]} / {limits.target} 篇合格笔记，更新 {report["updated"]} 篇')

        for note, candidate in verified:
            budget.check()
            if report['new'] >= limits.target:
                break
            if not candidate.get('full_note'):
                try:
                    note = read(candidate)
                except BudgetExceeded as exc:
                    if str(exc) == 'time_budget':
                        raise
                    outcome(candidate['note_id'], 'failed', reason='visit_budget')
                    continue
            if note:
                pending.append(note)
            if len(pending) >= min(5, limits.target - report['new']):
                flush()
        flush()
        report['stop_reason'] = ('target_reached' if report['new'] >= limits.target else
                                 report.get('verification_stop') or 'candidates_exhausted')
    except BudgetExceeded as exc:
        report['stop_reason'] = str(exc)
    except Exception as exc:
        from .crawler import LoginRequired
        report['stop_reason'] = 'login_required' if isinstance(exc, LoginRequired) else 'collection_failed'
        raise
    finally:
        if not report['candidate_count'] and report.get('_candidates'):
            partial = report['_candidates']
            report['candidate_count'] = len(partial)
            for candidate in partial:
                audit.setdefault(candidate['note_id'], {'status': 'unverified', 'reason': report['stop_reason'],
                    'url': f'https://www.xiaohongshu.com/explore/{candidate["note_id"]}'})
        report.pop('_candidates', None)
        report['visits'] = budget.visits
        report['elapsed_seconds'] = round(budget.clock() - budget.started, 2)
        report['counts'] = dict(Counter(item.get('status', 'unverified') for item in audit.values()))
        report['shortfall'] = max(0, limits.target - report['new'])
        labels = {'target_reached': '已达到新增目标', 'candidates_exhausted': '可用候选已处理完',
                  'time_budget': '达到时间预算', 'visit_budget': '达到详情访问上限',
                  'time_reserved_for_ingestion': '日期核实阶段停止，已预留入库时间',
                  'login_required': '需要登录或完成验证', 'collection_failed': '采集异常'}
        report['stop_message'] = labels.get(report['stop_reason'], report['stop_reason'])
        # Sanitize each item separately: generic debug truncation must not lose candidates 101–200.
        report['items'] = {note_id: sanitize(item) for note_id, item in audit.items()}
        record('crawl.finish', report=report)
        browser.report = report
        emit('crawl', f'采集结束：新增 {report["new"]} / {limits.target} 篇；日期核实 {report["date_verified"]} / {report["candidate_count"]} 个候选；{report["stop_message"]}')
    return report
