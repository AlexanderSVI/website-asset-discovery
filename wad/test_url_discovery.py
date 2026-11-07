"""Local HTTPS coverage for the CP-02.3 discovery slice."""
import csv
import multiprocessing
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import ssl
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from wad.url_discovery import _discover_urls
from wad.run import initialize
from executor import Limits
from test_executor import certificate


class Fixture(BaseHTTPRequestHandler):
    events = []
    mode = 'normal'

    def log_message(self, *_):
        pass

    def do_GET(self):
        Fixture.events.append(self.path)
        base = f'https://127.0.0.1:{self.server.server_port}'
        if self.path == '/':
            data = b'<a href="/services?utm_source=x">Services</a><a href="/about">About</a><a href="https://example.invalid/">External</a>'
            kind = 'text/html'
        elif self.path == '/robots.txt':
            data = ('User-agent: *\nSitemap: ' + base + '/map.xml\n').encode() if Fixture.mode != 'no_sitemap' else b'User-agent: *\n'
            kind = 'text/plain'
        elif self.path == '/map.xml':
            data = b'<invalid' if Fixture.mode == 'bad' else (f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{base}/services</loc></url><url><loc>{base}/contact</loc></url></urlset>').encode()
            kind = 'application/xml'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class URLDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        cls.folder = Path(cls.temp.name)
        cls.cert, key = certificate(cls.folder)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cls.cert, key)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'https://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)
        cls.temp.cleanup()
        assert not cls.folder.exists() and not cls.thread.is_alive()
        assert not multiprocessing.active_children()

    def setUp(self):
        Fixture.events = []
        Fixture.mode = 'normal'

    def run_case(self, name, limits=None):
        run = initialize('127.0.0.1')
        output = self.folder / name
        result = _discover_urls(run, output, _fixture_url=self.base+'/', _fixture_ca=str(self.cert),
                                _work_dir=self.folder, _limits=limits)
        return run, output, result

    def test_discovery_provenance_and_output(self):
        run, output, result = self.run_case('normal')
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(Fixture.events, ['/', '/robots.txt', '/map.xml'])
        self.assertEqual(set(run.candidates), {self.base+'/services', self.base+'/about', self.base+'/contact'})
        ids = {s['source_type']: s['source_id'] for s in result['sources']}
        self.assertEqual(run.candidates[self.base+'/services'], {ids['WEB_PAGE'], ids['SITEMAP']})
        self.assertNotIn(ids['ROBOTS_TXT'], run.candidates[self.base+'/services'])
        self.assertEqual(result['sitemaps'], 1)
        self.assertEqual(result['budgets']['attempts'], 3)
        self.assertEqual(result['sitemap_provenance'][self.base+'/map.xml'], {ids['ROBOTS_TXT']})
        names = ['Sources.csv', 'Evidence.csv', 'Asset_Inventory.csv', 'Occurrences.csv', 'Relationships.csv']
        for name in names:
            with (output/'data'/name).open(encoding='utf-8-sig', newline='') as stream:
                rows = list(csv.DictReader(stream))
            if name == 'Sources.csv':
                self.assertEqual(len(rows), 3)
            if name == 'Asset_Inventory.csv':
                self.assertEqual(len(rows), 1)
            self.assertNotIn('/services', str(rows))
        report = (output/'Discovery_Report.md').read_text(encoding='utf-8')
        self.assertIn('Status: COMPLETE', report)
        self.assertIn('Уникальных обнаруженных URL: 3', report)
        self.assertIn('Записей Source: 3', report)
        self.assertIn('Статусы Source: OBSERVED=3', report)
        self.assertIn('HTTP(S)-попыток: 3', report)
        self.assertIn('Успешных HTTP(S)-попыток: 3', report)
        self.assertIn('Неуспешных HTTP(S)-попыток: 0', report)
        self.assertIn('Начальный домен: 127.0.0.1', report)
        self.assertIn('Начало UTC:', report)
        self.assertIn('Завершение UTC:', report)

    def test_budget_partial_preserves_package(self):
        run, output, result = self.run_case('partial', replace(Limits(), attempts=2))
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['stop_reason'], 'request_limit')
        self.assertEqual(Fixture.events, ['/', '/robots.txt'])
        self.assertEqual(len(run.candidates), 2)
        self.assertEqual(len(list((output/'data').glob('*.csv'))), 5)
        report = (output/'Discovery_Report.md').read_text(encoding='utf-8')
        self.assertIn('Status: PARTIAL', report)
        self.assertIn('Записей Source: 2', report)
        self.assertIn('Статусы Source: OBSERVED=2', report)
        self.assertIn('HTTP(S)-попыток: 2', report)
        self.assertIn('Успешных HTTP(S)-попыток: 2', report)
        self.assertIn('Неуспешных HTTP(S)-попыток: 0', report)

    def test_run_byte_budget_is_shared(self):
        run, output, result = self.run_case('byte-partial', replace(Limits(), run_base=150))
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['stop_reason'], 'run_bytes')
        self.assertLessEqual(result['budgets']['retained'], 150)
        self.assertLessEqual(result['budgets']['decoded'], 150 + 65536)
        self.assertNotIn('/map.xml', Fixture.events)
        self.assertTrue(run.candidates)
        self.assertIn('Status: PARTIAL', (output/'Discovery_Report.md').read_text(encoding='utf-8'))

    def test_bad_sitemap_preserves_results(self):
        Fixture.mode = 'bad'
        run, _, result = self.run_case('bad')
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(len(run.candidates), 2)
        self.assertTrue(any(item[2] == 'invalid XML' for item in result['observations']))

    def test_no_sitemap_and_serializer_error(self):
        Fixture.mode = 'no_sitemap'
        _, output, result = self.run_case('none')
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(result['sitemaps'], 0)
        with patch('wad.url_discovery.write_csv', side_effect=ValueError('serialization failure')):
            with self.assertRaisesRegex(ValueError, 'serialization failure'):
                self.run_case('error')
        self.assertTrue((output/'Discovery_Report.md').exists())

    def test_no_response_is_failed_with_no_fabricated_records(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        run = initialize('127.0.0.1')
        output = self.folder/'unreachable'
        result = _discover_urls(run, output, _fixture_url=f'https://127.0.0.1:{port}/',
                                _fixture_ca=str(self.cert), _work_dir=self.folder)
        self.assertEqual(result['status'], 'FAILED')
        self.assertFalse(result['sources'])
        self.assertFalse(run.candidates)
        self.assertEqual(len(list((output/'data').glob('*.csv'))), 5)
        report = (output/'Discovery_Report.md').read_text(encoding='utf-8')
        self.assertIn('Status: FAILED', report)
        self.assertIn(f"HTTP(S)-попыток: {result['budgets']['attempts']}", report)
        self.assertIn('Успешных HTTP(S)-попыток: 0', report)
        self.assertIn(f"Неуспешных HTTP(S)-попыток: {result['budgets']['attempts']}", report)


if __name__ == '__main__':
    unittest.main()
