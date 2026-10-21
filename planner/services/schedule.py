"""Program-owned timetable. The model edits data; Python decides time conflicts."""
import re
import uuid
from copy import deepcopy
from datetime import date, timedelta

from planner.schemas import Activity, ScheduleEntry


def minute(value, *, end=False):
    if end and value == '24:00':
        return 1440
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        return None
    hour, minutes = map(int, value.split(':'))
    return hour * 60 + minutes


def check_days(days):
    issues = []

    def issue(code, day, activities, message):
        issues.append({'code': code, 'day': day, 'ids': [a.get('id', '') for a in activities], 'message': message})

    if not days:
        issue('empty', None, [], '时间表没有活动')
    seen_days, seen_dates = set(), set()
    dated = []
    for day in days:
        number = day['day']
        if number in seen_days:
            issue('duplicate_day', number, [], f'第 {number} 天重复')
        seen_days.add(number)
        if day.get('date'):
            try:
                value = date.fromisoformat(day['date'])
                if value in seen_dates:
                    issue('duplicate_date', number, [], f'第 {number} 天的日期重复')
                seen_dates.add(value)
                dated.append((number, value))
            except ValueError:
                issue('date', number, [], f'第 {number} 天日期格式应为 YYYY-MM-DD')
        activities = day.get('activities', [])
        if not activities:
            issue('empty_day', number, [], f'第 {number} 天没有活动')
        valid = []
        for activity in activities:
            start = minute(activity['time'])
            end = minute(activity['end_time'], end=True)
            if start is None or end is None or end <= start:
                issue('time', number, [activity], f"{activity['name']} 的时间格式或起止顺序不合理；跨日活动请分段记录")
                continue
            valid.append((start, end, activity))
            if activity['kind'] == '交通' and end - start < activity['travel_minutes']:
                issue('transport_duration', number, [activity], f"{activity['name']} 的交通时段短于交通耗时 {activity['travel_minutes']} 分钟")
        valid.sort(key=lambda row: (row[0], row[1]))
        for i, (start, end, activity) in enumerate(valid):
            for prior_start, prior_end, prior in valid[:i]:
                if start < prior_end:
                    issue('overlap', number, [prior, activity], f"第 {number} 天 {prior['name']} 与 {activity['name']} 时间重叠 {min(end, prior_end) - start} 分钟")
            if i:
                _, previous_end, previous = valid[i - 1]
                # A transport row occupies its own interval. Other rows reserve
                # travel_minutes between the previous row and their start.
                required_gap = 0 if activity['kind'] == '交通' else activity['travel_minutes']
                gap = start - previous_end
                if gap >= 0 and gap < required_gap:
                    issue('travel_gap', number, [previous, activity], f"{activity['name']} 与上一项活动之间没有留够交通时间：需要 {required_gap} 分钟，只有 {gap} 分钟")
    # Dates must describe the same trip, including intentionally empty days.
    dated.sort()
    if dated:
        first_number, first_date = dated[0]
        for number, value in dated[1:]:
            if (value - first_date).days != number - first_number:
                issue('date_order', number, [], f'第 {number} 天的日期与行程天数不一致')
    return issues


class Timetable:
    def __init__(self, data=None, trip_input=None):
        self.trip_input = trip_input or {}
        self.data = deepcopy(data or {'days': []})
        for day in self.data['days']:
            for activity in day['activities']:
                activity.setdefault('id', uuid.uuid4().hex)
        self._sort(self.data)

    @staticmethod
    def _sort(data):
        data['days'].sort(key=lambda day: day['day'])
        for day in data['days']:
            day['activities'].sort(key=lambda a: (a['time'], a['end_time'], a['id']))

    def read(self):
        issues = check_days(self.data['days'])
        if self.trip_input:
            for day in self.data['days']:
                try:
                    expected = self.trip_date(day['day'])
                    if day.get('date') != expected:
                        raise ValueError(f"第 {day['day']} 天日期应为 {expected}")
                except ValueError as exc:
                    issues.append({'code': 'trip_date', 'day': day['day'], 'ids': [], 'message': str(exc)})
        return {'ok': True, 'conflict_free': not issues, 'issues': issues, **deepcopy(self.data)}

    def trip_date(self, number):
        start = date.fromisoformat(self.trip_input['start_date'])
        end = date.fromisoformat(self.trip_input['end_date'])
        if not 1 <= number <= (end - start).days + 1:
            raise ValueError(f'第 {number} 天超出所选旅行日期')
        return (start + timedelta(days=number - 1)).isoformat()

    def edit(self, action, entries):
        if action not in ('add', 'update', 'delete'):
            return {'ok': False, 'error': '未知时间表操作'}
        candidate = deepcopy(self.data)
        try:
            if not entries:
                raise ValueError('请提供 entries')
            for raw in entries:
                entry = raw if isinstance(raw, ScheduleEntry) else ScheduleEntry.model_validate(raw)
                source = activity = None
                if action != 'add':
                    for day in candidate['days']:
                        for row in day['activities']:
                            if row['id'] == entry.id:
                                source, activity = day, row
                    if activity is None:
                        raise ValueError(f'活动 ID 不存在：{entry.id}')
                    source['activities'].remove(activity)
                if action == 'delete':
                    continue
                number = entry.day if entry.day is not None else (source['day'] if source else None)
                if number is None:
                    raise ValueError('新增活动需要 day')
                expected_date = self.trip_date(number) if self.trip_input else None
                if expected_date and entry.date not in (None, '', expected_date):
                    raise ValueError(f'第 {number} 天日期应为 {expected_date}')
                fields = {**(activity or {}), **entry.activity}
                # UUID ownership belongs to the program, never activity payloads.
                row = Activity.model_validate(fields).model_dump()
                if minute(row['time']) is None or minute(row['end_time'], end=True) is None or minute(row['time']) >= minute(row['end_time'], end=True):
                    raise ValueError(f"{row['name']} 的时间格式或起止顺序不合理；跨日活动请分段记录")
                row['id'] = activity['id'] if activity else uuid.uuid4().hex
                target = next((d for d in candidate['days'] if d['day'] == number), None)
                if target is None:
                    target = {'day': number, 'title': f'第 {number} 天', 'date': '', 'activities': []}
                    candidate['days'].append(target)
                for key in ('title', 'date'):
                    value = getattr(entry, key)
                    if value is not None:
                        if key == 'date' and value and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                            raise ValueError('日期格式应为 YYYY-MM-DD')
                        if key == 'date' and value:
                            date.fromisoformat(value)
                        target[key] = value
                if expected_date:
                    target['date'] = expected_date
                target['activities'].append(row)
            candidate['days'] = [d for d in candidate['days'] if d['activities']]
            self._sort(candidate)
        except (ValueError, TypeError) as exc:
            return {**self.read(), 'ok': False, 'error': str(exc)}
        self.data = candidate
        return self.read()
