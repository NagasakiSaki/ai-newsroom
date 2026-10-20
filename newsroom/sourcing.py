from datetime import datetime, timedelta, timezone

from . import collect
from .config import sources
from .db import now
from .source_quality import assessment, snapshot, policy, as_date, validate_claims
from .editorial_team import image_candidates
from .models import content_hash


class SourceControl:
    def source_catalog(self):
        from .source_catalog import catalog
        return catalog(self.store)

    def collection_settings(self):
        from .source_catalog import configuration
        return configuration(self.store)

    def writing_rules(self):
        from .source_catalog import writing_rules
        return writing_rules(self.store)

    def collect_catalog(self, max_age, log, health=None):
        from .source_catalog import matches
        from .crawler import crawl_source
        config = self.collection_settings()
        catalog = self.source_catalog()
        def accepted(batch):
            return [item for item in batch if matches(item['title'] + ' ' + item.get('feed_text', '') + ' ' + item.get('excerpt', ''), config)
                    and (not config.max_age_hours or as_date(item.get('published_at')) and
                         datetime.now(timezone.utc) - as_date(item['published_at']) <= timedelta(hours=config.max_age_hours))]
        incoming = collect.collect([s for s in catalog if not s.get('custom')], max_age, log, health=health,
            settings=self.settings, checkpoint=self.controls.checkpoint, on_items=lambda batch: self.ingest(accepted(batch)))
        incoming = accepted(incoming)
        for source in catalog:
            if not source.get('custom') or not source.get('enabled', True):
                continue
            self.controls.checkpoint()
            batch, report = crawl_source(source, config, self.settings, self.controls.checkpoint,
                                         on_item=lambda item: self.ingest([item]))
            incoming.extend(batch)
            if health:
                health(report)
            log('source_error' if report['status'] == 'error' else 'collect',
                source['name'] + '：内置爬虫保存 ' + str(len(batch)) + ' 条；检查 ' + str(report['pages_fetched']) + ' 页；未调用模型')
        return incoming

    def recent_collection(self):
        """几分钟内刚采集过时复用线索池，避免点击写稿又串行重抓全部信源。"""
        for run in self.store.all('runs'):
            if run.get('kind') != 'collection' or run.get('status') not in {'done', 'partial'}:
                continue
            from .source_catalog import fingerprint
            config = self.collection_settings()
            if run.get('collection_fingerprint') != fingerprint(config) and (config.revision or run.get('collection_fingerprint')):
                continue
            finished = as_date(run.get('finished_at'))
            if finished and timedelta(0) <= datetime.now(timezone.utc) - finished < timedelta(minutes=5):
                return run
        return None

    def source_for(self, item):
        source = next((s for s in self.source_catalog() if s['id'] == item.get('source_id') or s['name'] == item.get('source')), None)
        if not source:
            raise ValueError('信源未登记，不能自动撰稿')
        return source

    def health_update(self, report):
        try:
            previous = self.store.get('source_health', report['id'])
        except KeyError:
            previous = {}
        self.store.put('source_health', {**previous, **report, 'consecutive_errors': 0 if report['status'] in {'ok', 'partial'} else previous.get('consecutive_errors', 0) + 1})

    def ingest(self, incoming):
        existing = {s['id']: s for s in self.store.all('stories')}
        for item in incoming:
            old = existing.get(item['id'])
            if old:
                # RSS 的更新时间不能覆盖首次发表时间，也不能重置工作状态。
                old.update(last_seen_at=now(), updated_at=item.get('updated_at'), source_id=item.get('source_id', old.get('source_id')))
                if item.get('feed_text'):
                    old.update({key: item[key] for key in ('feed_html', 'feed_text', 'feed_snapshot')})
                if item.get('custom_source'):
                    old.update({key: item[key] for key in ('title', 'source', 'custom_source', 'text', 'excerpt', 'crawl_html', 'crawl_snapshot', 'source_report')})
                    if not old.get('published_at') and item.get('published_at'):
                        old.update(published_at=item['published_at'], date_basis=item['date_basis'], date_precision=item['date_precision'])
                self.store.put('stories', old)
            else:
                duplicate = next((s for s in existing.values() if s.get('demo') == item.get('demo')
                    and as_date(s.get('published_at')) and as_date(item.get('published_at'))
                    and abs((as_date(s['published_at']) - as_date(item['published_at'])).total_seconds()) < 72 * 3600
                    and collect.similar(s['title'], item['title'])), None)
                if duplicate:
                    item.update(status='duplicate', duplicate_of=duplicate['id'])
                self.store.put('stories', item)
                existing[item['id']] = item

    def collect_sources(self, job=None):
        run = self.new_run('collection', job=job)
        from .source_catalog import fingerprint
        run.update(stage='collect', progress=5, message='正在采集登记网址与订阅，不调用模型',
                   collection_fingerprint=fingerprint(self.collection_settings()))
        self.store.put('runs', run)
        errors = []
        reports = []
        def update_health(report):
            reports.append(report)
            self.health_update(report)
        def log(stage, message):
            self.store.event(run['id'], stage, message)
            if stage == 'source_error':
                errors.append(message)
        try:
            incoming = self.collect_catalog(policy()['paper_max_age_hours'], log, health=update_health)
            self.ingest(incoming)
            self.index_events()
            status = 'failed' if reports and all(r['status'] == 'error' for r in reports) else 'partial' if errors or any(r['status'] == 'partial' for r in reports) else 'done'
            self.finish_run(run, status, collected=len(incoming), source_failures=len(errors), progress=100, stage='finished',
                            message=f'信源采集结束：保存 {len(incoming)} 条信息；{len(errors)} 个来源失败；未调用模型')
        except Exception:
            self.finish_run(run, 'failed')
            raise
        return incoming

    def collection_tick(self):
        if self.controls.state().get('dispatch_paused'):
            return
        try:
            plan = self.store.get('collection', 'main')
        except KeyError:
            plan = {'id': 'main', 'next_run_at': now()}
        if as_date(plan['next_run_at']) <= datetime.now(timezone.utc):
            self.enqueue('collect', trigger='collection_schedule')
            plan.update(next_run_at=(datetime.now(timezone.utc) + timedelta(minutes=self.settings.collection_interval_minutes)).isoformat(), interval_minutes=self.settings.collection_interval_minutes)
            self.store.put('collection', plan)

    def prepare_source(self, item, corroboration=False):
        if item.get('demo'):
            item['source_snapshot'] = {'state': 'demo', 'tier': 'demo', 'warnings': ['虚构演示，不是真实新闻核验']}
            return item
        source = self.source_for(item)
        import httpx
        from .source_quality import identity
        identity(source, item['url'])
        method = 'article_page'
        try:
            cached = item.get('crawl_snapshot') or {}
            checked = as_date(cached.get('checked_at'))
            if source.get('custom') and checked and datetime.now(timezone.utc) - checked < timedelta(hours=24) and item.get('crawl_html'):
                document = {'html': item['crawl_html'], 'url': cached['url']}
                method = 'crawler_snapshot'
            else:
                document = collect.fetch_source(source, item['url'], settings=self.settings, document=True)
            if document.get('route_fallback'):
                self.store.event(item['id'], 'source_route', '新闻原文直连失败，使用已配置新闻代理重试；微信路由不变')
            if source.get('custom'):
                from .crawler import extract
                parsed = extract(document['html'], document['url'], source)
                text = parsed['text'] if parsed else ''
            else:
                text = collect.plain(document['html'])[:24000]
            if len(text) < 120:
                raise ValueError('页面正文过短')
        except (httpx.HTTPError, ValueError) as exc:
            provenance = item.get('feed_snapshot') or {}
            if (len(item.get('feed_text', '')) < 300 or provenance.get('source_id') != source['id'] or
                    provenance.get('feed_url') != source['url'] or provenance.get('article_url') != item['url']):
                raise
            # Use content openly syndicated by the registered publisher, not invented page text.
            document = {'html': '<article>' + item['feed_html'] + '</article>', 'url': item['url']}
            text = item['feed_text']
            method = 'publisher_feed'
            self.store.event(item['id'], 'source_fallback', '原文页暂不可用，采用该发布机构公开RSS提供的正文；未绕过登录或付费限制')
        item['source_snapshot'] = snapshot(source, item, document['html'], text, document['url'], corroboration=corroboration)
        item['source_snapshot']['fetch_method'] = method
        if method == 'publisher_feed':
            item['source_snapshot'].update(feed_url=source['url'], page_date_verified=False)
        item['text'] = text
        item['image_candidates'] = image_candidates(document['html'], document['url'], source)
        from .visual_discovery import official_links
        item['visual_links'] = official_links(document['html'], document['url'])
        item['image_page_checked_at'] = now()
        self.store.put('stories', item)
        return item

    @staticmethod
    def source_changed(previous, current, quotes, text):
        """页面是否被实质替换：引用摘录仍在且篇幅相近时，视为广告位等易变内容。"""
        old_length = previous.get('body_length') or 0
        new_length = current.get('body_length') or len(text)
        if old_length and new_length and abs(new_length - old_length) / max(old_length, 1) > 0.5:
            return True
        return any(quote and quote not in text for quote in quotes)

    def verify_source(self, draft, force=False, allow_cached=False):
        if draft.get('demo'):
            return draft
        if self.settings.drafts_only and not force and self.settings.fast_mode and self.settings.review_mode != 'strict':
            # Saving an already written article is archival delivery, not a new claim that the event is fresh.
            import hashlib
            from .source_quality import identity
            from .render import validate_article, validate_evidence
            item = self.store.get('stories', draft['story_id'])
            report = draft.get('source_snapshot') or {}
            source = self.source_for(item)
            if not source.get('enabled', True):
                raise ValueError('保存草稿失败：原信源已停用，请检查来源设置')
            identity(source, item['url'])
            text = item.get('text') or ''
            body_hash = hashlib.sha256(text.encode()).hexdigest()
            saved = item.get('source_snapshot') or {}
            equivalent_saved_page = (bool(report.get('body_hash')) and saved.get('body_hash') == body_hash
                and bool(saved.get('url')) and bool(report.get('url'))
                and collect.canonical(saved['url']) == collect.canonical(report['url']) == collect.canonical(item['url'])
                and saved.get('published_at') == report.get('published_at')
                and not self.source_changed(report, saved, [f['quote'] for f in draft['evidence']['facts']], text))
            if not text or (report.get('body_hash') != body_hash and not equivalent_saved_page):
                raise ValueError('保存草稿失败：本地原文快照缺失或已改变，请恢复原文材料或重新撰稿')
            if not (draft.get('crosscheck') or {}).get('passed'):
                raise ValueError('保存草稿失败：缺少已完成的信源对照材料')
            validate_article(draft['article'], draft['evidence'])
            validate_evidence(draft['evidence'], item)
            validate_claims(draft['article'], draft['evidence'], report)
            published = as_date(report.get('published_at'))
            draft['storage_check'] = {'mode': 'saved_material', 'checked_at': now(), 'online_rechecked': False,
                'body_hash': body_hash, 'equivalent_saved_page': report.get('body_hash') != body_hash,
                'published_at': report.get('published_at'),
                'outside_news_window': not published or datetime.now(timezone.utc) - published > timedelta(hours=report.get('max_age_hours', 24))}
            # The caller persists its complete, revision-guarded update. Never save half a format repair here.
            return draft
        report = draft.get('source_snapshot') or {}
        checked = as_date(report.get('checked_at'))
        published = as_date(report.get('published_at'))
        fresh = published and datetime.now(timezone.utc) - published <= timedelta(hours=report.get('max_age_hours', 24))
        def cached_available():
            import hashlib
            from .source_quality import identity
            item = self.store.get('stories', draft['story_id'])
            identity(self.source_for(item), item['url'])
            text = item.get('text') or ''
            return bool(text and report.get('body_hash') == hashlib.sha256(text.encode()).hexdigest() and
                        all(f['quote'] in text for f in draft['evidence']['facts']) and
                        (draft.get('crosscheck') or {}).get('passed'))
        try:
            seal = draft.get('generation_check') or {}
            if (not force and self.settings.fast_mode and self.settings.review_mode != 'strict' and fresh
                    and (seal.get('fingerprint') == self.final_hash(draft) or
                         draft.get('review_hash') == content_hash(draft['article']))
                    and cached_available()):
                # No paid model call or network refetch for an unchanged, still-current generation.
                from .render import validate_article, validate_evidence
                validate_article(draft['article'], draft['evidence'])
                validate_evidence(draft['evidence'], self.store.get('stories', draft['story_id']))
                validate_claims(draft['article'], draft['evidence'], report)
                return draft
            recheck = as_date(report.get('last_recheck_at'))
            if (allow_cached and fresh and report.get('verification_method') == 'cached_original' and recheck and
                    datetime.now(timezone.utc) - recheck < timedelta(minutes=5) and cached_available()):
                validate_claims(draft['article'], draft['evidence'], report)
                return draft
            if not force and fresh and checked and datetime.now(timezone.utc) - checked < timedelta(minutes=policy()['recheck_minutes']):
                validate_claims(draft['article'], draft['evidence'], report)
                return draft
            item = self.prepare_source(self.store.get('stories', draft['story_id']))
            current = item['source_snapshot']
            if not report.get('body_hash'):
                raise ValueError('缺少原文快照，请重新撰稿并审核')
            if current['body_hash'] != report['body_hash']:
                # 整页哈希对广告位、推荐位过于敏感：只要稿件引用的摘录都还在、篇幅相近，就按"页面有改动"继续。
                quotes = [fact.get('quote') or '' for fact in draft['evidence']['facts']]
                if self.source_changed(report, current, quotes, item.get('text') or ''):
                    raise ValueError('原文已改变：稿件引用的摘录不再存在，请重新撰稿并审核')
                current['page_changed'] = True
            validate_claims(draft['article'], draft['evidence'], current)
            self.verify_crosscheck(draft)
            draft['source_snapshot'] = current
            self.store.put('drafts', draft)
            return draft
        except Exception as exc:
            import httpx
            transient = isinstance(exc, httpx.TransportError) or (
                isinstance(exc, ValueError) and str(exc).startswith('独立HTTPS DNS暂不可用'))
            if allow_cached and transient and fresh:
                if cached_available():
                    validate_claims(draft['article'], draft['evidence'], report)
                    draft['source_snapshot'] = {**report, 'verification_method': 'cached_original',
                        'last_recheck_at': now(), 'last_recheck_error': self.safe_error(exc)}
                    self.store.put('drafts', draft)
                    self.store.event(draft['id'], 'source_cached_handoff', '原站暂时连接失败，本地交稿复用时效内已读取原文；未重新验证在线页面，自动审定暂停')
                    return draft
            draft.update(status='needs_source_review', approved_hash=None, delivery=None, error='信源复查未通过：' + self.safe_error(exc))
            self.store.put('drafts', draft)
            raise ValueError(draft['error']) from None
