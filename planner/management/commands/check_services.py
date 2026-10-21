import asyncio
import json
from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = '检查 DeepSeek 与高德 MCP 连接，不输出密钥。'

    def handle(self, *args, **options):
        asyncio.run(self.check_connections())

    async def check_connections(self):
        from planner.services.agent import amap_tools
        from planner.services.llm import model, safe_error
        from langchain_core.messages import HumanMessage
        try:
            result = await model().ainvoke([HumanMessage(content='连接测试，请只回复 OK。')])
            self.stdout.write('DeepSeek: ' + ('OK' if result.content else 'EMPTY'))
        except Exception as exc:
            self.stdout.write('DeepSeek: ' + safe_error(exc))
        try:
            tools = await amap_tools()
            self.stdout.write('AMAP MCP: ' + str(len(tools)) + ' tools')
            search = next(t for t in tools if t.name == 'maps_text_search')
            result = await search.ainvoke({'keywords': '西湖', 'city': '杭州', 'citylimit': True})
            from planner.services.agent import content_text
            text = content_text(result)
            parsed = json.loads(text)
            self.stdout.write('AMAP search: ' + ('OK' if parsed.get('pois') and parsed['pois'][0].get('id') else 'No POI returned'))
            data = [{'name': t.name, 'schema': t.args_schema if isinstance(t.args_schema, dict) else t.args_schema.model_json_schema()} for t in tools]
            (settings.RUNTIME_DIR / 'amap-tools.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        except Exception as exc:
            self.stdout.write('AMAP MCP: ' + safe_error(exc))
