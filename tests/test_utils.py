import logging
import unittest

from blinkbridge.utils import RedactingFilter, redact_sensitive, sanitize_stream_name


class SanitizeStreamNameTests(unittest.TestCase):
    def test_normalizes_spaces_and_path_characters(self):
        self.assertEqual(sanitize_stream_name(' Front / Door '), 'front_door')

    def test_rejects_empty_result(self):
        with self.assertRaises(ValueError):
            sanitize_stream_name(' / ')

    def test_limits_path_segment_length(self):
        self.assertEqual(len(sanitize_stream_name('a' * 200)), 128)


class RedactionTests(unittest.TestCase):
    def test_redacts_url_tokens_and_email(self):
        value = 'user@example.invalid https://service.invalid/x?token=secret-value&ok=1'
        redacted = redact_sensitive(value)
        self.assertNotIn('secret-value', redacted)
        self.assertNotIn('user@example.invalid', redacted)
        self.assertIn('token=<redacted>', redacted)

    def test_filter_removes_format_arguments(self):
        record = logging.LogRecord('test', logging.INFO, __file__, 1, 'password=%s', ('secret',), None)
        self.assertTrue(RedactingFilter().filter(record))
        self.assertEqual(record.args, ())
        self.assertEqual(record.msg, 'password=<redacted>')


if __name__ == '__main__':
    unittest.main()
