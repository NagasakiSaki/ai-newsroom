"""Bounded discovery of actual news pictures from the article and its explicit official links."""
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from . import collect
from .config import sources
from .db import now
from .source_quality import as_date


def discovery_error(engine, exc):
    import httpx
    if isinstance(exc, httpx.HTTPStatusError):
        return '原网页返回 HTTP ' + str(exc.response.status_code) + '；保留订阅正文和其他已引用来源，不绕过登录或访问限制'
    return engine.safe_error(exc)

# Linked-only public primary material; no arbitrary web image search or unrelated stock photos.
LINKED_PRIMARY = [
    {'name': 'Cloudflare 官方', 'hosts': {'blog.cloudflare.com', 'developers.cloudflare.com'}, 'prefixes': ['/']},
    {'name': 'DeepSeek 官方代码', 'hosts': {'github.com'}, 'prefixes': ['/deepseek-ai/']},
    {'name': 'Qwen 官方代码', 'hosts': {'github.com'}, 'prefixes': ['/QwenLM/']},
    {'name': 'Cloudflare 官方模型', 'hosts': {'huggingface.co'}, 'prefixes': ['/Cloudflare/']},
]


def official_link(url):
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.port not in {None, 443}:
        return None
    for row in LINKED_PRIMARY:
        if parts.hostname in row['hosts'] and any(parts.path.startswith(p) for p in row['prefixes']):
            return row['name']
    for row in sources():
        if row.get('enabled', True) and row.get('kind') == 'official' and parts.hostname in row.get('allowed_hosts', []):
            if any(parts.path.startswith(p) for p in row.get('article_prefixes', ['/'])):
                return row['name']
    return None


def official_links(html, page_url):
    soup = BeautifulSoup(html, 'html.parser')
    roots = soup.select('article, main, #paragraph, .post_content, .entry-content, .lph-article-comView, .markdown-body')
    if not roots and soup.find('body') is None:
        roots = [soup]
    result = []
    for root in roots:
        for anchor in root.select('a[href]'):
            url = urljoin(page_url, anchor['href'])
            if official_link(url) and collect.canonical(url) != collect.canonical(page_url) and url not in result:
                result.append(url)
    return result[:3]


def discover_story_images(engine, story, force=False):
    """Never replace source text/evidence, never request an LLM, cache even empty/failed discovery."""
    from .editorial_team import image_candidates
    previous = story.get('image_discovery') or {}
    checked = as_date(previous.get('checked_at'))
    if not force and previous.get('version') == 3 and checked and datetime.now(timezone.utc) - checked < timedelta(minutes=30):
        return story
    source = engine.source_for(story)
    if not source.get('enabled', True):
        return story
    candidates = list(story.get('image_candidates') or [])
    linked_candidates = []
    errors = []
    documents = [(story.get('feed_html') or '', story['url'])]
    engine.controls.checkpoint()
    page_checked = as_date(story.get('image_page_checked_at'))
    if force or not page_checked or datetime.now(timezone.utc) - page_checked > timedelta(minutes=30):
        try:
            doc = collect.fetch_source(source, story['url'], settings=engine.settings, document=True)
            documents.append((doc['html'], doc['url']))
        except Exception as exc:
            errors.append({'stage': 'article', 'error': discovery_error(engine, exc)})
    links = list(story.get('visual_links') or [])
    for html, url in documents:
        candidates.extend(image_candidates(html, url, source))
        links.extend(official_links(html, url))
    for url in list(dict.fromkeys(links))[:2]:
        engine.controls.checkpoint()
        try:
            doc = collect.fetch_document(url, allowed_hosts=[urlsplit(url).hostname], settings=engine.settings)
            name = official_link(doc['url'])
            if not name:
                continue
            linked = image_candidates(doc['html'], doc['url'], {'kind': 'official', 'name': name})
            for candidate in linked:
                candidate['linked_from'] = story['url']
            linked_candidates.extend(linked)
        except Exception as exc:
            errors.append({'stage': 'linked_primary', 'source_url': url, 'error': discovery_error(engine, exc)})
    story['image_candidates'] = list({row['url']: row for row in candidates}.values())[:32]
    story['linked_image_candidates'] = list({row['url']: row for row in linked_candidates}.values())[:24]
    story['image_discovery'] = {'checked_at': now(), 'version': 3, 'article_count': len(story['image_candidates']),
                                'linked_count': len(linked_candidates), 'errors': errors}
    engine.store.put('stories', story)
    return story
