from django.core.management.base import BaseCommand
from planner.models import Run


class Command(BaseCommand):
    help = '将服务重启中断的规划任务标记为可重新发送。'

    def handle(self, *args, **options):
        count = Run.objects.filter(status='running').update(status='error', error='服务已重启，请重新发送消息继续。')
        self.stdout.write(f'Recovered interrupted runs: {count}')
