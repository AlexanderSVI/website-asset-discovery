import unittest
from uuid import UUID
from wad.run import initialize


class RunInitializationTests(unittest.TestCase):
    def test_independent_runs_and_https(self):
        a, b = initialize('Example.COM'), initialize('Example.COM')
        self.assertEqual(a.initial_url, 'https://example.com')
        self.assertEqual(UUID(a.run_id[4:]).version, 4)
        self.assertNotEqual(a.run_id, b.run_id)

    def test_invalid_inputs(self):
        for value in ('', ' example.com', 'https://example.com', 'example.com/path',
                      'user@example.com', 'example.com:443', '-invalid.example'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                initialize(value)


if __name__ == '__main__':
    unittest.main()
