import uuid
from django.db import models


class Conversation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.CharField(max_length=64, db_index=True)
    title = models.CharField(max_length=80, default='新行程')
    requirements = models.JSONField(default=dict)
    trip_input = models.JSONField(default=dict)
    plan = models.JSONField(default=dict)
    schedule = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Message(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='messages')
    role = models.CharField(max_length=12)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['id']


class Run(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='runs')
    status = models.CharField(max_length=16, default='running')
    events = models.JSONField(default=list)
    error = models.TextField(blank=True)
    debug_enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Note(models.Model):
    note_id = models.CharField(max_length=80, unique=True)
    title = models.CharField(max_length=300)
    content = models.TextField()
    url = models.URLField(max_length=2000)
    city = models.CharField(max_length=80, blank=True, db_index=True)
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    fetched_at = models.DateTimeField(auto_now=True)
    content_hash = models.CharField(max_length=64)


class Chunk(models.Model):
    note = models.ForeignKey(Note, on_delete=models.CASCADE, related_name='chunks')
    position = models.PositiveIntegerField()
    content = models.TextField()
    embedding = models.JSONField()
    embedding_model = models.CharField(max_length=150)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['note', 'position'], name='unique_note_chunk')]


class CrawlLog(models.Model):
    query = models.CharField(max_length=300)
    city = models.CharField(max_length=80, blank=True)
    count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
