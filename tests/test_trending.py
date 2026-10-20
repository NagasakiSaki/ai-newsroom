from datetime import datetime, timedelta, timezone

import pytest

from newsroom import trending
from newsroom.db import Store, now


def test_parsers_extract_titles_and_ranks():
    weibo = trending.parse_weibo({'data': {'realtime': [{'word': '某模型发布'}, {'note': '第二个'}]}})
    assert [row['title'] for row in weibo] == ['某模型发布', '第二个'] and weibo[1]['rank'] == 2
    zhihu = trending.parse_zhihu({'data': [{'target': {'title': '知乎热榜一', 'url': 'https://www.zhihu.com/question/1'}}]})
    assert zhihu == [{'source': 'zhihu', 'rank': 1, 'title': '知乎热榜一', 'url': 'https://www.zhihu.com/question/1'}]
    baidu = trending.parse_baidu({'data': {'cards': [{'content': [{'content': [
        {'word': '百度热榜一', 'url': 'https://top.baidu.com/1'}]}]}]}})
    assert baidu[0]['title'] == '百度热榜一' and baidu[0]['rank'] == 1
    toutiao = trending.parse_toutiao({'data': [{'Title': '头条热榜一', 'Url': 'https://www.toutiao.com/1'}]})
    assert toutiao == [{'source': 'toutiao', 'rank': 1, 'title': '头条热榜一', 'url': 'https://www.toutiao.com/1'}]


def test_match_requires_overlap_and_prefers_best_rank():
    entries = [{'source': 'weibo', 'rank': 20, 'title': 'OpenAI 正式发布 GPT-5', 'url': ''},
               {'source': 'zhihu', 'rank': 3, 'title': 'OpenAI 发布 GPT-5 新模型', 'url': ''},
               {'source': 'baidu', 'rank': 1, 'title': '某地暴雨预警', 'url': ''}]
    heat = trending.match('OpenAI 发布 GPT-5 新模型', entries, 2)
    assert heat and heat['rank'] == 3 and heat['source'] == 'zhihu'
    assert trending.match('完全无关的标题内容', entries, 2) is None


def test_annotate_sets_boost_and_skips_demo_or_existing():
    entries = [{'source': 'weibo', 'rank': 2, 'title': 'OpenAI 发布 GPT-5 新模型', 'url': ''}]
    story = {'id': 's1', 'title': 'OpenAI 发布 GPT-5 新模型', 'demo': False}
    demo = {'id': 's2', 'title': 'OpenAI 发布 GPT-5 新模型', 'demo': True}
    changed = trending.annotate([story, demo], entries)
    assert changed == [story] and story['heat_boost'] == 6 and story['heat']['rank'] == 2
    assert 'heat' not in demo
    # 已命中过的条目不会重复加权
    assert trending.annotate([story], entries) == []


def test_items_uses_fresh_cache_and_falls_back_to_stale(tmp_path, monkeypatch):
    store = Store(tmp_path / 'trending.db')
    entries = [{'source': 'weibo', 'rank': 1, 'title': '缓存条目', 'url': ''}]
    store.put('integrations', {'id': 'trending', 'items': entries, 'fetched_at': now(), 'errors': {}})
    monkeypatch.setattr(trending, 'fetch', lambda *a, **k: pytest.fail('fresh cache must not refetch'))
    rows, note = trending.items(store, None)
    assert rows == entries and '缓存' in note

    monkeypatch.setattr(trending, 'fetch', lambda *a, **k: ([], {'weibo': 'ConnectError'}))
    two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    store.put('integrations', {'id': 'trending', 'items': entries,
                               'fetched_at': two_hours_ago, 'errors': {}})
    rows, note = trending.items(store, None)
    assert rows == entries and '旧缓存' in note

    store.put('integrations', {'id': 'trending', 'items': entries,
                               'fetched_at': '2026-10-01T00:00:00+00:00', 'errors': {}})
    rows, note = trending.items(store, None)
    assert rows == [] and '不可用' in note


def test_items_refreshes_and_caches_success(tmp_path, monkeypatch):
    store = Store(tmp_path / 'trending.db')
    entries = [{'source': 'zhihu', 'rank': 1, 'title': '新热榜', 'url': ''}]
    monkeypatch.setattr(trending, 'fetch', lambda *a, **k: (entries, {}))
    rows, note = trending.items(store, None)
    assert rows == entries and store.get('integrations', 'trending')['items'] == entries


def test_heat_boost_raises_effective_selection_score(tmp_path):
    from newsroom.config import Settings
    from newsroom.engine import Engine
    engine = Engine(Settings(data_dir=tmp_path, admin_token='test'))
    def story(ident, score, boost=None):
        row = {'id': ident, 'source_id': 'techcrunch-ai', 'source': 'TechCrunch AI', 'score': score,
               'demo': False, 'source_report': {'tier': 'secondary', 'organization': 'TechCrunch'}}
        if boost:
            row['heat_boost'] = boost
        return row
    hot, cold = story('hot', 66, 6), story('cold', 70)
    order = engine.attempt_order([cold, hot], allowance=1, support={})
    assert [item['id'] for item in order] == ['hot', 'cold']
