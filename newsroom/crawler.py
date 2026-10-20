"""Bounded public-web crawler: RSS, HTML, articles, sitemap; no model/search API."""
import hashlib
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import feedparser
import httpx
from bs4 import BeautifulSoup

from . import collect
from .db import now
from .source_catalog import matches, source_url
from .source_quality import as_date, assessment, page_dates

USER_AGENT = 'AINewsroom'
SKIP_EXTENSIONS = re.compile(r'\.(?:pdf|zip|gz|jpg|jpeg|png|webp|gif|svg|mp[34]|exe|css|js|woff2?)(?:$|[?#])', re.I)


def extract(html, url, source):
    soup = BeautifulSoup(html, 'html.parser')
    title = soup.find('h1') or soup.title
    title = title.get_text(' ', strip=True)[:300] if title else ''
    node = soup.select_one(source['content_selector']) if source.get('content_selector') else soup.find('article') or soup.find('main') or soup.body or soup
    if node is None:
        return None
    dates = page_dates(html)
    # A sidebar/event-list <time> is not the publication date of this document.
    dated_tag = node.find('time', attrs={'itemprop': 'datePublished'})
    if dated_tag is None and node.name == 'article':
        for candidate in node.find_all('time', datetime=True):
            header = candidate.find_parent('header')
            if candidate.parent is node or header and header.parent is node:
                dated_tag = candidate
                break
    if not dates.get('published_at') and dated_tag:
        date = as_date(dated_tag.get('datetime'))
        if date:
            dates['published_at'] = date.isoformat()
    for tag in node.find_all(['script', 'style', 'nav', 'header', 'footer', 'form', 'aside', 'noscript']):
        tag.decompose()
    text = re.sub(r'[ \t]+', ' ', node.get_text('\n', strip=True)).strip()[:24000]
    link_text = sum(len(a.get_text(strip=True)) for a in node.find_all('a'))
    if not title or len(text) < 100 or link_text > len(text) * 0.65:
        return None
    return {'title': title, 'text': text, **dates}


class Session:
    def __init__(self, source, settings, checkpoint):
        self.source, self.settings, self.checkpoint = source, settings, checkpoint or (lambda: None)
        self.robots, self.last_request = {}, {}
        self.pages = 0

    def pause(self, delay):
        until = time.monotonic() + delay
        while time.monotonic() < until:
            self.checkpoint()
            time.sleep(min(.1, max(0, until - time.monotonic())))

    def request(self, url, robots=False):
        source_url(url)
        host = urlsplit(url).hostname
        if host not in self.source['allowed_hosts']:
            raise ValueError('爬虫不跟随来源域名之外的链接')
        origin = urlunsplit((urlsplit(url).scheme, urlsplit(url).netloc, '', '', ''))
        if not robots:
            parser = self.robots_for(origin)
            if not parser.can_fetch(USER_AGENT, url):
                raise ValueError('robots.txt 不允许抓取此路径')
            self.throttle(origin, parser)
        self.checkpoint()
        self.last_request[origin] = time.monotonic()
        # The fetcher revalidates DNS and domain on every redirect and never sends API credentials.
        return collect.fetch_document(url, limit=512000 if robots else 2000000,
                                      allowed_hosts=self.source['allowed_hosts'], settings=self.settings,
                                      redirect_guard=None if robots else self.guard_redirect)

    def throttle(self, origin, parser):
        delay = max(self.source.get('delay_seconds', 1), parser.crawl_delay(USER_AGENT) or 0)
        rate = parser.request_rate(USER_AGENT)
        if rate and rate.requests:
            delay = max(delay, rate.seconds / rate.requests)
        if delay > 60:
            raise ValueError('来源要求超过一分钟的抓取间隔，本轮不自动抓取')
        self.pause(max(0, self.last_request.get(origin, 0) + delay - time.monotonic()))
        self.checkpoint()
        self.last_request[origin] = time.monotonic()

    def guard_redirect(self, url):
        source_url(url)
        origin = urlunsplit((urlsplit(url).scheme, urlsplit(url).netloc, '', '', ''))
        parser = self.robots_for(origin)
        if not parser.can_fetch(USER_AGENT, url):
            raise ValueError('robots.txt 不允许抓取跳转后的路径')
        self.throttle(origin, parser)

    def robots_for(self, origin):
        if origin not in self.robots:
            parser = RobotFileParser(origin + '/robots.txt')
            try:
                page = self.request(origin + '/robots.txt', robots=True)
                parser.parse(page['html'].splitlines())
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if code in {404, 410}:
                    parser.parse(['User-agent: *', 'Allow: /'])
                else:
                    raise ValueError('robots.txt 无法读取或访问被拒绝，未继续抓取') from None
            self.robots[origin] = parser
        return self.robots[origin]


def crawl_source(source, config, settings, checkpoint=None, on_item=None):
    session = Session(source, settings, checkpoint)
    queue = deque([(source['url'], 0, source.get('mode', 'auto'), None)])
    visited, queued, items, errors = set(), {collect.canonical(source['url'])}, [], []
    skipped, fetched = 0, set()

    def enqueue(url, depth, mode='auto', feed=None):
        try:
            url = source_url(url)
            ident = collect.canonical(url)
            if (urlsplit(url).hostname not in source['allowed_hosts'] or ident in queued
                    or SKIP_EXTENSIONS.search(url) or len(queued) >= 300):
                return
            queued.add(ident)
            queue.append((url, depth, mode, feed))
        except ValueError:
            return

    while queue and session.pages < source.get('max_pages', 8):
        session.checkpoint()
        url, depth, mode, feed = queue.popleft()
        ident = collect.canonical(url)
        if ident in visited:
            continue
        visited.add(ident)
        session.pages += 1
        try:
            page = session.request(url)
            final_url = page['url']
            final_id = collect.canonical(final_url)
            if final_id in fetched:
                continue
            fetched.add(final_id)
            visited.add(collect.canonical(final_url))
            html = page['html']
            stripped = html.lstrip('\ufeff \t\n\r')
            if mode in {'auto', 'rss', 'sitemap'} and stripped.startswith('<'):
                try:
                    root = ElementTree.fromstring(html)
                    tag = root.tag.split('}')[-1].lower()
                except ElementTree.ParseError:
                    tag = ''
                if tag in {'urlset', 'sitemapindex'}:
                    for loc in root.iter():
                        if loc.tag.split('}')[-1] == 'loc' and loc.text:
                            if tag == 'sitemapindex' and depth >= source.get('max_depth', 1):
                                continue
                            enqueue(urljoin(final_url, loc.text.strip()), depth + 1,
                                    'sitemap' if tag == 'sitemapindex' else 'article')
                    continue
                if tag in {'rss', 'feed', 'rdf'} or mode == 'rss':
                    parsed = feedparser.parse(html)
                    for entry in parsed.entries[:100]:
                        if entry.get('link'):
                            enqueue(urljoin(final_url, entry.link), depth + 1, 'article', entry)
                    continue
                if mode == 'sitemap':
                    raise ValueError('不是支持的 XML 站点地图')
            parsed = extract(html, final_url, source)
            if parsed:
                if feed:
                    parsed['title'] = parsed['title'] or collect.plain(feed.get('title', ''))[:300]
                    if not parsed.get('published_at') and feed.get('published_parsed'):
                        parsed['published_at'] = datetime(*feed['published_parsed'][:6], tzinfo=timezone.utc).isoformat()
                published = as_date(parsed.get('published_at'))
                if published and (published > datetime.now(timezone.utc) + timedelta(minutes=10)
                                  or config.max_age_hours and datetime.now(timezone.utc) - published > timedelta(hours=config.max_age_hours)):
                    skipped += 1
                elif matches(parsed['title'] + '\n' + parsed['text'], config, source):
                    item = {'id': hashlib.sha256(collect.canonical(final_url).encode()).hexdigest()[:24],
                            'url': final_url, 'title': parsed['title'], 'text': parsed['text'],
                            'excerpt': parsed['text'][:600], 'published_at': parsed.get('published_at'),
                            'updated_at': parsed.get('updated_at'), 'collected_at': now(),
                            'date_basis': 'published' if parsed.get('published_at') else 'unknown',
                            'date_precision': 'timestamp' if parsed.get('published_at') else 'unknown',
                            'source_id': source['id'], 'source': source['name'], 'kind': 'media',
                            'demo': False, 'status': 'new', 'custom_source': True,
                            'crawl_html': html[:120000], 'crawl_snapshot': {'url': final_url, 'checked_at': now(),
                                'body_hash': hashlib.sha256(parsed['text'].encode()).hexdigest()}}
                    item['source_report'] = assessment(source, item)
                    if not item['source_report']['auto_eligible']:
                        item['status'] = 'discovery_only'
                    items.append(item)
                    if on_item:
                        on_item(item)
                else:
                    skipped += 1
            if mode not in {'article', 'rss', 'sitemap'} and depth < source.get('max_depth', 1):
                soup = BeautifulSoup(html, 'html.parser')
                for link in soup.select('link[rel="alternate"][href]'):
                    if 'rss' in link.get('type', '') or 'atom' in link.get('type', ''):
                        enqueue(urljoin(final_url, link['href']), depth + 1, 'rss')
                selector = source.get('article_selector') or 'article a[href], main a[href], h2 a[href], h3 a[href]'
                links = soup.select(selector)
                if not links:
                    links = soup.select('a[href]')
                for link in links[:200]:
                    if link.get('href') and (source.get('article_selector') or len(link.get_text(strip=True)) >= 4):
                        enqueue(urljoin(final_url, link['href']), depth + 1)
        except (ValueError, httpx.HTTPError, OSError) as exc:
            # Never echo URLs, response bodies, cookies, or query parameters in error diagnostics.
            errors.append({'page': session.pages, 'error': str(exc)[:180] if isinstance(exc, ValueError) else type(exc).__name__})
    report = {'id': source['id'], 'name': source['name'], 'status': 'error' if not items and errors else 'partial' if errors else 'ok',
              'last_checked_at': now(), 'recent_count': len(items), 'pages_fetched': len(fetched), 'pages_attempted': session.pages,
              'remaining_urls': len(queue), 'truncated': bool(queue), 'filtered_count': skipped,
              'errors': errors[:10], 'error': errors[0]['error'] if errors else None,
              'latest_published_at': max((i['published_at'] for i in items if i.get('published_at')), default=None),
              'collection_method': 'builtin_crawler', 'model_calls': 0}
    if items or not errors:
        report['last_success_at'] = now()
    return items, report
