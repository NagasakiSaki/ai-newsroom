"""只在用户点击时测试微信令牌接口；不上传素材、不创建草稿。"""
import hashlib
import hmac
import ipaddress
import re
import socket
import ssl
import threading
import time
from typing import Literal

import httpx
from pydantic import BaseModel

from .db import now
from .network import options
from .wechat import ERROR_HINTS


class WechatTestResult(BaseModel):
    ok: bool
    status: Literal['success', 'not_configured', 'network_error', 'http_error', 'wechat_error', 'invalid_response']
    checked_at: str
    duration_ms: int
    message: str
    expires_in: int | None = None
    wechat_errcode: int | None = None
    http_status: int | None = None
    failure_stage: Literal['dns', 'tls', 'timeout', 'connect', 'request'] | None = None
    whitelist_ip: str | None = None


def network_failure_stage(error):
    """只检查异常类型，不回显可能含凭证的异常文本。"""
    seen, current = set(), error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, socket.gaierror):
            return 'dns'
        if isinstance(current, ssl.SSLError):
            return 'tls'
        current = current.__cause__ or current.__context__
    if isinstance(error, httpx.TimeoutException):
        return 'timeout'
    return 'connect' if isinstance(error, httpx.ConnectError) else 'request'


def whitelist_ip(data):
    """只提取微信 40164 中的出口 IP，不保留 errmsg、rid 或其他响应字段。"""
    message = data.get('errmsg')
    if not isinstance(message, str):
        return None
    match = re.search(r'\binvalid ip\s+([^\s,;]+)', message, re.IGNORECASE)
    if match:
        try:
            address = ipaddress.ip_address(match.group(1).strip('[]'))
            return str(address) if address.is_global else None
        except ValueError:
            pass
    return None


class WechatConnectionStatus(BaseModel):
    configured: bool
    appid_masked: str
    secret_configured: bool
    testing: bool
    last_test: WechatTestResult | None = None


class WechatConnection:
    URL = 'https://api.weixin.qq.com/cgi-bin/stable_token'

    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.lock = threading.Lock()

    def fingerprint(self):
        value = self.settings.wechat_app_id + '\0' + self.settings.wechat_app_secret
        return hmac.new(self.settings.admin_token.encode(), value.encode(), hashlib.sha256).hexdigest()

    def status(self):
        appid = self.settings.wechat_app_id
        last = None
        try:
            record = self.store.get('integrations', 'wechat_connection')
            if record.get('credential_fingerprint') == self.fingerprint():
                last = WechatTestResult.model_validate(record['result'])
        except KeyError:
            pass
        return WechatConnectionStatus(
            configured=bool(appid and self.settings.wechat_app_secret),
            appid_masked=(appid[:2] + '…' + appid[-4:]) if len(appid) >= 8 else ('已填写' if appid else '未填写'),
            secret_configured=bool(self.settings.wechat_app_secret), testing=self.lock.locked(), last_test=last)

    def test(self):
        if not self.lock.acquire(blocking=False):
            raise ValueError('微信连接测试正在进行，请等待结果，不要重复点击')
        started = time.monotonic()
        try:
            result = self.probe()
            result.update(checked_at=now(), duration_ms=round((time.monotonic() - started) * 1000))
            safe = WechatTestResult.model_validate(result)
            self.store.put('integrations', {'id': 'wechat_connection', 'credential_fingerprint': self.fingerprint(), 'result': safe.model_dump()})
            return safe
        finally:
            self.lock.release()

    def probe(self):
        if not self.settings.wechat_app_id or not self.settings.wechat_app_secret:
            return {'ok': False, 'status': 'not_configured', 'message': '尚未配置完整 AppID 和 AppSecret；请在 API 与网络设置中保存后再测试。'}
        # 不复用内存缓存：每次点击实际请求一次。保留 TLS，不使用 force_refresh，也不自动重试。
        try:
            with httpx.Client(timeout=httpx.Timeout(20, connect=8), **options(self.settings, 'wechat')) as client:
                response = client.post(self.URL, json={
                    'grant_type': 'client_credential', 'appid': self.settings.wechat_app_id,
                    'secret': self.settings.wechat_app_secret, 'force_refresh': False})
            if response.status_code != 200:
                return {'ok': False, 'status': 'http_error', 'http_status': response.status_code,
                        'message': '微信接口未返回 HTTP 200；请检查网络、代理或服务状态，稍后手动重试。'}
            data = response.json()
        except httpx.RequestError as error:
            stage = network_failure_stage(error)
            reason = {'dns': '微信域名解析失败', 'tls': '微信 TLS 安全握手失败',
                      'timeout': '微信连接或响应超时', 'connect': '无法建立微信网络连接',
                      'request': '微信网络请求中断'}[stage]
            return {'ok': False, 'status': 'network_error', 'failure_stage': stage,
                    'message': reason + '。请检查 DNS 与网络；Clash 的 TUN 模式仍会接管应用直连，请使用规则模式并让 api.weixin.qq.com 走 DIRECT。未关闭 TLS 证书验证。'}
        except ValueError:
            return {'ok': False, 'status': 'invalid_response', 'message': '接口返回的内容不是有效 JSON，请检查网络或代理后手动重试。'}
        if not isinstance(data, dict):
            return {'ok': False, 'status': 'invalid_response', 'message': '微信返回的数据结构异常，未将本次测试判为成功。'}
        code = data.get('errcode', 0)
        if type(code) is not int:
            return {'ok': False, 'status': 'invalid_response', 'message': '微信错误码格式异常，未将本次测试判为成功。'}
        if code:
            # 不保存或回显 errmsg、响应体或异常，避免它们含凭证、令牌或请求信息。
            address = whitelist_ip(data) if code == 40164 else None
            hint = ERROR_HINTS.get(code, '请按官方说明核对 AppID、AppSecret 和接口权限')
            if code == 40164:
                hint = ('请在微信开发者平台的公众号基础信息 → API IP 白名单中添加当前出口 IP：' + address
                        if address else '请在微信开发者平台核对当前网络出口的 API IP 白名单')
                hint += '；网络或代理出口变化后需要重新核对'
            return {'ok': False, 'status': 'wechat_error', 'wechat_errcode': code,
                    'whitelist_ip': address, 'message': f'微信返回错误码 {code}；{hint}。'}
        token, expires = data.get('access_token'), data.get('expires_in')
        if not isinstance(token, str) or not token.strip() or type(expires) is not int or expires <= 0:
            return {'ok': False, 'status': 'invalid_response', 'message': '微信未返回有效令牌与有效期，未将本次测试判为成功。'}
        return {'ok': True, 'status': 'success', 'expires_in': expires,
                'message': '已成功连接微信并获取调用凭据。仅验证令牌接口，未验证草稿权限，也未上传素材、创建草稿或发表文章。'}
