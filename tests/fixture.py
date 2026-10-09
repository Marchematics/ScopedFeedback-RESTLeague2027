"""Owned loopback-only CRUD fixture. Not an official benchmark or security target."""
import contextlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def specification():
    spec = {'openapi': '3.0.3', 'info': {'title': 'Owned CRUD fixture', 'version': '1'}, 'paths': {}}
    for name in ['projects', 'teams']:
        body = {'required': True, 'content': {'application/json': {'schema': {'type': 'object', 'required': ['name'], 'properties': {'name': {'type': 'string', 'minLength': 1}}}}}}
        response = {'200': {'description': 'OK'}}
        spec['paths']['/' + name] = {'post': {'requestBody': body, 'responses': response}, 'get': {'responses': response}}
        spec['paths']['/' + name + '/{id}'] = {'parameters': [{'name': 'id', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}], 'get': {'responses': response}, 'patch': {'requestBody': body, 'responses': response}}
    return spec


@contextlib.contextmanager
def serve():
    state = {'projects': {}, 'teams': {}}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def handle_api(self):
            parts = self.path.split('?')[0].strip('/').split('/')
            kind = parts[0]
            status, result = 404, {'error': 'not found'}
            payload = {}
            if self.headers.get('Content-Length'):
                try:
                    payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                except ValueError:
                    status, result = 400, {'error': 'invalid json'}
            if kind in state:
                items = state[kind]
                if len(parts) == 1 and self.command == 'POST':
                    if isinstance(payload.get('name'), str) and payload['name']:
                        ident = f'{kind}-opaque-{len(items) + 1:05d}'
                        items[ident] = {'id': ident, 'name': payload['name']}
                        status, result = 201, items[ident]
                    else:
                        status, result = 400, {'error': 'name required'}
                elif len(parts) == 1 and self.command == 'GET':
                    status, result = 200, list(items.values())
                elif len(parts) == 2 and parts[1] in items:
                    if self.command == 'PATCH':
                        if not isinstance(payload.get('name'), str) or not payload['name']:
                            status, result = 400, {'error': 'name required'}
                        else:
                            items[parts[1]]['name'] = payload['name']
                            status, result = 200, items[parts[1]]
                    elif self.command == 'GET':
                        status, result = 200, items[parts[1]]
            raw = json.dumps(result).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        do_GET = do_POST = do_PATCH = handle_api
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
