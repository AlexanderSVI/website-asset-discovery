"""CP-02.2 verified local HTTPS integration; no external network."""
import csv
import json
import multiprocessing
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from wad.run import initialize
from wad.discovery import _observe_page
from test_executor import certificate


class HTMLFixture(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    events = []
    active = 0
    maximum = 0
    lock = threading.Lock()

    def log_message(self, *_):
        pass

    def do_GET(self):
        with self.lock:
            type(self).active += 1
            type(self).maximum = max(type(self).maximum, type(self).active)
            type(self).events.append({'path': self.path, 'status': 302 if self.path == '/redirect' else 200})
        try:
            if self.path == '/redirect':
                self.send_response(302)
                self.send_header('Location', '/final?utm_source=fixture')
                body = b''
            else:
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                body = b'<!doctype html><title>Local fixture</title><h1>Test</h1>'
            self.send_header('Content-Length', str(len(body) + (10 if self.path == '/truncated' else 0)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)
        finally:
            with self.lock:
                type(self).active -= 1


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        cls.folder = Path(cls.temp.name)
        cert, key = certificate(cls.folder)
        cls.cert, cls.key = cert, key
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), HTMLFixture)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        cls.server.socket = ctx.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'https://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)
        cls.temp.cleanup()
        assert not cls.thread.is_alive()
        assert not cls.folder.exists()
        assert not multiprocessing.active_children()

    def verify_csv(self, result, folder):
        tables = {}
        minimum = {
            'Asset_Inventory.csv': {'schema_version','run_id','asset_id','asset_category','asset_type','observation_status','evidence_ids_json'},
            'Occurrences.csv': {'schema_version','run_id','occurrence_id','asset_id','placement_page_url','observation_status','evidence_ids_json'},
            'Sources.csv': {'schema_version','run_id','source_id','source_type','requested_url','final_url','normalized_final_url','observation_status'},
            'Evidence.csv': {'schema_version','run_id','evidence_id','source_id','observed_at','evidence_type','discovery_method','observation_result'},
            'Relationships.csv': {'schema_version','run_id','relationship_id','source_object_type','source_object_id','relationship_type','target_object_type','confirmation_status','evidence_ids_json'},
        }
        for name in ('Asset_Inventory.csv','Occurrences.csv','Sources.csv','Evidence.csv','Relationships.csv'):
            raw = (folder / name).read_bytes()
            self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
            self.assertNotIn(b'\n', raw.replace(b'\r\n', b''))
            self.assertNotIn(b'Local fixture', raw)
            with (folder / name).open(encoding='utf-8-sig', newline='') as stream:
                reader = csv.DictReader(stream)
                self.assertTrue(minimum[name].issubset(reader.fieldnames))
                tables[name] = list(reader)
        self.assertEqual(len(tables['Occurrences.csv']), 0)
        self.assertEqual(len(tables['Relationships.csv']), 0)
        asset, source, evidence = (tables[name][0] for name in ('Asset_Inventory.csv','Sources.csv','Evidence.csv'))
        mandatory = ((asset, ('schema_version','run_id','asset_id','asset_category','asset_type','observation_status','evidence_ids_json','primary_url')),
                     (source, ('schema_version','run_id','source_id','source_type','requested_url','final_url','normalized_final_url','observation_status')),
                     (evidence, ('schema_version','run_id','evidence_id','source_id','observed_at','evidence_type','discovery_method','observation_result')))
        for row, fields in mandatory:
            self.assertTrue(all(row[field] for field in fields), row)
        self.assertEqual({asset['run_id'], source['run_id'], evidence['run_id']}, {result['run'].run_id})
        self.assertTrue(all(row['schema_version'] for row in (asset, source, evidence)))
        self.assertEqual(source['source_id'], evidence['source_id'])
        self.assertEqual(json.loads(asset['evidence_ids_json']), [evidence['evidence_id']])
        self.assertEqual(evidence['evidence_type'], 'HTTP_RESPONSE')
        self.assertEqual(evidence['discovery_method'], 'STATIC_HTTP')
        self.assertEqual(asset['asset_type'], 'WEB_PAGE')
        self.assertEqual(asset['asset_category'], 'CONTENT')
        self.assertEqual(source['source_type'], 'WEB_PAGE')
        self.assertEqual(source['observation_status'], 'OBSERVED')
        self.assertEqual(asset['primary_url'], source['final_url'])
        self.assertEqual(source['final_url'], result['source']['final_url'])
        self.assertEqual(source['normalized_final_url'], result['source']['normalized_final_url'])
        attrs = json.loads(evidence['technical_attributes_json'])
        self.assertEqual(attrs['attempts'][-1]['http_status'], 200)
        self.assertNotIn('Local fixture', json.dumps(attrs))
        self.assertFalse(multiprocessing.active_children())
        print(json.dumps({'run_id': result['run'].run_id, 'source_id': source['source_id'],
                          'evidence_id': evidence['evidence_id'], 'asset_id': asset['asset_id'],
                          'requested_url': source['requested_url'], 'final_url': source['final_url'],
                          'normalized_final_url': source['normalized_final_url'],
                          'redirects': attrs['redirects'], 'fixture_max_active': HTMLFixture.maximum}), flush=True)

    def test_direct_html(self):
        run = initialize('127.0.0.1')
        folder = self.folder / 'direct'
        result = _observe_page(run, folder, _fixture_url=self.base+'/direct',
                               _fixture_ca=str(self.cert), _work_dir=self.folder)
        self.assertEqual(result['reason'], 'observed')
        self.assertEqual(result['source']['requested_url'], self.base+'/direct')
        self.assertEqual(result['source']['final_url'], self.base+'/direct')
        self.assertEqual(result['http']['attempts'][-1]['retained'], len(b'<!doctype html><title>Local fixture</title><h1>Test</h1>'))
        self.assertIn({'path': '/direct', 'status': 200}, HTMLFixture.events)
        self.verify_csv(result, folder)

    def test_redirect_to_actual_html(self):
        run = initialize('127.0.0.1')
        folder = self.folder / 'redirect-output'
        result = _observe_page(run, folder, _fixture_url=self.base+'/redirect',
                               _fixture_ca=str(self.cert), _work_dir=self.folder)
        self.assertEqual(result['source']['final_url'], self.base+'/final?utm_source=fixture')
        self.assertEqual(result['source']['normalized_final_url'], self.base+'/final')
        self.assertIn({'path': '/redirect', 'status': 302}, HTMLFixture.events)
        self.assertIn({'path': '/final?utm_source=fixture', 'status': 200}, HTMLFixture.events)
        self.assertEqual(HTMLFixture.maximum, 1)
        self.verify_csv(result, folder)

    def test_no_response_and_override_rejection(self):
        run = initialize('127.0.0.1')
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()
        result = _observe_page(run, self.folder/'failed', _fixture_url=f'https://127.0.0.1:{port}/',
                               _fixture_ca=str(self.cert), _work_dir=self.folder)
        self.assertEqual(result['reason'], 'no_http_response')
        self.assertIsNone(result['source']['final_url'])
        self.assertIsNone(result['evidence'])
        self.assertIsNone(result['asset'])
        self.assertFalse(multiprocessing.active_children())
        with self.assertRaises(ValueError):
            _observe_page(run, self.folder/'invalid', _fixture_url='https://example.invalid/',
                          _fixture_ca=str(self.cert), _work_dir=self.folder)

    def test_truncated_response_preserves_partial_status(self):
        run = initialize('127.0.0.1')
        result = _observe_page(run, self.folder/'truncated-output', _fixture_url=self.base+'/truncated',
                               _fixture_ca=str(self.cert), _work_dir=self.folder)
        self.assertEqual(result['source']['final_url'], self.base+'/truncated')
        self.assertEqual(result['source']['observation_status'], 'PARTIALLY_OBSERVED')
        self.assertIn(result['reason'], {'observed', 'no_confirmed_html_body'})
        if result['evidence'] is not None:
            self.assertGreater(result['http']['attempts'][-1]['retained'], 0)


if __name__ == '__main__':
    multiprocessing.freeze_support()
    unittest.main(verbosity=2)
