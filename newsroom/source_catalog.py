"""User-owned collection configuration; no credentials or external calls on save."""
import hashlib
import ipaddress
import json
import re
import uuid
from urllib.parse import parse_qsl, urlsplit

import soupsieve
from pydantic import Field, field_validator, model_validator

from .config import editorial, sources
from .db import now
from .models import StrictModel


def source_url(value):
    value = value.strip()
    parts = urlsplit(value)
    if (parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password
            or parts.fragment or parts.port not in {None, 80, 443} or any(c.isspace() for c in value)):
        raise ValueError('请填写不含账号密码的公开 HTTP(S) 网址，仅支持 80/443 端口')
    host = parts.hostname.lower()
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')) or '.' not in host:
        raise ValueError('不能配置本机或内网网址')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise ValueError('不能配置本机、内网或保留地址')
    sensitive = {'key', 'apikey', 'accesskey', 'token', 'accesstoken', 'authtoken', 'secret', 'appsecret',
                 'password', 'signature', 'credential', 'credentials', 'authorization', 'auth', 'sig',
                 'xamzsignature', 'xamzcredential', 'xamzsecuritytoken', 'awsaccesskeyid',
                 'xgoogsignature', 'xgoogcredential'}
    if any(re.sub(r'[^a-z0-9]', '', k.lower()) in sensitive for k, _ in parse_qsl(parts.query)):
        raise ValueError('信源网址不能包含密钥、令牌或签名参数')
    return value


class CrawlSource(StrictModel):
    id: str = Field(default_factory=lambda: 'custom-' + uuid.uuid4().hex[:12], pattern=r'^custom-[a-z0-9-]{1,60}$')
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=8, max_length=2000)
    mode: str = Field(default='auto', pattern=r'^(auto|rss|html|article|sitemap)$')
    enabled: bool = True
    max_pages: int = Field(default=8, ge=1, le=30)
    max_depth: int = Field(default=1, ge=0, le=2)
    delay_seconds: float = Field(default=1, ge=0.2, le=30)
    article_selector: str = Field(default='', max_length=300)
    content_selector: str = Field(default='', max_length=300)
    extra_hosts: list[str] = Field(default_factory=list, max_length=10)
    include_keywords: list[str] = Field(default_factory=list, max_length=100)
    allow_writing: bool = False

    _url = field_validator('url')(source_url)

    @field_validator('name')
    @classmethod
    def name_not_blank(cls, value):
        if not value.strip():
            raise ValueError('来源名称不能为空')
        return value.strip()

    @field_validator('article_selector', 'content_selector')
    @classmethod
    def valid_selector(cls, value):
        if value:
            try:
                soupsieve.compile(value)
            except Exception:
                raise ValueError('CSS 选择器不合法') from None
        return value

    @field_validator('include_keywords')
    @classmethod
    def valid_words(cls, values):
        if any(not v.strip() or len(v) > 100 for v in values):
            raise ValueError('每个关键词为 1–100 字符')
        return list(dict.fromkeys(v.strip() for v in values))

    @field_validator('extra_hosts')
    @classmethod
    def valid_hosts(cls, values):
        result = []
        for value in values:
            host = value.strip().lower()
            if any(c in host for c in '/:@?#*'):
                raise ValueError('额外域名只填写主机名，不含协议、路径、端口或通配符')
            source_url('https://' + host + '/')
            result.append(host)
        return list(dict.fromkeys(result))

    def registered(self):
        result = self.model_dump()
        host = urlsplit(self.url).hostname
        return {**result, 'custom': True, 'allowed_hosts': list(dict.fromkeys([host, *self.extra_hosts])), 'article_prefixes': ['/'],
                'kind': 'media', 'tier': 'secondary' if self.allow_writing else 'discovery',
                'organization': host, 'evidence_note': '用户指定的信息来源，未自动认证权威性；引用须归因原站'}


class CollectionSettings(StrictModel):
    revision: int = Field(default=0, ge=0)
    topic: str = Field(default='通用信息', min_length=1, max_length=100)
    audience: str = Field(default='关注该主题的读者', min_length=1, max_length=200)
    writing_style: str = Field(default='清楚、具体、信息充足，按主题选择结构，区分事实与评论。', max_length=2000)
    include_builtins: bool = True
    keywords: list[str] = Field(default_factory=list, max_length=100)
    exclude_keywords: list[str] = Field(default_factory=list, max_length=100)
    keyword_mode: str = Field(default='any', pattern=r'^(any|all)$')
    max_age_hours: int = Field(default=0, ge=0, le=87600)
    custom_sources: list[CrawlSource] = Field(default_factory=list, max_length=100)

    @field_validator('topic', 'audience')
    @classmethod
    def nonblank_profile(cls, value):
        if not value.strip():
            raise ValueError('主题和受众不能为空白')
        return value.strip()

    _words = field_validator('keywords', 'exclude_keywords')(CrawlSource.valid_words.__func__)

    @model_validator(mode='after')
    def unique_sources(self):
        ids = [s.id for s in self.custom_sources]
        if len(ids) != len(set(ids)):
            raise ValueError('自定义来源 ID 重复')
        return self


def configuration(store):
    try:
        record = store.get('source_settings', 'main')
        return CollectionSettings.model_validate({k: v for k, v in record.items() if k not in {'id', 'updated_at'}})
    except KeyError:
        return CollectionSettings()


def fingerprint(config):
    return hashlib.sha256(json.dumps(config.model_dump(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def catalog(store):
    config = configuration(store)
    return ([{**s, 'builtin': True} for s in sources()] if config.include_builtins else []) + [s.registered() for s in config.custom_sources]


def save(store, config):
    if config.revision != configuration(store).revision:
        raise ValueError('来源设置已被其他页面修改，请刷新后重试')
    config = config.model_copy(update={'revision': config.revision + 1})
    store.put('source_settings', {'id': 'main', **config.model_dump(), 'updated_at': now()})
    return config


def matches(text, config, source=None):
    from .collect import matches_keywords
    if config.exclude_keywords and matches_keywords(text, config.exclude_keywords):
        return False
    if source and source.get('include_keywords') and not matches_keywords(text, source['include_keywords']):
        return False
    if not config.keywords:
        return True
    if config.keyword_mode == 'all':
        return all(matches_keywords(text, [word]) for word in config.keywords)
    return matches_keywords(text, config.keywords)


def writing_rules(store):
    config = configuration(store)
    base = editorial()
    return {**base, 'topic': config.topic, 'audience': config.audience, 'keywords': config.keywords,
            'positioning': '围绕“' + config.topic + '”整理有出处的信息，不把主题自动改成 AI 或科技。',
            'style': config.writing_style or base['style']}
