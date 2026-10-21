import json
import re
import time

try:
    from builtins import BaseExceptionGroup
except ImportError:
    from exceptiongroup import BaseExceptionGroup

from django.conf import settings
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_deepseek import ChatDeepSeek
from .debug import record_async


def model():
    if not settings.DEEPSEEK_API_KEY:
        raise RuntimeError('请先在后端 .env 中填写 DeepSeek API Key。')
    return ChatDeepSeek(
        model=settings.DEEPSEEK_MODEL,
        api_key=settings.DEEPSEEK_API_KEY,
        api_base=settings.DEEPSEEK_BASE_URL,
        temperature=0.25,
        timeout=100,
        max_retries=1,
        extra_body={'thinking': {'type': 'disabled'}},
    )


def parse_json(text):
    if isinstance(text, list):
        text = ''.join(block.get('text', '') if isinstance(block, dict) else str(block) for block in text)
    text = re.sub(r'^\s*```(?:json)?\s*|\s*```\s*$', '', text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find('{'), text.rfind('}')
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise ValueError('模型没有返回有效的结构化结果，请重试。')


async def invoke(llm, messages, purpose):
    request = [{'role': m.type, 'content': m.content,
                **({'tool_calls': m.tool_calls} if getattr(m, 'tool_calls', None) else {}),
                **({'tool_call_id': m.tool_call_id} if getattr(m, 'tool_call_id', None) else {})}
               for m in messages]
    await record_async('llm.request', purpose=purpose, model=settings.DEEPSEEK_MODEL, messages=request)
    started = time.perf_counter()
    try:
        result = await llm.ainvoke(messages)
    except Exception as exc:
        await record_async('llm.error', purpose=purpose, elapsed_ms=round((time.perf_counter() - started) * 1000),
                           error_type=type(exc).__name__, message=str(exc))
        raise
    await record_async('llm.response', purpose=purpose, elapsed_ms=round((time.perf_counter() - started) * 1000),
                       content=result.content, tool_calls=getattr(result, 'tool_calls', []),
                       usage=getattr(result, 'usage_metadata', None))
    return result


async def structured(schema, system, content):
    instructions = system + '\n按以下 JSON Schema 返回 JSON：\n' + json.dumps(schema.model_json_schema(), ensure_ascii=False)
    messages = [SystemMessage(content=instructions), HumanMessage(content=content)]
    llm = model().bind(response_format={'type': 'json_object'})
    result = await invoke(llm, messages, schema.__name__)
    try:
        parsed = schema.model_validate(parse_json(result.content))
        await record_async('llm.parsed', purpose=schema.__name__, result=parsed.model_dump())
        return parsed
    except (ValueError, TypeError) as exc:
        await record_async('llm.validation_retry', purpose=schema.__name__, error=str(exc))
        messages.append(HumanMessage(content='请重新返回符合 Schema 的完整 JSON。'))
        result = await invoke(llm, messages, schema.__name__ + '.retry')
        parsed = schema.model_validate(parse_json(result.content))
        await record_async('llm.parsed', purpose=schema.__name__, result=parsed.model_dump())
        return parsed


def safe_error(exc):
    if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        return safe_error(exc.exceptions[0])
    text = str(exc)
    for secret in (settings.DEEPSEEK_API_KEY, settings.AMAP_API_KEY, settings.SECRET_KEY):
        if secret:
            text = text.replace(secret, '[已隐藏]')
    if '401' in text or 'Authentication' in type(exc).__name__:
        return '服务认证失败，请检查后端 API Key。'
    if '402' in text or 'Insufficient Balance' in text:
        return 'DeepSeek 账户余额不足，请充值后重试。'
    if '429' in text:
        return '服务请求频率受限，请稍后重试。'
    if 'timeout' in text.lower() or 'timed out' in text.lower():
        return '外部服务响应超时，请稍后重试。'
    return text[:350] or '服务暂时不可用，请重试。'
