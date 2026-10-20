"""Batch draft storage with deterministic format repair and no formal publication."""
import copy
import re
import uuid

from bs4 import BeautifulSoup

from .db import now
from .models import content_hash
from .render import validate_article, validate_evidence
from .workflow_control import WorkflowCancelled


def short_title(draft, limit=32):
    current = re.sub(r'[*#`\r\n]', '', draft['article']['title']).strip()
    if len(current) <= limit:
        return current
    plan = draft.get('editorial_plan') or {}
    alternatives = list(plan.get('headline_candidates', []))
    alternatives += [(draft.get('angle') or {}).get('selected', ''), (plan.get('angle') or {}).get('selected', '')]
    for title in alternatives:
        cleaned = re.sub(r'[*#`\r\n]', '', title).strip()
        if 4 <= len(cleaned) <= limit:
            return cleaned
    # Cutting a long headline can remove a negation or availability condition. Use a neutral fallback instead.
    entity = re.search(r'\b[A-Za-z][A-Za-z0-9.-]{1,14}\b', current)
    lead = entity.group(0) if entity else 'AI'
    neutral = lead + '新消息：关键变化与影响'
    return neutral if len(neutral) <= limit else 'AI新消息：关键变化与影响'


def compact_html(value):
    soup = BeautifulSoup(value, 'html.parser')
    for node in soup.find_all(True):
        if node.name == 'p':
            node['style'] = 'margin:0 0 22px;'
        elif node.name not in {'section', 'h2', 'h3', 'img', 'span', 'blockquote', 'figcaption'}:
            node.attrs.pop('style', None)
    return str(soup)


class DraftWorkflow:
    def enqueue_draft_batch(self):
        with self.lock:
            for batch in self.store.all('draft_batches'):
                if batch['status'] in {'queued', 'running', 'paused'} and batch['account'] == self.settings.wechat_app_id:
                    job = self.store.get('jobs', batch['job_id'])
                    if job['status'] in {'queued', 'running', 'paused'}:
                        return batch
            rules = self.publication_rules()
            if not rules['enabled'] or self.publication_settings().get('target_app_id') != self.settings.wechat_app_id:
                raise ValueError('请先保存并启用当前公众号的草稿设置')
            entries = []
            for draft in reversed(self.store.all('drafts')):
                if draft.get('demo') or draft['status'] in {'rejected', 'manual_published', 'published', 'submitted', 'publish_unknown'}:
                    continue
                if not draft.get('article'):
                    continue
                records = [p for p in self.store.all('publications') if p['draft_id'] == draft['id'] and p.get('revision') == draft['revision'] and p.get('account') == self.settings.wechat_app_id]
                if any(p.get('media_id') and p['status'] in {'draft_only', 'draft_created', 'published'} for p in records):
                    continue
                entries.append({'draft_id': draft['id'], 'revision': draft['revision'], 'title': draft['article']['title'], 'status': 'queued'})
            batch = {'id': uuid.uuid4().hex, 'account': self.settings.wechat_app_id, 'status': 'queued', 'created_at': now(), 'entries': entries,
                     'total': len(entries), 'saved': 0, 'failed': 0, 'blocked': 0, 'needs_check': 0}
            job = self.enqueue('draft_batch', batch['id'])
            batch['job_id'] = job['id']
            self.store.put('draft_batches', batch)
            return batch

    def prepare_draft_storage(self, draft_id, revision, run):
        draft = copy.deepcopy(self.store.get('drafts', draft_id))
        if draft['revision'] != revision:
            raise ValueError('稿件已被修改，本批次保留新版本供下次提交')
        self.assert_editable(draft_id)
        original_article = copy.deepcopy(draft['article'])
        draft['article']['title'] = short_title(draft)
        draft['article']['digest'] = re.sub(r'\s+', ' ', draft['article']['digest']).strip()[:120]
        draft['article']['markdown'] = re.sub(r'(?im)\n\s*[*_]*(?:本文|本稿)(?:由|使用|采用|经)\s*AI\s*(?:辅助|生成|整理|撰写)[^\n]{0,120}[*_]*\s*$', '', draft['article']['markdown']).strip()
        draft['author'] = self.publication_rules()['author']
        validate_article(draft['article'], draft['evidence'])
        story = self.store.get('stories', draft['story_id'])
        if story.get('text'):
            validate_evidence(draft['evidence'], story)
        # Explicit batch click authorizes local completion for the current generated version.
        draft.update(review_hash=content_hash(draft['article']), review={'passed': True, 'score': None, 'mode': 'off', 'issues': [],
                     'summary': '批量建草稿：复用采编材料，完成本地引用与格式检查；未调用复审模型。'})
        self.verify_source(draft)
        if original_article != draft['article']:
            draft.setdefault('format_repairs', []).append({'at': now(), 'original_title': original_article['title'], 'title': draft['article']['title'], 'reason': '适配微信标题／摘要字段'})
            draft['revision'] += 1
            if draft.get('visuals'):
                draft['visuals']['article_hash'] = content_hash(draft['article'])
        self.prepare_visuals(draft, None)
        validate_article(draft['article'], draft['evidence'])
        self.seal_checks(draft)
        draft.update(status='pending_human', approved_hash=None, updated_at=now())
        with self.lock:
            current = self.store.get('drafts', draft_id)
            if current['revision'] != revision:
                raise ValueError('稿件在格式处理时被人工更新，未覆盖新版本')
            self.store.put('drafts', draft)
        return draft

    def run_draft_batch(self, job):
        batch = self.store.get('draft_batches', job['subject'])
        run = self.new_run('draft_batch', job)
        batch.update(status='running', run_id=run['id'])
        self.store.put('draft_batches', batch)
        try:
            for entry in batch['entries']:
                self.controls.checkpoint()
                if entry['status'] != 'queued':
                    continue
                try:
                    if batch['account'] != self.settings.wechat_app_id:
                        raise ValueError('公众号已切换，停止向原账号提交')
                    draft = self.store.get('drafts', entry['draft_id'])
                    if draft['revision'] != entry['revision']:
                        raise ValueError('版本已更新，请在下一批提交新版本')
                    records = self.publication_for_draft(draft['id'], draft['revision'])
                    records = [record for record in records if record.get('account') == batch['account']]
                    existing = (next((record for record in records if record['status'] == 'unknown' or record.get('intent')), None)
                                or next((record for record in records if record.get('media_id') or record.get('publish_id')), None)
                                or next(iter(records), None))
                    if existing:
                        if existing['status'] == 'unknown' or existing.get('intent'):
                            existing = self.check_publication(existing['id'])
                            entry.update(status='needs_check', publication_id=existing['id'], error=existing.get('error') or '已有请求结果待核对；不重复创建草稿')
                            if existing.get('media_id') and existing['status'] in {'draft_only', 'draft_created'}:
                                entry.update(status='saved', media_id=existing['media_id'])
                            continue
                        if existing.get('media_id') and existing['status'] in {'draft_only', 'draft_created'}:
                            entry.update(status='saved', media_id=existing['media_id'], publication_id=existing['id'])
                            continue
                        if existing.get('media_id') or existing.get('publish_id'):
                            entry.update(status='needs_check', publication_id=existing['id'],
                                         error='历史记录已有微信草稿或发布编号；请先在公众号后台核对，系统不会重复创建')
                            continue
                        if existing['status'] in {'prepared', 'failed', 'blocked'} and not existing.get('media_id'):
                            self.finish_publication(existing, 'cancelled', error='未建立草稿，按本次批量格式适配重新准备')
                    draft = self.prepare_draft_storage(draft['id'], draft['revision'], run)
                    entry['revision'] = draft['revision']
                    self.controls.checkpoint()
                    publication = self.submit_wechat_draft(draft['id'], draft['revision'], enqueue_job=False)
                    publication['batch_id'] = batch['id']
                    self.store.put('publications', publication)
                    entry['publication_id'] = publication['id']
                    self.store.put('draft_batches', batch)
                    publication = self.run_publication(publication['id'])
                    # Only a definite platform rejection can be repaired/retried. Unknown results never replay writes.
                    if publication['status'] == 'failed' and publication.get('errcode') in {45002, 45003, 45004, 45005, 40007} and not publication.get('media_id') and not publication.get('format_retry'):
                        publication.update(status='prepared', intent='', target_mode='draft', format_retry=1, error='')
                        self.store.put('publications', publication)
                        self.store.event(draft['id'], 'draft_format_retry', '平台明确拒绝本次格式；本地压缩字段／排版后只重试一次，不调用文字模型')
                        self.controls.checkpoint()
                        publication = self.run_publication(publication['id'])
                    entry.update(publication_id=publication['id'], status='saved' if publication['status'] == 'draft_only' else 'needs_check' if publication['status'] == 'unknown' else 'failed',
                                 media_id=publication.get('media_id', ''), error=publication.get('error', ''))
                    if publication.get('errcode') in {40013, 40125, 40164, 45009, 45011, 48001} and publication['status'] in {'failed', 'blocked'}:
                        # These prerequisites are shared by this account, not repairable article formatting.
                        batch['blocker'] = {'errcode': publication['errcode'], 'error': publication['error']}
                        for remaining in batch['entries']:
                            if remaining['status'] == 'queued':
                                remaining.update(status='blocked', error='尚未提交：' + publication['error'])
                        break
                except Exception as exc:
                    entry.update(status='failed', error=self.safe_error(exc))
                    import traceback
                    entry['diagnostic'] = {'type': type(exc).__name__, 'frames': [
                        {'file': frame.filename.replace('\\', '/').rsplit('/', 1)[-1], 'line': frame.lineno, 'function': frame.name}
                        for frame in traceback.extract_tb(exc.__traceback__)[-5:]]}
                finally:
                    batch.update(saved=sum(e['status'] == 'saved' for e in batch['entries']), failed=sum(e['status'] == 'failed' for e in batch['entries']),
                                 blocked=sum(e['status'] == 'blocked' for e in batch['entries']),
                                 needs_check=sum(e['status'] == 'needs_check' for e in batch['entries']), updated_at=now())
                    self.store.put('draft_batches', batch)
                    current = self.store.get('runs', run['id'])
                    current.update(message=f"批量草稿 {batch['saved']} 已建立 / {batch['total']} 篇；失败 {batch['failed']}，待处理 {batch['blocked']}，待核对 {batch['needs_check']}",
                                   progress=round(100 * sum(e['status'] != 'queued' for e in batch['entries']) / max(1, batch['total'])))
                    self.store.put('runs', current)
            batch.update(status='blocked' if batch.get('blocker') else 'partial' if batch['failed'] or batch['needs_check'] else 'done', finished_at=now())
            self.store.put('draft_batches', batch)
            self.finish_run(run, batch['status'], generated=batch['saved'])
        except WorkflowCancelled:
            for entry in batch['entries']:
                if not entry.get('publication_id'):
                    continue
                publication = self.store.get('publications', entry['publication_id'])
                if publication['status'] == 'prepared' and not publication.get('intent') and not publication.get('media_id'):
                    self.finish_publication(publication, 'cancelled', error='批次已中断；未发起建草稿请求，等待用户再次批量提交')
                    entry.update(status='cancelled', error='后续步骤已中断')
            batch.update(status='cancelled', finished_at=now())
            self.store.put('draft_batches', batch)
            raise
