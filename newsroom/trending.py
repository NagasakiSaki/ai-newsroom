"""公共热榜信号：只用于选题排序与加权，不写进正文；失败不影响采编。

借鉴 TrendRadar 的多平台热榜聚合思路，只做最轻量的一层：
抓取公开热榜标题，与本轮候选标题做词项重叠匹配，命中则给候选一个排序加分。
热榜不是事实来源，也不代表报道价值；文章正文不会出现热榜名次。
"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from .config import ROOT
from .db import now
from .network import options

ENDPOINTS = {
    'baidu': 'https://top.baidu.com/api/board?platform=wise&tab=realtime',
    'toutiao': 'https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc',
    # 微博／知乎接口需要登录态，未登录会 403/401；保留定义，默认不启用。
    'weibo': 'https://weibo.com/ajax/side/hotSearch',
    'zhihu': 'https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total?limit=50',
}
DEFAULT = {'enabled': True, 'sources': ['baidu', 'toutiao'], 'cache_minutes': 10,
           'min_overlap': 2, 'boost_top10': 6, 'boost_any': 3, 'stale_hours': 6}


def config():
    try:
        raw = json.loads((ROOT / 'config/trending.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return dict(DEFAULT)
    merged = {**DEFAULT, **raw}
    merged['sources'] = [name for name in merged.get('sources', []) if name in ENDPOINTS]
    return merged


def _title(value):
    text = str(value or '').strip()
    return text[:120]


def parse_weibo(payload):
    rows = ((payload or {}).get('data') or {}).get('realtime') or []
    items = []
    for index, row in enumerate(rows[:30]):
        title = _title(row.get('word') or row.get('note'))
        if title:
            items.append({'source': 'weibo', 'rank': index + 1, 'title': title,
                          'url': 'https://s.weibo.com/weibo?q=' + quote(title)})
    return items


def parse_zhihu(payload):
    rows = (payload or {}).get('data') or []
    items = []
    for index, row in enumerate(rows[:30]):
        target = row.get('target') or {}
        title = _title(target.get('title'))
        if title:
            items.append({'source': 'zhihu', 'rank': index + 1, 'title': title, 'url': str(target.get('url') or '')})
    return items


def parse_baidu(payload):
    items = []
    for card in ((payload or {}).get('data') or {}).get('cards') or []:
        for group in card.get('content') or []:
            for row in group.get('content') or []:
                title = _title(row.get('word'))
                if title and all(row['title'] != title for row in items):
                    items.append({'source': 'baidu', 'rank': len(items) + 1, 'title': title,
                                  'url': str(row.get('url') or '')})
                if len(items) >= 30:
                    return items
    return items


def parse_toutiao(payload):
    items = []
    for index, row in enumerate(((payload or {}).get('data') or [])[:30]):
        title = _title(row.get('Title'))
        if title:
            items.append({'source': 'toutiao', 'rank': index + 1, 'title': title,
                          'url': str(row.get('Url') or '')})
    return items


PARSERS = {'weibo': parse_weibo, 'zhihu': parse_zhihu, 'baidu': parse_baidu, 'toutiao': parse_toutiao}


def _fetch_one(name, settings):
    url = ENDPOINTS[name]
    with httpx.Client(timeout=httpx.Timeout(8, connect=5), **options(settings, 'news'),
                      headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36',
                               'Accept': 'application/json,text/plain,*/*'}) as client:
        response = client.get(url)
        response.raise_for_status()
        payload = response.json()
    return PARSERS[name](payload)


def fetch(settings, sources=None):
    """并发抓取各平台热榜；单源失败只记录错误。"""
    names = [name for name in (sources or config()['sources']) if name in ENDPOINTS]
    items, errors = [], {}
    if not names:
        return items, errors
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        results = list(pool.map(lambda name: (name, _safe_fetch(name, settings)), names))
    for name, (rows, error) in results:
        if error:
            errors[name] = error
        else:
            items.extend(rows)
    return items, errors


def _safe_fetch(name, settings):
    try:
        return _fetch_one(name, settings), ''
    except Exception as exc:
        return [], (type(exc).__name__ + '：' + str(exc)[:120])


def items(store, settings):
    """带 TTL 与失败回退的缓存读取。返回 (items, note)。"""
    cfg = config()
    if not cfg.get('enabled'):
        return [], '热榜信号已关闭'
    try:
        cached = store.get('integrations', 'trending')
    except KeyError:
        cached = {}
    fetched_at = cached.get('fetched_at')
    fresh = False
    if fetched_at:
        try:
            fresh = datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at) < timedelta(minutes=cfg['cache_minutes'])
        except ValueError:
            fresh = False
    if fresh and cached.get('items'):
        return cached['items'], '使用 ' + str(len(cached['items'])) + ' 条缓存热榜'
    rows, errors = fetch(settings, cfg['sources'])
    if rows:
        store.put('integrations', {'id': 'trending', 'items': rows, 'fetched_at': now(), 'errors': errors})
        note = str(len(rows)) + ' 条热榜'
        if errors:
            note += '；失败源：' + '、'.join(errors)
        return rows, note
    age_ok = False
    if fetched_at and cached.get('items'):
        try:
            age_ok = datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at) < timedelta(hours=cfg['stale_hours'])
        except ValueError:
            age_ok = False
    if age_ok:
        return cached['items'], '热榜刷新失败，使用 ' + str(len(cached['items'])) + ' 条旧缓存'
    return [], '热榜不可用：' + '、'.join(errors) if errors else '热榜不可用'


def match(title, entries, min_overlap=2):
    from .crosscheck import terms
    keys = terms(title)
    best = None
    for entry in entries:
        overlap = keys & terms(entry['title'])
        if len(overlap) < min_overlap:
            continue
        score = (len(overlap), -int(entry.get('rank') or 99))
        if best is None or score > best[0]:
            best = (score, entry, len(overlap))
    if not best:
        return None
    entry = best[1]
    return {'source': entry['source'], 'rank': entry['rank'], 'title': entry['title'],
            'url': entry.get('url', ''), 'overlap': best[2]}


def annotate(stories, entries, cfg=None):
    """给候选写 heat／heat_boost；返回被改动的条目（调用方负责持久化）。"""
    cfg = cfg or config()
    if not entries:
        return []
    changed = []
    for story in stories:
        if story.get('demo') or story.get('heat'):
            continue
        heat = match(story['title'], entries, cfg.get('min_overlap', 2))
        if not heat:
            continue
        heat['checked_at'] = now()
        story['heat'] = heat
        story['heat_boost'] = cfg['boost_top10'] if int(heat['rank']) <= 10 else cfg['boost_any']
        changed.append(story)
    return changed
