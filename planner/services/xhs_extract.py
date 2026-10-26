"""Pure, ID-scoped extraction. No network, OCR, or inferred publication dates."""
import hashlib
import re
from datetime import datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from django.utils import timezone

SHANGHAI = ZoneInfo('Asia/Shanghai')
ID_PATTERN = re.compile(r'^[a-fA-F0-9]{24}$')
SKIP_BRANCHES = {'user', 'author', 'comments', 'comment', 'recommend', 'recommendations',
                 'related', 'relatednotes', 'interactinfo', 'imagelist', 'images', 'video'}
DATE_KEYS = {'publishtime', 'publishedtime', 'publishedat', 'publishat', 'publicationtime',
             'publishtimestamp', 'datepublished', 'publishdate'}


def key_name(value):
    return re.sub(r'[^a-z]', '', value.lower())


def id_from_url(url):
    try:
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname not in ('www.xiaohongshu.com', 'xiaohongshu.com'):
            return ''
        match = re.fullmatch(r'/(?:explore|search_result)/([a-fA-F0-9]{24})/?', parsed.path)
        return match[1].lower() if match else ''
    except (TypeError, ValueError):
        return ''


def explicit_id(node):
    for key in ('noteId', 'note_id', 'id'):
        value = node.get(key)
        if isinstance(value, str) and ID_PATTERN.fullmatch(value):
            return value.lower()
    return ''


def parse_date(value, now=None):
    now = (now or timezone.now()).astimezone(SHANGHAI)
    proof = {'raw': value, 'observed_at': now.isoformat(), 'basis': '', 'failure': ''}

    def fail(reason):
        proof['failure'] = reason
        return None, proof

    if value is None or value == '':
        return fail('missing_date')
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return fail('invalid_date_type')
    text = str(value).strip()
    if re.search(r'编辑|修改|更新|edited|updated|modified', text, re.I):
        return fail('edited_time')
    try:
        if isinstance(value, (int, float)) or re.fullmatch(r'\d{10,13}(?:\.\d+)?', text):
            stamp = float(value)
            millis = stamp >= 100_000_000_000
            result = datetime.fromtimestamp(stamp / 1000 if millis else stamp, tz=SHANGHAI)
            proof['basis'] = 'unix_milliseconds' if millis else 'unix_seconds'
        else:
            text = re.sub(r'^(?:发布于|发布时间[:：]?|发表于)\s*', '', text).strip()
            # The visible label may include a location suffix, e.g. 昨天 18:30 上海.
            relative = re.fullmatch(r'(\d+)\s*(秒|分钟|小时|天|周)前(?:\s+[^\d]+)?', text)
            calendar = re.fullmatch(r'(今天|昨天|前天)(?:\s+(\d{1,2}):(\d{2}))?(?:\s+[^\d]+)?', text)
            if relative:
                seconds = {'秒': 1, '分钟': 60, '小时': 3600, '天': 86400, '周': 604800}
                result = now - timedelta(seconds=int(relative[1]) * seconds[relative[2]])
                proof['basis'] = f'relative_{relative[2]}_from_observed_at'
            elif text == '刚刚':
                result = now
                proof['basis'] = 'relative_just_now_from_observed_at'
            elif calendar:
                result = (now - timedelta(days={'今天': 0, '昨天': 1, '前天': 2}[calendar[1]])).replace(
                    hour=int(calendar[2] or 0), minute=int(calendar[3] or 0), second=0, microsecond=0)
                proof['basis'] = 'relative_calendar_Asia/Shanghai_' + ('minute' if calendar[2] else 'day_precision')
            else:
                iso = re.sub(r'Z$', '+00:00', text)
                try:
                    result = datetime.fromisoformat(iso)
                    proof['basis'] = 'explicit_timezone' if result.tzinfo else 'Asia/Shanghai_' + ('day_precision' if len(text) == 10 else 'local_time')
                except ValueError:
                    match = re.fullmatch(r'(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})日?(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?(?:\s+[^\d]+)?', text)
                    if not match:
                        return fail('unknown_year' if re.match(r'^\d{1,2}[-月/.]\d{1,2}', text) else 'unrecognized_date')
                    result = datetime(*[int(v or 0) for v in match.groups()])
                    proof['basis'] = 'Asia/Shanghai_' + ('local_time' if match[4] else 'day_precision')
                if result.tzinfo is None:
                    result = result.replace(tzinfo=SHANGHAI)
        if result.year < 2000 or result > now + timedelta(minutes=5):
            return fail('date_out_of_range')
        return result, proof
    except (ValueError, OverflowError, OSError):
        return fail('invalid_date')


def parse_published(value, now=None):
    return parse_date(value, now)[0]


def matching_records(value, target, path='$'):
    """Find actual matching note objects, including noteDetailMap[id].note."""
    if isinstance(value, dict):
        if explicit_id(value) == target:
            yield path, value
        for key, child in value.items():
            if key_name(str(key)) in SKIP_BRANCHES:
                continue
            if key == target and isinstance(child, dict) and explicit_id(child) in ('', target):
                yield f'{path}.{key}', child
            yield from matching_records(child, target, f'{path}.{key}')
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from matching_records(child, target, f'{path}[{index}]')


def scoped_fields(node, target, path):
    if not isinstance(node, dict) or explicit_id(node) not in ('', target):
        return
    for key, value in node.items():
        name = key_name(key)
        if name in SKIP_BRANCHES:
            continue
        field_path = f'{path}.{key}'
        if any(word in name for word in ('update', 'edit', 'modify')):
            if isinstance(value, (str, int, float)):
                yield 'ignored_edit_time', value, field_path
            continue
        if isinstance(value, dict):
            yield from scoped_fields(value, target, field_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from scoped_fields(child, target, f'{field_path}[{index}]')
        else:
            # Generic 'time' is meaningful only on the actual note object or publication metadata.
            if name == 'time' and not (any(k in node for k in ('desc', 'title', 'noteId', 'note_id')) or 'publish' in path.lower()):
                continue
            yield name, value, field_path


def content_type(records):
    types, has_images = [], False

    def visit(node, target):
        nonlocal has_images
        if not isinstance(node, dict) or explicit_id(node) not in ('', target):
            return
        for key, value in node.items():
            name = key_name(key)
            if name in ('type', 'notetype', 'contenttype') and isinstance(value, str):
                types.append(value.lower())
            if name in ('imagelist', 'images') and value:
                has_images = True
            if name not in SKIP_BRANCHES and isinstance(value, dict):
                visit(value, target)

    for _, node, target in records:
        visit(node, target)
    if any(t in ('normal', 'image', 'images', 'image_text', '图文') for t in types):
        return 'image', 'image_note'
    if 'video' in types:
        return 'video', ''
    if has_images:
        return 'image', 'image_dependent'
    if any(t in ('text', '文字', '文本') for t in types):
        return 'text', ''
    return 'unknown', 'content_type_unknown'


def extract_note(note_id, url, sources, now=None):
    """sources ordered detail_response -> initial_state -> visible_page.

    Visible snapshots must carry a verified DOM note_id, never just the requested URL.
    """
    now = now or timezone.now()
    note = {'note_id': note_id, 'url': url, 'title': '', 'content': '', 'published_at': None,
            'date_provenance': {}, 'content_type': 'unknown'}
    date_attempts, records, inspected = [], [], []
    priority = {'detail_response': 0, 'initial_state': 1, 'visible_page': 2, 'search_response': 3}
    for source, payload in sorted(sources, key=lambda pair: priority.get(pair[0], 99)):
        matched = list(matching_records(payload, note_id))
        for path, node in matched:
            records.append((path, node, note_id))
            for name, value, field_path in scoped_fields(node, note_id, path):
                inspected.append(f'{source}:{field_path}')
                if name == 'ignored_edit_time':
                    date_attempts.append({'raw': value, 'observed_at': now.isoformat(), 'basis': '',
                                          'failure': 'edited_time', 'source': source, 'path': field_path, 'note_id': note_id})
                    continue
                if isinstance(value, str):
                    if name in ('title', 'displaytitle') and not note['title']:
                        note['title'] = value.strip()
                    if name in ('desc', 'description', 'content') and len(value.strip()) > len(note['content']):
                        note['content'] = value.strip()
                if name in DATE_KEYS or name == 'time' or (name == 'timestamp' and 'publish' in field_path.lower()):
                    published, proof = parse_date(value, now)
                    proof.update(source=source, path=field_path, note_id=note_id)
                    date_attempts.append(proof)
                    if published and note['published_at'] is None:
                        note['published_at'], note['date_provenance'] = published, proof
    note['content_type'], exclusion = content_type(records)
    if re.search(r'攻略.{0,6}(?:见图|在图|看图)|(?:路线|行程|详情).{0,5}(?:见图|在图|看图)|长图攻略|截图攻略|看图\d|图\d看', note['content']):
        exclusion = 'image_dependent'
    note['exclusion'] = exclusion
    note['date_attempts'] = date_attempts
    note['inspected_fields'] = inspected[:100]
    note['date_failure'] = '' if note['published_at'] else (date_attempts[-1]['failure'] if date_attempts else ('missing_date' if records else 'note_id_mismatch'))
    note['content_hash'] = hashlib.sha256((note['title'] + '\n' + note['content']).encode()).hexdigest()
    return note


def validate_note(note):
    for field in ('note_id', 'title', 'content', 'url'):
        if not isinstance(note.get(field), str) or not note[field].strip():
            return f'missing_{field}'
    if id_from_url(note['url']) != note['note_id']:
        return 'note_id_mismatch'
    if len(note['title']) > 300 or len(note['url']) > 2000:
        return 'field_too_long'
    published = note.get('published_at')
    proof = note.get('date_provenance', {})
    if not isinstance(published, datetime) or timezone.is_naive(published):
        return 'missing_verified_date'
    if proof.get('note_id') != note['note_id'] or proof.get('source') not in ('detail_response', 'initial_state', 'visible_page', 'search_response'):
        return 'missing_date_provenance'
    try:
        parsed, _ = parse_date(proof['raw'], datetime.fromisoformat(proof['observed_at']))
    except (KeyError, TypeError, ValueError):
        return 'invalid_date_provenance'
    if parsed != published or published > timezone.now() + timedelta(minutes=5):
        return 'invalid_date_provenance'
    if note.get('exclusion') or note.get('content_type') not in ('text', 'video'):
        return note.get('exclusion') or 'content_type_unknown'
    return ''
