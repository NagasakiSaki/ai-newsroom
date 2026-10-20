import hashlib
import json
import ipaddress
import re
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import feedparser
import httpx
from bs4 import BeautifulSoup

from .db import now
from .source_quality import assessment, policy, page_dates
from .network import options

FAKE_IP = ipaddress.ip_network("198.18.0.0/15")
# This machine's TUN also synthesizes AAAA in the IPv6 benchmark range.
# It is accepted only for curated HTTPS publishers after independent public DNS validation.
FAKE_IP6 = ipaddress.ip_network('2001:2::/64')
CURATED_HOSTS = {'www.anthropic.com','anthropic.com','aws.amazon.com','machinelearning.apple.com','github.blog','export.arxiv.org','arxiv.org','techcrunch.com','arstechnica.com','www.theverge.com','theverge.com',"openai.com", "www.openai.com", "blog.google", "research.google", "deepmind.google", "www.microsoft.com", "microsoft.com", "huggingface.co", "rss.arxiv.org", "arxiv.org"}
DNS_CACHE = {}
CURATED_HOSTS.update({'www.deepseek.com', 'deepseek.com', 'api-docs.deepseek.com', 'qwenlm.github.io',
    'www.minimax.cn', 'www.minimaxi.com', 'www.minimax.io', 'mistral.ai', 'cohere.com', 'arena.ai',
    'aider.chat', 'artificialanalysis.ai', 'www.stepfun.com', 'z.ai', 'moonshotai.github.io', 'x.ai'})
CURATED_HOSTS.update({'blogs.nvidia.com', 'nvidianews.nvidia.com', 'engineering.fb.com', 'the-decoder.com',
    'www.marktechpost.com', 'www.leiphone.com', 'www.interconnects.ai', 'www.latent.space', 'www.ithome.com',
    'www.unite.ai', 'venturebeat.com', 'www.qbitai.com'})
CURATED_HOSTS.update({'blog.cloudflare.com', 'developers.cloudflare.com', 'github.com'})
ROUTE = {'proxy_fallback': 0}
ROUTE_LOCAL = threading.local()


def verify_public_dns(host, settings=None, proxy_active=True):
    cached = DNS_CACHE.get(host)
    if cached and cached > time.monotonic():
        return True
    # 只向固定的可信 HTTPS DNS 服务查询固定信源，仍校验 TLS 和公网 IP。
    client_options = options(settings, 'news')
    if not proxy_active:
        client_options.pop('proxy', None)
    endpoints = ['https://cloudflare-dns.com/dns-query', 'https://dns.alidns.com/resolve']
    if not client_options.get('proxy'):
        endpoints.reverse()  # Domestic publishers use domestic HTTPS DNS when the page is direct.
    data = None
    with httpx.Client(timeout=10, **client_options) as client:
        for endpoint in endpoints:
            try:
                response = client.get(endpoint, params={"name": host, "type": "A"}, headers={"Accept": "application/dns-json"})
                response.raise_for_status()
                candidate = response.json()
                if not isinstance(candidate, dict):
                    continue
                data = candidate
                break
            except (httpx.HTTPError, ValueError):
                continue
    if data is None:
        raise ValueError('独立HTTPS DNS暂不可用，请检查新闻代理或网络后重试')
    ips = [item["data"] for item in data.get("Answer", []) if item.get("type") == 1]
    if data.get("Status") != 0 or not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError("信源未通过独立公网 DNS 校验")
    DNS_CACHE[host] = time.monotonic() + 300
    return True


def canonical(url):
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("仅支持公开 HTTP(S) 新闻 URL")
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", urlencode(sorted(query)), ""))


def public_url(url, curated=False, settings=None, proxy_active=None):
    original_path = urlsplit(url).path
    url = canonical(url)
    if original_path.endswith('/') and original_path != '/':
        parts = urlsplit(url)
        url = urlunsplit((parts.scheme, parts.netloc, parts.path + '/', parts.query, ''))
    parts = urlsplit(url)
    if parts.port not in {None, 80, 443}:
        raise ValueError("新闻采集只允许 80/443 端口")
    if proxy_active is None:
        proxy_active = bool(options(settings, 'news').get('proxy'))
    if curated and parts.scheme == 'https' and parts.hostname in CURATED_HOSTS and proxy_active:
        # 显式代理可在远端解析域名；固定信源先通过同一路由校验真实公网 DNS，避免本地 DNS 污染阻断。
        verify_public_dns(parts.hostname, settings=settings, proxy_active=True)
        return url
    addresses = socket.getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)
    ips = [ipaddress.ip_address(a[4][0]) for a in addresses]
    if curated and parts.scheme == "https" and parts.hostname in CURATED_HOSTS and ips and all(ip in FAKE_IP or ip in FAKE_IP6 or ip.is_global for ip in ips):
        if any(ip in FAKE_IP or ip in FAKE_IP6 for ip in ips):
            # 只在带 settings 且回退直连时传 proxy_active，保持既有调用形状。
            if settings is None:
                verify_public_dns(parts.hostname)
            elif proxy_active:
                verify_public_dns(parts.hostname, settings=settings)
            else:
                verify_public_dns(parts.hostname, settings=settings, proxy_active=False)
        return url
    if not ips or any(not ip.is_global for ip in ips):
        raise ValueError("新闻源不能指向内网、本机或保留地址")
    return url


def _fetch_document(url, limit, allowed_hosts, settings, client_options, proxy_active):
    # 每次跳转重新检查；不携带任何模型/微信凭证。
    with httpx.Client(timeout=20, **client_options, headers={"User-Agent": "AINewsroom/0.1 (+RSS reader)"}) as client:
        for _ in range(5):
            url = public_url(url, curated=bool(allowed_hosts), settings=settings, proxy_active=proxy_active)
            if allowed_hosts and urlsplit(url).hostname not in allowed_hosts:
                raise ValueError("信源跳转到了未允许的域名")
            with client.stream("GET", url) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers["location"])
                    continue
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("新闻页面超过采集大小上限")
                    chunks.append(chunk)
                return {"html": b"".join(chunks).decode(response.encoding or "utf-8", errors="replace"), "url": url}
    raise ValueError("新闻页面跳转过多")


def fetch_document(url, limit=2_000_000, allowed_hosts=None, settings=None):
    """显式代理不可用时自动回退直连；回退只影响本次采集，不修改页面配置。"""
    client_options = options(settings, 'news')
    try:
        return _fetch_document(url, limit, allowed_hosts, settings, client_options, bool(client_options.get('proxy')))
    except (httpx.ProxyError, httpx.ConnectError):
        if not client_options.get('proxy'):
            raise
        direct = {key: value for key, value in client_options.items() if key != 'proxy'}
        result = _fetch_document(url, limit, allowed_hosts, settings, direct, False)
        result['proxy_fallback'] = True
        ROUTE['proxy_fallback'] += 1
        ROUTE_LOCAL.proxy_fallback = getattr(ROUTE_LOCAL, 'proxy_fallback', 0) + 1
        return result


def fetch_text(url, limit=2_000_000, allowed_hosts=None, settings=None):
    return fetch_document(url, limit, allowed_hosts, settings=settings)["html"]


def fetch_source(source, url, settings=None, document=False):
    """Prefer the publisher's route; domestic news may retry its configured news proxy on transport failure."""
    from dataclasses import replace
    direct = settings is not None and source.get('network') == 'direct'
    route = replace(settings, news_proxy_url='') if direct else settings
    fetch = fetch_document if document else fetch_text
    try:
        return fetch(url, allowed_hosts=source.get('allowed_hosts'), settings=route)
    except httpx.TransportError:
        if not direct or not settings.news_proxy_url:
            raise
        result = fetch(url, allowed_hosts=source.get('allowed_hosts'), settings=settings)
        if document:
            result['route_fallback'] = 'news_proxy'
        return result


def plain(html):
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style", "nav", "footer", "header", "form"]):
        element.decompose()
    main = soup.find("article") or soup.find("main") or soup
    return re.sub(r"[ \t]+", " ", main.get_text(" ", strip=True)).strip()


def similar(a, b):
    def grams(text):
        text = re.sub(r"\W+", "", text.lower())
        return {text[i:i+3] for i in range(max(1, len(text)-2))}
    x, y = grams(a), grams(b)
    return len(x & y) / max(1, len(x | y)) >= 0.76


def matches_keywords(text, keywords):
    if not keywords:
        return True
    for word in keywords:
        if word.isascii() and re.fullmatch(r'[a-zA-Z]+', word):
            if re.search(r'(?<![a-z])' + re.escape(word) + r'(?![a-z])', text, re.I):
                return True
        elif word.lower() in text.lower():
            return True
    return False


def parse_news_html(html, source):
    """已实测的官方新闻页适配；日期只有天时明确保留精度。"""
    soup = BeautifulSoup(html, 'html.parser')
    entries = []
    seen = set()
    for link in soup.find_all('a', href=True):
        date_tag = link.find('time')
        url = urljoin(source['url'], link['href'])
        if not date_tag or url in seen or not urlsplit(url).path.startswith('/news/'):
            continue
        try:
            date = datetime.strptime(date_tag.get_text(' ', strip=True), '%b %d, %Y').replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        heading = link.find(['h2', 'h3'])
        title = heading.get_text(' ', strip=True) if heading else link.get_text(' ', strip=True).replace(date_tag.get_text(' ', strip=True), '').strip()
        entries.append(feedparser.FeedParserDict({'title': title, 'link': url, 'published_parsed': date.utctimetuple(), 'date_precision': 'day', 'summary': link.get_text(' ', strip=True)}))
        seen.add(url)
    if not entries:
        raise ValueError('官方新闻页结构变化，无法解析日期；不会伪造 RSS')
    return feedparser.FeedParserDict({'feed': {'title': source['name']}, 'entries': entries})


def date_in_text(value):
    patterns = [(r'\b\d{4}-\d{2}-\d{2}\b', '%Y-%m-%d'),
                (r'\d{4}年\d{1,2}月\d{1,2}日', '%Y年%m月%d日'),
                (r'\b[A-Z][a-z]+ \d{1,2},? \d{4}\b', '%B %d, %Y'),
                (r'\b[A-Z][a-z]{2} \d{1,2},? \d{4}\b', '%b %d, %Y')]
    for pattern, fmt in patterns:
        found = re.search(pattern, value)
        if found:
            try:
                text = found.group(0)
                if '%d,' in fmt and ',' not in text:
                    fmt = fmt.replace('%d,', '%d')
                return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def parse_article_index(html, source, settings=None):
    """官方博客／实测列表：只用明确发布日期，不把抓取时间、更新时间当新消息。"""
    soup = BeautifulSoup(html, 'html.parser')
    entries, seen, inspected = [], set(), 0
    for link in soup.find_all('a', href=True):
        try:
            url = canonical(urljoin(source['url'], link['href']))
        except ValueError:
            continue
        parts = urlsplit(url)
        if parts.hostname not in source['allowed_hosts'] or not any(parts.path.startswith(p) for p in source['article_prefixes']):
            continue
        if url in seen or url == canonical(source['url']) or any(x in parts.path for x in ['/tag/', '/category/']):
            continue
        seen.add(url)
        if inspected >= source.get('index_fetch_limit', 6):
            break
        inspected += 1
        text = link.get_text(' ', strip=True)
        date = date_in_text(text)
        heading = link.find(['h2', 'h3', 'h4'])
        title = heading.get_text(' ', strip=True) if heading else text
        if date is None:
            try:
                page = fetch_source(source, url, settings=settings)
                dates = page_dates(page)
                value = dates.get('published_at')
                if value:
                    date = datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
                article = BeautifulSoup(page, 'html.parser')
                if date is None:
                    time_tag = article.find('time')
                    if time_tag:
                        value = time_tag.get('datetime', '')
                        try: date = datetime.fromisoformat(value.replace('Z', '+00:00')).replace(tzinfo=timezone.utc)
                        except ValueError: date = date_in_text(time_tag.get_text(' ', strip=True))
                h1 = article.find('h1')
                if h1: title = h1.get_text(' ', strip=True)
            except (ValueError, httpx.HTTPError):
                continue
        if not date or not title:
            continue
        entries.append(feedparser.FeedParserDict({'title': title[:300], 'link': url,
            'published_parsed': date.utctimetuple(), 'date_precision': 'day', 'summary': text}))
    if not entries:
        raise ValueError('列表未提供可核对的发布日期，保留为信源检查失败，不伪造新消息')
    return feedparser.FeedParserDict({'feed': {'title': source['name']}, 'entries': entries})


def collect(sources, max_age, log, health=None, settings=None, checkpoint=None, on_items=None):
    if checkpoint:
        checkpoint()
    if settings is not None and not settings.demo and len(sources) > 1:
        # Bounded I/O only: a slow publisher must not serialize every other feed.
        from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
        active = [source for source in sources if source.get('enabled', True)]
        items = []
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix='news-feed') as pool:
            remaining = iter(active)
            futures = set()
            def launch():
                source = next(remaining, None)
                if source is not None:
                    futures.add(pool.submit(collect, [source], max_age, log, health, settings))
            for _ in range(4):
                launch()
            try:
                while futures:
                    completed, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in completed:
                        futures.remove(future)
                        batch = future.result()
                        items.extend(batch)
                        if on_items:
                            on_items(batch)
                    if checkpoint:
                        checkpoint()
                    for _ in completed:
                        launch()
            finally:
                # Drain only already-started I/O; cancellation never launches the remaining feeds.
                for future in futures:
                    if not future.cancel():
                        batch = future.result()
                        if on_items:
                            on_items(batch)
        return sorted(items, key=lambda item: item['published_at'], reverse=True)
    items = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age)
    fallback_noted = False
    for source in sources:
        if not source.get("enabled", True):
            continue
        fallback_before = getattr(ROUTE_LOCAL, 'proxy_fallback', 0)
        try:
            document = fetch_source(source, source['url'], settings=settings)
            feed = (parse_article_index(document, source, settings=settings) if source.get('format') == 'article_index'
                    else parse_news_html(document, source) if source.get('format') == 'news_html' else feedparser.parse(document))
            if not feed.entries and not feed.get("feed", {}).get("title"):
                raise ValueError("RSS 没有可解析的新闻条目")
            count = 0
            latest_date = None
            skipped = 0
            item_limit = source.get('max_entries', policy().get('max_feed_items', 400))
            for entry in feed.entries[:item_limit]:
                date = entry.get("published_parsed") or entry.get("updated_parsed")
                # 没有可核对时间的条目不自动当作新新闻。
                if not date:
                    continue
                published = datetime(*date[:6], tzinfo=timezone.utc)
                latest_date = max(latest_date or published, published)
                if published < cutoff or published > datetime.now(timezone.utc) + timedelta(minutes=10):
                    continue
                url = canonical(urljoin(source["url"], entry.link))
                title = plain(entry.title)[:300]
                summary = plain(entry.get('summary', ''))[:6000]
                if not matches_keywords(title + ' ' + summary, source.get('include_keywords')):
                    continue
                feed_html = next((part.get('value', '') for part in entry.get('content', []) if part.get('value')), '') or entry.get('summary', '')
                item = {"id": hashlib.sha256(url.encode()).hexdigest()[:24], "title": plain(entry.title)[:300],
                              "url": url, "published_at": published.isoformat(), "collected_at": now(),
                              "updated_at": datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc).isoformat() if entry.get("updated_parsed") else None,
                              "date_basis": 'published_date_only' if entry.get('date_precision') == 'day' else "published" if entry.get("published_parsed") else "updated_only",
                              'date_precision': entry.get('date_precision', 'timestamp'),
                              "source_id": source.get("id"), "author": entry.get("author"),
                              "source": source["name"], "kind": source.get("kind", "media"),
                              "excerpt": summary, "demo": False, "status": "new",
                              'feed_html': feed_html[:120000], 'feed_text': plain(feed_html)[:24000],
                              'feed_snapshot': {'source_id': source.get('id', source['url']), 'feed_url': source['url'], 'article_url': url, 'collected_at': now()}}
                item["source_report"] = assessment(source, item)
                if not item["source_report"]["auto_eligible"]:
                    item["status"] = "discovery_only"
                    skipped += 1
                items.append(item)
                count += 1
            report = {"id": source.get("id", source["url"]), "name": source["name"], "status": "ok", "last_success_at": now(), "last_checked_at": now(), "latest_published_at": latest_date.isoformat() if latest_date else None, "recent_count": count, "discovery_only": skipped, 'feed_total': len(feed.entries), 'inspected_count': min(len(feed.entries), item_limit), 'truncated': len(feed.entries) > item_limit}
            if getattr(ROUTE_LOCAL, 'proxy_fallback', 0) > fallback_before:
                report.update(proxy_fallback=True, note='代理不可用，已自动回退直连')
                if not fallback_noted:
                    fallback_noted = True
                    log('route_note', source['name'] + '：采集代理不可用，已自动回退直连；本次采集其余来源同样按直连处理')
            log("collect", f"{source['name']}：发现 {count} 条时效内新闻")
            if health:
                health(report)
        except Exception as exc:
            log("source_error", f"{source['name']}：{type(exc).__name__}；请检查新闻源或网络")
            if health:
                health({"id": source.get("id", source["url"]), "name": source["name"], "status": "error", "last_checked_at": now(), "error": str(exc)[:200] if isinstance(exc, ValueError) else type(exc).__name__})
    return sorted(items, key=lambda item: item["published_at"], reverse=True)


def demo_items():
    text = ("【虚构演示材料，不是真实新闻】示例实验室发布了名为 Demo-Assistant 的开源文档助手。"
            "该项目提供本地文档检索和回答出处显示。演示说明称，它只在内部的二十份示例文档上做了测试。"
            "项目没有提供第三方评测，也没有提供与其他产品的速度或准确率对比。"
            "项目代码采用示例许可，真实商业使用前需要重新确认许可条款。")
    return [{"id": "demo-document-assistant", "title": "【虚构演示】开源文档助手发布：回答可以显示出处", "url": "https://example.com/demo-assistant",
             "published_at": now(), "collected_at": now(), "source": "虚构演示来源", "kind": "official", "excerpt": text,
             "text": text, "demo": True, "status": "new"}]
