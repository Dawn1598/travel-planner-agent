"""Export the current model-facing prompts verbatim for inspection."""
import ast
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()
from planner.schemas import Activity, Analysis, Plan, PlanDetails, ScheduleEntry, Review, EvidenceCheck, NoteQualityBatch
from planner.services.agent import amap_tools
from planner.services.debug import sanitize


def parse(relative):
    source = (BASE / relative).read_text(encoding='utf-8')
    return source, ast.parse(source)


def string(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return string(node.left) + string(node.right)
    return ''


def constant(tree, name):
    return next(n.value.value for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets))


def schema_prompt(tree, schema_name):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and len(node.args) > 1 and isinstance(node.args[0], ast.Name) and node.args[0].id == schema_name:
            result = string(node.args[1])
            if result:
                return result
    raise ValueError(schema_name + ' prompt not found')


def human_prompts(tree):
    result = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'HumanMessage':
            for keyword in node.keywords:
                if keyword.arg == 'content':
                    text = string(keyword.value)
                    if text:
                        result.append((node.lineno, text))
    return sorted(result)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--offline', action='store_true', help='Reuse the saved MCP tool descriptions; no external calls')
    options = parser.parse_args()
    agent_source, agent_tree = parse('planner/services/agent.py')
    _, knowledge_tree = parse('planner/services/knowledge.py')
    _, llm_tree = parse('planner/services/llm.py')
    _, check_tree = parse('planner/management/commands/check_services.py')
    sections = ['# 当前全部提示词与模型输入结构\n',
        '本文件由源代码自动提取，是原文快照，不会改写任何提示词。修改本文件不会改变程序；实际生效位置已标注。\n',
        '程序已移除 `max_tokens=7000`，不再主动发送输出 token 上限，采用服务端默认行为。\n']

    def block(title, origin, text, language='text'):
        sections.extend([f'## {title}\n', f'来源：`{origin}`\n', f'```{language}\n{text}\n```\n'])

    block('1. 需求提取 System 提示词', 'planner/services/agent.py / ANALYZE_PROMPT', constant(agent_tree, 'ANALYZE_PROMPT'))
    block('2. 旅行规划 Agent System 提示词', 'planner/services/agent.py / AGENT_PROMPT', constant(agent_tree, 'AGENT_PROMPT'))
    block('3. RAG 资料检查 System 提示词', 'planner/services/knowledge.py / judge_evidence', schema_prompt(knowledge_tree, 'EvidenceCheck'))
    block('3.1 入库质量判断 System 提示词', 'planner/services/knowledge.py / QUALITY_PROMPT', constant(knowledge_tree, 'QUALITY_PROMPT'))
    block('4. 行程检查 System 提示词', 'planner/services/agent.py / structured(Review, ...)', schema_prompt(agent_tree, 'Review'))

    sections.append('## 5. 自动追加的格式要求与反馈\n')
    block('5.1 结构化输出的 System 后缀', 'planner/services/llm.py / structured', '按以下 JSON Schema 返回 JSON：\n{对应的 JSON Schema}')
    for index, (line, text) in enumerate(human_prompts(llm_tree), 1):
        block(f'5.2.{index} 结构化格式修复 Human 消息', f'planner/services/llm.py:{line}', text)
    for index, (line, text) in enumerate(human_prompts(agent_tree), 1):
        suffix = '{problems 列表经 json.dumps 序列化后的实际检查问题}' if '请修正以下问题' in text else ''
        block(f'5.3.{index} Agent 追加 Human 消息', f'planner/services/agent.py:{line}', text + suffix)
    block('5.4 Agent System 后缀', 'planner/services/agent.py / messages', 'Activity JSON Schema:\n{Activity.model_json_schema() 的 JSON}')

    sections.append('## 6. 本地工具说明\n这些说明由 @tool 作为工具描述传给模型，同样会影响行为。\n')
    for node in ast.walk(agent_tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in ('get_info', 'ask_user', 'manage_schedule', 'submit_plan'):
            name = 'get_travel_info' if node.name == 'get_info' else node.name
            signature = ast.get_source_segment(agent_source, node).split('->', 1)[0].strip()
            block(name, f'planner/services/agent.py:{node.lineno}', signature + '\n' + ast.get_docstring(node))
    block('6.1 固定工具反馈', 'planner/services/agent.py / 本地工具返回值',
          'ask_user：已暂停，等待用户补充。\nsubmit_plan：无时间冲突时草案已提交，等待检查；有冲突时返回 issues。\n'
          'manage_schedule：返回 ok、conflict_free、issues、days；草稿可暂存冲突，提交前必须修正。\nget_travel_info：返回 sufficient、evidence、missing、crawl_error、crawl_report、note。\n'
          'note：已核实发布时间不保证内容仍然有效；仅按报告中的已核实范围排序。\n'
          '工具异常：返回 {"error": "实际错误内容"}。')

    sections.append('## 7. 每一阶段实际发送的动态内容\n下面是字段结构，花括号中的值由本次任务填入，不是额外的固定提示词。\n')
    dynamic = {
        '需求提取 Human': {'today': '{本地日期}', 'known': '{已有需求}', 'conversation': '{最近30条用户/助手消息}', 'previous_plan': '{上一版行程}'},
        'Agent 初始 Human': {'today': '{本地日期}', 'requirements': '{提取的需求}', 'conversation': '{最近30条用户/助手消息}', 'previous_plan': '{上一版行程}', 'schedule': '{当前时间表及程序检查结果}'},
        'RAG 检查 Human': {'today': '{本地日期}', 'query': '{本次检索查询}', 'evidence': '{候选片段，含ID、内容、链接、发布时间和匹配分数}'},
        '入库质量判断 Human': {'notes': '{每批最多5篇；标题、完整正文、note_id、published_at、date_provenance、content_type；实际输入为数组，无当前查询}'},
        '行程检查 Human': {'requirements': '{用户需求}', 'plan': '{完整行程草案，活动来自时间表}', 'sources': '{所引攻略}', 'tool_observations': '{最近30条高德调用记录}'},
    }
    for title, data in dynamic.items():
        if title in ('需求提取 Human', 'Agent 初始 Human', '行程检查 Human'):
            data['trip_input'] = '{用户填写的开始地点、开始日期、结束日期和要求，以表单为准}'
        block(title, 'agent.py / knowledge.py', json.dumps(data, ensure_ascii=False, indent=2), 'json')
    sections.append('Agent 后续还会携带此前全部 assistant 消息及工具返回（ToolMessage）。这些不是固定提示词，但会进入模型上下文。\n')

    sections.append('## 8. 完整输出 Schema（包含字段描述与约束）\n字段 description、枚举、默认值及约束也属于模型可见内容。Decision 未用于实际模型调用，因此不在此列。\n')
    for schema in (Analysis, EvidenceCheck, NoteQualityBatch, Activity, ScheduleEntry, PlanDetails, Plan, Review):
        block(schema.__name__, 'planner/schemas.py', json.dumps(schema.model_json_schema(), ensure_ascii=False, indent=2), 'json')

    sections.append('## 9. 高德 MCP 实际工具描述与参数 Schema\n来自当前服务的只读工具发现。仅列出目前会绑定给 Agent 的工具，不包含 IP 定位、唤起导航和打车工具。\n')
    tools = [] if options.offline else await amap_tools()
    if options.offline:
        previous = (BASE / 'PROMPTS.md').read_text(encoding='utf-8')
        old_section = previous.split('## 9. 高德 MCP 实际工具描述与参数 Schema', 1)[1].split('## 10.', 1)[0]
        start = old_section.find('## maps_')
        sections.append('离线导出：以下为上次保存的 MCP 描述，未访问外部服务。\n' + (old_section[start:] if start >= 0 else '无缓存工具描述。\n'))
    for remote in tools:
        if any(part in remote.name for part in ('search', 'direction', 'distance', 'geo', 'weather', 'personal_map')):
            schema = remote.args_schema if isinstance(remote.args_schema, dict) else remote.args_schema.model_json_schema()
            block(remote.name, '高德 MCP 服务返回（description + args_schema）', remote.description + '\n\n' + json.dumps(schema, ensure_ascii=False, indent=2), 'text')

    sections.append('## 10. 其他模型输入\n')
    block('Embedding 查询前缀', 'planner/services/knowledge.py / LocalEmbeddings.embed_query', '为这个句子生成表示以用于检索相关文章：{query}')
    block('Embedding 文档输入', 'planner/services/knowledge.py / save_notes', '{笔记标题}\n{分块正文}')
    for line, text in human_prompts(check_tree):
        block('连接检查提示词（不参与旅行规划）', f'planner/management/commands/check_services.py:{line}', text)
    sections.append('## 11. 当前其他运行参数\n不属于提示词，但会影响执行。\n')
    from django.conf import settings
    block('参数', 'planner/services/llm.py 与 config/settings.py', json.dumps({
        'model': settings.DEEPSEEK_MODEL, 'temperature': 0.25,
        'output_token_limit': '不发送 max_tokens/max_completion_tokens，使用服务端默认值',
        'thinking': 'disabled', 'timeout_seconds': 100, 'max_retries': 1,
        'max_agent_steps': settings.MAX_AGENT_STEPS, 'max_revisions': settings.MAX_REVISIONS,
        'xhs_candidate_limit': settings.XHS_CANDIDATE_LIMIT, 'xhs_target_new': settings.XHS_TARGET_NEW,
        'xhs_max_visits': settings.XHS_MAX_VISITS, 'xhs_time_budget_seconds': settings.XHS_TIME_BUDGET_SECONDS,
        'xhs_retries': settings.XHS_RETRIES, 'xhs_refresh_hours': settings.XHS_REFRESH_HOURS,
    }, ensure_ascii=False, indent=2), 'json')
    # Sanitize each section separately so the full inventory is not log-truncated.
    document = '\n'.join(sanitize(section) for section in sections)
    (BASE / 'PROMPTS.md').write_text(document, encoding='utf-8')
    print(f'Exported PROMPTS.md: {len(document)} characters, {len(tools)} discovered tools')


asyncio.run(main())
