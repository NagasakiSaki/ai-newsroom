from datetime import datetime, timedelta, timezone

import pytest

from newsroom.config import Settings, sources
from newsroom.db import now
from newsroom.engine import Engine

QUOTE = 'Anthropic said that it will commit 5% of its shares to the fund'


@pytest.fixture
def engine(tmp_path):
    return Engine(Settings(data_dir=tmp_path, admin_token='test'))


def fixture(engine, text=QUOTE + ' and more details about the structure. ' * 20):
    story = {'id': 's1', 'source_id': 'techcrunch-ai', 'source': 'TechCrunch AI', 'title': 'Anthropic 宣布公益基金',
             'url': 'https://techcrunch.com/2026/10/18/test/', 'published_at': now(), 'date_basis': 'published',
             'demo': False, 'status': 'drafted', 'text': text}
    engine.store.put('stories', story)
    draft = {'id': 'd1', 'story_id': 's1', 'demo': False, 'revision': 1, 'status': 'ready_to_publish',
             'article': {'title': 'Anthropic 宣布设立公益基金', 'digest': '机构公告',
                         'markdown': '正文内容。' * 40},
             'evidence': {'summary': '宣布基金',
                          'facts': [{'id': 'F1', 'text': '宣布基金', 'source_url': story['url'], 'quote': QUOTE}],
                          'uncertainties': []},
             'source_snapshot': {'body_hash': 'old', 'body_length': len(text), 'published_at': now(),
                                 'checked_at': now(), 'max_age_hours': 24, 'tier': 'secondary',
                                 'warnings': [], 'url': story['url']},
             'crosscheck': {'passed': True, 'state': 'crosschecked', 'sources': []}}
    engine.store.put('drafts', draft)
    return story, draft


def prepare(engine, text):
    def run(item, corroboration=False):
        item['text'] = text
        item['source_snapshot'] = {'body_hash': 'new', 'body_length': len(text), 'published_at': now(),
                                   'checked_at': now(), 'max_age_hours': 24, 'tier': 'secondary',
                                   'warnings': [], 'url': item['url']}
        engine.store.put('stories', item)
        return item
    return run


def test_page_churn_with_all_quotes_present_is_tolerated(engine):
    # 站点加了推荐位/广告位：整页哈希变了，但稿件引用的摘录都还在、篇幅相近。
    story, draft = fixture(engine)
    engine.prepare_source = prepare(engine, story['text'] + ' 本页新增推荐位与广告位。')
    result = engine.verify_source(engine.store.get('drafts', 'd1'), force=True)
    assert result['source_snapshot']['page_changed'] is True


def test_missing_quote_blocks_delivery(engine):
    story, draft = fixture(engine)
    engine.prepare_source = prepare(engine, '完全不同的文章内容，没有原来的引文。' * 20)
    with pytest.raises(ValueError, match='引用的摘录'):
        engine.verify_source(engine.store.get('drafts', 'd1'), force=True)
    assert engine.store.get('drafts', 'd1')['status'] == 'needs_source_review'


def test_replaced_page_with_quote_but_very_different_length_blocks(engine):
    story, draft = fixture(engine)
    engine.prepare_source = prepare(engine, story['text'] * 3)
    with pytest.raises(ValueError, match='原文已改变'):
        engine.verify_source(engine.store.get('drafts', 'd1'), force=True)


def test_discovery_only_item_is_promoted_when_now_eligible(engine):
    source = next(s for s in sources() if s['id'] == 'anthropic-news')
    item = {'id': 'old', 'url': 'https://www.anthropic.com/news/old', 'source_id': 'anthropic-news',
            'source': source['name'], 'title': '官方公告',
            'published_at': (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat(),
            'date_basis': 'published_date_only', 'demo': False, 'status': 'discovery_only'}
    engine.store.put('stories', item)
    engine.refresh_source_reports()
    promoted = engine.store.get('stories', 'old')
    assert promoted['status'] == 'new' and promoted['source_report']['auto_eligible']
