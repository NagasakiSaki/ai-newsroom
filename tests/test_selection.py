import pytest

from newsroom.config import Settings
from newsroom.engine import Engine


@pytest.fixture
def engine(tmp_path):
    return Engine(Settings(data_dir=tmp_path, admin_token='test'))


def candidate(ident, source_id, tier, score):
    return {'id': ident, 'source_id': source_id, 'source': source_id, 'score': score, 'demo': False,
            'source_report': {'tier': tier, 'organization': source_id}}


def test_supported_news_and_official_come_before_research(engine):
    paper = candidate('paper', 'arxiv-ai', 'research', 95)
    official = candidate('official', 'openai-news', 'primary', 72)
    news = candidate('news', 'techcrunch-ai', 'secondary', 70)
    order = engine.attempt_order([paper, official, news], allowance=1, support={'news': 1})
    assert [item['id'] for item in order] == ['news', 'official', 'paper']


def test_supported_secondary_beats_unsupported_official(engine):
    # 没有第二家机构报道的官方公告仍会先放后面：官方的窗口更宽，明天还在。
    official = candidate('official', 'openai-news', 'primary', 90)
    news = candidate('news', 'techcrunch-ai', 'secondary', 69)
    order = engine.attempt_order([official, news], allowance=1, support={'news': 1})
    assert [item['id'] for item in order] == ['news', 'official']


def test_research_is_capped_per_run(engine):
    papers = [candidate('p%d' % i, 'arxiv-ai', 'research', 90 - i) for i in range(4)]
    order = engine.attempt_order(papers, allowance=1, support={})
    assert [item['id'] for item in order] == ['p0']


def test_news_keeps_slots_even_when_papers_score_higher(engine):
    papers = [candidate('p%d' % i, 'arxiv-ai', 'research', 99 - i) for i in range(4)]
    news = [candidate('n1', 'techcrunch-ai', 'secondary', 70), candidate('n2', 'ars-ai', 'secondary', 69)]
    order = engine.attempt_order(papers + news, allowance=1, support={'n1': 1})
    assert [item['id'] for item in order] == ['n1', 'n2', 'p0']


def test_event_support_counts_other_organizations_and_ignores_paper_noise(engine):
    def story(ident, org, tier):
        return {'id': ident, 'source': org, 'demo': False, 'source_report': {'organization': org, 'tier': tier}}

    engine.store.put('stories', story('a', 'TechCrunch', 'secondary'))
    engine.store.put('stories', story('b', 'Anthropic', 'primary'))
    engine.store.put('stories', story('c', '论文作者', 'research'))
    engine.store.put('event_candidates', {'id': 'a', 'related_ids': ['b', 'c']})
    engine.store.put('event_candidates', {'id': 'b', 'related_ids': ['a', 'c']})
    support = engine.event_support()
    assert support['a'] == 1           # 只算其他机构，推算力作者噪音
    assert support['b'] == 1
    assert support['c'] == 2           # 论文被两家机构报道也算高热度


def test_demo_item_stays_first_in_demo_mode(engine):
    demo = {'id': 'demo', 'source': '演示', 'demo': True, 'score': 100, 'source_report': {'tier': 'demo'}}
    paper = candidate('paper', 'arxiv-ai', 'research', 90)
    assert engine.attempt_order([paper, demo], allowance=1, support={})[0]['id'] == 'demo'


def test_high_heat_beats_official_when_both_are_publishable(engine):
    hot = candidate('hot', 'theverge-ai', 'secondary', 69)
    official = candidate('official', 'openai-news', 'primary', 80)
    order = engine.attempt_order([official, hot], allowance=1, support={'hot': 2, 'official': 1})
    assert order[0]['id'] == 'hot'
