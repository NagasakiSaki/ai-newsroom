"""候选召回不是确认；模型对照后逐字验证引用，规则决定是否放行。"""
import re
from datetime import datetime, timedelta, timezone

from .db import now
from .config import sources
from .models import Crosscheck, OutputCheckError
from .source_quality import assessment, as_date, policy

STOP = set('the a an and or to of in for on with from new ai artificial intelligence model models introduces introducing launch launches released release using how we our is are its this that has have will can more than'.split())
ALIASES = {'Google': {'google', 'gemini', 'deepmind'}, 'OpenAI': {'openai', 'gpt', 'chatgpt'}, 'Anthropic': {'anthropic', 'claude'}, 'Microsoft': {'microsoft', 'github', 'copilot'}, 'Amazon': {'amazon', 'aws', 'bedrock'}, 'Apple': {'apple', 'mlx'}}
ALIASES.update({'NVIDIA': {'nvidia', 'nemotron'}, 'Meta': {'meta', 'llama'}, 'MiniMax': {'minimax', 'm3.1'},
                'Alibaba': {'qwen', 'alibaba'}, 'DeepSeek': {'deepseek'}})


class CorroborationPending(ValueError):
    pass


def terms(text):
    english = set(re.findall(r'[a-z][a-z0-9.-]{2,}', text.lower())) - STOP
    chinese = set()
    for phrase in re.findall(r'[\u4e00-\u9fff]+', text):
        chinese.update(phrase[i:i+2] for i in range(len(phrase)-1))
    return english | chinese


def near_copy(left, right):
    def grams(text):
        clean = re.sub(r'\W+', '', text.lower())[:12000]
        return {clean[i:i+8] for i in range(max(0, len(clean)-7))}
    a, b = grams(left), grams(right)
    return bool(a and b and len(a & b) / max(1, len(a | b)) >= 0.9)


class CrossSource:
    def related_stories(self, anchor, pool=None, catalog=None, rules=None):
        rules = rules or policy()
        catalog = catalog or sources()
        def lookup(item):
            return next((s for s in catalog if s['id'] == item.get('source_id') or s['name'] == item.get('source')), None)
        anchor_source = lookup(anchor)
        owner = anchor_source.get('organization', '') if anchor_source else ''
        keys = terms(anchor['title'])
        anchor_text = (anchor['title'] + ' ' + anchor.get('excerpt', '')).lower()
        aliases = {key for words in ALIASES.values() for key in words if key in anchor_text}
        aliases |= ALIASES.get(owner, set())
        scored = []
        for item in pool if pool is not None else self.store.all('stories'):
            if item['id'] == anchor['id'] or item.get('demo') or not as_date(item.get('published_at')):
                continue
            if abs((as_date(item['published_at']) - as_date(anchor['published_at'])).total_seconds()) > rules['crosscheck_max_age_hours'] * 3600:
                continue
            source = lookup(item)
            if not source:
                continue
            if not assessment(source, item, corroboration=True, rules=rules)['auto_eligible']:
                continue
            text = (item['title'] + ' ' + item.get('excerpt', '')).lower()
            overlap = keys & terms(text)
            linked = anchor['url'].rstrip('/') in text or item['url'].rstrip('/') in anchor_text
            entity = any(re.search(r'\b' + re.escape(a) + r'\b', text) for a in aliases)
            content_overlap = overlap - aliases
            if not linked and len(content_overlap) < 2 and not (entity and content_overlap):
                continue
            score = len(overlap) + (10 if linked else 0) + (2 if source['tier'] in {'primary', 'research'} else 0)
            scored.append((score, item, source['organization']))
        # 同一发布机构最多两个候选，留出位置给其他编辑部。
        result, counts = [], {}
        for _, item, owner in sorted(scored, key=lambda pair: pair[0], reverse=True):
            if counts.get(owner, 0) >= 2:
                continue
            result.append(item)
            counts[owner] = counts.get(owner, 0) + 1
            if len(result) >= rules['crosscheck_candidate_limit']:
                break
        return result

    def index_events(self):
        pool, catalog, rules = self.store.all('stories'), sources(), policy()
        for item in pool:
            if item.get('demo') or not as_date(item.get('published_at')) or datetime.now(timezone.utc) - as_date(item['published_at']) > timedelta(hours=72):
                continue
            related = self.related_stories(item, pool=pool, catalog=catalog, rules=rules)
            self.store.put('event_candidates', {'id': item['id'], 'related_ids': [r['id'] for r in related], 'updated_at': now(), 'note': '机器召回候选，尚未确认同一事件'})

    def event_support(self):
        """候选组里出现的其他机构数量，只作为排序用的热度信号，不构成旁证。"""
        pool = {item['id']: item for item in self.store.all('stories')}

        def organization(item):
            return (item.get('source_report') or {}).get('organization') or item.get('source') or ''

        support = {}
        for group in self.store.all('event_candidates'):
            members = [pool[i] for i in [group.get('id'), *(group.get('related_ids') or [])] if i in pool]
            if len(members) < 2:
                continue
            organizations = {organization(member) for member in members}
            for member in members:
                others = organizations - {organization(member)}
                if organization(member) != '论文作者':
                    others.discard('论文作者')
                support[member['id']] = max(support.get(member['id'], 0), len(others))
        return support

    def crosscheck_story(self, anchor, evidence, run):
        if anchor.get('demo'):
            return {'state': 'demo', 'passed': True, 'checked_at': now(), 'sources': [], 'summary': '演示数据，不代表执行了真实多信源验证', 'warnings': ['真实模式需要其他来源的原文对照；演示不伪造独立报道。']}
        candidates = self.related_stories(anchor)
        fast = self.settings.fast_mode and self.settings.review_mode != 'strict'
        if fast and (anchor.get('crosscheck') or {}).get('state') == 'conflict':
            raise CorroborationPending('已有事实冲突记录；快速采编不绕过已发现的冲突，请人工核实或改用完整核对')
        source_limit = 1 if fast else policy()['crosscheck_max_sources']
        loaded, failures = [], []
        for candidate in candidates[:2] if fast else candidates:
            try:
                self.prepare_source(candidate, corroboration=True)
                if len(candidate.get('text', '')) < 120:
                    raise ValueError('对照来源正文过短')
                loaded.append(candidate)
            except Exception as exc:
                failures.append({'story_id': candidate['id'], 'source': candidate['source'], 'error': self.safe_error(exc)})
            if len(loaded) >= source_limit:
                break
        if loaded:
            compared_evidence = {**evidence, 'facts': evidence['facts'][:5]} if fast else evidence
            context = {'anchor': {k: anchor[k] for k in ('id', 'title', 'url')} | {'text': anchor['text'][:12000]}, 'evidence': compared_evidence,
                       'candidates': [{k: item.get(k) for k in ('id', 'title', 'url', 'source', 'published_at', 'source_snapshot')} | {'text': item['text'][:policy()['crosscheck_source_chars']]} for item in loaded], 'content_note': '正文有长度限制，未提及不代表事实不存在，不得据此否定主消息。'}
            validate = lambda output: self.validate_crosscheck(anchor, evidence, loaded, output)
            if self.settings.review_mode == 'strict':
                result = self.model.complete('crosscheck', context, run, validator=validate)
            else:
                partials = []
                for candidate in context['candidates']:
                    try:
                        partials.append(self.model.complete('crosscheck', {**context, 'candidates': [candidate]}, run, validator=validate))
                    except ValueError as exc:
                        failures.append({'story_id': candidate['id'], 'source': candidate['source'], 'stage': 'crosscheck', 'error': self.safe_error(exc)})
                        self.store.event(anchor['id'], 'crosscheck_partial', '部分模型对照未完成；保留已有有效对照，按主信源归因，不声称完成独立验证')
                relations = {r['anchor_relationship'] for r in partials} - {'unknown'}
                result = {'anchor_relationship': relations.pop() if len(relations) == 1 else 'unknown',
                          'sources': [row for r in partials for row in r['sources']],
                          'summary': '；'.join(r['summary'] for r in partials) or '模型对照未完成，未取得有效旁证结果'}
        else:
            result = {'sources': [], 'summary': '没有可读取的同事件对照候选'}
        report = self.validate_crosscheck(anchor, evidence, loaded, result)
        report.update(failures=failures, candidate_count=len(candidates))
        if fast:
            report.update(check_mode='fast', checked_source_count=len(report['sources']))
            report['warnings'].append('快速采编最多对照一家可读候选，未声称完成全部来源检查。')
        anchor['crosscheck'] = report
        self.store.put('stories', anchor)
        if not report['passed']:
            raise CorroborationPending(report['summary'])
        return report

    def validate_crosscheck(self, anchor, evidence, candidates, result):
        result = Crosscheck.model_validate(result).model_dump()
        by_id = {item['id']: item for item in candidates}
        facts = {f['id']: f for f in evidence['facts']}
        owner = self.source_for(anchor)['organization']
        matches, seen, conflicts, useful, bodies = [], set(), [], [], []
        for row in result['sources']:
            if row['story_id'] not in by_id or row['story_id'] in seen:
                raise OutputCheckError('对照模型引用了未知或重复信源', 'unknown_or_duplicate_source', 'sources')
            seen.add(row['story_id'])
            item = by_id[row['story_id']]
            source = self.source_for(item)
            if near_copy(anchor['text'], item['text']) or any(near_copy(body, item['text']) for body in bodies):
                row['relationship'] = 'syndicated'
                row['reason'] += '；程序检测到正文高度重复，按转载处理'
            bodies.append(item['text'])
            for comparison in row['comparisons']:
                if comparison['fact_id'] not in facts:
                    raise OutputCheckError('对照报告引用了不存在的事实编号', 'unknown_fact_id', 'sources.comparisons')
                if comparison['judgment'] != 'not_mentioned' and (len(comparison['quote']) < 8 or comparison['quote'] not in item['text']):
                    raise OutputCheckError('对照摘录并非原文连续片段，停止采编', 'comparison_quote_mismatch',
                                           'sources.' + item['id'] + '.comparisons.' + comparison['fact_id'] + '.quote')
                if comparison['judgment'] == 'not_mentioned':
                    comparison['quote'] = ''
            qualifies = row['same_event'] and row['confidence'] >= policy()['crosscheck_min_confidence']
            supported = any(c['judgment'] == 'supports' and c['fact_id'] == evidence['facts'][0]['id'] for c in row['comparisons'])
            row.update(url=item['url'], title=item['title'], source=item['source'], organization=source['organization'], tier=source['tier'], snapshot=item['source_snapshot'])
            row['counted_as_corroboration'] = bool(qualifies and supported and source['organization'] != owner and row['relationship'] not in {'unknown', 'syndicated'})
            if qualifies and any(c['judgment'] == 'contradicts' for c in row['comparisons']):
                conflicts.append(row['story_id'])
            if row['counted_as_corroboration']:
                useful.append(row)
            matches.append(row)
        anchor_source = self.source_for(anchor)
        primary_anchor = anchor_source['tier'] in {'primary', 'research'}
        has_primary = primary_anchor or any(r['tier'] in {'primary', 'research'} for r in useful)
        independent = [r for r in useful if r['tier'] == 'secondary' and r['relationship'] == 'original_reporting']
        # 多站同稿只证明重复刊载；二手起稿需找到一手或另一家独立原创报道。
        enough = bool(useful) if primary_anchor else bool(has_primary or (independent and result['anchor_relationship'] == 'original_reporting'))
        # 机构官方公告本身是一手材料：允许在没有第二家机构报道时成稿，但必须标注为公司自述。
        # 媒体二手（小道消息）仍然要求另一家机构的同事件报道。
        official_only = bool(not conflicts and not enough and anchor_source['tier'] == 'primary'
                             and policy().get('allow_primary_announcement_only', False))
        media_only = bool(not conflicts and not enough and anchor_source['tier'] == 'secondary'
                          and self.settings.review_mode != 'strict' and policy().get('allow_trusted_media_only', False))
        passed = not conflicts and (enough or not policy()['require_corroboration'] or official_only or media_only)
        state = ('conflict' if conflicts else 'crosschecked' if enough
                 else 'primary_announcement' if official_only else 'media_report' if media_only else 'awaiting_corroboration')
        summary = ('多源对照存在事实冲突，暂停自动成稿，须人工核实' if conflicts
                   else '已找到其他机构的同事件支持材料；不等于独立性能验证' if enough
                   else '仅有机构官方公告，没有其他机构的同事件报道；稿件只陈述公告内容，公告中的性能与效果属于机构自述，不能视为独立验证' if official_only
                   else '按宽松采编设置采用已登记媒体的原文报道，稿件须归因于该媒体；不宣称完成独立验证' if media_only
                   else '缺少可核对的跨机构同事件旁证，保留线索等待补充，不自动成稿')
        warnings = ['同一机构多站、转载和公告改写不能计为多份独立证据。', '同事件与报道关系由模型辅助判断，引用逐字检查；仍需人工核对语义。', '媒体确认有此消息，不代表产品性能或论文结论得到独立复现。']
        if official_only:
            warnings.append('本条只有官方一手公告，没有独立报道；标题与摘要要保留“官方宣布”的限定，不得写成已被独立验证。')
        return {'state': state, 'passed': passed, 'checked_at': now(), 'sources': matches, 'summary': summary,
                'model_summary': result['summary'], 'has_primary': has_primary, 'supporting_organizations': sorted({owner} | {r['organization'] for r in useful}),
                'anchor_relationship': result['anchor_relationship'],
                'independent_reporting_count': len({r['organization'] for r in independent} | ({owner} if not primary_anchor and result['anchor_relationship'] == 'original_reporting' else set())), 'conflicts': conflicts,
                'primary_announcement_only': official_only, 'warnings': warnings}

    def verify_crosscheck(self, draft):
        if draft.get('demo'):
            return
        report = draft.get('crosscheck') or {}
        acceptable = {'crosschecked'}
        if policy().get('allow_primary_announcement_only', False):
            acceptable.add('primary_announcement')
        if self.settings.review_mode != 'strict' and policy().get('allow_trusted_media_only', False):
            acceptable.add('media_report')
        if not report.get('passed') or (policy()['require_corroboration'] and report.get('state') not in acceptable):
            raise ValueError('当前稿件缺少通过的多源对照报告，请重新撰稿')
        for row in report['sources']:
            if not row.get('same_event') or row['confidence'] < policy()['crosscheck_min_confidence']:
                continue
            item = self.prepare_source(self.store.get('stories', row['story_id']), corroboration=True)
            current = item['source_snapshot']
            if current['body_hash'] != row['snapshot']['body_hash']:
                quotes = [c.get('quote') or '' for c in row.get('comparisons') or [] if c.get('judgment') != 'not_mentioned']
                if self.source_changed(row['snapshot'], current, quotes, item.get('text') or ''):
                    raise ValueError('对照信源原文已变化，请重新对照并审核')
