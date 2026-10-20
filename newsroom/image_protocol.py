"""Offline image endpoint/payload/response adapters; never move credentials to another host."""
import re
from math import gcd
from urllib.parse import urlsplit, urlunsplit

PROVIDERS = {'auto', 'openai_compatible', 'minimax'}
MINIMAX_HOSTS = {'api.minimax.cn', 'api.minimax.io', 'api.minimaxi.com'}
BUSINESS_HINTS = {
    1002: '请求限流，请稍后再试', 1004: '鉴权失败，请核对图片 API Key 及账号区域',
    1008: '图片服务余额不足，请检查图片服务账户', 1026: '图片描述未通过供应商内容检查',
    2013: '图片请求参数不符合供应商要求', 2049: '图片 API Key 无效，请核对密钥',
}


class ImageBusinessError(ValueError):
    def __init__(self, code):
        self.provider_code = code
        super().__init__(f"MiniMax 生图业务错误 {code}：" + BUSINESS_HINTS.get(code, '供应商拒绝请求，请核对账户和图片参数'))


def image_endpoint(base_url, provider='auto'):
    if provider not in PROVIDERS:
        raise ValueError('生图协议只支持 auto、openai_compatible 或 minimax')
    parts = urlsplit(base_url.strip().rstrip('/'))
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or any(c.isspace() for c in base_url)):
        raise ValueError('生图地址须为不含密钥或查询参数的 HTTPS 地址')
    # Validate ports without changing the user's domain or silently forwarding to another region.
    _ = parts.port
    path = parts.path.rstrip('/')
    if path.endswith('/image_generation/images/generations'):
        path = path.removesuffix('/images/generations')
    resolved = ('minimax' if parts.hostname in MINIMAX_HOSTS or path.endswith('/image_generation')
                else 'openai_compatible') if provider == 'auto' else provider
    if resolved == 'minimax':
        if path.endswith('/images/generations'):
            path = path.removesuffix('/images/generations')
        if not path.endswith('/image_generation'):
            path = (path or '/v1') + '/image_generation'
    else:
        if path.endswith('/image_generation'):
            raise ValueError('该地址是 MiniMax 原生图片接口，请选择自动识别或 MiniMax 原生协议')
        if not path.endswith('/images/generations'):
            path += '/images/generations'
    return resolved, urlunsplit((parts.scheme, parts.netloc, path, '', ''))


def image_request(settings, prompt):
    provider, endpoint = image_endpoint(settings.image_base_url, settings.image_provider)
    payload = {'model': settings.image_model, 'prompt': prompt, 'n': 1}
    if provider == 'openai_compatible':
        payload['size'] = settings.image_size
        return provider, endpoint, payload
    payload.update(prompt=prompt[:1500], response_format='base64', prompt_optimizer=False)
    if settings.image_size == 'auto':
        payload['aspect_ratio'] = '16:9'
    else:
        match = re.fullmatch(r'(\d+)x(\d+)', settings.image_size)
        if not match:
            raise ValueError('MiniMax 图片尺寸请填写 auto 或宽x高，例如 1024x1024')
        width, height = map(int, match.groups())
        if settings.image_model == 'image-01-live':
            divisor = gcd(width, height)
            ratio = f'{width // divisor}:{height // divisor}'
            if ratio not in {'1:1', '16:9', '4:3', '3:2', '2:3', '3:4', '9:16'}:
                raise ValueError('image-01-live 不支持该尺寸比例，请使用 auto 或支持的标准宽高比')
            payload['aspect_ratio'] = ratio
        else:
            if any(value < 512 or value > 2048 or value % 8 for value in (width, height)):
                raise ValueError('MiniMax 自定义宽高须为 512–2048 且为 8 的倍数')
            payload.update(width=width, height=height)
    return provider, endpoint, payload


def image_result(provider, data):
    if not isinstance(data, dict):
        raise ValueError('生图响应不是有效对象')
    if provider == 'minimax':
        status = data.get('base_resp')
        if not isinstance(status, dict) or type(status.get('status_code')) is not int:
            raise ValueError('MiniMax 生图响应缺少明确业务状态，结果未确认，不自动重复请求')
        code = status['status_code']
        if code:
            raise ImageBusinessError(code)
        result = data.get('data')
        if not isinstance(result, dict):
            raise ValueError('MiniMax 未返回图片数据，可能被过滤；不自动重复请求')
        encoded, urls = result.get('image_base64'), result.get('image_urls')
        if isinstance(encoded, list) and encoded and isinstance(encoded[0], str) and encoded[0]:
            return 'base64', encoded[0]
        if isinstance(urls, list) and urls and isinstance(urls[0], str) and urls[0]:
            return 'url', urls[0]
        raise ValueError('MiniMax 未返回 image_base64 或 image_urls；不自动重复请求')
    rows = data.get('data')
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise ValueError('兼容生图接口未返回有效 data 数组')
    if isinstance(rows[0].get('b64_json'), str) and rows[0]['b64_json']:
        return 'base64', rows[0]['b64_json']
    if isinstance(rows[0].get('url'), str) and rows[0]['url']:
        return 'url', rows[0]['url']
    raise ValueError('兼容生图接口未返回 b64_json 或图片 URL')
