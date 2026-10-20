"""采集新闻原文图片，保存未经改动的原文件及适合微信的展示副本。"""
import hashlib
import io
import ipaddress
import socket
import time
import warnings
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from .collect import canonical, FAKE_IP, FAKE_IP6, verify_public_dns
from .network import options

SOURCE_LIMIT = 20 * 1024 * 1024
PIXEL_LIMIT = 40_000_000
DISPLAY_LIMIT = 1_000_000
FORMATS = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp', 'GIF': '.gif', 'AVIF': '.avif'}


class SourceImageError(ValueError):
    def __init__(self, message, code='image_invalid', status=None):
        super().__init__(message)
        self.code, self.status = code, status


def image_url(url, settings=None, proxy_active=None):
    """每次重定向都核对地址；Fake-IP 通过独立公网 DNS 核对，不放宽内网限制。"""
    canonical(url)  # 校验 URL，但不重排 CDN 签名参数或改变原图路径。
    url = urlsplit(url)._replace(fragment='').geturl()
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.port not in {None, 443}:
        raise SourceImageError('原图仅允许公开 HTTPS 地址')
    host = parts.hostname
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise SourceImageError('图片地址不能指向本机、内网或保留地址')
    if host == 'localhost' or host.endswith(('.localhost', '.local')):
        raise SourceImageError('图片地址不能指向本机或内网域名')
    try:
        addresses = [ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]
    except OSError:
        raise SourceImageError('图片域名暂时无法解析', 'dns_failed') from None
    if not addresses or any(not address.is_global and address not in FAKE_IP and address not in FAKE_IP6 for address in addresses):
        raise SourceImageError('图片域名解析到了内网或保留地址')
    if any(address in FAKE_IP or address in FAKE_IP6 for address in addresses):
        if literal is not None:
            raise SourceImageError('不能直接请求 Fake-IP 图片地址')
        verify_public_dns(host, settings=settings, proxy_active=bool(proxy_active))
    return url


def fetch_image(url, source_url, settings=None):
    routes = [options(settings, 'news')]
    if routes[0].get('proxy'):
        routes.append({k: v for k, v in routes[0].items() if k != 'proxy'})
    for index, route in enumerate(routes):
        try:
            with httpx.Client(timeout=httpx.Timeout(15, connect=6), **route,
                    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36',
                             'Accept': 'image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8', 'Referer': source_url}) as client:
                current = url
                started = time.monotonic()
                for _ in range(5):
                    current = image_url(current, settings, proxy_active=bool(route.get('proxy')))
                    with client.stream('GET', current) as response:
                        if response.is_redirect:
                            current = urljoin(current, response.headers.get('location', ''))
                            continue
                        if response.status_code != 200:
                            raise SourceImageError('原图下载返回 HTTP ' + str(response.status_code), 'http_error', response.status_code)
                        mime = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
                        if mime and mime not in {'application/octet-stream', 'binary/octet-stream'} and not mime.startswith('image/'):
                            raise SourceImageError('图片地址返回的不是图片')
                        chunks, length = [], 0
                        for chunk in response.iter_bytes():
                            length += len(chunk)
                            if length > SOURCE_LIMIT or time.monotonic() - started > 60:
                                raise SourceImageError('原图超过下载大小或耗时限制')
                            chunks.append(chunk)
                        return b''.join(chunks), current
                raise SourceImageError('原图重定向过多')
        except SourceImageError as exc:
            if index + 1 < len(routes) and (exc.code == 'dns_failed' or exc.status in {403, 404, 410, 429, 502, 503, 504}):
                continue
            raise
        except (httpx.ProxyError, httpx.ConnectError):
            if index + 1 == len(routes):
                raise SourceImageError('无法建立原图下载连接；请检查新闻代理／TUN 分流', 'connect_failed') from None
        except httpx.HTTPError:
            if index + 1 < len(routes):
                continue
            raise SourceImageError('原图下载超时或中断，已尝试配置的可用路线', 'timeout') from None


def save_source_image(root, candidate, raw, final_url):
    """保留原始字节；只为微信生成无裁切的副本，不移除图片上的标识。"""
    if not raw or len(raw) > SOURCE_LIMIT:
        raise SourceImageError('原图为空或超过大小限制')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                fmt = source.format
                width, height = source.size
                if fmt not in FORMATS or width * height > PIXEL_LIMIT:
                    raise SourceImageError('原图格式不支持或像素过大')
                if width < 240 or height < 100:
                    raise SourceImageError('图片尺寸过小，已排除图标或追踪像素')
                animated = getattr(source, 'is_animated', False)
                source.seek(0)
                source.load()
                display = ImageOps.exif_transpose(source).copy()
    except SourceImageError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise SourceImageError('无法解码原图，可能是损坏文件或不支持的格式') from None
    original_sha = hashlib.sha256(raw).hexdigest()
    originals = Path(root) / 'original-media'
    originals.mkdir(parents=True, exist_ok=True)
    original_file = original_sha + FORMATS[fmt]
    (originals / original_file).write_bytes(raw)
    normalized = fmt not in {'PNG', 'JPEG'} or len(raw) >= DISPLAY_LIMIT or animated
    if not normalized:
        data, suffix = raw, FORMATS[fmt]
    else:
        # 白底合成透明通道，保持完整画面与长宽比例。
        if display.mode in {'RGBA', 'LA'} or 'transparency' in display.info:
            rgba = display.convert('RGBA')
            converted = Image.new('RGB', rgba.size, 'white')
            converted.paste(rgba, mask=rgba.getchannel('A'))
            display = converted
        else:
            display = display.convert('RGB')
        while True:
            for quality in (92, 85, 75, 65):
                output = io.BytesIO()
                display.save(output, format='JPEG', quality=quality, optimize=True)
                data = output.getvalue()
                if len(data) < DISPLAY_LIMIT:
                    break
            if len(data) < DISPLAY_LIMIT:
                break
            if min(display.size) < 160:
                raise SourceImageError('原图无法在保留完整画面的条件下适配微信大小限制')
            display = display.resize((max(1, round(display.width * .8)), max(1, round(display.height * .8))), Image.Resampling.LANCZOS)
        suffix = '.jpg'
    digest = hashlib.sha256(data).hexdigest()
    media = Path(root) / 'media'; media.mkdir(parents=True, exist_ok=True)
    file = digest + suffix
    (media / file).write_bytes(data)
    kind = 'official_image' if candidate['origin'] == 'official' else 'benchmark_image' if candidate['origin'] == 'benchmark' else 'media_image'
    credit = '图源：' + candidate['source_name'] + ({'official_image': '（官方原图）', 'benchmark_image': '（原始评测配图）', 'media_image': '（原报道配图）'}[kind])
    if animated:
        credit += ' · 首帧预览'
    return {'id': digest, 'file': file, 'path': str((media / file).resolve()), 'sha256': digest,
            'kind': kind, 'title': candidate['caption'], 'after_heading': candidate.get('after_heading', ''),
            'credit': credit, 'mime': 'image/png' if suffix == '.png' else 'image/jpeg',
            'source_url': candidate['source_url'], 'original_url': candidate['url'], 'download_url': final_url,
            'source_name': candidate['source_name'], 'image_id': candidate['id'], 'fact_ids': [],
            'original_file': original_file, 'original_sha256': original_sha,
            'original_width': width, 'original_height': height, 'width': display.width, 'height': display.height,
            'normalized_for_wechat': normalized, 'animated_original': animated}
