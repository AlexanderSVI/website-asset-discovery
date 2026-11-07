"""Synthetic loopback HTTPS verification for D-112 document inventory."""
import csv
import json
import ssl
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import patch

from executor import Limits
from run_ids import RunIds
from test_executor import certificate
from wad.page_inspection import _inspect_site, _process
from wad.run import initialize
from foundation_output import FIELDS, _csv_value, write_csv


MIMES = {
    'pdf':'application/pdf', 'doc':'application/msword',
    'docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'xls':'application/vnd.ms-excel',
    'xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
}


class Documents(BaseHTTPRequestHandler):
    visits = []
    mode = 'normal'

    def log_message(self, *_):
        pass

    def do_GET(self):
        Documents.visits.append(self.path)
        base = f'https://127.0.0.1:{self.server.server_port}'
        status, mime = 200, 'text/html'
        if self.path == '/':
            if Documents.mode == 'budget':
                body = b'<main><a href="/large.pdf">Large</a></main>'
            elif Documents.mode == 'cp02_r20_complete':
                body = (b'<!doctype html><html lang="en"><head><title>CP02 integration site</title>'
                        b'<meta name="robots" content="noindex, nofollow">'
                        b'<meta name="googlebot" content="noarchive">'
                        b'<link rel="stylesheet" href="/assets/shared.css">'
                        b'<script src="/assets/app.js"></script>'
                        b'<link rel="preload" as="font" href="/assets/site.woff2" type="font/woff2" crossorigin>'
                        b'</head><body><nav itemscope itemid="/" aria-label="Main navigation">'
                        b'<a href="/">Home</a><a href="/about">About</a></nav>'
                        b'<main><section aria-label="Benefits"><h1>Benefits</h1><p>Repair and reuse.</p>'
                        b'<ul><li>Fast service</li><li>Reliable service</li></ul></section>'
                        b'<img src="/media/hero.jpg">'
                        b'<form role="search" aria-label="Site search"><input type="search" name="q" aria-label="Query"><button>Search</button></form>'
                        b'<iframe title="Campus map" src="https://maps.example.invalid/embed"></iframe>'
                        b'<a href="/files/confirmed.pdf">Manual</a><a href="/files/confirmed.pdf">Manual again</a>'
                        b'<a href="/files/partial.pdf">Partial manual</a>'
                        b'<section aria-label="Formula equals"><p>=1+1</p></section>'
                        b'<section aria-label="Formula plus"><p>+SUM(A1:A2)</p></section>'
                        b'<section aria-label="Formula minus"><p>-4</p></section>'
                        b'<section aria-label="Formula at"><p>@cmd</p></section></main></body></html>')
            elif Documents.mode == 'cp02_r20_partial':
                body = (b'<!doctype html><html><head><title>CP02 partial site</title></head><body>'
                        b'<main><h1>Collected page</h1><p>Preserve this before the byte stop.</p>'
                        b'<a href="/files/partial.pdf">Partial response</a>'
                        b'<a href="/files/large.pdf">Large document</a></main></body></html>')
            elif Documents.mode == 'redirect':
                body = b'<main><a href="/files/a.pdf">Direct</a><a href="/alias.pdf">Alias</a></main>'
            elif Documents.mode == 'robots_doc':
                body = b'<main><a href="/files/a.pdf">Blocked</a><a href="/other">Other</a></main>'
            elif Documents.mode == 'base':
                body = b'<base href="/base/"><main><a href="nested.pdf">PDF</a><a href="endpoint" type="application/pdf">Typed PDF</a></main>'
            else:
                body = (b'<main><a href="/files/a.pdf">PDF</a><a href="/files/a.pdf">PDF again</a>'
                        b'<a href="/files/b.doc">DOC</a><a href="/files/c.docx">DOCX</a>'
                        b'<a href="/files/d.xls">XLS</a><a href="/files/e.xlsx">XLSX</a>'
                        b'<a href="/files/partial.pdf">Partial</a><a href="/files/fake.pdf">Fake</a>'
                        b'<a href="/files/generic.docx">Generic</a><a href="/files/missing.pdf">Missing</a>'
                        b'<embed src="/files/embed.pdf" title="Embedded"><object data="/files/object.pdf"></object>'
                        b'<iframe src="/files/frame.pdf"></iframe><a href="https://outside.invalid/file.pdf">External</a>'
                        b'<iframe src="https://outside.invalid/embed.pdf"></iframe><a href="/other">Other</a></main>')
        elif self.path == '/robots.txt':
            body, mime = f'User-agent: *\nSitemap: {base}/map.xml\n'.encode(), 'text/plain'
            if Documents.mode == 'robots_doc':
                body = f'User-agent: *\nDisallow: /files/a.pdf\nSitemap: {base}/map.xml\n'.encode()
        elif self.path == '/map.xml':
            sitemap_url = '/sitemap-only' if Documents.mode in ('cp02_r20_complete', 'cp02_r20_partial') else '/files/sitemap.pdf'
            body, mime = f'<urlset><url><loc>{base}{sitemap_url}</loc></url></urlset>'.encode(), 'application/xml'
        elif self.path in ('/other', '/about'):
            if Documents.mode in ('cp02_r20_complete', 'cp02_r20_partial') and self.path == '/about':
                body = (b'<!doctype html><html><head><title>About CP02</title>'
                        b'<link rel="stylesheet" href="/assets/shared.css">'
                        b'<script src="/assets/app.js"></script>'
                        b'<link rel="preload" as="font" href="/assets/site.woff2" type="font/woff2" crossorigin>'
                        b'</head><body><main><section aria-label="About"><h1>About</h1><p>Local fixture.</p></section>'
                        b'<a href="/files/confirmed.pdf">Manual on about page</a></main></body></html>')
            elif self.path == '/about':
                body = b'<title>About</title><a href="/files/a.pdf">PDF on about page</a>'
            else:
                body = b'<title>Other</title><a href="/files/a.pdf">PDF on another page</a>'
        elif self.path == '/files/missing.pdf':
            status, body = 404, b'not found'
        elif self.path == '/files/fake.pdf':
            body = b'<html>not a document</html>'
        elif self.path == '/files/generic.docx':
            body, mime = b'generic binary', 'application/octet-stream'
        elif self.path == '/files/partial.pdf':
            status, body, mime = 206, b'%PDF-partial', 'application/pdf'
        elif self.path == '/alias.pdf':
            self.send_response(302)
            self.send_header('Location', '/files/a.pdf')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        elif self.path in ('/large.pdf', '/files/large.pdf'):
            body, mime = ((b'%PDF-' + b'x' * 10_300_000, 'application/pdf')
                          if Documents.mode == 'cp02_r20_partial' else
                          (b'%PDF-'+b'x'*1600, 'application/pdf'))
        elif self.path == '/files/confirmed.pdf':
            body, mime = b'%PDF-confirmed', 'application/pdf'
        elif self.path in ('/base/nested.pdf', '/base/endpoint'):
            body, mime = b'%PDF-base', 'application/pdf'
        elif self.path == '/sitemap-only' and Documents.mode in ('cp02_r20_complete', 'cp02_r20_partial'):
            body, mime = b'<title>Sitemap page</title><main><h1>Sitemap page</h1><p>Static fixture.</p></main>', 'text/html'
        elif self.path.startswith('/files/'):
            ext = self.path.rsplit('.', 1)[-1]
            mime = MIMES[ext]
            body = b'%PDF-synthetic' if ext == 'pdf' else b'synthetic fixture bytes'
        else:
            status, body = 404, b'not found'
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        if status == 206:
            self.send_header('Content-Range', f'bytes 0-{len(body)-1}/100000')
        self.end_headers()
        self.wfile.write(body)


class DocumentInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        cls.folder = Path(cls.temp.name)
        cls.cert, key = certificate(cls.folder)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Documents)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cls.cert, key)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'https://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(3); cls.temp.cleanup()

    def setUp(self):
        Documents.visits = []
        Documents.mode = 'normal'

    def run_case(self, name, **options):
        output = self.folder/name
        result = _inspect_site(initialize('127.0.0.1'), output, _fixture_url=self.base+'/',
                               _fixture_ca=str(self.cert), _work_dir=self.folder, **options)
        tables = {}
        for name in ('Asset_Inventory.csv','Occurrences.csv','Sources.csv','Evidence.csv','Relationships.csv'):
            path = output/'data'/name
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
            self.assertNotIn(b'\n', raw.replace(b'\r\n', b''))
            with path.open(encoding='utf-8-sig', newline='') as stream:
                tables[name] = list(csv.DictReader(stream))
        return result, tables

    def assert_output_contract(self, result, output, tables, expected_status):
        self.assertEqual(result['status'], expected_status)
        self.assertEqual(len(tables), 5)
        report = output/'Discovery_Report.md'
        self.assertTrue(report.is_file())
        self.assertIn('Status: '+expected_status, report.read_text(encoding='utf-8'))
        self.assertEqual(set(tables), set(FIELDS))
        for name, columns in FIELDS.items():
            raw = (output/'data'/name).read_bytes()
            self.assertTrue(raw.startswith(b'\xef\xbb\xbf'), name)
            self.assertNotIn(b'\n', raw.replace(b'\r\n', b''), name)
            with (output/'data'/name).open(encoding='utf-8-sig', newline='') as stream:
                self.assertEqual(tuple(next(csv.reader(stream))), columns, name)

    def assert_cross_table_references(self, result, tables):
        run_id = result['run'].run_id
        sources = tables['Sources.csv']
        assets = tables['Asset_Inventory.csv']
        occurrences = tables['Occurrences.csv']
        evidence = tables['Evidence.csv']
        relationships = tables['Relationships.csv']
        self.assertTrue(all(row['run_id'] == run_id for rows in tables.values() for row in rows))
        self.assertTrue(all(row['schema_version'] == '1.0' for rows in tables.values() for row in rows))

        source_by_id = {row['source_id']: row for row in sources}
        asset_by_id = {row['asset_id']: row for row in assets}
        evidence_by_id = {row['evidence_id']: row for row in evidence}
        self.assertEqual(len(source_by_id), len(sources))
        self.assertEqual(len(asset_by_id), len(assets))
        self.assertEqual(len(evidence_by_id), len(evidence))
        self.assertEqual(len({row['occurrence_id'] for row in occurrences}), len(occurrences))
        self.assertEqual(len({row['relationship_id'] for row in relationships}), len(relationships))

        for row in evidence:
            self.assertIn(row['source_id'], source_by_id)
            self.assertEqual(source_by_id[row['source_id']]['run_id'], run_id)
            self.assertIsInstance(json.loads(row['technical_attributes_json']), dict)
            if row['observation_result'].startswith(('{', '[')):
                json.loads(row['observation_result'])
        for row in assets:
            self.assertTrue(row['asset_type'])
            references = json.loads(row['evidence_ids_json'])
            self.assertIsInstance(references, list)
            self.assertTrue(references)
            self.assertTrue(all(eid in evidence_by_id for eid in references))
        for row in occurrences:
            self.assertIn(row['asset_id'], asset_by_id)
            references = json.loads(row['evidence_ids_json'])
            self.assertIsInstance(references, list)
            self.assertTrue(references)
            self.assertTrue(all(eid in evidence_by_id for eid in references))
        object_tables = {'ASSET': asset_by_id, 'SOURCE': source_by_id}
        for row in relationships:
            references = json.loads(row['evidence_ids_json'])
            self.assertIsInstance(references, list)
            self.assertTrue(references)
            self.assertTrue(all(eid in evidence_by_id for eid in references))
            self.assertIn(row['source_object_type'], object_tables)
            self.assertIn(row['source_object_id'], object_tables[row['source_object_type']])
            if row['target_object_id']:
                self.assertIn(row['target_object_type'], object_tables)
                self.assertIn(row['target_object_id'], object_tables[row['target_object_type']])
            if row['target_url']:
                target = urlsplit(row['target_url'])
                self.assertIn(target.scheme, {'http', 'https'})
                self.assertTrue(target.hostname)

    def test_cp02_r20_complete_integrated_package(self):
        Documents.mode = 'cp02_r20_complete'
        result, tables = self.run_case('cp02-r20-complete')
        output = self.folder/'cp02-r20-complete'
        self.assert_output_contract(result, output, tables, 'COMPLETE')
        self.assert_cross_table_references(result, tables)

        assets = tables['Asset_Inventory.csv']
        occurrences = tables['Occurrences.csv']
        sources = tables['Sources.csv']
        evidence = tables['Evidence.csv']
        relationships = tables['Relationships.csv']
        root_url = self.base+'/'
        root_page = next(row for row in assets if row['asset_type'] == 'WEB_PAGE' and row['primary_url'] == root_url)
        about_page = next(row for row in assets if row['asset_type'] == 'WEB_PAGE' and row['primary_url'] == self.base+'/about')
        self.assertTrue(any(row['asset_type'] == 'CONTENT_BLOCK' for row in assets))
        self.assertTrue(any(row['asset_type'] == 'NAVIGATION_STRUCTURE' for row in assets))
        self.assertTrue(any(row['asset_category'] == 'MEDIA' and row['asset_type'] == 'IMAGE' for row in assets))
        self.assertTrue(any(row['asset_category'] == 'FUNCTIONAL' and row['asset_type'] == 'SEARCH' for row in assets))

        evidence_by_id = {row['evidence_id']: row for row in evidence}
        page_evidence = next(row for row in evidence
            if json.loads(row['technical_attributes_json']).get('page', {}).get('title') == 'CP02 integration site')
        page_data = json.loads(page_evidence['technical_attributes_json'])['page']
        self.assertEqual(page_data['language'], 'en')
        self.assertEqual(page_data['indexing_signals'], [
            {'name': 'robots', 'content': 'noindex, nofollow'},
            {'name': 'googlebot', 'content': 'noarchive'},
        ])

        partial_doc = next(row for row in assets if row['asset_type'] == 'DOCUMENT' and row['primary_url'] == self.base+'/files/partial.pdf')
        self.assertEqual(partial_doc['observation_status'], 'PARTIALLY_OBSERVED')
        document_evidence = evidence_by_id[json.loads(partial_doc['evidence_ids_json'])[0]]
        document_attrs = json.loads(document_evidence['technical_attributes_json'])['document']
        self.assertEqual(document_attrs['http_status'], 206)
        self.assertFalse(document_attrs['response_complete'])
        self.assertEqual(result['status'], 'COMPLETE')

        confirmed = next(row for row in assets if row['asset_type'] == 'DOCUMENT' and row['primary_url'] == self.base+'/files/confirmed.pdf')
        confirmed_occurrences = [row for row in occurrences if row['asset_id'] == confirmed['asset_id']]
        self.assertEqual(len(confirmed_occurrences), 3)
        self.assertEqual(Documents.visits.count('/files/confirmed.pdf'), 1)
        self.assertEqual({row['usage_context'] for row in confirmed_occurrences},
                         {'Manual', 'Manual again', 'Manual on about page'})

        tech = [row for row in sources if row['source_type'] == 'TECHNICAL_RESOURCE']
        external = [row for row in sources if row['source_type'] == 'EXTERNAL_DEPENDENCY']
        self.assertTrue(any(row['requested_url'] == self.base+'/assets/shared.css' for row in tech))
        self.assertTrue(any(row['requested_url'] == self.base+'/assets/app.js' for row in tech))
        self.assertTrue(any(row['requested_url'] == self.base+'/assets/site.woff2' for row in tech))
        shared_source = next(row for row in tech if row['requested_url'] == self.base+'/assets/shared.css')
        shared_relationships = [row for row in relationships if row['target_object_id'] == shared_source['source_id']]
        self.assertEqual({row['source_object_id'] for row in shared_relationships},
                         {root_page['asset_id'], about_page['asset_id']})
        self.assertTrue(all(row['relationship_type'] == 'USES' for row in shared_relationships))

        map_source = next(row for row in external if row['requested_url'] == 'https://maps.example.invalid/embed')
        self.assertEqual(map_source['observation_status'], 'NOT_INSPECTED')
        map_embed = next(row for row in relationships if row['target_object_id'] == map_source['source_id'])
        self.assertEqual((map_embed['relationship_type'], map_embed['target_object_type']), ('EMBEDS', 'SOURCE'))
        self.assertNotIn('https://maps.example.invalid/embed', Documents.visits)
        self.assertEqual(result['budgets']['attempts'], len(Documents.visits))
        self.assertNotIn('/media/hero.jpg', Documents.visits)
        self.assertNotIn('/assets/shared.css', Documents.visits)
        self.assertNotIn('/assets/app.js', Documents.visits)
        self.assertNotIn('/assets/site.woff2', Documents.visits)

        formulas = {'=1+1', '+SUM(A1:A2)', '-4', '@cmd'}
        exported_observations = {row['observation_result'] for row in evidence}
        for value in formulas:
            self.assertIn("'"+value, exported_observations)
        self.assertIn('/map.xml', Documents.visits)
        self.assertIn('/sitemap-only', Documents.visits)

    def test_cp02_r20_controlled_partial_preserves_package(self):
        Documents.mode = 'cp02_r20_partial'
        result, tables = self.run_case('cp02-r20-partial')
        output = self.folder/'cp02-r20-partial'
        self.assert_output_contract(result, output, tables, 'PARTIAL')
        self.assertEqual(result['stop_reason'], 'byte_limit')
        self.assertEqual(result['budgets']['attempts'], len(Documents.visits))
        self.assertTrue(any(row['asset_type'] == 'WEB_PAGE' for row in tables['Asset_Inventory.csv']))
        self.assertTrue(tables['Evidence.csv'])
        self.assertTrue(tables['Sources.csv'])
        self.assertLessEqual(result['budgets']['retained'], result['budgets']['decoded'])
        self.assertLessEqual(result['budgets']['retained'], Limits().resource_base + Limits().block)
        self.assertLessEqual(result['budgets']['decoded'], Limits().resource_base + 2*Limits().block)
        self.assertIn('/files/large.pdf', Documents.visits)
        report = (output/'Discovery_Report.md').read_text(encoding='utf-8')
        self.assertIn('Причина остановки: byte_limit', report)
        self.assertTrue(any(row['asset_type'] == 'WEB_PAGE' for row in tables['Asset_Inventory.csv']))

    def test_cp02_r20_d102_formula_protection_during_csv_export(self):
        values = ['=1+1', '+SUM(A1:A2)', '-4', '@cmd', ' \t=leading', '\x01+control']
        originals = list(values)
        run_id = 'RUN-CP02-R20-FORMULA'
        source = {'schema_version': '1.0', 'run_id': run_id, 'source_id': 'SRC-000001',
                  'source_type': 'WEB_PAGE', 'requested_url': 'https://example.invalid/',
                  'final_url': 'https://example.invalid/', 'normalized_final_url': 'https://example.invalid/',
                  'observation_status': 'OBSERVED'}
        evidence = [{'schema_version': '1.0', 'run_id': run_id, 'evidence_id': f'EVD-{index:06d}',
                     'source_id': source['source_id'], 'observed_at': '2026-11-06T00:00:00Z',
                     'evidence_type': 'HTTP_RESPONSE', 'discovery_method': 'STATIC_HTTP',
                     'observation_result': value, 'technical_attributes_json': '{}'}
                    for index, value in enumerate(values, 1)]
        folder = self.folder/'cp02-r20-formula-protection'
        folder.mkdir()
        for _ in range(2):
            write_csv(folder, sources=[source], assets=[], evidence=evidence)
        with (folder/'Evidence.csv').open(encoding='utf-8-sig', newline='') as stream:
            exported = [row['observation_result'] for row in csv.DictReader(stream)]
        self.assertEqual(exported, ["'"+value for value in originals])
        self.assertEqual([row['observation_result'] for row in evidence], originals)
        self.assertEqual(_csv_value("'=1+1"), "'=1+1")
        self.assertEqual(_csv_value(42), 42)

    def test_formats_placements_provenance_and_false_positives(self):
        result, tables = self.run_case('formats')
        self.assertEqual(result['status'], 'COMPLETE')
        assets = [r for r in tables['Asset_Inventory.csv'] if r['asset_type']=='DOCUMENT']
        urls = {r['primary_url']:r for r in assets}
        for ext in MIMES:
            self.assertIn(self.base+'/files/'+{'pdf':'a','doc':'b','docx':'c','xls':'d','xlsx':'e'}[ext]+'.'+ext, urls)
        self.assertNotIn(self.base+'/files/fake.pdf', urls)
        self.assertNotIn(self.base+'/files/generic.docx', urls)
        self.assertNotIn(self.base+'/files/missing.pdf', urls)
        self.assertEqual(Documents.visits.count('/files/a.pdf'), 1)
        evidence = {r['evidence_id']:r for r in tables['Evidence.csv']}
        sources = {r['source_id']:r for r in tables['Sources.csv']}
        for row in assets:
            for eid in json.loads(row['evidence_ids_json']):
                self.assertIn(evidence[eid]['source_id'], sources)
                self.assertEqual(sources[evidence[eid]['source_id']]['source_type'], 'DOCUMENT_RESOURCE')
                self.assertEqual(sources[evidence[eid]['source_id']]['final_url'], row['primary_url'])
                self.assertTrue(sources[evidence[eid]['source_id']]['requested_url'])
                self.assertEqual(evidence[eid]['evidence_type'], 'HTTP_RESPONSE')
                attrs = json.loads(evidence[eid]['technical_attributes_json'])['document']
                self.assertIn(attrs['http_status'], (200,206))
                ext = row['primary_url'].rsplit('.', 1)[-1]
                self.assertEqual(attrs['mime'], MIMES[ext])
                expected = (b'%PDF-partial' if row['primary_url'].endswith('/partial.pdf') else
                            b'%PDF-synthetic' if ext == 'pdf' else b'synthetic fixture bytes')
                self.assertEqual(attrs['declared_content_length'], str(len(expected)))
        partial = urls[self.base+'/files/partial.pdf']
        self.assertEqual(partial['observation_status'], 'PARTIALLY_OBSERVED')
        attrs = json.loads(evidence[json.loads(partial['evidence_ids_json'])[0]]['technical_attributes_json'])['document']
        self.assertEqual(attrs['http_status'], 206)
        self.assertFalse(attrs['response_complete'])
        occurrences = [o for o in tables['Occurrences.csv'] if o['asset_id']==urls[self.base+'/files/a.pdf']['asset_id']]
        self.assertEqual(len(occurrences), 3)
        self.assertEqual({o['usage_context'] for o in occurrences}, {'PDF','PDF again','PDF on another page'})
        sitemap = urls[self.base+'/files/sitemap.pdf']
        self.assertFalse(any(o['asset_id']==sitemap['asset_id'] for o in tables['Occurrences.csv']))
        for name in ('embed','object','frame'):
            self.assertIn(self.base+'/files/'+name+'.pdf', urls)
        external = [s for s in tables['Sources.csv'] if s['source_type']=='EXTERNAL_DEPENDENCY']
        self.assertTrue(any(s['requested_url']=='https://outside.invalid/embed.pdf' and s['observation_status']=='NOT_INSPECTED' for s in external))
        relation = next(r for r in tables['Relationships.csv'] if r['relationship_type']=='EMBEDS'
                        and r['target_object_id'] in {s['source_id'] for s in external})
        page_ev = evidence[json.loads(relation['evidence_ids_json'])[0]]
        self.assertEqual(sources[page_ev['source_id']]['final_url'], self.base+'/')
        refs = json.loads(page_ev['technical_attributes_json'])['document_references']
        self.assertTrue(any(r['url']=='https://outside.invalid/embed.pdf' and r['element']=='iframe'
                            and r['attribute']=='src' for r in refs))
        self.assertTrue(any(r['relationship_type']=='EMBEDS' and r['target_url']==self.base+'/files/embed.pdf'
                            for r in tables['Relationships.csv']))
        self.assertTrue(any(r['relationship_type']=='LINKS_TO' and r['target_url']=='https://outside.invalid/file.pdf'
                            for r in tables['Relationships.csv']))

    def test_page_limit_continues_document_without_inspecting_html(self):
        result, tables = self.run_case('page-limit', _test_caps=(1,0))
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertNotIn('/other', Documents.visits)
        self.assertIn('/files/a.pdf', Documents.visits)
        self.assertIn('/files/sitemap.pdf', Documents.visits)
        pages = [r for r in tables['Asset_Inventory.csv'] if r['asset_type']=='WEB_PAGE']
        self.assertEqual(len(pages), 1)

    def test_request_budget_preserves_output(self):
        result, tables = self.run_case('request-limit', _limits=replace(Limits(), attempts=4))
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertLessEqual(result['budgets']['attempts'], 4)
        self.assertTrue(any(r['asset_type']=='DOCUMENT' for r in tables['Asset_Inventory.csv']))

    def test_redirect_reuses_document_and_preserves_sources(self):
        Documents.mode = 'redirect'
        result, tables = self.run_case('redirect')
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(Documents.visits.count('/files/a.pdf'), 1)
        self.assertEqual(Documents.visits.count('/alias.pdf'), 1)
        doc = next(r for r in tables['Asset_Inventory.csv'] if r['primary_url']==self.base+'/files/a.pdf')
        rows = [r for r in tables['Occurrences.csv'] if r['asset_id']==doc['asset_id']]
        self.assertEqual({r['usage_context'] for r in rows}, {'Direct','Alias'})
        alias = next(s for s in tables['Sources.csv'] if s['requested_url']==self.base+'/alias.pdf')
        evidence = {r['evidence_id']:r for r in tables['Evidence.csv']}
        attrs = json.loads(evidence[json.loads(doc['evidence_ids_json'])[0]]['technical_attributes_json'])
        self.assertTrue(any(r['source_id']==alias['source_id'] and r['redirects'][0]['status']==302
                            for r in attrs['reused_redirects']))

    def test_individual_robots_restricted_document_does_not_force_partial(self):
        Documents.mode = 'robots_doc'
        result, tables = self.run_case('robots-doc')
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertNotIn('/files/a.pdf', Documents.visits)
        self.assertIn('/other', Documents.visits)
        self.assertTrue(any('robots restriction' in row[2] for row in result['observations']))
        self.assertFalse(any(r['primary_url']==self.base+'/files/a.pdf' for r in tables['Asset_Inventory.csv']))

    def test_document_links_respect_base_without_wrong_requests(self):
        Documents.mode = 'base'
        result, tables = self.run_case('base')
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(Documents.visits.count('/base/nested.pdf'), 1)
        self.assertEqual(Documents.visits.count('/base/endpoint'), 1)
        self.assertNotIn('/nested.pdf', Documents.visits)
        self.assertNotIn('/endpoint', Documents.visits)
        documents = {r['primary_url'] for r in tables['Asset_Inventory.csv'] if r['asset_type']=='DOCUMENT'}
        self.assertIn(self.base+'/base/nested.pdf', documents)
        self.assertIn(self.base+'/base/endpoint', documents)

    def test_resource_byte_budget_preserves_partial_document(self):
        Documents.mode = 'budget'
        result, tables = self.run_case('byte-limit', _limits=replace(Limits(), resource_base=400))
        self.assertEqual(result['status'], 'PARTIAL')
        document = next(r for r in tables['Asset_Inventory.csv'] if r['asset_type']=='DOCUMENT')
        self.assertEqual(document['observation_status'], 'PARTIALLY_OBSERVED')
        self.assertLessEqual(result['budgets']['retained'], result['budgets']['decoded'])
        evidence = {r['evidence_id']:r for r in tables['Evidence.csv']}
        attrs = json.loads(evidence[json.loads(document['evidence_ids_json'])[0]]['technical_attributes_json'])['document']
        self.assertLessEqual(attrs['retained'], 400)
        self.assertLessEqual(attrs['delivered'], 400 + 65536)
        self.assertFalse(attrs['response_complete'])

    def test_run_byte_budget_preserves_partial_package(self):
        Documents.mode = 'budget'
        result, tables = self.run_case('run-byte-limit', _limits=replace(Limits(), run_base=700))
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertLessEqual(result['budgets']['retained'], 700)
        self.assertLessEqual(result['budgets']['decoded'], 700 + 65536)
        self.assertTrue(any(r['asset_type']=='DOCUMENT' for r in tables['Asset_Inventory.csv']))

    def test_expired_run_deadline_stops_document_discovery(self):
        run = initialize('127.0.0.1')
        ids = RunIds(run.run_id)
        base = 'https://127.0.0.1/'
        source = {'schema_version':'1.0','run_id':run.run_id,'source_id':ids.next('SRC'),
                  'source_type':'WEB_PAGE','requested_url':base,'final_url':base,
                  'normalized_final_url':base,'observation_status':'OBSERVED'}
        page = {'schema_version':'1.0','run_id':run.run_id,'asset_id':ids.next('AST'),
                'asset_type':'WEB_PAGE','asset_category':'CONTENT','primary_url':base,
                'observation_status':'OBSERVED','evidence_ids_json':'["EVD-000001"]'}
        ev = {'schema_version':'1.0','run_id':run.run_id,'evidence_id':ids.next('EVD'),
              'source_id':source['source_id'],'observed_at':'2026-11-05T00:00:00Z',
              'evidence_type':'HTTP_RESPONSE','discovery_method':'STATIC_HTTP',
              'observation_result':'HTML page observed','technical_attributes_json':'{}'}
        body = self.folder/'deadline.html'
        body.write_text('<a href="/document.pdf">Document</a>', encoding='utf-8')
        executor = SimpleNamespace(state=SimpleNamespace(started=0), limits=SimpleNamespace(run_target=1))
        with patch('wad.page_inspection.time.monotonic_ns', return_value=2_000_000_000):
            result = _process(run=run, executor=executor, ids=ids, sources=[source], assets=[page],
                evidence=[ev], homepage={'source':source,'evidence':ev,
                    'http':{'attempts':[{'url':base,'http_status':200,'body':str(body)}]}},
                robot=None, add_candidate=lambda *args: self.fail('candidate added after deadline'),
                internal=lambda value:True, observations=[], attempts=[], stop='', partial=False)
        self.assertTrue(result['partial'])
        self.assertEqual(result['stop'], 'run_target')
        self.assertFalse(run.candidates)


if __name__ == '__main__':
    unittest.main()
