"""Live planning integration check using the configured model and MCP."""
import asyncio
import json
import os
import sys
from pathlib import Path
from datetime import date, timedelta

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()
from planner.services.agent import plan_trip


async def main():
    text = '杭州，当天上午9点从杭州东站出发，去西湖断桥，下午13点在西湖结束，不返回车站。1个人，预算300元，公共交通。只安排交通和湖边散步，不需要门票、预约、酒店、餐馆或游玩攻略。'
    trip_date = (date.today() + timedelta(days=7)).isoformat()
    trip = {'start_location': '杭州东站', 'start_date': trip_date, 'end_date': trip_date, 'requirements': text}
    result = await plan_trip([{'role': 'user', 'content': text}], {}, {}, lambda stage, text: print(stage + ': ' + text, flush=True), trip_input=trip)
    print('STATUS:', result['status'])
    print('MESSAGE:', result['message'])
    if result.get('plan'):
        (BASE / '.runtime' / 'validated-plan.json').write_text(json.dumps(result['plan'], ensure_ascii=False), encoding='utf-8')
        print('DAYS:', len(result['plan']['days']))
    assert result['status'] == 'done', result['message']


asyncio.run(main())
