import asyncio
import json
import re
import time
from typing import Literal

from django.conf import settings
from django.utils import timezone
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool, tool
from langchain_mcp_adapters.client import MultiServerMCPClient

from planner.schemas import Activity, Analysis, Plan, PlanDetails, Review, ScheduleEntry
from .crawler import LoginRequired
from .knowledge import get_travel_info
from .llm import invoke, model, safe_error, structured
from .debug import record, record_async
from .schedule import Timetable, check_days


ANALYZE_PROMPT = (
    '根据最新对话提取并更新旅行需求，保留已确认的信息。'
    '开始地点和日期以 trip_input 表单为准，用户要修改时请其修改表单。'
    '缺少影响规划的必要信息时提问，否则 question 留空。'
    'city 填目的地城市，title 填简短行程标题。'
)

AGENT_PROMPT = (
    '你是旅行规划助手，根据用户需求制定可执行的行程。'
    '每轮根据已有信息选择工具，读取结果后再决定下一步；需要用户补充时使用 ask_user。'
    '从 trip_input 的开始地点出发，在所选日期内安排旅行，查询起点到首站及站点间路线。'
    '用高德查询地点和路线，沿途酒店、餐馆先查高德周边搜索和地点详情。'
    '需要攻略、体验或评价时用 get_travel_info，先检索本地资料，不足时自动补爬小红书。'
    '依据查询结果安排路线与时间，不编造缺失信息。'
    '活动通过 manage_schedule 增删改查，可批量操作；按工具返回的时间冲突调整。'
    '完成后调用 submit_plan，根据检查反馈修改行程。'
)


def validate_plan(plan, route_count):
    issues = [i['message'] for i in check_days([day.model_dump() for day in plan.days])]
    if not route_count and any(len(d.activities) > 1 for d in plan.days):
        issues.append('尚未成功查询活动之间的高德路线，不能确认交通可行性')
    return issues


async def amap_tools():
    if not settings.AMAP_API_KEY:
        raise RuntimeError('请先在后端 .env 中填写高德 API Key。')
    client = MultiServerMCPClient({'amap': {
        'transport': 'streamable_http',
        'url': 'https://mcp.amap.com/mcp?key=' + settings.AMAP_API_KEY,
        'timeout': 35,
    }})
    return await client.get_tools()


def content_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(v.get('text', '') if isinstance(v, dict) else str(v) for v in value)
    return json.dumps(value, ensure_ascii=False, default=str)


def route_succeeded(text):
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return False
    if isinstance(data, dict) and (data.get('isError') or str(data.get('status', '1')) == '0' or data.get('error')):
        return False

    def positive_duration(value):
        if isinstance(value, dict):
            try:
                if float(value.get('duration', 0)) > 0:
                    return True
            except (ValueError, TypeError):
                pass
            return any(positive_duration(v) for v in value.values())
        if isinstance(value, list):
            return any(positive_duration(v) for v in value)
        return False
    return positive_duration(data)


async def plan_trip(history, requirements, previous_plan, emit, schedule=None, save_schedule=None, trip_input=None):
    trip_input = trip_input or {}
    # emit performs synchronous DB writes, so keep it off the async event loop.
    async def event(stage, text):
        await asyncio.to_thread(emit, stage, text)
        await record_async('stage', stage=stage, message=text)

    await event('analyze', '正在整理你的旅行需求')
    analysis = await structured(Analysis, ANALYZE_PROMPT, json.dumps({
        'today': timezone.localdate().isoformat(), 'known': requirements,
        'conversation': history, 'previous_plan': previous_plan, 'trip_input': trip_input,
    }, ensure_ascii=False))
    state = {'requirements': analysis.requirements, 'title': analysis.title, 'city': analysis.city}
    if analysis.question:
        return {**state, 'status': 'waiting', 'message': analysis.question}

    await event('map', '正在连接高德地图工具')
    remote_tools = await amap_tools()
    await record_async('mcp.tools', tools=[{'name': t.name, 'schema': t.args_schema if isinstance(t.args_schema, dict) else t.args_schema.model_json_schema()} for t in remote_tools])
    if not remote_tools:
        raise RuntimeError('高德 MCP 未返回可用工具，请检查 Key 和服务权限。')

    sources, observations, map_cache = {}, [], {}
    route_count = 0
    pending = {}
    timetable = Timetable(schedule if schedule else {'days': previous_plan.get('days', [])}, trip_input=trip_input)

    @tool
    def get_info(query: str, city: str = '', freshness_days: int = 365) -> str:
        """检索旅行攻略，资料不足时自动补爬。freshness_days 表示时效要求。"""
        result = get_travel_info(query, city or analysis.city, freshness_days, emit)
        for evidence in result['evidence']:
            sources[evidence['note_id']] = {k: evidence[k] for k in ('note_id', 'title', 'url', 'published_at')}
        return json.dumps(result, ensure_ascii=False)

    get_info.name = 'get_travel_info'

    @tool
    def ask_user(question: str) -> str:
        """向用户提问并暂停，等待补充。"""
        pending['question'] = question
        return '已暂停，等待用户补充。'

    @tool
    async def manage_schedule(action: Literal['list', 'add', 'update', 'delete', 'check'],
                              entries: list[ScheduleEntry] | None = None) -> str:
        """管理时间表。新增填写 day、activity；更新/删除填写 id。支持批量操作，每次返回程序检查的冲突。"""
        result = timetable.read() if action in ('list', 'check') else timetable.edit(action, entries)
        if result['ok'] and action in ('add', 'update', 'delete') and save_schedule:
            await asyncio.to_thread(save_schedule, timetable.data)
        await event('schedule', '检查时间表：' + (result.get('error') or '；'.join(i['message'] for i in result['issues'][:2])
                    if result['issues'] else '无时间冲突'))
        return json.dumps(result, ensure_ascii=False)

    @tool
    def submit_plan(plan: PlanDetails, message: str) -> str:
        """提交行程概述；每日活动直接使用时间表。有时间冲突时返回问题。"""
        checked = timetable.read()
        if checked['issues']:
            return json.dumps({'ok': False, 'issues': checked['issues']}, ensure_ascii=False)
        validated = Plan.model_validate({**plan.model_dump(), 'days': timetable.data['days']})
        pending['plan'] = validated
        pending['message'] = message
        return '草案已提交，等待检查。'

    def wrap_remote(remote):
        async def call(**kwargs):
            nonlocal route_count
            cache_key = remote.name + json.dumps(kwargs, sort_keys=True, ensure_ascii=False)
            if cache_key in map_cache:
                await record_async('mcp.cache_hit', tool=remote.name, arguments=kwargs, result=map_cache[cache_key])
                return map_cache[cache_key]
            label = ('查询交通路线' if any(x in remote.name for x in ('direction', 'distance'))
                     else '查询天气' if 'weather' in remote.name else '查询地点和周边信息')
            detail = kwargs.get('keywords') or kwargs.get('address') or ''
            await event('map', label + (f'：{detail}' if detail else ''))
            await record_async('mcp.request', tool=remote.name, arguments=kwargs)
            started = time.perf_counter()
            try:
                result = await remote.ainvoke(kwargs)
                text = content_text(result)
                await record_async('mcp.response', tool=remote.name, elapsed_ms=round((time.perf_counter() - started) * 1000), result=text)
                if 'direction' in remote.name and route_succeeded(text):
                    route_count += 1
                observations.append({'tool': remote.name, 'args': kwargs, 'result': text[:10000]})
                if not any(error in text for error in ('INVALID_USER_KEY', 'SERVICE_NOT_AVAILABLE', '"error"', '"isError":true', '"isError": true')):
                    map_cache[cache_key] = text[:24000]
                return text[:24000]
            except Exception as exc:
                error = safe_error(exc)
                await record_async('mcp.error', tool=remote.name, error_type=type(exc).__name__, message=str(exc), elapsed_ms=round((time.perf_counter() - started) * 1000))
                observations.append({'tool': remote.name, 'error': error})
                return json.dumps({'error': error}, ensure_ascii=False)
        return StructuredTool(name=remote.name, description=remote.description,
                              args_schema=remote.args_schema, coroutine=call)

    allowed = [t for t in remote_tools if any(s in t.name for s in ('search', 'direction', 'distance', 'geo', 'weather', 'personal_map'))]
    tools = [get_info, ask_user, manage_schedule, submit_plan, *[wrap_remote(t) for t in allowed]]
    by_name = {t.name: t for t in tools}
    llm = model().bind_tools(tools)
    messages = [SystemMessage(content=AGENT_PROMPT + '\nActivity JSON Schema:\n' + json.dumps(Activity.model_json_schema(), ensure_ascii=False)),
                HumanMessage(content=json.dumps({'today': timezone.localdate().isoformat(),
                    'requirements': analysis.requirements, 'trip_input': trip_input, 'conversation': history,
                    'previous_plan': previous_plan, 'schedule': timetable.read()}, ensure_ascii=False))]
    revisions = 0
    await event('plan', '开始查找资料并安排路线')
    for step in range(settings.MAX_AGENT_STEPS):
        await record_async('agent.iteration', step=step + 1, max_steps=settings.MAX_AGENT_STEPS, revisions=revisions)
        answer = await invoke(llm, messages, f'agent.step.{step + 1}')
        messages.append(answer)
        if not answer.tool_calls:
            messages.append(HumanMessage(content='请继续调用工具，完成后调用 submit_plan。'))
            continue
        for call in answer.tool_calls:
            name, args = call['name'], call['args']
            await record_async('tool.request', tool=name, arguments=args, call_id=call['id'])
            started = time.perf_counter()
            try:
                result = await by_name[name].ainvoke(args)
            except LoginRequired as exc:
                await record_async('tool.waiting_login', tool=name, message=str(exc))
                return {**state, 'status': 'waiting', 'message': str(exc)}
            except Exception as exc:
                await record_async('tool.error', tool=name, error_type=type(exc).__name__, message=str(exc))
                result = json.dumps({'error': safe_error(exc)}, ensure_ascii=False)
            await record_async('tool.response', tool=name, elapsed_ms=round((time.perf_counter() - started) * 1000), result=content_text(result))
            messages.append(ToolMessage(content=content_text(result), tool_call_id=call['id']))
        if pending.get('question'):
            await record_async('agent.waiting_user', question=pending['question'])
            return {**state, 'status': 'waiting', 'message': pending['question']}
        if pending.get('plan'):
            plan = pending.pop('plan')
            # Always read the current table, even if further edits followed submit.
            checked = timetable.read()
            if checked['issues']:
                messages.append(HumanMessage(content='请修正时间表冲突后重新提交：\n' + json.dumps(checked['issues'], ensure_ascii=False)))
                continue
            plan.days = Plan.model_validate({**plan.model_dump(), 'days': timetable.data['days']}).days
            await event('review', '程序校验时间表，检查行程安排')
            issues = validate_plan(plan, route_count)
            review = await structured(Review,
                '检查行程是否符合用户需求和已查到的地点信息。时间冲突由程序检查。'
                '只指出影响执行的具体问题，不额外增加要求。'
                '无问题时 reasonable 为 true，issues 为空。',
                json.dumps({'requirements': analysis.requirements, 'trip_input': trip_input, 'plan': plan.model_dump(),
                            'sources': list(sources.values()), 'tool_observations': observations[-30:]}, ensure_ascii=False))
            problems = list(dict.fromkeys(issues + (review.issues if not review.reasonable else [])))
            await record_async('review.result', revision=revisions, route_count=route_count,
                               program_issues=issues, model_review=review.model_dump(), problems=problems, plan=plan.model_dump())
            if review.reasonable and not problems:
                data = plan.model_dump()
                data['sources'] = list(sources.values())
                data['checked_at'] = timezone.now().isoformat()
                # Only genuine retrieved citation IDs survive into the UI.
                for day in data['days']:
                    for activity in day['activities']:
                        activity['source_ids'] = [s for s in activity['source_ids'] if s in sources]
                await event('done', '行程检查完成')
                return {**state, 'status': 'done', 'message': pending.get('message', '行程已规划完成。'), 'plan': data}
            revisions += 1
            if revisions > settings.MAX_REVISIONS:
                return {**state, 'status': 'waiting', 'message': '当前安排仍有问题：\n' + '\n'.join('• ' + x for x in problems[:5]) + '\n可以补充偏好或减少景点后继续。'}
            await event('revise', '正在调整：' + ('；'.join(problems[:2]) or '行程安排需要优化'))
            messages.append(HumanMessage(content='请修正以下问题，必要时补查资料，然后重新提交：\n' + json.dumps(problems, ensure_ascii=False)))
    return {**state, 'status': 'waiting', 'message': '本轮查询已达到上限。已保留需求，可补充或简化行程后继续。'}
