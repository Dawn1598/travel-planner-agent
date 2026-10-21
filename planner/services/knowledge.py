import asyncio
import hashlib
import json
import re
import threading
import time
from datetime import timedelta

from asgiref.sync import async_to_sync
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from langchain_core.embeddings import Embeddings

from planner.models import Chunk, CrawlLog, Note
from planner.schemas import EvidenceCheck
from . import crawler
from .llm import structured
from .debug import record

_embedder = None
_embedding_lock = threading.Lock()


class LocalEmbeddings(Embeddings):
    def embed_documents(self, texts):
        global _embedder
        with _embedding_lock:
            started = time.perf_counter()
            if _embedder is None:
                from fastembed import TextEmbedding
                _embedder = TextEmbedding(settings.EMBEDDING_MODEL, cache_dir=str(settings.RUNTIME_DIR / 'embeddings'), threads=2)
            vectors = [v.tolist() for v in _embedder.embed(texts)]
            record('embedding.complete', model=settings.EMBEDDING_MODEL, count=len(texts),
                   dimensions=len(vectors[0]) if vectors else 0, elapsed_ms=round((time.perf_counter() - started) * 1000))
            return vectors

    def embed_query(self, text):
        return self.embed_documents(['为这个句子生成表示以用于检索相关文章：' + text])[0]


def split_text(text, size=420, overlap=60):
    paragraphs = re.split(r'\n\s*\n|(?<=[。！？])\s*\n', text.strip())
    chunks, current = [], ''
    for paragraph in paragraphs:
        if len(current) + len(paragraph) + 1 <= size:
            current = (current + '\n' + paragraph).strip()
        else:
            if current:
                chunks.append(current)
            while len(paragraph) > size:
                chunks.append(paragraph[:size])
                paragraph = paragraph[size - overlap:]
            current = paragraph
    if current:
        chunks.append(current)
    return chunks


def save_notes(notes, emit=lambda *_: None):
    count = 0
    embedder = LocalEmbeddings()
    for data in notes:
        old = Note.objects.filter(note_id=data['note_id']).first()
        digest = data.get('content_hash') or hashlib.sha256(data['content'].encode()).hexdigest()
        if old and old.content_hash == digest and old.chunks.filter(embedding_model=settings.EMBEDDING_MODEL).exists():
            record('index.unchanged', note_id=data['note_id'], title=data['title'], published_at=data.get('published_at'))
            old.fetched_at = timezone.now()
            old.published_at = data.get('published_at') or old.published_at
            old.save(update_fields=['fetched_at', 'published_at'])
            count += 1
            continue
        texts = split_text(data['content'])
        if not texts:
            continue
        emit('index', '正在为攻略建立本地向量索引')
        vectors = embedder.embed_documents([data['title'] + '\n' + t for t in texts])
        defaults = {k: data[k] for k in ('title', 'content', 'url', 'city', 'published_at')}
        if old and not defaults['published_at']:
            defaults['published_at'] = old.published_at
        defaults['content_hash'] = digest
        with transaction.atomic():
            note, _ = Note.objects.update_or_create(note_id=data['note_id'], defaults=defaults)
            note.chunks.all().delete()
            Chunk.objects.bulk_create([Chunk(note=note, position=i, content=t, embedding=v, embedding_model=settings.EMBEDDING_MODEL) for i, (t, v) in enumerate(zip(texts, vectors))])
        record('index.saved', note_id=note.note_id, title=note.title, published_at=note.published_at,
               chunks=len(texts), created=old is None)
        count += 1
    return count


def retrieve_window(query, city, vector, min_days, max_days):
    started = time.perf_counter()
    now = timezone.now()
    qs = Chunk.objects.select_related('note').filter(embedding_model=settings.EMBEDDING_MODEL)
    if city:
        qs = qs.filter(Q(note__city__icontains=city) | Q(note__title__icontains=city) | Q(note__content__icontains=city))
    # Date filters happen BEFORE vector candidate selection.
    if max_days is not None:
        qs = qs.filter(note__published_at__gte=now - timedelta(days=max_days))
    if min_days:
        if max_days is None:
            qs = qs.filter(Q(note__published_at__lt=now - timedelta(days=min_days)) | Q(note__published_at__isnull=True))
        else:
            qs = qs.filter(note__published_at__lt=now - timedelta(days=min_days))
    scored = []
    import numpy as np
    qv = np.asarray(vector, dtype=float)
    qn = float(np.linalg.norm(qv)) or 1
    scanned = 0
    for chunk in qs.iterator(chunk_size=200):
        scanned += 1
        v = np.asarray(chunk.embedding, dtype=float)
        if v.shape != qv.shape:
            continue
        score = float(np.dot(qv, v) / (qn * (np.linalg.norm(v) or 1)))
        if score >= 0.40:
            scored.append((score, chunk))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    results, note_counts = [], {}
    for score, chunk in scored[:30]:
        if note_counts.get(chunk.note_id, 0) >= 2:
            continue
        note_counts[chunk.note_id] = note_counts.get(chunk.note_id, 0) + 1
        results.append({'id': chunk.pk, 'note_id': chunk.note.note_id, 'title': chunk.note.title,
                        'content': chunk.content, 'url': chunk.note.url,
                        'published_at': chunk.note.published_at.isoformat() if chunk.note.published_at else None,
                        'score': round(score, 4)})
        if len(results) >= 10:
            break
    results.sort(key=lambda item: item['published_at'] or '', reverse=True)
    record('rag.window', query=query, city=city, min_days=min_days, max_days=max_days,
           scanned=scanned, similarity_threshold=0.40, candidates=results,
           elapsed_ms=round((time.perf_counter() - started) * 1000))
    return results


def judge_evidence(query, items):
    return async_to_sync(structured)(EvidenceCheck,
        '你是旅行资料检索检查器。选出对查询有帮助的片段 ID，允许多篇资料互相补充。'
        '判断资料是否足够回答查询；不足时在 missing 中说明具体缺少的信息。'
        '按查询本身判断，不额外要求官方或权威来源。',
        json.dumps({'today': timezone.localdate().isoformat(), 'query': query, 'evidence': items}, ensure_ascii=False))


def get_travel_info(query, city='', freshness_days=365, emit=lambda *_: None):
    """Recent-first RAG, then one bounded crawl and a fresh retrieval."""
    emit('retrieve', '检索本地近期攻略')
    freshness_days = max(1, min(int(freshness_days), 3650))
    record('rag.request', query=query, city=city, freshness_days=freshness_days)
    all_selected = {}
    missing = '未找到可用资料'
    vector = None

    def search():
        nonlocal vector, missing
        if not Chunk.objects.exists():
            record('rag.empty', reason='本地没有已建立索引的资料')
            return False
        if vector is None:
            vector = LocalEmbeddings().embed_query(query)
        windows = sorted(set([min(30, freshness_days), min(90, freshness_days), min(365, freshness_days), freshness_days]))
        previous = 0
        for days in windows:
            emit('retrieve', f'查找近 {days} 天内的相关资料')
            found = retrieve_window(query, city, vector, previous, days)
            previous = days
            if not found:
                continue
            combined = {**all_selected, **{i['id']: i for i in found}}
            check = judge_evidence(query, list(combined.values()))
            record('rag.judgment', max_days=days, result=check.model_dump())
            all_selected.clear()
            all_selected.update({i: combined[i] for i in check.relevant_ids if i in combined})
            missing = check.missing
            if check.sufficient and all_selected:
                return True
        # Stable advice can fall back to historical/undated notes, clearly labelled.
        if freshness_days >= 365:
            found = retrieve_window(query, city, vector, previous, None)
            if found:
                combined = {**all_selected, **{i['id']: i for i in found}}
                check = judge_evidence(query, list(combined.values()))
                record('rag.judgment', max_days=None, result=check.model_dump())
                all_selected.clear()
                all_selected.update({i: combined[i] for i in check.relevant_ids if i in combined})
                missing = check.missing
                if check.sufficient and all_selected:
                    return True
        return False

    sufficient = search()
    crawl_error = ''
    if not sufficient:
        # A recent failed crawl is not repeated on every agent tool call.
        recent = CrawlLog.objects.filter(query=query[:300], city=city[:80], created_at__gte=timezone.now() - timedelta(minutes=10)).exists()
        if not recent:
            record('rag.supplement', query=query, reason=missing)
            try:
                notes = crawler.crawl(query, city, emit)
                count = save_notes(notes, emit)
                CrawlLog.objects.create(query=query[:300], city=city[:80], count=count)
                if count:
                    sufficient = search()
            except crawler.LoginRequired:
                raise
            except Exception as exc:
                from .llm import safe_error
                crawl_error = safe_error(exc)
        else:
            record('crawl.cooldown', query=query, cooldown_minutes=10)
            crawl_error = '最近已尝试采集此问题，暂无更多可用资料。'
    evidence = sorted(all_selected.values(), key=lambda i: i['published_at'] or '', reverse=True)[:10]
    record('rag.result', sufficient=sufficient, evidence=evidence, missing=missing, crawl_error=crawl_error)
    return {'sufficient': sufficient, 'evidence': evidence, 'missing': '' if sufficient else missing,
            'crawl_error': crawl_error, 'note': '发布时间仅代表笔记发布，不保证内容就是当前事实；未知日期已明确标记。'}
