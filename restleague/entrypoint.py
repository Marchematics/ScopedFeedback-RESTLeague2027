"""RESTgym adapter. HOST is the explicit organizer-authorized sandbox target."""
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from .core import load_spec, run


def configuration(env):
    host = env.get('HOST')
    port = env.get('PORT')
    if not host or not port or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError('RESTgym requires HOST and valid PORT')
    if any(x in host for x in '/?#@\r\n'):
        raise ValueError('HOST must be a plain hostname or IP')
    api = env.get('API', '')
    if api and not all(x.isalnum() or x in '-_' for x in api):
        raise ValueError('API must be a simple slug')
    candidates = [env.get('OPENAPI_SPEC', '')] if env.get('OPENAPI_SPEC') else [f'/specifications/{api}-openapi.json', f'/specifications/{api}.yaml', f'/specifications/{api}.yml']
    path = next((p for p in candidates if p and Path(p).is_file()), None)
    if not path:
        raise ValueError('Mount specification as OPENAPI_SPEC or /specifications/$API-openapi.json')
    spec = load_spec(path)
    base_path = env.get('BASE_PATH')
    if base_path is None:
        base_path = spec.get('basePath', '')
        if not base_path and spec.get('servers'):
            url = spec['servers'][0].get('url', '')
            for key, value in spec['servers'][0].get('variables', {}).items():
                url = url.replace('{' + key + '}', str(value.get('default', '')))
            base_path = urlsplit(url).path
    if base_path and (not base_path.startswith('/') or base_path.startswith('//')):
        raise ValueError('BASE_PATH must be an absolute API path')
    scheme = env.get('SCHEME', 'http')
    if scheme not in {'http', 'https'}:
        raise ValueError('SCHEME must be http or https')
    url_host = '[' + host + ']' if ':' in host and not host.startswith('[') else host
    return spec, f'{scheme}://{url_host}:{port}{base_path}', host.strip('[]'), float(env.get('TIME_BUDGET', '60')) * 60


def main():
    spec, url, host, seconds = configuration(os.environ)
    headers = json.loads(os.environ.get('RESTLEAGUE_AUTH_HEADERS', '{}'))
    auth_query = json.loads(os.environ.get('RESTLEAGUE_AUTH_QUERY', '{}'))
    if seconds <= 0:
        raise ValueError('TIME_BUDGET must be positive')
    output_root = Path(os.environ.get('OUTPUT_DIR', '/output'))
    output_root.mkdir(parents=True, exist_ok=True)
    session = Path(tempfile.mkdtemp(prefix='session-', dir=output_root))
    cycle = 0
    while True:
        output = session / f'cycle-{cycle:06d}'
        summary = run(spec, url, output, seed=int(os.environ.get('SEED', '20261009')) + cycle,
                      max_requests=0, seconds=seconds, rate=float(os.environ.get('RATE', '20')),
                      allowed_host=host, auth_headers=headers, auth_query=auth_query,
                      trace_stream=sys.stdout)
        print(json.dumps(summary), flush=True)
        cycle += 1

if __name__ == '__main__':
    main()
