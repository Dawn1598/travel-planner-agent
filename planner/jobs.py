import asyncio
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.db import close_old_connections

from .models import Conversation, Message, Run
from .services.llm import safe_error
from .services.debug import RunTrace, record, trace_context

EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix='travel-agent')
EVENT_LOCK = threading.Lock()


def record_event(run_id, stage, text):
    close_old_connections()
    from django.utils import timezone
    with EVENT_LOCK:
        run = Run.objects.get(pk=run_id)
        events = run.events
        if events and events[-1]['text'] == text:
            return
        events.append({'stage': stage, 'text': text, 'at': timezone.now().isoformat()})
        run.events = events[-120:]
        run.save(update_fields=['events', 'updated_at'])


def execute(run_id):
    with trace_context(RunTrace(run_id)):
        _execute(run_id)


def _execute(run_id):
    close_old_connections()
    try:
        from .services.agent import plan_trip
        run = Run.objects.select_related('conversation').get(pk=run_id)
        conversation = run.conversation
        history = list(conversation.messages.order_by('-id').values('role', 'content')[:30])[::-1]
        record('run.start', model=settings.DEEPSEEK_MODEL,
               history=history, requirements=conversation.requirements, previous_plan=conversation.plan,
               schedule=conversation.schedule, trip_input=conversation.trip_input)

        def save_schedule(data):
            Conversation.objects.filter(pk=conversation.pk).update(schedule=data)

        result = asyncio.run(plan_trip(history, conversation.requirements, conversation.plan,
                                       lambda stage, text: record_event(run_id, stage, text),
                                       schedule=conversation.schedule, save_schedule=save_schedule,
                                       trip_input=conversation.trip_input))
        conversation.requirements = result['requirements']
        conversation.title = result['title'][:80]
        if result.get('plan'):
            conversation.plan = result['plan']
        conversation.save(update_fields=['requirements', 'title', 'plan', 'updated_at'])
        Message.objects.create(conversation=conversation, role='assistant', content=result['message'])
        Run.objects.filter(pk=run_id).update(status=result['status'])
        record('run.finish', status=result['status'], result=result)
    except Exception as exc:
        record('run.error', error_type=type(exc).__name__, message=str(exc), traceback=traceback.format_exc())
        message = safe_error(exc)
        run = Run.objects.get(pk=run_id)
        Message.objects.create(conversation_id=run.conversation_id, role='assistant', content=message)
        Run.objects.filter(pk=run_id).update(status='error', error=message)
    finally:
        close_old_connections()


def enqueue(run_id):
    EXECUTOR.submit(execute, run_id)
