import json
import os
import subprocess
import sys
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.http import FileResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .jobs import enqueue
from .forms import TripInputForm
from .models import Conversation, Message, Run
from .services.crawler import LOGIN_FILE, login_status
from .services.debug import RunTrace, log_path, read_entries


def owner(request):
    if not request.session.session_key:
        request.session.create()
    return request.session.session_key


def payload(request):
    try:
        data = json.loads(request.body)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (ValueError, UnicodeDecodeError):
        return None


def serialize(conversation):
    active = conversation.runs.filter(status='running').first()
    if active and active.created_at < timezone.now() - timedelta(minutes=20):
        active.status = 'error'
        active.error = '任务已中断，请重新发送消息。'
        active.save(update_fields=['status', 'error'])
        active = None
    return {'id': str(conversation.pk), 'title': conversation.title,
            'messages': list(conversation.messages.values('id', 'role', 'content')),
            'plan': conversation.plan, 'trip_input': conversation.trip_input,
            'active_run': str(active.pk) if active else None,
            'last_run': str(conversation.runs.order_by('-created_at').values_list('pk', flat=True).first() or '')}


@ensure_csrf_cookie
@require_GET
def index(request):
    owner(request)
    return render(request, 'planner/index.html')


@require_GET
def state(request):
    conversations = Conversation.objects.filter(owner=owner(request)).order_by('-updated_at')
    selected = request.GET.get('conversation')
    try:
        conversation = conversations.filter(pk=uuid.UUID(selected)).first() if selected else conversations.first()
    except ValueError:
        return JsonResponse({'error': '无效的行程编号'}, status=400)
    return JsonResponse({'conversations': list(conversations.values('id', 'title')[:30]),
                         'conversation': serialize(conversation) if conversation else None,
                         'xhs': login_status(), 'debug_enabled': request.session.get('travel_debug', False)})


@require_POST
def new_conversation(request):
    conversation = Conversation.objects.create(owner=owner(request))
    return JsonResponse(serialize(conversation), status=201)


@require_POST
def chat(request):
    data = payload(request)
    if data is None:
        return JsonResponse({'error': '请求格式不正确'}, status=400)
    message = data.get('message', '')
    trip = None
    if 'trip' in data:
        if not isinstance(data['trip'], dict):
            return JsonResponse({'error': '旅行参数格式不正确'}, status=400)
        form = TripInputForm(data['trip'])
        if not form.is_valid():
            return JsonResponse({'error': '；'.join(f'{form.fields[key].label}：{error}'
                                 for key, errors in form.errors.items() for error in errors)}, status=400)
        trip = form.as_trip()
        message = (f"开始地点：{trip['start_location']}\n开始日期：{trip['start_date']}\n"
                   f"结束日期：{trip['end_date']}\n用户要求：{trip['requirements'] or '请帮我推荐行程'}")
    if not isinstance(message, str) or not message.strip() or (trip is None and len(message) > 6000):
        return JsonResponse({'error': '请输入 1～6000 字的旅行需求'}, status=400)
    try:
        conversation_id = uuid.UUID(str(data.get('conversation_id', '')))
    except ValueError:
        return JsonResponse({'error': '无效的行程编号'}, status=400)
    with transaction.atomic():
        conversation = Conversation.objects.filter(pk=conversation_id, owner=owner(request)).first()
        if not conversation:
            return JsonResponse({'error': '行程不存在'}, status=404)
        active = conversation.runs.filter(status='running').first()
        if active:
            return JsonResponse({'error': '正在规划，请等待本轮完成', 'run_id': str(active.pk)}, status=409)
        if trip is not None:
            conversation.trip_input = trip
        Message.objects.create(conversation=conversation, role='user', content=message.strip())
        run = Run.objects.create(conversation=conversation, debug_enabled=request.session.get('travel_debug', False))
        conversation.save(update_fields=['trip_input', 'updated_at'])
        transaction.on_commit(lambda: enqueue(run.pk))
    return JsonResponse({'run_id': str(run.pk), 'message': message.strip(),
                         'trip_input': conversation.trip_input}, status=202)


@require_GET
def run_status(request, run_id):
    run = Run.objects.select_related('conversation').filter(pk=run_id, conversation__owner=owner(request)).first()
    if not run:
        return JsonResponse({'error': '任务不存在'}, status=404)
    return JsonResponse({'id': str(run.pk), 'status': run.status, 'events': run.events,
                         'debug_enabled': run.debug_enabled,
                         'conversation': serialize(run.conversation) if run.status != 'running' else None})


@require_POST
def debug_mode(request):
    data = payload(request)
    if data is None or not isinstance(data.get('enabled'), bool):
        return JsonResponse({'error': 'enabled 必须是布尔值'}, status=400)
    enabled = data['enabled']
    request.session['travel_debug'] = enabled
    active = list(Run.objects.filter(conversation__owner=owner(request), status='running'))
    for run in active:
        run.debug_enabled = enabled
        run.save(update_fields=['debug_enabled'])
        if enabled:
            RunTrace(run.pk).write('debug.enabled', {'message': '从此刻开始记录后续执行步骤。'})
    return JsonResponse({'debug_enabled': enabled})


@require_GET
def debug_logs(request, run_id):
    run = Run.objects.filter(pk=run_id, conversation__owner=owner(request)).first()
    if not run:
        return JsonResponse({'error': '任务不存在'}, status=404)
    if request.GET.get('download') == '1':
        path = log_path(run.pk)
        if not path.exists():
            return JsonResponse({'error': '这次任务还没有调试日志'}, status=404)
        return FileResponse(path.open('rb'), as_attachment=True, filename=f'travel-debug-{run.pk}.jsonl', content_type='application/x-ndjson')
    try:
        cursor = max(0, int(request.GET.get('after', '0')))
    except (ValueError, TypeError):
        return JsonResponse({'error': '无效的日志位置'}, status=400)
    return JsonResponse({**read_entries(run.pk, cursor), 'debug_enabled': run.debug_enabled})


@require_POST
def xhs_login(request):
    # A visible browser login only makes sense on the local development host.
    if not settings.XHS_LOGIN_ENABLED:
        return JsonResponse({'error': '服务器使用已同步的小红书登录状态；失效后请在本机重新登录并同步。'}, status=403)
    if request.META.get('REMOTE_ADDR') not in ('127.0.0.1', '::1'):
        return JsonResponse({'error': '请在运行后端的本机打开登录窗口'}, status=403)
    status = login_status()
    if status['opening']:
        return JsonResponse({'message': '登录窗口已经打开，请完成登录。'})
    import time
    LOGIN_FILE.write_text(json.dumps({'running': True, 'heartbeat': time.time()}), encoding='utf-8')
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    subprocess.Popen([sys.executable, str(settings.BASE_DIR / 'manage.py'), 'xhs_login'],
                     cwd=str(settings.BASE_DIR), creationflags=flags,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return JsonResponse({'message': '请在打开的小红书浏览器中登录；完成后返回这里继续。'})
