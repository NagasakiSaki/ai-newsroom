"""轻量模型输入、明确输出契约与可复用的结构化阶段缓存。"""
import hashlib
import json
import re


def clean_json_text(value):
    if not isinstance(value, str):
        raise ValueError('模型未返回文本 JSON')
    value = value.lstrip('\ufeff').strip()
    # Only unwrap one complete code fence. Never invent fields, fix quotes or evaluate code.
    fence = re.fullmatch(r'```(?:json)?\s*([\s\S]*?)\s*```', value, re.I)
    return fence.group(1).strip() if fence else value


def stable(value):
    volatile = {'checked_at', 'collected_at', 'updated_at', 'last_seen_at', 'last_checked_at', 'last_success_at'}
    if isinstance(value, dict):
        return {k: stable(v) for k, v in value.items() if k not in volatile}
    if isinstance(value, list):
        return [stable(v) for v in value]
    return value


def compact_context(stage, context):
    context = dict(context)
    if stage in {'plan_auto', 'plan_article', 'prompt', 'write', 'rewrite', 'visual', 'expand_article', 'polish'}:
        if context.get('editorial'):
            context['editorial'] = {k: v for k, v in context['editorial'].items() if k in {
                'account_name', 'audience', 'positioning', 'style', 'article_length', 'commercial_policy'}}
        if context.get('evidence'):
            context['evidence'] = {k: v for k, v in context['evidence'].items() if k != 'uncertainties'}
        if context.get('source_snapshot'):
            context['source_snapshot'] = {k: v for k, v in context['source_snapshot'].items()
                                          if k in {'published_at', 'organization', 'tier', 'url', 'date_precision'}}
        if context.get('crosscheck'):
            report = context['crosscheck']
            context['crosscheck'] = {'state': report.get('state'), 'sources': [
                {k: row.get(k) for k in ('source', 'title', 'url', 'relationship', 'comparisons')}
                for row in report.get('sources', []) if row.get('same_event')]}
    if stage in {'score', 'extract', 'prepare_article'} and 'news' in context:
        keys = ('id', 'title', 'url', 'source', 'kind', 'published_at', 'source_report', 'excerpt')
        if stage in {'extract', 'prepare_article'}:
            keys = (*keys, 'text')
        context['news'] = {k: context['news'][k] for k in keys if k in context['news']}
        if context['news'].get('source_report'):
            context['news']['source_report'] = {k: v for k, v in context['news']['source_report'].items()
                                                if k in {'tier', 'organization', 'date_precision', 'date_basis'}}
    if stage == 'review' and context.get('visuals'):
        visual = context['visuals']
        context['visuals'] = {k: visual.get(k) for k in ('state', 'plan')}
        context['visuals']['assets'] = [{k: a.get(k) for k in ('kind', 'title', 'credit', 'fact_ids', 'source_url')}
                                        for a in visual.get('assets', [])]
    return context


def cache_key(stage, context, route, instructions, schema):
    if stage not in {'score', 'score_batch', 'extract', 'crosscheck', 'angle', 'plan', 'plan_auto', 'plan_article', 'prepare_article', 'prompt', 'expand_article'} and not (
            stage == 'write' and context.get('autonomous_article') and context.get('cache_body')):
        return None  # Explicitly rewriting body/review must make a fresh model call.
    material = {'stage': stage, 'context': stable(context), 'model': route['model'], 'base': route['base_url'],
                'instructions': instructions, 'schema': schema, 'version': 1}
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def contract(stage, context, schema):
    keys = list(schema.get('properties', {}))
    text = '输出顶层字段仅为：' + ', '.join(keys) + '。不得改名、嵌套另一个结果对象或输出 Markdown 代码围栏。'
    if stage == 'crosscheck' and context.get('candidates'):
        ids = [c['id'] for c in context['candidates']]
        text += ' sources 只能放对照候选，不包含 anchor。必须逐一返回且仅返回这些 story_id：' + json.dumps(ids) + '。'
        text += ' 每个来源使用 story_id/same_event/confidence/relationship/reason/comparisons；每条 comparisons 使用 fact_id/judgment/quote/explanation。'
        text += ' 原文未提及填 not_mentioned 和空 quote；不同事件允许 comparisons=[]。逐字摘录保持原文语言，不译为中文。'
    if stage == 'extract' and context.get('news'):
        text += ' 每条 source_url 必须原样使用：' + context['news']['url'] + '。quote 复制原文连续片段，保留原文语言。'
    if stage == 'score_batch':
        text += ' scores 必须逐一且仅返回这些 story_id，每项包含 story_id 和 score（五维分数与reason）：' + json.dumps([n['id'] for n in context['news_items']])
    if stage == 'plan_article':
        text += ' writing_prompt 是一个对象，含 system_prompt 和 user_prompt 两个完整字符串；检查表是可选字段，不要把 writing_prompt 写成字符串。'
    return text
