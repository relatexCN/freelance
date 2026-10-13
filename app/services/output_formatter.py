"""Bounded, deterministic formatting. Never infer missing business values."""
import json
import math
import re

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


class OutputFormatError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# Deliberately excludes references, regexes and recursive/combinatorial schemas.
_SCHEMA_KEYS = {'type', 'properties', 'required', 'additionalProperties', 'items',
                'enum', 'const', 'minimum', 'maximum', 'exclusiveMinimum',
                'exclusiveMaximum', 'minLength', 'maxLength', 'minItems',
                'maxItems', 'minProperties', 'maxProperties', 'title', 'description'}


def validate_schema(schema, depth=0):
    if depth > 12 or not isinstance(schema, dict) or set(schema) - _SCHEMA_KEYS:
        raise ValueError('Unsupported output_schema keyword, type or nesting depth')
    if len(json.dumps(schema, ensure_ascii=False, allow_nan=False)) > 16384:
        raise ValueError('output_schema exceeds 16384 characters')
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError('Invalid output_schema') from exc
    for child in schema.get('properties', {}).values():
        validate_schema(child, depth + 1)
    for key in ('items', 'additionalProperties'):
        child = schema.get(key)
        if isinstance(child, dict):
            validate_schema(child, depth + 1)


def formatting_contract(request):
    """Return effective mode/schema without forwarding gateway-only controls."""
    schema = request.output_schema
    fmt = request.response_format
    required = request.output_format == 'json' or schema is not None
    if fmt and fmt.type in ('json_object', 'json_schema'):
        required = True
        if fmt.type == 'json_schema':
            container = getattr(fmt, 'json_schema', None)
            if not isinstance(container, dict) or not isinstance(container.get('schema'), dict):
                raise ValueError('response_format.json_schema.schema is required')
            upstream_schema = container['schema']
            if schema is not None and schema != upstream_schema:
                raise ValueError('output_schema conflicts with response_format schema')
            schema = upstream_schema
        elif schema is None:
            schema = {'type': 'object'}
    if request.output_format == 'text' and required:
        raise ValueError('output_format=text conflicts with JSON/schema requirements')
    if schema is not None:
        validate_schema(schema)
    return ('json' if required else request.output_format), schema


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError('Non-finite JSON number')


def _check_value(value, depth=0):
    if depth > 64 or (isinstance(value, float) and not math.isfinite(value)):
        raise ValueError('Invalid JSON depth or number')
    children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    for child in children:
        _check_value(child, depth + 1)


def format_output(content, mode, schema=None, finish_reason=None):
    if mode == 'text':
        return content, None, 'text'
    raw = (content or '').strip().lstrip('\ufeff')
    structured_hint = raw.startswith(('{', '[', '```json'))
    if finish_reason == 'length' and (mode == 'json' or structured_hint):
        raise OutputFormatError('output_truncated', 'Model output reached its token limit; no JSON repair attempted')
    if len(raw) > 262144:
        if mode == 'json' or structured_hint:
            raise OutputFormatError('output_too_large', 'Structured output exceeds formatting limit')
        return content, None, 'text'
    fenced = re.fullmatch(r'```(?:json)?\s*\n?(.*?)\s*```', raw, re.DOTALL | re.IGNORECASE)
    candidate = fenced.group(1).strip() if fenced else raw
    decoder = json.JSONDecoder(object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    try:
        try:
            data = decoder.decode(candidate)
        except json.JSONDecodeError:
            # Only extract the first object/array; never salvage nested pieces of
            # malformed/truncated JSON, and reject multiple possible answers.
            positions = [i for char in ('{', '[') if (i := candidate.find(char)) >= 0]
            if not positions:
                raise ValueError('No JSON object or array')
            start = min(positions)
            data, end = decoder.raw_decode(candidate, start)
            tail = candidate[end:]
            if start == 0 or any(char in tail for char in '{}[]'):
                raise ValueError('Ambiguous or trailing JSON')
        if not isinstance(data, (dict, list)):
            raise ValueError('Expected an object or array')
        _check_value(data)
    except (ValueError, RecursionError) as exc:
        if mode == 'json' or structured_hint:
            raise OutputFormatError('invalid_output_json', 'Model output is not a single complete JSON object/array') from exc
        return content, None, 'text'
    if finish_reason == 'length':
        raise OutputFormatError('output_truncated', 'Model output reached its token limit')
    if schema is not None and next(Draft202012Validator(schema).iter_errors(data), None) is not None:
        raise OutputFormatError('output_schema_mismatch', 'Model output does not satisfy the requested schema')
    return json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':')), data, 'json'
