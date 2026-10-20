"""使机器可读接口说明与现有 Bearer 中间件一致，不改变鉴权行为。"""
from fastapi.openapi.utils import get_openapi


def install_openapi(app):
    def schema():
        if app.openapi_schema:
            return app.openapi_schema
        result = get_openapi(title=app.title, version=app.version, routes=app.routes)
        result['info']['description'] = (
            '本地单人 AI 新闻编辑台。除 health 和一次性票据兑换接口外，/api/* 使用 Bearer 审核台口令。'
            '任务入队返回 HTTP 200，不代表采编成功；通过 /api/state 轮询任务、运行、稿件与发布状态。'
            '桌面工作流止于建立微信草稿；一键批量提交所有未入库真实稿件，支持本地格式修正与明确失败的有限重试。'
            '历史 publish 路径保留兼容，桌面运行时固定仅存草稿；正式发表请在公众号后台完成。'
            '发布记录区分准备、草稿已建、已受理、处理中、成功、明确失败与结果待核对，超时不会自动重提。'
        )
        result['servers'] = [{'url': 'http://127.0.0.1:8088'}]
        schemes = result.setdefault('components', {}).setdefault('securitySchemes', {})
        schemes['AdminBearer'] = {'type': 'http', 'scheme': 'bearer', 'description': 'ADMIN_TOKEN 或 data/admin-token.txt；不是模型或公众号密钥'}
        for path, methods in result['paths'].items():
            for method, operation in methods.items():
                if method not in {'get', 'post', 'put', 'patch', 'delete', 'head', 'options'}:
                    continue
                operation['security'] = [{'AdminBearer': []}] if path.startswith('/api/') and path not in {'/api/health', '/api/desktop/login'} else []
                if path == '/':
                    operation['responses']['200']['content'] = {'text/html': {'schema': {'type': 'string'}}}
                elif path == '/api/drafts/{draft_id}/export/{format}':
                    operation['responses']['200']['description'] = '当前版本附件；来源复查失败可改变状态并返回 409，详见 API.md。'
                    operation['responses']['200']['content'] = {
                        mime: {'schema': {'type': 'string', 'format': 'binary'}}
                        for mime in ('text/html', 'text/markdown', 'application/json', 'application/zip', 'application/octet-stream')
                    }
        app.openapi_schema = result
        return result
    app.openapi = schema
