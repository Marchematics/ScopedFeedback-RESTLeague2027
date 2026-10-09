"""OpenAPI 2/3 generation; deliberately no target-source or online schema access."""
from __future__ import annotations
import collections
import http.client
import ipaddress
import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit, parse_qsl

METHODS = {'get', 'post', 'put', 'patch', 'delete', 'head', 'options'}
SECRET = re.compile(r'authorization|cookie|password|passwd|secret|token|api.?key', re.I)
MAX_BYTES = 1024 * 1024


def load_spec(path):
    raw = Path(path).read_text(encoding='utf-8')
    if len(raw.encode()) > 16 * MAX_BYTES:
        raise ValueError('Specification exceeds 16 MiB')
    if str(path).lower().endswith('.json'):
        obj = json.loads(raw)
    else:
        import yaml
        obj = yaml.safe_load(raw)
    if not isinstance(obj, dict) or not isinstance(obj.get('paths'), dict):
        raise ValueError('Expected an OpenAPI 2/3 document with paths')
    if not (str(obj.get('openapi', '')).startswith('3.') or obj.get('swagger') == '2.0'):
        raise ValueError('Only OpenAPI 3.x and Swagger 2.0 are supported')
    return obj


def resource(part):
    return re.sub(r'[^a-z0-9]', '', part.lower()).removesuffix('s')


def redact(value):
    if isinstance(value, dict):
        return {k: '[REDACTED]' if SECRET.search(k) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def redact_echoes(value, secrets):
    """Remove auth echoes from payload data before JSON encoding, never its syntax."""
    representations = sorted({x for secret in secrets for x in (secret, quote(secret, safe='')) if x}, key=len, reverse=True)

    def clean(item):
        if isinstance(item, dict):
            return {clean(k): clean(v) for k, v in item.items()}
        if isinstance(item, list):
            return [clean(v) for v in item]
        if isinstance(item, tuple):
            return tuple(clean(v) for v in item)
        if isinstance(item, str):
            if item == '[REDACTED]':
                return item
            for secret in representations:
                item = item.replace(secret, '[REDACTED]')
            return item
        if item is not None and json.dumps(item) in secrets:
            return '[REDACTED]'
        return item

    return clean(value)


class Schema:
    def __init__(self, spec):
        self.spec = spec

    def resolve(self, node, seen=()):
        if not isinstance(node, dict):
            return {}
        node = dict(node)
        ref = node.pop('$ref', None)
        if ref:
            if not ref.startswith('#/'):
                raise ValueError('External $ref is disabled: supply a bundled local specification')
            if ref in seen:
                return {}
            target = self.spec
            for part in ref[2:].split('/'):
                target = target[part.replace('~1', '/').replace('~0', '~')]
            target = self.resolve(target, seen + (ref,))
            target.update(node)
            node = target
        if 'allOf' in node:
            result = {k: v for k, v in node.items() if k != 'allOf'}
            for item in node['allOf']:
                item = self.resolve(item, seen)
                result.setdefault('properties', {}).update(item.get('properties', {}))
                result['required'] = list(dict.fromkeys(result.get('required', []) + item.get('required', [])))
                result.update({k: v for k, v in item.items() if k not in {'properties', 'required'}})
            node = result
        return node


@dataclass
class Operation:
    method: str
    path: str
    data: dict
    parameters: list
    consumes: list

    @property
    def key(self):
        return self.method.upper() + ' ' + self.path

    @property
    def scope(self):
        return '/'.join(resource(x) for x in self.path.split('/') if x and not x.startswith('{'))


class Generator:
    def __init__(self, spec, seed=0, mechanism='scoped-feedback'):
        self.spec = spec
        self.schema = Schema(spec)
        self.rng = random.Random(seed)
        self.mechanism = mechanism
        self.pool = collections.defaultdict(list)
        self.last_scope = {}
        self.operations = []
        for path, data in spec['paths'].items():
            if not path.startswith('/') or path.startswith('//'):
                raise ValueError('OpenAPI paths must be absolute API paths')
            data = self.schema.resolve(data)
            common = data.get('parameters', [])
            for method, op in data.items():
                if method.lower() not in METHODS:
                    continue
                op = self.schema.resolve(op)
                op['_effective_servers'] = op.get('servers', data.get('servers', spec.get('servers', [])))
                params = {}
                for p in common + op.get('parameters', []):
                    p = self.schema.resolve(p)
                    params[p['in'], p['name']] = p
                self.operations.append(Operation(method.lower(), path, op, list(params.values()), op.get('consumes', spec.get('consumes', ['application/json']))))
        # Create first; shallow paths first; destructive operations last.
        self.operations.sort(key=lambda x: (x.method == 'delete', x.path.count('/'), x.method != 'post', x.path, x.method))
        if not self.operations:
            raise ValueError('No supported HTTP operations in specification')

    def _lookup(self, scope, name):
        keys = [(scope, name.lower())]
        own_resource = scope.rsplit('/', 1)[-1].split('=', 1)[0]
        if re.sub(r'[^a-z0-9]', '', name.lower()) == own_resource + 'id':
            keys.append((scope, 'id'))
        values = [v for key in keys for v in self.pool.get(key, [])]
        return self.rng.choice(values) if values else None

    def value(self, schema, name='', scope='', depth=0):
        s = self.schema.resolve(schema)
        if depth > 8:
            return None
        for key in ('example', 'default'):
            if key in s:
                return s[key]
        if s.get('enum'):
            return self.rng.choice(s['enum'])
        if 'const' in s:
            return s['const']
        if 'oneOf' in s or 'anyOf' in s:
            return self.value(self.rng.choice(s.get('oneOf', s.get('anyOf'))), name, scope, depth + 1)
        typ = s.get('type', 'object' if 'properties' in s else 'string')
        if isinstance(typ, list):
            typ = next((t for t in typ if t != 'null'), 'null')
        if self.mechanism == 'scoped-feedback' and typ not in {'object', 'array'}:
            cached = self._lookup(scope, name)
            if cached is not None and ((typ == 'string' and isinstance(cached, str)) or (typ in {'number', 'integer'} and isinstance(cached, (float, int)) and not isinstance(cached, bool))):
                return cached
        if typ == 'object':
            return {k: self.value(v, k, scope, depth + 1) for k, v in s.get('properties', {}).items() if not self.schema.resolve(v).get('readOnly')}
        if typ == 'array':
            size = min(8, max(1, s.get('minItems', 1)))
            if s.get('maxItems') is not None:
                size = min(size, s['maxItems'])
            return [self.value(s.get('items', {}), name, scope, depth + 1) for _ in range(size)]
        if typ in {'number', 'integer'}:
            low = s.get('minimum', 0)
            high = s.get('maximum', low + 10)
            if isinstance(s.get('exclusiveMinimum'), (int, float)) and not isinstance(s.get('exclusiveMinimum'), bool):
                low = s['exclusiveMinimum'] + 1
            elif s.get('exclusiveMinimum'):
                low += 1
            if isinstance(s.get('exclusiveMaximum'), (int, float)) and not isinstance(s.get('exclusiveMaximum'), bool):
                high = s['exclusiveMaximum'] - 1
            elif s.get('exclusiveMaximum'):
                high -= 1
            if high < low:
                return low
            return self.rng.randint(int(low), int(high)) if typ == 'integer' else self.rng.uniform(low, high)
        if typ == 'boolean':
            return bool(self.rng.getrandbits(1))
        if typ == 'null':
            return None
        formats = {'date': '2026-10-09', 'date-time': '2026-10-09T00:00:00Z', 'email': 'fixture@example.invalid', 'uuid': '00000000-0000-4000-8000-000000000001', 'uri': 'https://example.invalid/', 'hostname': 'example.invalid', 'ipv4': '127.0.0.1', 'byte': 'dGVzdA=='}
        val = formats.get(s.get('format'), 'test' + str(self.rng.randrange(100000)))
        val = val.ljust(min(4096, s.get('minLength', 0)), 'a')
        return val[:min(4096, s.get('maxLength', 4096))]

    def _bound_scope(self, op, bindings, before=None):
        parts, scope = [p for p in op.path.split('/') if p], []
        for index, part in enumerate(parts):
            if part.startswith('{'):
                name = part[1:-1]
                if name == before:
                    break
                if index < len(parts) - 1 and scope and name in bindings:
                    scope[-1] += '=' + quote(str(bindings[name]), safe='')
            else:
                scope.append(resource(part))
        return '/'.join(scope)

    def request(self, op):
        if op.data.get('_effective_servers') != self.spec.get('servers', []):
            raise ValueError('Unsupported operation/path-specific servers override')
        if op.consumes and op.consumes[0] == 'multipart/form-data':
            raise ValueError('Unsupported multipart body')
        path, query, headers, body, form = op.path, [], {}, None, []
        bindings = {}
        ordered = sorted(op.parameters, key=lambda p: (p['in'] != 'path', op.path.find('{' + p['name'] + '}') if p['in'] == 'path' else 0))
        for p in ordered:
            name, location = p['name'], p['in']
            schema = p.get('schema', p)
            parameter_content = p.get('content', {})
            if parameter_content:
                if set(parameter_content) != {'application/json'}:
                    raise ValueError('Unsupported parameter content type')
                schema = parameter_content['application/json'].get('schema', {})
            if 'example' in p:
                schema = dict(schema, example=p['example'])
            scope = self._bound_scope(op, bindings, before=name if location == 'path' else None)
            value = self.value(schema, name, scope)
            if parameter_content:
                value = json.dumps(value, separators=(',', ':'))
            style = p.get('style', 'simple' if location in {'path', 'header'} else 'form')
            if location in {'path', 'header'} and style != 'simple':
                raise ValueError('Unsupported path/header parameter style')
            if location == 'path' and isinstance(value, (list, dict)):
                if isinstance(value, dict):
                    value = ','.join(f'{k}={v}' for k, v in value.items()) if p.get('explode', False) else ','.join(str(x) for pair in value.items() for x in pair)
                else:
                    value = ','.join(map(str, value))
            if location == 'path':
                bindings[name] = value
                path = path.replace('{' + name + '}', quote(str(value), safe=''))
            elif location in {'query', 'formData'}:
                destination = query if location == 'query' else form
                if isinstance(value, list):
                    if self.spec.get('swagger') == '2.0':
                        fmt = p.get('collectionFormat', 'csv')
                        if fmt == 'multi':
                            destination.extend((name, v) for v in value)
                        else:
                            separator = {'csv': ',', 'ssv': ' ', 'tsv': '\t', 'pipes': '|'}.get(fmt, ',')
                            destination.append((name, separator.join(map(str, value))))
                    elif p.get('style', 'form') == 'form' and p.get('explode', True):
                        destination.extend((name, v) for v in value)
                    else:
                        separator = {'spaceDelimited': ' ', 'pipeDelimited': '|'}.get(p.get('style'), ',')
                        destination.append((name, separator.join(map(str, value))))
                elif isinstance(value, dict):
                    if style == 'deepObject':
                        destination.extend((f'{name}[{k}]', v) for k, v in value.items())
                    elif style == 'form' and p.get('explode', True):
                        destination.extend(value.items())
                    elif style == 'form':
                        destination.append((name, ','.join(str(x) for pair in value.items() for x in pair)))
                    else:
                        raise ValueError('Unsupported object query style')
                else:
                    destination.append((name, str(value).lower() if isinstance(value, bool) else value))
            elif location == 'header':
                if isinstance(value, list):
                    value = ','.join(map(str, value))
                elif isinstance(value, dict):
                    value = ','.join(f'{k}={v}' for k, v in value.items()) if p.get('explode', False) else ','.join(str(x) for pair in value.items() for x in pair)
                headers[name] = str(value)
            elif location == 'cookie':
                if isinstance(value, (dict, list)):
                    raise ValueError('Unsupported complex cookie parameter')
                headers['Cookie'] = headers.get('Cookie', '') + name + '=' + quote(str(value), safe='') + '; '
            elif location == 'body':
                body = self.value(schema, scope=scope)
        request_scope = self._bound_scope(op, bindings)
        self.last_scope[op.key] = request_scope
        content_type = op.consumes[0] if op.consumes else 'application/json'
        if 'requestBody' in op.data:
            req = self.schema.resolve(op.data['requestBody'])
            content = req.get('content', {})
            content_type = next((k for k in content if k == 'application/json' or k.endswith('+json')), next(iter(content), 'application/json'))
            media = content.get(content_type, {})
            body = media.get('example', self.value(media.get('schema', {}), scope=request_scope))
        if form or (content_type == 'application/x-www-form-urlencoded' and isinstance(body, dict)):
            body = urlencode(form or list(body.items()), doseq=True).encode()
            content_type = 'application/x-www-form-urlencoded'
        elif body is not None:
            if content_type == 'application/json' or content_type.endswith('+json'):
                body = json.dumps(body, separators=(',', ':')).encode()
            elif content_type.startswith('text/'):
                body = str(body).encode()
            else:
                raise ValueError('Unsupported body media type: ' + content_type)
        if body is not None:
            headers['Content-Type'] = content_type
        headers['Accept'] = 'application/json'
        return path, query, headers, body

    def observe(self, op, status, data):
        if self.mechanism != 'scoped-feedback' or not 200 <= status < 300:
            return
        scope = self.last_scope.get(op.key, op.scope)
        # Only the main resource object(s), not unrelated nested objects/envelopes.
        items = data[:64] if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            for k, v in item.items():
                if SECRET.search(k):
                    continue
                if isinstance(v, (str, int, float, bool)) and len(str(v)) < 4096:
                    key = (scope, k.lower())
                    if v not in self.pool[key]:
                        self.pool[key].append(v)
                        self.pool[key] = self.pool[key][-64:]



class Target:
    """Explicit origin confinement. No proxies, redirects, DNS to unapproved origins."""
    def __init__(self, base_url, allowed_host=None, timeout=2):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Target must be an http(s) URL without credentials/query/fragment')
        hostname = parsed.hostname
        try:
            local = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            local = hostname == 'localhost'
        if not local and hostname != allowed_host:
            raise ValueError('Non-loopback target requires explicit --allow-target-host authorization')
        self.parsed, self.timeout = parsed, timeout
        self.base_path = parsed.path.rstrip('/')
        self.origin = f'{parsed.scheme}://{parsed.netloc}'

    def send(self, method, path, query, headers, body, timeout=None):
        # http.client never follows redirects or honors HTTP_PROXY.
        route = self.base_path + path
        if not path.startswith('/') or path.startswith('//') or any(c in route for c in '\r\n'):
            raise ValueError('Invalid relative API path')
        if query:
            route += '?' + urlencode(query, doseq=True)
        cls = http.client.HTTPSConnection if self.parsed.scheme == 'https' else http.client.HTTPConnection
        conn = cls(self.parsed.hostname, self.parsed.port, timeout=timeout or self.timeout)
        start = time.monotonic()
        try:
            conn.request(method.upper(), route, body=body, headers=headers)
            response = conn.getresponse()
            data = response.read(MAX_BYTES + 1)
            return response.status, dict(response.getheaders()), data[:MAX_BYTES], len(data) > MAX_BYTES, time.monotonic() - start
        finally:
            conn.close()


def run(spec, base_url, out, seed=0, mechanism='scoped-feedback', max_requests=120,
        seconds=60, rate=20, allowed_host=None, auth_headers=None, auth_query=None,
        trace_stream=None):
    if max_requests < 0 or seconds <= 0 or rate <= 0:
        raise ValueError('Positive time/rate and nonnegative request cap required')
    generator = Generator(spec, seed, mechanism)
    target = Target(base_url, allowed_host)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'summary.json').exists():
        raise FileExistsError('Refusing to overwrite an existing run summary')
    start = time.monotonic()
    counts, covered, failures, errors = collections.Counter(), set(), set(), 0
    sent, attempt = 0, 0
    with (out / 'http.jsonl').open('x', encoding='utf-8') as log:
        while (not max_requests or sent < max_requests) and time.monotonic() - start < seconds:
            op = generator.operations[attempt % len(generator.operations)]
            attempt += 1
            record = {'record_type': 'http_attempt', 'attempt': attempt, 'operation': op.key, 'elapsed_s': time.monotonic() - start}
            secret_values = []
            try:
                path, query, headers, body = generator.request(op)
                headers.update(auth_headers or {})
                query.extend((auth_query or {}).items())
                secret_values = [str(v) for v in list((auth_headers or {}).values()) + list((auth_query or {}).values()) if v]
                secret_values += [str(v) for k, v in headers.items() if SECRET.search(k) and v]
                secret_values += [str(v) for k, v in query if SECRET.search(k) and v]
                # Suppress all caller-supplied auth header values in logs, including custom names.
                safe_headers = {k: '[REDACTED]' if k in (auth_headers or {}) or SECRET.search(k) else v for k, v in headers.items()}
                safe_query = [(k, '[REDACTED]' if SECRET.search(k) or k in (auth_query or {}) else v) for k, v in query]
                safe_path = path
                for parameter in op.parameters:
                    if parameter.get('in') == 'path' and SECRET.search(parameter['name']):
                        safe_path = op.path  # Do not persist a credential-bearing expanded path
                safe_body = None
                if body:
                    content_type = headers.get('Content-Type', '').split(';')[0]
                    if content_type == 'application/json' or content_type.endswith('+json'):
                        safe_body = redact(json.loads(body))
                    elif content_type == 'application/x-www-form-urlencoded':
                        safe_body = [(k, '[REDACTED]' if SECRET.search(k) else v) for k, v in parse_qsl(body.decode(), keep_blank_values=True)]
                    else:
                        safe_body = {'omitted': 'non-JSON/non-form body', 'bytes': len(body)}
                record['request'] = {'method': op.method.upper(), 'url': target.origin + target.base_path + safe_path, 'query': safe_query, 'headers': safe_headers, 'body': safe_body}
                remaining = seconds - (time.monotonic() - start)
                if remaining <= 0:
                    break
                sent += 1
                status, resp_headers, raw, truncated, latency = target.send(op.method, path, query, headers, body, min(target.timeout, remaining))
                counts[str(status)] += 1
                try:
                    payload = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    payload = raw.decode('utf-8', errors='replace')
                safe_response = redact(payload) if not isinstance(payload, str) else {'omitted': 'non-JSON response', 'bytes': len(raw)}
                record['response'] = {'status': status, 'headers': {k: v for k, v in resp_headers.items() if k.lower() in {'content-type', 'content-length', 'date'}}, 'body': safe_response, 'truncated': truncated, 'latency_s': latency}
                if 200 <= status < 300:
                    covered.add(op.key)
                if status >= 500:
                    failures.add((op.key, status, json.dumps(redact(payload), sort_keys=True)))
                generator.observe(op, status, payload)
            except (OSError, ValueError, KeyError, http.client.HTTPException) as e:
                errors += 1
                record['error'] = {'type': type(e).__name__, 'category': 'generation_or_transport_error'}
            record['covered_operations'] = len(covered)
            if 'request' in record:
                request = record['request']
                request['url'] = redact_echoes(request['url'], secret_values)
                request['query'] = [(k, redact_echoes(v, secret_values)) for k, v in request['query']]
                request['headers'] = {k: redact_echoes(v, secret_values) for k, v in request['headers'].items()}
                request['body'] = redact_echoes(request['body'], secret_values)
            if 'response' in record:
                record['response']['body'] = redact_echoes(record['response']['body'], secret_values)
                record['response']['headers'] = {k: redact_echoes(v, secret_values) for k, v in record['response']['headers'].items()}
            encoded = json.dumps(record, sort_keys=True)
            log.write(encoded + '\n')
            log.flush()
            if trace_stream is not None:
                trace_stream.write(encoded + '\n')
                trace_stream.flush()
            delay = min(1 / rate, seconds - (time.monotonic() - start))
            if delay > 0:
                time.sleep(delay)
            # Invalid specs must not spin forever without making requests.
            if attempt >= max(100, max_requests * 5) and sent == 0:
                break
    summary = {'record_type': 'summary', 'mechanism': mechanism, 'seed': seed, 'requests_sent': sent, 'attempts': attempt,
               'operation_count': len(generator.operations), 'covered_operations': sorted(covered),
               'status_counts': dict(counts), 'unique_5xx_signatures_internal': len(failures), 'client_errors': errors,
               'elapsed_s': time.monotonic() - start, 'official_metrics': False,
               'limits': {'max_requests': max_requests, 'seconds': seconds, 'rate': rate},
               'notes': 'Internal operation coverage; 5xx signatures are not Restats unique faults or code coverage.'}
    with (out / 'summary.json').open('x', encoding='utf-8') as handle:
        handle.write(json.dumps(summary, indent=2, sort_keys=True) + '\n')
    return summary
