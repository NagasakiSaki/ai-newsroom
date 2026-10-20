"""本机页面配置：脱敏读取、原子保存；Windows 使用当前用户 DPAPI 加密。"""
import base64
import ctypes
import json
import os
from dataclasses import replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .editors import choose_editor, resolve_profile
from .network import local_proxy_hint, options, proxy_url

PREFIX = {'writer': 'llm', 'prompt': 'prompt', 'review': 'review', 'visual': 'visual'}
MANAGED = {'demo', 'json_mode', 'fast_mode', 'max_output_tokens', 'wechat_app_id', 'wechat_app_secret', 'news_proxy_url', 'editorial_mode', 'review_mode'} | {
    prefix + '_' + suffix for prefix in PREFIX.values() for suffix in ('base_url', 'api_key', 'model', 'proxy_url')}
IMAGE_FIELDS = {'image_enabled', 'image_provider', 'image_size', 'merged_preparation'} | {'image_' + s for s in ('base_url', 'api_key', 'model', 'proxy_url')}
MANAGED |= IMAGE_FIELDS
FILENAME = 'runtime-settings.json'


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)


def text(value):
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('字段不得包含控制字符')
    return value.strip()


def model_url(value):
    value = text(value).rstrip('/')
    if not value:
        return ''
    try:
        parts = urlsplit(value)
        valid = (parts.scheme == 'https' and parts.hostname and not parts.username and not parts.password
                 and not parts.query and not parts.fragment and (parts.port is None or parts.port > 0))
    except ValueError:
        valid = False
    if not valid or any(c.isspace() for c in value):
        raise ValueError('模型 Base URL 需要 HTTPS，不得含账号、密钥、查询参数或 fragment')
    if parts.path.endswith('/chat/completions'):
        raise ValueError('请填写 Base URL，不要包含 /chat/completions')
    return value


class ModelConfigPatch(StrictConfig):
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200)
    api_key: SecretStr | None = Field(default=None, max_length=4096)
    clear_api_key: bool = False
    proxy_url: str | None = Field(default=None, max_length=200)

    @field_validator('base_url')
    @classmethod
    def base(cls, value):
        return model_url(value) if value is not None else value

    @field_validator('model')
    @classmethod
    def model_id(cls, value):
        return text(value) if value is not None else value

    @field_validator('proxy_url')
    @classmethod
    def proxy(cls, value):
        return proxy_url(value) if value is not None else value


class ModelsPatch(StrictConfig):
    writer: ModelConfigPatch | None = None
    prompt: ModelConfigPatch | None = None
    review: ModelConfigPatch | None = None
    visual: ModelConfigPatch | None = None


class ImageGenerationPatch(ModelConfigPatch):
    enabled: bool | None = None
    provider: Literal['auto', 'openai_compatible', 'minimax'] | None = None
    size: str | None = Field(default=None, pattern=r'^(?:auto|[1-9][0-9]{2,3}x[1-9][0-9]{2,3})$')

    @field_validator('base_url')
    @classmethod
    def image_base(cls, value):
        return value.removesuffix('/images/generations') if value else value


class WechatConfigPatch(StrictConfig):
    app_id: str | None = Field(default=None, max_length=100)
    app_secret: SecretStr | None = Field(default=None, max_length=4096)
    clear_app_secret: bool = False

    @field_validator('app_id')
    @classmethod
    def appid(cls, value):
        return text(value) if value is not None else value


class RuntimeConfigPatch(StrictConfig):
    merged_preparation: bool | None = None
    image_generation: ImageGenerationPatch | None = None
    fast_mode: bool | None = None
    editorial_mode: Literal['auto', 'manual'] | None = None
    review_mode: Literal['off', 'advisory', 'strict'] | None = None
    demo: bool | None = None
    json_mode: bool | None = None
    max_output_tokens: int | None = Field(default=None, ge=0)
    models: ModelsPatch | None = None
    wechat: WechatConfigPatch | None = None
    news_proxy_url: str | None = Field(default=None, max_length=200)

    @field_validator('news_proxy_url')
    @classmethod
    def proxy(cls, value):
        return proxy_url(value) if value is not None else value


class ModelTestResult(BaseModel):
    ok: bool
    profile: Literal['writer', 'prompt', 'review', 'visual']
    message: str
    route: Literal['direct', 'proxy']


def protect(raw, decrypt=False):
    """DPAPI 无交互，只绑定当前 Windows 用户，不自行管理加密密钥。"""
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_byte))]
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    target = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError('Windows 本机配置加密失败')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def load_saved(directory):
    file = Path(directory) / FILENAME
    if not file.exists():
        return {}
    try:
        envelope = json.loads(file.read_text(encoding='utf-8'))
        if envelope['format'] == 'windows-dpapi-v1' and os.name == 'nt':
            values = json.loads(protect(base64.b64decode(envelope['payload'], validate=True), decrypt=True))
        elif envelope['format'] == 'local-file-v1' and os.name != 'nt':
            values = envelope['values']
        else:
            raise ValueError()
        optional = {'editorial_mode', 'review_mode', 'fast_mode'} | IMAGE_FIELDS
        visual_fields = {'visual_' + suffix for suffix in ('base_url', 'api_key', 'model', 'proxy_url')}
        if not isinstance(values, dict) or not (MANAGED - optional - visual_fields <= set(values) <= MANAGED) or not all(
            type(value) is (bool if key in {'demo', 'json_mode', 'fast_mode', 'image_enabled', 'merged_preparation'} else int if key == 'max_output_tokens' else str)
            for key, value in values.items()):
            raise ValueError()
        if (set(values) & visual_fields) not in (set(), visual_fields):
            raise ValueError()
        values.setdefault('editorial_mode', 'auto')
        values.setdefault('review_mode', 'off')
        values.setdefault('fast_mode', True)
        if values.get('image_provider', 'auto') not in {'auto', 'openai_compatible', 'minimax'}:
            raise ValueError()
        return values
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError('无法读取页面配置：文件损坏或不是当前系统用户保存的配置；请保留原文件后检查') from None


def persist(settings):
    values = {key: getattr(settings, key) for key in sorted(MANAGED)}
    try:
        if os.name == 'nt':
            envelope = {'format': 'windows-dpapi-v1', 'payload': base64.b64encode(
                protect(json.dumps(values, ensure_ascii=False).encode('utf-8'))).decode('ascii')}
        else:
            envelope = {'format': 'local-file-v1', 'values': values}
    except OSError:
        raise ValueError('本机配置加密失败；原有配置未更改，请检查当前 Windows 用户环境') from None
    temporary = None
    try:
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=settings.data_dir, prefix='.settings-', suffix='.tmp', delete=False) as file:
            temporary = Path(file.name)
            os.chmod(temporary, 0o600)
            json.dump(envelope, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, settings.data_dir / FILENAME)
    except OSError:
        raise ValueError('保存本机配置失败；原有配置未更改，请检查数据目录写入权限') from None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def patch_secret(current, incoming, clear):
    new = text(incoming.get_secret_value()) if incoming is not None else ''
    if clear and new:
        raise ValueError('填写新密钥与清除密钥不能同时选择')
    return '' if clear else new or current


def candidate(settings, patch):
    updates = {key: getattr(patch, key) for key in ('demo', 'json_mode', 'max_output_tokens', 'news_proxy_url', 'editorial_mode', 'review_mode', 'fast_mode')
               if getattr(patch, key) is not None}
    if patch.merged_preparation is not None:
        updates['merged_preparation'] = patch.merged_preparation
    if patch.image_generation:
        change = patch.image_generation
        if change.base_url and settings.image_api_key:
            old, new = urlsplit(settings.image_base_url), urlsplit(change.base_url)
            if (old.hostname, old.port or 443) != (new.hostname, new.port or 443) and not (change.clear_api_key or (change.api_key and change.api_key.get_secret_value().strip())):
                raise ValueError('修改生图服务主机时请重新填写或清除密钥')
        for field in ('base_url', 'model', 'size', 'proxy_url', 'enabled', 'provider'):
            if getattr(change, field) is not None:
                updates['image_' + field] = getattr(change, field)
        updates['image_api_key'] = patch_secret(settings.image_api_key, change.api_key, change.clear_api_key)
        if updates.get('image_enabled', settings.image_enabled) and not all(updates.get('image_' + f, getattr(settings, 'image_' + f)) for f in ('base_url', 'model', 'api_key')):
            raise ValueError('启用生图前请配置 HTTPS Base URL、模型和密钥')
        if updates.get('image_enabled', settings.image_enabled):
            from .image_protocol import image_request
            image_request(replace(settings, **updates), 'configuration validation')
    if patch.models:
        for role, prefix in PREFIX.items():
            change = getattr(patch.models, role)
            if not change:
                continue
            old_base, old_key = getattr(settings, prefix + '_base_url'), getattr(settings, prefix + '_api_key')
            if change.base_url and old_key:
                old, new = urlsplit(old_base), urlsplit(change.base_url)
                changed_host = (old.hostname, old.port or 443) != (new.hostname, new.port or 443)
                explicit_key = change.api_key is not None and bool(text(change.api_key.get_secret_value()))
                if changed_host and not (explicit_key or change.clear_api_key):
                    raise ValueError('修改模型服务主机时，请重新填写 API Key 或明确清除旧 Key，避免把保存的密钥送往其他服务')
            for field in ('base_url', 'model', 'proxy_url'):
                if getattr(change, field) is not None:
                    updates[prefix + '_' + field] = getattr(change, field)
            updates[prefix + '_api_key'] = patch_secret(getattr(settings, prefix + '_api_key'), change.api_key, change.clear_api_key)
    if patch.wechat:
        if patch.wechat.app_id is not None:
            updates['wechat_app_id'] = patch.wechat.app_id
        updates['wechat_app_secret'] = patch_secret(settings.wechat_app_secret, patch.wechat.app_secret, patch.wechat.clear_app_secret)
    return replace(settings, **updates)


def readiness(settings, store):
    problems = []
    editor = choose_editor(store)
    routes = [('正文及选题', 'writer', ''), ('事实对照及审核', 'review', ''),
              ('当前默认小编提示词', editor['prompt_profile'], editor['prompt_model']),
              ('当前默认小编写作', editor['writer_profile'], editor['writer_model'])]
    for name, role, override in routes:
        try:
            route = resolve_profile(settings, role, override)
            if not (route['base_url'].startswith('https://') and route['api_key'] and route['model']):
                problems.append(name + '：请填写 HTTPS 地址、密钥和模型 ID')
        except ValueError:
            problems.append(name + '：模型服务配置不完整')
    return problems


def public_config(settings, store):
    from .image_protocol import image_endpoint
    try:
        provider, endpoint = image_endpoint(settings.image_base_url, settings.image_provider)
    except ValueError:
        provider, endpoint = None, None
    image_calls = sorted((row for row in store.all('image_calls') if row.get('endpoint') == endpoint
                          and row.get('model') == settings.image_model), key=lambda row: row.get('at', ''), reverse=True)
    last_image = ({key: image_calls[0].get(key) for key in ('at', 'outcome', 'error', 'http_status', 'provider_code', 'duration_ms')}
                  if image_calls else None)
    models = {}
    for role, prefix in PREFIX.items():
        models[role] = {'base_url': getattr(settings, prefix + '_base_url'), 'model': getattr(settings, prefix + '_model'),
                        'api_key_configured': bool(getattr(settings, prefix + '_api_key')),
                        'proxy_url': getattr(settings, prefix + '_proxy_url')}
    problems = readiness(settings, store)
    return {'demo': settings.demo, 'json_mode': settings.json_mode, 'max_output_tokens': settings.max_output_tokens,
            'merged_preparation': settings.merged_preparation,
            'image_generation': {'enabled': settings.image_enabled, 'provider': settings.image_provider,
                                 'resolved_provider': provider, 'endpoint': endpoint, 'last_result': last_image,
                                 'base_url': settings.image_base_url, 'model': settings.image_model,
                                 'size': settings.image_size, 'proxy_url': settings.image_proxy_url, 'api_key_configured': bool(settings.image_api_key)},
            'editorial_mode': settings.editorial_mode, 'review_mode': settings.review_mode, 'fast_mode': settings.fast_mode,
            'models': models, 'wechat': {'app_id': settings.wechat_app_id, 'secret_configured': bool(settings.wechat_app_secret)},
            'news_proxy_url': settings.news_proxy_url, 'wechat_route': 'direct', 'proxy_hint': local_proxy_hint(),
            'storage': 'Windows 当前用户 DPAPI 加密' if os.name == 'nt' else '本机权限 0600 文件',
            'ready': not problems, 'problems': problems,
            'saved': (settings.data_dir / FILENAME).exists()}


def ensure_idle(engine):
    if any(job['status'] in {'running', 'paused'} for job in engine.store.all('jobs')):
        raise ValueError('任务正在运行，请等待完成后保存配置或测试模型；不会中途切换服务')


def save_config(engine, patch):
    # 与任务领取串行，并与微信手动测试互斥；不等待长网络请求占用页面。
    if not engine.lock.acquire(blocking=False):
        raise ValueError('当前有操作正在进行，请稍后保存配置')
    acquired = False
    publish_acquired = False
    try:
        ensure_idle(engine)
        acquired = engine.wechat_connection.lock.acquire(blocking=False)
        if not acquired:
            raise ValueError('微信测试正在进行，请等待结束后保存配置')
        publish_acquired = engine.publish_lock.acquire(blocking=False)
        if not publish_acquired:
            raise ValueError('微信发布或权限检测正在进行，请等待结束后保存配置')
        updated = candidate(engine.settings, patch)
        problems = readiness(updated, engine.store)
        if not updated.demo and problems:
            raise ValueError('真实采编配置不完整：' + '；'.join(problems))
        previous_app_id = engine.settings.wechat_app_id
        persist(updated)
        engine.settings.__dict__.update(updated.__dict__)
        engine.wechat.token, engine.wechat.expires = '', 0
        # 账号换了就关闭自动发布，避免把稿件提交到另一个公众号。
        if updated.wechat_app_id != previous_app_id:
            engine.on_credentials_changed()
        return public_config(engine.settings, engine.store)
    finally:
        if publish_acquired:
            engine.publish_lock.release()
        if acquired:
            engine.wechat_connection.lock.release()
        engine.lock.release()


def test_model(engine, role):
    if not engine.lock.acquire(blocking=False):
        raise ValueError('当前有操作正在进行，请稍后测试模型')
    try:
        ensure_idle(engine)
        try:
            route = resolve_profile(engine.settings, role)
            if not (route['base_url'].startswith('https://') and route['api_key'] and route['model']):
                raise ValueError()
        except ValueError:
            raise ValueError('请先保存此服务的 HTTPS 地址、密钥和模型 ID') from None
        result = {'profile': role, 'route': 'proxy' if options(engine.settings, role).get('proxy') else 'direct'}
        try:
            with httpx.Client(timeout=httpx.Timeout(25, connect=8), **options(engine.settings, role)) as client:
                response = client.post(route['base_url'] + '/chat/completions', headers={'Authorization': 'Bearer ' + route['api_key']},
                                       json={'model': route['model'], 'messages': [{'role': 'user', 'content': 'Reply OK.'}], 'max_tokens': 64})
            if response.status_code != 200:
                return {**result, 'ok': False, 'message': f'模型接口 HTTP {response.status_code}，请核对地址、模型权限、余额和代理；未回显响应正文'}
            data = response.json()
            message = data['choices'][0]['message']
            if not isinstance(message, dict) or not isinstance(message.get('content'), str) or not message['content'].strip():
                raise ValueError()
            return {**result, 'ok': True, 'message': '真实模型请求成功；本次只验证基本调用，不代表复杂采编及结构化输出均已通过'}
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            return {**result, 'ok': False, 'message': '未能取得有效模型响应，请检查网络、代理和接口协议；未回显凭证、异常或响应正文'}
    finally:
        engine.lock.release()
