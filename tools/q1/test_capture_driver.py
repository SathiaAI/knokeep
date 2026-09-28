"""Harmless coordinator checks; no model, network or live store API calls."""
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import checkpoint
import claude_turn


class CaptureDriverTests(unittest.TestCase):
    def test_stream_rejects_wrong_or_missing_session_and_model(self):
        events = [{'type': 'system', 'subtype': 'init', 'session_id': 'first', 'model': 'model-a'},
                  {'type': 'result', 'session_id': 'first', 'is_error': False}]
        raw = '\n'.join(json.dumps(e) for e in events).encode()
        parsed = claude_turn.inspect_stream(raw, 'first', 'model-a')
        self.assertTrue(parsed['session_continuity'])
        self.assertFalse(claude_turn.inspect_stream(raw, 'second', 'model-a')['session_continuity'])
        self.assertFalse(claude_turn.inspect_stream(raw, 'first', 'model-b')['model_matches'])
        self.assertFalse(claude_turn.inspect_stream(b'', 'first', 'model-a')['session_continuity'])
        self.assertEqual(claude_turn.inspect_stream(raw + b'\nbad-json', 'first', 'model-a')['malformed_lines'], [3])

    def test_snapshot_preserves_bytes_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / 'source'
            source.mkdir()
            (source / 'blob').write_bytes(b'\x00\xffexact\r\n')
            before = checkpoint.manifest(source)
            checkpoint.snapshot(source, root / 'copy')
            self.assertEqual(before, checkpoint.manifest(source))
            self.assertEqual(before, checkpoint.manifest(root / 'copy'))
            with self.assertRaises(FileExistsError):
                checkpoint.snapshot(source, root / 'copy')

    def test_mixed_journal_detects_torn_fence_and_bad_digest(self):
        key, body = b'demo/session_log', b'original bytes'
        record = struct.pack('>I', len(key)) + key + struct.pack('>Q', len(body)) + body + hashlib.sha256(body).digest()
        v2 = b'KKJ2' + record + struct.pack('>Q', 7)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'journal' / 'journal.log'
            path.parent.mkdir()
            path.write_bytes(record + v2)
            result = checkpoint.journal_versions(d)
            self.assertIsNone(result['tail_error'])
            self.assertEqual([r['fence'] for r in result['versions']], [0, 7])
            path.write_bytes(record + v2[:-1])
            result = checkpoint.journal_versions(d)
            self.assertEqual(len(result['versions']), 1)
            self.assertIsNotNone(result['tail_error'])
            path.write_bytes(record[:-1] + b'x')
            self.assertEqual(checkpoint.journal_versions(d)['versions'], [])

    def test_child_environment_excludes_paid_provider_overrides(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'fake', 'ANTHROPIC_BASE_URL': 'fake',
                                     'CLAUDE_CODE_USE_BEDROCK': '1', 'OPENROUTER_API_KEY': 'fake'}):
            env = claude_turn.environment('python-dir')
            for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_USE_BEDROCK', 'OPENROUTER_API_KEY'):
                self.assertNotIn(key, env)


if __name__ == '__main__':
    unittest.main()
