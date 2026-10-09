import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from restleague.core import Generator, run
from restleague.entrypoint import configuration
from tests.fixture import specification


def one_param(p, version='3.0.3', consumes=None):
    spec = {'paths': {'/items': {'get': {'parameters': [p]}}}}
    if version == '2.0':
        spec['swagger'] = version
    else:
        spec['openapi'] = version
    if consumes:
        spec['consumes'] = consumes
    return spec


class AdapterTests(unittest.TestCase):
    def test_swagger_array_formats(self):
        for fmt, expected in [('csv', [('tags', '1,2')]), ('multi', [('tags', 1), ('tags', 2)]), ('pipes', [('tags', '1|2')])]:
            p = {'name': 'tags', 'in': 'query', 'type': 'array', 'example': [1, 2], 'collectionFormat': fmt}
            g = Generator(one_param(p, '2.0'))
            self.assertEqual(g.request(g.operations[0])[1], expected)
        p.pop('collectionFormat')
        g = Generator(one_param(p, '2.0'))
        self.assertEqual(g.request(g.operations[0])[1], [('tags', '1,2')])

    def test_oas3_query_object(self):
        for style, expected in [('form', [('role', 'admin')]), ('deepObject', [('filter[role]', 'admin')])]:
            p = {'name': 'filter', 'in': 'query', 'style': style, 'schema': {'example': {'role': 'admin'}}}
            g = Generator(one_param(p))
            self.assertEqual(g.request(g.operations[0])[1], expected)

    def test_multipart_rejected(self):
        p = {'name': 'file', 'in': 'formData', 'type': 'string'}
        g = Generator(one_param(p, '2.0', ['multipart/form-data']))
        with self.assertRaisesRegex(ValueError, 'multipart'):
            g.request(g.operations[0])

    def test_nested_id_not_collected(self):
        g = Generator(specification())
        op = next(o for o in g.operations if o.path == '/projects' and o.method == 'post')
        g.observe(op, 201, {'id': 'p1', 'children': [{'id': 'c1'}]})
        self.assertEqual(g.pool[('project', 'id')], ['p1'])

    def test_foreign_key_not_given_own_id(self):
        g = Generator(specification())
        op = next(o for o in g.operations if o.path == '/projects' and o.method == 'post')
        g.observe(op, 201, {'id': 'p1'})
        self.assertNotEqual(g.value({'type': 'string'}, 'customerId', 'project'), 'p1')
        self.assertEqual(g.value({'type': 'string'}, 'projectId', 'project'), 'p1')

    def test_parent_scope_separates_child_pool(self):
        spec = {'openapi': '3.0.3', 'paths': {'/projects/{projectId}/items/{id}': {'get': {'parameters': [{'name': 'id', 'in': 'path', 'schema': {'type': 'string'}}, {'name': 'projectId', 'in': 'path', 'schema': {'type': 'string', 'example': 'p1'}}]}}}}
        g = Generator(spec)
        g.pool['project=p1/item', 'id'] = ['child1']
        g.pool['project=p2/item', 'id'] = ['child2']
        self.assertEqual(g.request(g.operations[0])[0], '/projects/p1/items/child1')

    def test_entrypoint_basepath_minutes(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'spec.json'
            s = specification(); s['servers'] = [{'url': 'https://example.invalid/api/v2'}]
            p.write_text(json.dumps(s))
            _, url, host, seconds = configuration({'HOST': '127.0.0.1', 'PORT': '9999', 'OPENAPI_SPEC': str(p), 'TIME_BUDGET': '1.5'})
            self.assertEqual((url, host, seconds), ('http://127.0.0.1:9999/api/v2', '127.0.0.1', 90))

    def test_entrypoint_requires_host_port(self):
        with self.assertRaises(ValueError):
            configuration({})

    def test_local_server_override_rejected(self):
        spec = specification(); spec['paths']['/projects']['servers'] = [{'url': '/different'}]
        g = Generator(spec)
        op = next(o for o in g.operations if o.path == '/projects')
        with self.assertRaisesRegex(ValueError, 'servers'):
            g.request(op)

    def test_logs_redact_auth_echo_and_location(self):
        response = (200, {'Location': '/done?token=not-to-record', 'Content-Type': 'application/json'}, b'{"echo":"sentinel-credential", "password":"hidden"}', False, 0.1)
        with tempfile.TemporaryDirectory() as d, patch('restleague.core.Target.send', return_value=response):
            run(specification(), 'http://127.0.0.1:1', d, max_requests=1, seconds=2, rate=100, auth_headers={'X-Custom': 'sentinel-credential'})
            text = (Path(d) / 'http.jsonl').read_text()
            for secret in ['sentinel-credential', 'not-to-record', 'hidden']:
                self.assertNotIn(secret, text)
            json.loads(text)

    def test_logs_omit_nonjson_and_exception_message(self):
        with tempfile.TemporaryDirectory() as d, patch('restleague.core.Target.send', return_value=(200, {}, b'sentinel-credential', False, .1)):
            run(specification(), 'http://127.0.0.1:1', d, max_requests=1, rate=100)
            self.assertNotIn('sentinel-credential', (Path(d) / 'http.jsonl').read_text())
        with tempfile.TemporaryDirectory() as d, patch('restleague.core.Target.send', side_effect=OSError('sentinel-credential')):
            run(specification(), 'http://127.0.0.1:1', d, max_requests=1, rate=100)
            self.assertNotIn('sentinel-credential', (Path(d) / 'http.jsonl').read_text())

if __name__ == '__main__':
    unittest.main()
