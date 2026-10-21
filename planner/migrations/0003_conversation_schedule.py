from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('planner', '0002_run_debug_enabled')]
    operations = [migrations.AddField(model_name='conversation', name='schedule', field=models.JSONField(default=dict))]
