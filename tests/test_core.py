import json
import tempfile
import unittest
from pathlib import Path
from restleague.core import Generator, Schema, Target, load_spec, redact, run
from tests.fixture import serve, specification


class CoreTests(unittest.TestCase):
    def test_openapi3_operations(self):
        self.assertEqual(len(Generator(specification()).operations), 8)

    def test_swagger2_parameters(self):
        spec = {'swagger': '2.0', 'paths': {'/items/{id}': {'get': {'parameters': [{'name': 'id', 'in': 'path', 'required': True, 'type': 'integer', 'enum': [7]}], 'responses': {}}}}}
        g = Generator(spec)
        self.assertEqual(g.request(g.operations[0])[0], '/items/7')

    def test_local_ref(self):
        self.assertEqual(Schema({'definitions': {'Item': {'type': 'integer'}}}).resolve({'$ref': '#/definitions/Item'}), {'type': 'integer'})

    def test_external_ref_rejected(self):
        with self.assertRaisesRegex(ValueError, 'External'):
            Schema({}).resolve({'$ref': 'https://example.invalid/schema.json'})

    def test_recursive_ref_terminates(self):
        s = Schema({'x': {'$ref': '#/x'}})
        self.assertEqual(s.resolve({'$ref': '#/x'}), {})

    def test_allof_properties(self):
        schema = Schema({}).resolve({'allOf': [{'type': 'object', 'properties': {'a': {'type': 'string'}}}, {'properties': {'b': {'type': 'integer'}}}]})
        self.assertEqual(set(schema['properties']), {'a', 'b'})

    def test_scope_does_not_mix_ids(self):
        g = Generator(specification())
        for op in g.operations:
            if op.method == 'post':
                g.observe(op, 201, {'id': 'only-' + op.scope})
        for op in g.operations:
            if '{id}' in op.path:
                path, _, _, _ = g.request(op)
                self.assertIn('only-' + op.scope, path)

    def test_independent_ignores_feedback(self):
        g = Generator(specification(), mechanism='independent')
        g.observe(g.operations[0], 201, {'id': 'opaque'})
        self.assertFalse(g.pool)

    def test_failed_response_ignored(self):
        g = Generator(specification())
        g.observe(g.operations[0], 500, {'id': 'opaque'})
        self.assertFalse(g.pool)

    def test_secrets_not_cached(self):
        g = Generator(specification())
        g.observe(g.operations[0], 200, {'access_token': 'do-not-store', 'id': 'okay'})
        self.assertNotIn('do-not-store', json.dumps(dict((str(k), v) for k, v in g.pool.items())))

    def test_redaction_recursive(self):
        self.assertEqual(redact({'nested': [{'password': 'x'}]}), {'nested': [{'password': '[REDACTED]'}]})

    def test_nonlocal_denied_without_authorization(self):
        with self.assertRaisesRegex(ValueError, 'authorization'):
            Target('https://example.invalid/')

    def test_target_credentials_rejected(self):
        with self.assertRaises(ValueError):
            Target('http://secret@example.invalid/')

    def test_schema_server_not_used(self):
        spec = specification()
        spec['servers'] = [{'url': 'https://example.invalid'}]
        g = Generator(spec)
        self.assertTrue(g.request(g.operations[0])[0].startswith('/'))

    def test_request_body_generated(self):
        g = Generator(specification())
        op = next(o for o in g.operations if o.method == 'post')
        _, _, headers, body = g.request(op)
        self.assertEqual(headers['Content-Type'], 'application/json')
        self.assertTrue(json.loads(body)['name'])

    def test_path_encoding(self):
        spec = {'openapi': '3.0.3', 'paths': {'/a/{id}': {'get': {'parameters': [{'name': 'id', 'in': 'path', 'schema': {'example': 'a/b?c#d'}}]}}}}
        g = Generator(spec)
        self.assertEqual(g.request(g.operations[0])[0], '/a/a%2Fb%3Fc%23d')

    def test_load_json_yaml(self):
        with tempfile.TemporaryDirectory() as d:
            for suffix in ['json', 'yaml']:
                p = Path(d) / ('spec.' + suffix)
                p.write_text(json.dumps(specification()))
                self.assertEqual(load_spec(p)['openapi'], '3.0.3')

    def test_budget_and_logs_live_loopback(self):
        with tempfile.TemporaryDirectory() as d, serve() as endpoint:
            summary = run(specification(), endpoint, d, max_requests=8, seconds=5, rate=100, auth_headers={'X-Custom-Credential': 'never-log-this'})
            self.assertEqual(summary['requests_sent'], 8)
            logs = (Path(d) / 'http.jsonl').read_text()
            self.assertEqual(len(logs.splitlines()), 8)
            self.assertNotIn('never-log-this', logs)
            self.assertEqual(len(summary['covered_operations']), 8)

if __name__ == '__main__':
    unittest.main()
