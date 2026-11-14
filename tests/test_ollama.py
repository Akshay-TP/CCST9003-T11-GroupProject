import json
import unittest
from unittest.mock import MagicMock, patch

from ollama_explanations import explain


class OllamaTests(unittest.TestCase):
    def setUp(self):
        self.row = dict(app='Demo browser', category='Cache', confidence='Inferred',
                        size=1024, days=95, candidate=True, explanation='Cache may be rebuilt.',
                        path='private/path/not/to/send', actionable=True)

    def test_disabled_does_not_contact_ollama(self):
        with patch('urllib.request.build_opener') as factory:
            self.assertFalse(explain(self.row, None)['available'])
            factory.assert_not_called()

    def test_request_and_success_preserve_classification(self):
        original = dict(self.row)
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(
            dict(done=True, response='This cache may be rebuilt by the app.')).encode()
        with patch('urllib.request.build_opener', return_value=opener):
            result = explain(self.row, 'local-model')
        self.assertTrue(result['available'])
        request = opener.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, 'http://127.0.0.1:11434/api/generate')
        self.assertFalse(payload['stream'])
        self.assertNotIn(self.row['path'], payload['prompt'])
        self.assertEqual(self.row, original)

    def test_timeout_and_incomplete_output_use_rules(self):
        opener = MagicMock()
        with patch('urllib.request.build_opener', return_value=opener):
            opener.open.side_effect = TimeoutError()
            self.assertEqual(explain(self.row, 'local-model')['text'], self.row['explanation'])
            opener.open.side_effect = None
            opener.open.return_value.__enter__.return_value.read.return_value = b'{"done":false,"response":"partial"}'
            self.assertFalse(explain(self.row, 'local-model')['available'])


if __name__ == '__main__':
    unittest.main()
