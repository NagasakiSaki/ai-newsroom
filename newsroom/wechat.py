"""公众号官方接口的最小适配：令牌、素材、草稿与发布任务。

错误分为两类，发布状态机依赖这个区分：
- WechatAPIError：平台返回了业务错误码，可判定本次请求未被接受，属于明确失败。
- WechatUncertain：超时、连接中断、5xx 或响应不可解析，结果未知，不能盲目重试。
"""
import time
from pathlib import Path

import httpx

from .network import options
from .render import render_article

COVER_TYPES = {'.png', '.jpg', '.jpeg', '.gif', '.bmp'}
INLINE_TYPES = {'.jpg', '.jpeg', '.png'}
COVER_LIMIT = 10 * 1024 * 1024
INLINE_LIMIT = 1024 * 1024

# 官方文档中与发布链路直接相关的错误码，仅用于给出安全的中文说明。
ERROR_HINTS = {
    40001: '调用凭据无效或已过期，将重新获取令牌后再试',
    40005: '素材文件格式不符合接口要求',
    40007: 'media_id 无效或素材已被删除',
    40009: '图片尺寸或大小超出接口限制',
    40125: 'AppSecret 无效，请核对后台设置',
    40164: '本机出口 IP 不在公众号 IP 白名单中',
    43002: '该接口要求 POST 请求',
    45009: '接口调用超过天级频率限制',
    45011: '调用过于频繁，需要稍后再试',
    48001: '接口未授权。官方文档标注 freepublish 系列仅认证公众号（企业主体）可调用；个人或未认证账号会返回此错误',
    53503: '该草稿未通过发布检查，请在公众号后台核对草稿内容',
    53504: '平台要求前往公众平台官网使用该草稿',
    53505: '平台要求先手动保存成功后再发表',
    89503: '此次调用需要公众号管理员确认',
}


class WechatError(Exception):
    pass


class WechatAPIError(WechatError):
    """平台明确返回错误码；本次请求已被拒绝。"""

    def __init__(self, errcode, endpoint='', whitelist_ip=None):
        self.errcode, self.endpoint = errcode, endpoint
        self.whitelist_ip = whitelist_ip
        hint = ERROR_HINTS.get(errcode, '请对照官方文档检查权限、配额和参数')
        if whitelist_ip:
            hint += '；请保留原条目，并将 ' + whitelist_ip + ' 加入该公众号 API IP 白名单'
        super().__init__(f'微信接口错误码 {errcode}（{endpoint}）：{hint}')


class WechatUncertain(WechatError):
    """请求可能已被平台处理，但本地没有拿到可判定结果。"""


class Wechat:
    BASE = 'https://api.weixin.qq.com/cgi-bin/'

    def __init__(self, settings):
        self.settings = settings
        self.token, self.expires = '', 0

    def access_token(self, refresh=False):
        if self.token and not refresh and time.time() < self.expires:
            return self.token
        if not self.settings.wechat_app_id or not self.settings.wechat_app_secret:
            raise WechatError('缺少公众号 AppID / AppSecret')
        # stable_token 普通模式：有效期内重复调用不会让其他服务的令牌失效。
        try:
            with httpx.Client(timeout=httpx.Timeout(20, connect=8), **options(self.settings, 'wechat')) as client:
                response = client.post(self.BASE + 'stable_token', json={
                    'grant_type': 'client_credential', 'appid': self.settings.wechat_app_id,
                    'secret': self.settings.wechat_app_secret, 'force_refresh': False})
        except httpx.HTTPError:
            raise WechatUncertain('获取微信令牌失败，请检查网络与后台 IP 白名单') from None
        if response.status_code >= 500:
            raise WechatUncertain('微信令牌接口暂时不可用，结果未确认') from None
        try:
            data = response.json()
        except ValueError:
            raise WechatUncertain('微信令牌响应不是有效 JSON，结果未确认') from None
        self.check(data, 'stable_token')
        self.token, self.expires = data['access_token'], time.time() + data['expires_in'] - 120
        return self.token

    @staticmethod
    def check(data, endpoint=''):
        if not isinstance(data, dict):
            raise WechatUncertain('微信返回无法识别的数据结构')
        code = data.get('errcode', 0)
        if type(code) is not int:
            raise WechatUncertain('微信错误码格式异常，结果未确认')
        if code:
            from .connection import whitelist_ip
            raise WechatAPIError(code, endpoint, whitelist_ip(data) if code == 40164 else None)

    def call(self, endpoint, body=None, method='POST', params=None, files=None, allow_reauth=True):
        token = self.access_token()
        query = {'access_token': token, **(params or {})}
        try:
            with httpx.Client(timeout=httpx.Timeout(30, connect=10), **options(self.settings, 'wechat')) as client:
                if method == 'GET':
                    response = client.get(self.BASE + endpoint, params=query)
                elif files is not None:
                    response = client.post(self.BASE + endpoint, params=query, files=files)
                else:
                    response = client.post(self.BASE + endpoint, params=query, json=body if body is not None else {})
        except httpx.HTTPError:
            raise WechatUncertain('微信请求结果未确认；请先查询状态，不要重复提交发布') from None
        if response.status_code >= 500:
            raise WechatUncertain('微信接口返回服务端错误，结果未确认；请查询状态')
        try:
            data = response.json()
        except ValueError:
            raise WechatUncertain('微信响应不是有效 JSON，结果未确认') from None
        if response.status_code >= 400 and (not isinstance(data, dict) or not data.get('errcode')):
            raise WechatUncertain('微信接口返回异常状态，结果未确认')
        if isinstance(data, dict) and data.get('errcode') in {40001, 40014, 42001} and allow_reauth:
            self.token, self.expires = '', 0
            self.access_token(refresh=True)
            return self.call(endpoint, body, method, params, files, allow_reauth=False)
        self.check(data, endpoint)
        return data

    # 兼容旧调用名：草稿同步沿用 request()。
    def request(self, endpoint, body):
        return self.call(endpoint, body)

    def upload_cover(self, path):
        path = Path(path)
        if not path.is_file():
            raise WechatError('封面文件不存在，请核对路径')
        if path.suffix.lower() not in COVER_TYPES:
            raise WechatError('封面素材需要 PNG／JPEG／GIF／BMP 文件')
        if path.stat().st_size > COVER_LIMIT:
            raise WechatError('封面素材需要小于 10MB')
        data = self.call('material/add_material', params={'type': 'image'},
                         files={'media': (path.name, path.read_bytes())})
        media_id = data.get('media_id')
        if not isinstance(media_id, str) or not media_id:
            raise WechatUncertain('封面素材上传结果未确认，未取得 media_id')
        return media_id

    def upload_inline_image(self, path):
        path = Path(path)
        if not path.is_file():
            raise WechatError('正文图片不存在，请核对路径')
        if path.suffix.lower() not in INLINE_TYPES:
            raise WechatError('正文图片仅支持 JPG／PNG')
        if path.stat().st_size >= INLINE_LIMIT:
            raise WechatError('正文图片必须小于 1MB')
        data = self.call('media/uploadimg', files={'media': (path.name, path.read_bytes())})
        url = data.get('url')
        if not isinstance(url, str) or not url:
            raise WechatUncertain('正文图片上传结果未确认，未取得 URL')
        return url

    def cover_id(self):
        if self.settings.wechat_thumb:
            return self.settings.wechat_thumb
        path = Path(self.settings.wechat_cover)
        if not self.settings.wechat_cover or not path.is_file():
            raise WechatError('请配置 WECHAT_THUMB_MEDIA_ID 或有使用权的 WECHAT_COVER_PATH')
        return self.upload_cover(path)

    def add_draft(self, article):
        return self.call('draft/add', {'articles': [article]})['media_id']

    def create_draft(self, draft):
        article = draft['article']
        # 兼容旧 DELIVERY_MODE=wechat_draft 入口，正文不能残留本机 /media/ 地址。
        from .editorial_team import asset_file, original_asset_file
        assets = (draft.get('visuals') or {}).get('assets', [])
        paths = []
        for asset in assets:
            path = asset_file(self.settings.data_dir, asset)
            if asset.get('original_file'):
                original_asset_file(self.settings.data_dir, asset)
            paths.append((asset, path))
        image_urls = {asset['id']: self.upload_inline_image(path) for asset, path in paths}
        return self.add_draft({'title': article['title'], 'author': self.settings.wechat_author,
                               'digest': article['digest'], 'content': render_article(draft, image_urls=image_urls),
                               'content_source_url': draft['news_url'], 'thumb_media_id': self.cover_id(),
                               'need_open_comment': 0, 'only_fans_can_comment': 0})

    def draft_detail(self, media_id):
        return self.call('draft/get', {'media_id': media_id})

    def draft_list(self, offset=0, count=20, no_content=0):
        # 对账需要标题与署名，必须请求 content（no_content=0）。
        return self.call('draft/batchget', {'offset': offset, 'count': count, 'no_content': no_content})

    def draft_exists(self, media_id):
        try:
            self.draft_detail(media_id)
        except WechatAPIError as error:
            if error.errcode == 40007:
                return False
            raise
        return True

    def draft_count(self):
        return int(self.call('draft/count', method='GET').get('total_count', 0) or 0)

    def material_count(self):
        return self.call('material/get_materialcount', method='GET')

    def submit_publish(self, media_id):
        return self.call('freepublish/submit', {'media_id': media_id})

    def query_publish(self, publish_id):
        return self.call('freepublish/get', {'publish_id': str(publish_id)})

    def published_list(self, offset=0, count=20, no_content=0):
        return self.call('freepublish/batchget', {'offset': offset, 'count': count, 'no_content': no_content})

    def published_article(self, article_id):
        return self.call('freepublish/getarticle', {'article_id': article_id})
