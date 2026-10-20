"""按用途显式选择 HTTP 代理；不继承系统代理环境，也不声称绕过 TUN。"""
from urllib.parse import urlsplit


def proxy_url(value):
    if not value:
        return ''
    try:
        parts = urlsplit(value)
        valid = (parts.scheme == 'http' and parts.hostname in {'127.0.0.1', 'localhost', '::1'}
                 and parts.port and not parts.username and not parts.password and parts.path in {'', '/'}
                 and not parts.query and not parts.fragment)
    except ValueError:
        valid = False
    if not valid or any(ord(c) < 33 for c in value):
        raise ValueError('代理须为本机 HTTP／混合端口，例如 http://127.0.0.1:7890；不支持认证或 SOCKS 端口')
    return value.rstrip('/')


def options(settings=None, purpose='news'):
    # 微信始终不读取任何代理设置；所有用途均保留默认 TLS 证书验证。
    result = {'trust_env': False, 'follow_redirects': False}
    attribute = {'news': 'news_proxy_url', 'writer': 'llm_proxy_url',
                 'prompt': 'prompt_proxy_url', 'review': 'review_proxy_url', 'visual': 'visual_proxy_url'}.get(purpose)
    value = getattr(settings, attribute, '') if settings is not None and attribute else ''
    if value:
        result['proxy'] = proxy_url(value)
    return result


def local_proxy_hint():
    """只读 Windows 系统代理中的本机 HTTP 端口，不探测端口、不发送网络请求。"""
    import os
    if os.name != 'nt':
        return ''
    try:
        from urllib.request import getproxies_registry
        value = getproxies_registry().get('https') or getproxies_registry().get('http') or ''
        return proxy_url(value)
    except (OSError, ValueError):
        return ''
