"""Optional display-only copies of native tool calls; originals remain untouched."""
import json
import re

from app.config import config, LoggingSettings
from app.core.observability import EventFormatter


def _secrets():
    values = [config.settings.api_key]
    values.extend(config.settings.api_keys.split(','))
    for provider in config.providers.values():
        values.extend(provider.api_keys)
    return [value for value in values if value]


def _safe(value):
    if isinstance(value, dict):
        return {key: '[REDACTED]' if re.sub(r'[^a-z]', '', key.lower()) in {
            'apikey', 'token', 'accesstoken', 'refreshtoken', 'appsecret',
            'secret', 'clientsecret', 'password', 'authorization', 'cookie'
        } else _safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if isinstance(value, str):
        # Preserve boundaries; suppress credential assignments in shell commands.
        value = re.sub(r'''(?i)(\b(?:\w*(?:TOKEN|SECRET|PASSWORD)|API_KEY)\s*=\s*)("[^"\n]*"|'[^'\n]*'|[^\s;]+)''',
                       r'\1[REDACTED]', value)
        value = re.sub(r'''(?i)(--(?:api-key|token|password|secret|access-token)(?:\s+|=))("[^"\n]*"|'[^'\n]*'|[^\s;]+)''',
                       r'\1[REDACTED]', value)
        return EventFormatter(LoggingSettings(max_field_chars=config.settings.tools.detail_max_chars), _secrets).redact(value)
    return value


def _block(value):
    text = json.dumps(_safe(value), ensure_ascii=False, indent=2)
    # JSON escapes command newlines; render a command separately as actual lines.
    if isinstance(value, dict) and isinstance(value.get('command'), str):
        remaining = {key: item for key, item in value.items() if key != 'command'}
        text = str(_safe(value['command']))
        if remaining:
            text += '\n\n其他参数：\n' + json.dumps(_safe(remaining), ensure_ascii=False, indent=2)
    limit = config.settings.tools.detail_max_chars
    if len(text) > limit:
        return '详情超出展示上限，已省略；请查看执行端日志。'
    fence = '`' * max(3, max((len(m.group()) for m in re.finditer(r'`+', text)), default=0) + 1)
    return fence + '\n' + text + '\n' + fence


def call_details(calls):
    sections = []
    for call in calls[:32]:
        function = call.get('function') or {}
        args = function.get('arguments', '{}')
        try:
            if isinstance(args, str) and len(args) > config.settings.tools.detail_max_chars:
                sections.append('工具参数超出展示上限，已省略。')
                continue
            args = json.loads(args) if isinstance(args, str) else args
            if not isinstance(args, dict):
                raise ValueError()
            body = _block(args)
        except (ValueError, TypeError):
            body = '参数不完整或无法解析，已省略；不代表工具已执行。'
        identity = _block({'tool': function.get('name', ''), 'tool_call_id': call.get('id', '')})
        sections.append(identity + '\n' + body)
    if not sections:
        return ''
    text = '\n\n'.join(sections)
    if len(text) > config.settings.tools.detail_max_chars:
        text = '工具详情总长度超出展示上限，已省略；请查看执行端日志。'
    return '\n\n【网关补充：模型请求的工具操作，尚未确认执行】\n' + text


def recent_results(request):
    if not config.settings.tools.append_results:
        return ''
    results = []
    for message in reversed(request.messages):
        if message.role != 'tool':
            break
        content = message.content
        try:
            content = json.loads(content) if isinstance(content, str) else content
        except ValueError:
            # Unstructured outputs may include credentials with unknown formats.
            content = {'notice': '非结构化工具结果已省略，请查看执行端日志。'}
        results.append({'tool_call_id': message.tool_call_id, 'result': content})
        if len(results) == 32:
            break
    if not results:
        return ''
    return '\n\n【网关补充：客户端本轮回传的工具结果，未经网关执行验证】\n' + _block(list(reversed(results)))


class ToolDetailStream:
    """Input is the provider's complete SSE frames, not arbitrary HTTP bytes."""
    def __init__(self, request):
        self.calls = {}
        self.finished = set()
        self.results = recent_results(request)
        self.result_sent = False
        self.size = 0
        self.overflow = False

    def feed(self, chunk):
        try:
            if not chunk.startswith('data:'):
                return [chunk]
            data = json.loads(chunk[5:].strip())
            if not isinstance(data, dict) or data.get('error'):
                return [chunk]
            added = []
            for choice in data.get('choices') or []:
                index = choice.get('index', 0)
                if index in self.finished:
                    continue
                for tool in (choice.get('delta') or {}).get('tool_calls') or []:
                    key = (index, tool.get('index', 0))
                    if len(self.calls) >= 32 and key not in self.calls:
                        self.overflow = True
                        continue
                    call = self.calls.setdefault(key, {'id': '', 'function': {'name': '', 'arguments': ''}})
                    for target, source, field in [(call, tool, 'id'), (call['function'], tool.get('function') or {}, 'name'),
                                                  (call['function'], tool.get('function') or {}, 'arguments')]:
                        part = source.get(field) or ''
                        self.size += len(part)
                        if self.size <= config.settings.tools.detail_max_chars:
                            target[field] += part
                        else:
                            self.overflow = True
                if choice.get('finish_reason') in {'tool_calls', 'stop', 'function_call'}:
                    self.finished.add(index)
                    text = ''
                    if not self.result_sent and self.results:
                        text += self.results
                        self.result_sent = True
                    if self.overflow:
                        text += '\n\n【网关补充】工具参数超出展示上限，已省略。'
                    else:
                        text += call_details([call for (ci, _), call in self.calls.items() if ci == index])
                    if text:
                        extra = {key: data[key] for key in ('id', 'object', 'created', 'model') if key in data}
                        extra['choices'] = [{'index': index, 'delta': {'content': text}, 'finish_reason': None}]
                        added.append('data: ' + json.dumps(extra, ensure_ascii=False) + '\n\n')
            return added + [chunk]
        except (ValueError, TypeError, AttributeError):
            return [chunk]
