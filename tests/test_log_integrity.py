"""Regression cases for preserving and exporting redacted traces; no live HTTP."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote
from unittest.mock import patch
from restleague.core import run
from restleague.entrypoint import main
from tests.fixture import specification


class TraceIntegrityTests(unittest.TestCase):
    def response(self, payload):
        return (200, {'Content-Type': 'application/json'}, json.dumps(payload).encode(), False, .01)

    def test_existing_trace_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'http.jsonl').write_text('previous evidence\n')
            with patch('restleague.core.Target.send') as send:
                with self.assertRaises(FileExistsError):
                    run(specification(), 'http://127.0.0.1:1', path, max_requests=1)
                send.assert_not_called()
            self.assertEqual((path / 'http.jsonl').read_text(), 'previous evidence\n')

    def test_existing_summary_prevents_new_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'summary.json').write_text('{"previous": true}\n')
            with patch('restleague.core.Target.send') as send:
                with self.assertRaises(FileExistsError):
                    run(specification(), 'http://127.0.0.1:1', path, max_requests=1)
                send.assert_not_called()
            self.assertFalse((path / 'http.jsonl').exists())

    def test_short_numeric_auth_keeps_metadata_json_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('restleague.core.Target.send', return_value=self.response({'echo': '1', 'numeric_echo': 1, 'nested': ['value1']})):
                run(specification(), 'http://127.0.0.1:1', directory, max_requests=1, rate=100,
                    auth_headers={'X-Custom': '1'})
            record = json.loads((Path(directory) / 'http.jsonl').read_text())
            self.assertEqual(record['attempt'], 1)
            self.assertEqual(record['response']['status'], 200)
            self.assertEqual(record['response']['body']['echo'], '[REDACTED]')
            self.assertEqual(record['response']['body']['numeric_echo'], '[REDACTED]')
            self.assertEqual(record['response']['body']['nested'], ['value[REDACTED]'])

    def test_json_sensitive_secret_strings_and_keys(self):
        for secret in ['"', '\\', 'null', 'true', 'a', 'e', 'a/b?']:
            with self.subTest(secret=secret), tempfile.TemporaryDirectory() as directory:
                with patch('restleague.core.Target.send', return_value=self.response({'echo': secret, 'key-' + secret: secret, 'encoded': quote(secret, safe='')})):
                    run(specification(), 'http://127.0.0.1:1', directory, max_requests=1, rate=100,
                        auth_headers={'X-Custom': secret})
                record = json.loads((Path(directory) / 'http.jsonl').read_text())
                self.assertEqual(record['attempt'], 1)
                self.assertEqual(set(record['request']), {'method', 'url', 'query', 'headers', 'body'})
                self.assertEqual(record['request']['method'], 'POST')
                self.assertIn('Content-Type', record['request']['headers'])
                # Payload keys may themselves echo a secret; fixed trace keys may not.
                self.assertTrue(all(v == '[REDACTED]' for v in record['response']['body'].values()))

    def test_stream_matches_persisted_redacted_trace(self):
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with patch('restleague.core.Target.send', return_value=self.response({'echo': 'sentinel'})):
                run(specification(), 'http://127.0.0.1:1', directory, max_requests=1, rate=100,
                    auth_headers={'X-Custom': 'sentinel'}, trace_stream=stream)
            text = (Path(directory) / 'http.jsonl').read_text()
            self.assertEqual(text, stream.getvalue())
            self.assertNotIn('sentinel', text)
            json.loads(text)

    def test_stream_includes_client_error_records(self):
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with patch('restleague.core.Target.send', side_effect=OSError('private diagnostic')):
                run(specification(), 'http://127.0.0.1:1', directory, max_requests=1,
                    rate=100, trace_stream=stream)
            record = json.loads(stream.getvalue())
            self.assertEqual(record['record_type'], 'http_attempt')
            self.assertEqual(record['error']['type'], 'OSError')
            self.assertNotIn('private diagnostic', stream.getvalue())

    def test_two_adapter_launches_use_unique_session_and_stdout(self):
        class StopAfterCycle(Exception):
            pass
        paths = []
        def one_cycle(*args, **kwargs):
            paths.append(args[2])
            self.assertIs(kwargs['trace_stream'], stream)
            raise StopAfterCycle()
        with tempfile.TemporaryDirectory() as directory:
            env = {'OUTPUT_DIR': directory}
            with patch('restleague.entrypoint.configuration', return_value=(specification(), 'http://127.0.0.1:1', '127.0.0.1', 60)), patch.dict('os.environ', env, clear=True), patch('restleague.entrypoint.run', side_effect=one_cycle):
                for _ in range(2):
                    stream = io.StringIO()
                    with patch('restleague.entrypoint.sys.stdout', stream), self.assertRaises(StopAfterCycle):
                        main()
            self.assertNotEqual(paths[0].parent, paths[1].parent)
            self.assertEqual(paths[0].name, 'cycle-000000')


if __name__ == '__main__':
    unittest.main()
