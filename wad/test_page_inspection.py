"""Loopback integration coverage for static page inspection."""
import csv
from dataclasses import replace
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import ssl
import tempfile
import threading
import time
import unittest

from wad.page_inspection import _inspect_site, _process, _resource_type, _Page, _resolve_redirect_dependencies, _classify_page, _AssetMarkup, _MediaMarkup, _srcset
from wad.run import initialize
from test_executor import certificate
from executor import Limits
from executor import Executor
from run_ids import RunIds
from source_adapter import SourceAdapter
from asset_identity import AssetIdentityRegistry
from url_normalizer import normalize_url
from foundation_output import FIELDS, attach_evidence, write_csv
from unittest.mock import patch


class Pages(BaseHTTPRequestHandler):
    visits = []
    mode = 'normal'
    def log_message(self, *_):
        pass

    def do_GET(self):
        Pages.visits.append(self.path)
        base = f'https://127.0.0.1:{self.server.server_port}'
        if self.path == '/':
            body = (b'<title>Inline references</title><img srcset="data:image/png;base64,AAAA 1x, /assets/valid.png 2x, ftp://cdn.example.invalid/other.png 3x, ipfs://cdn.example.invalid/image 4x">'
                    b'<img src="data:image/png;base64,BBBB"><img src="blob:https://127.0.0.1/id">'
                    b'<img src="blob:https://cdn.example.invalid/id">'
                    if Pages.mode == 'media_data' else
                    b'<title>Direct links</title><base href="/assets/"><a href="direct/image.jpg?rev=3">Image</a>'
                    b'<a href="direct/audio.ogg">Audio</a><a href="direct/video.mp4">Video</a>'
                    b'<a href="direct/play?id=1" type="video/mp4">Player</a><a href="#ordinary">Ordinary</a>'
                    if Pages.mode == 'media_direct' else
                    b'<title>Media fixture</title><main><img src="/assets/a.jpg?ver=1"><img src="/assets/a.jpg?ver=1">'
                    b'<img srcset="/assets/small.jpg 1x, /assets/large.jpg?ver=1,2 2x">'
                    b'<picture><source srcset="/assets/format.webp 1x, /assets/format2.webp 2x"><img src="/assets/pic.jpg"></picture>'
                    b'<video src="/assets/v.mp4" poster="/assets/poster.jpg"><source src="/assets/v.webm"></video>'
                    b'<audio src="/assets/a.mp3"><source src="/assets/a.ogg"></audio>'
                    b'<iframe title="Video player" src="/embed/local"></iframe><iframe title="Contact" src="/ordinary"></iframe>'
                    b'<img src="https://cdn.example.invalid/logo.png?x=1"><iframe title="Video player" src="https://video.example.invalid/embed/a"></iframe></main>'
                    if Pages.mode == 'media' else
                    b'<title>Home</title><nav><ul><li><a href="#top">Navigation</a></li></ul></nav><main><section aria-label="Benefits"><h1>Benefits</h1><p>Reliable work.</p><ul><li>Fast<ul><li>Same day</li><li>Next day</li></ul></li><li>Careful</li><li hidden>Hidden item</li></ul><ol><li>First</li><li>Second</li></ol><button>Request quote</button><a class="cta" href="#contact">Contact us</a><a href="#more">Read more</a><form><button>Submit form</button></form></section></main>'
                    if Pages.mode == 'features' else
                    b'<title>Home</title><header><nav itemscope itemid="/" aria-label="Main menu"><a href="#top">Home</a></nav></header>'
                    if Pages.mode in ('assets_repeat', 'assets_unverified') else
                    b'<title>Home</title><header><nav aria-label="Main menu"><ul><li><a href="#services">Services</a><ul><li><a href="#top"><span>Back</span></a></li></ul></li></ul></nav></header><main><section aria-label="Services"><h1 id="services">Services</h1><p>Metal, reuse &amp; repair.</p></section></main>'
                    if Pages.mode == 'assets' else
                    b'<title>Home</title><a href="/other">Other</a><a href="/alias">Alias</a>'
                    if Pages.mode == 'redirect' else
                    b'<title>Home</title><a href="/alias">Alias</a><a href="/other">Other</a>'
                    if Pages.mode == 'redirect_reverse' else
                    b'<title>Home</title><a href="/chain/1">Chain</a>'
                    if Pages.mode == 'depth' else
                    b'<title>Home</title><a href="/missing">Missing</a><a href="/alias-missing">Alias</a>'
                    if Pages.mode == 'redirect_missing' else
                    b'<title>Home</title><a href="/file.pdf">PDF</a><a href="/missing">Missing</a>'
                    if Pages.mode == 'resources' else
                    b'<title>Home</title><nav><a href="/nav">Nav</a></nav><main><a href="/main">Main</a></main><a href="/other">Other</a>')
            if Pages.mode == 'assets_unverified':
                body = body.replace(b'itemid="/"', b'itemid="/not-fetched"')
            if Pages.mode == 'functional':
                body = (b'<title>Functional fixture</title><nav><a href="/nav">Navigation</a></nav>'
                        b'<main><h1>Services</h1><a href="/main">Main</a></main><a href="/other">Other</a>'
                        b'<form aria-label="Contact form"><input type="email" name="email" required><button type="submit">Send</button></form>'
                        b'<form aria-label="Contact form"><input type="tel" name="phone"><button type="submit">Send</button></form>'
                        b'<form role="search" aria-label="Site search"><input type="search" name="q" aria-label="Query"><button>Search</button></form>'
                        b'<form aria-label="Product filter"><select name="category" aria-label="Category"><option>Steel</option></select><button>Apply filters</button></form>'
                        b'<form aria-label="Price calculator"><input type="number" name="quantity" aria-label="Quantity"><button>Calculate</button><output aria-label="Result">Result</output></form>'
                        b'<iframe title="Campus map" src="https://maps.example.invalid/embed"></iframe>'
                        b'<div role="application" aria-label="Interactive map"><button>Zoom</button></div>'
                        b'<div role="application" aria-label="Color picker"><input type="color" name="color"></div>'
                        b'<div role="application" aria-label="Interactive widget"><button>Run</button></div>'
                        b'<button>Plain button</button><input name="plain"><select name="generic"><option>Generic</option></select>')
            if Pages.mode == 'technical':
                body = (b'<title>Technical resources</title><base href="/assets/">'
                        b'<nav><a href="/nav">Navigation</a><a href="https://outside.example.invalid/reference">External reference</a></nav>'
                        b'<main><a href="/main">Main</a></main><a href="/other">Other page</a>'
                        b'<link rel="stylesheet" href="shared?utm_source=home">'
                        b'<link rel="stylesheet" href="print.css">'
                        b'<script src="shared?utm_source=home"></script>'
                        b'<link rel="preload" as="font" href="site.woff2" type="font/woff2" crossorigin>'
                        b'<link rel="manifest" href="site.webmanifest">'
                        b'<link rel="preload" href="unclear.bin">'
                        b'<script type="application/json" src="data.json"></script>'
                        b'<script src="https://cdn.example.invalid/app.js"></script>')
            mime = 'text/html'
        elif self.path in ('/loop-a', '/loop-b'):
            self.send_response(302)
            self.send_header('Location', '/loop-b' if self.path == '/loop-a' else '/loop-a')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        elif self.path.startswith('/normalized-loop?'):
            self.send_response(302)
            self.send_header('Location', '/normalized-loop?utm_next=1')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        elif self.path.startswith('/chain/'):
            number = int(self.path.rsplit('/', 1)[1])
            body, mime = f'<title>Chain</title><a href="/chain/{number + 1}">next</a>'.encode(), 'text/html'
        elif self.path == '/file.pdf':
            body, mime = b'%PDF-1.7 local fixture', 'application/pdf'
        elif self.path == '/retry':
            if Pages.visits.count('/retry') < 3:
                self.send_response(503)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            body, mime = b'<title>retried</title>', 'text/html'
        elif self.path in ('/alias', '/alias2'):
            self.send_response(302)
            self.send_header('Location', '/other')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        elif self.path == '/alias-missing':
            self.send_response(302)
            self.send_header('Location', '/missing')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        elif self.path == '/robots.txt':
            body = f'User-agent: *\nSitemap: {base}/map.xml\n'.encode()
            if Pages.mode == 'robots_disallow':
                body = f'User-agent: *\nDisallow: /nav\nSitemap: {base}/map.xml\n'.encode()
            if Pages.mode == 'media_direct':
                body = f'User-agent: *\nDisallow: /direct/\nSitemap: {base}/map.xml\n'.encode()
            mime = 'text/plain'
        elif self.path == '/map.xml':
            if Pages.mode == 'sitemap_redirect_home':
                self.send_response(302)
                self.send_header('Location', '/')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            body = f'<urlset><url><loc>{base}/sitemap-only</loc></url></urlset>'.encode()
            mime = 'application/xml'
        elif self.path == '/nav':
            body = b'<title>Nav</title><link rel="canonical" href="/other"><h1>Heading</h1><a href="/child">Child</a>'
            if Pages.mode == 'classification':
                body = b'<title>Company</title><main><h1>About company</h1><p>Company information.</p></main>'
            if Pages.mode == 't01_metadata':
                body = (b'<html lang="ru-RU"><title>Nav</title><link rel="canonical" href="/other">'
                        b'<h1>Heading</h1><a href="/child">Child</a>'
                        b'<meta name="robots" content="noindex, nofollow">'
                        b'<meta name="googlebot" content="noarchive">'
                        b'<meta name="ROBOTS" content="max-snippet:0">')
            if Pages.mode == 't01_empty_language':
                body = b'<html lang=""><title>Nav</title><link rel="canonical" href="/other"><h1>Heading</h1><a href="/child">Child</a>'
            if Pages.mode == 'metadata':
                body += (b'<meta name="description" content="Local fixture &amp; metadata">'
                         b'<link rel="alternate" hreflang="en-US" href="/en/nav">'
                         b'<link rel="alternate" hreflang="de" href="/de/nav">'
                         b'<link rel="alternate" hreflang="x-default" href="/nav">')
            mime = 'text/html'
        elif self.path in ('/main', '/other', '/child', '/sitemap-only'):
            body, mime = b'<title>Page</title><h1>Text</h1>', 'text/html'
            if self.path == '/other' and Pages.mode == 'technical':
                body = f'<title>Other</title><link rel="stylesheet" href="{base}/assets/shared?utm_source=other">'.encode()
            if self.path == '/sitemap-only' and Pages.mode in ('assets_repeat', 'assets_unverified'):
                body = b'<title>Page</title><footer><nav itemscope itemid="/" aria-label="Footer menu"><a href="/">Home</a></nav></footer>'
                if Pages.mode == 'assets_unverified':
                    body = body.replace(b'itemid="/"', b'itemid="/not-fetched"')
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class StaticInspectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        cls.folder = Path(cls.temp.name)
        cls.cert, key = certificate(cls.folder)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Pages)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cls.cert, key)
        cls.server.socket = ctx.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'https://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(3); cls.temp.cleanup()

    def test_static_pages_relationships_and_bfs(self):
        Pages.visits = []
        run = initialize('127.0.0.1')
        result = _inspect_site(run, self.folder/'output', _fixture_url=self.base+'/',
                               _fixture_ca=str(self.cert), _work_dir=self.folder)
        self.assertEqual(Pages.visits, ['/', '/robots.txt', '/map.xml', '/nav', '/main', '/other', '/child', '/sitemap-only'])
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(result['budgets']['attempts'], 8)
        data = self.folder/'output'/'data'
        with (data/'Relationships.csv').open(encoding='utf-8-sig', newline='') as f:
            relations = list(csv.DictReader(f))
        self.assertTrue(any(r['relationship_type'] == 'CANONICAL_OF' and r['target_url'].endswith('/other') for r in relations))
        self.assertTrue(any(r['relationship_type'] == 'LINKS_TO' and r['target_url'].endswith('/child') and not r['target_object_id'] for r in relations))
        self.assertTrue(all(json.loads(r['evidence_ids_json']) for r in relations))
        with (data/'Asset_Inventory.csv').open(encoding='utf-8-sig', newline='') as f:
            assets = list(csv.DictReader(f))
        self.assertEqual(sum(row['asset_type'] == 'WEB_PAGE' for row in assets), 6)
        with (data/'Evidence.csv').open(encoding='utf-8-sig', newline='') as f:
            evidence = list(csv.DictReader(f))
        page = next(json.loads(row['technical_attributes_json'])['page'] for row in evidence
                    if 'page' in json.loads(row['technical_attributes_json'])
                    and json.loads(row['technical_attributes_json'])['page']['title'] == 'Nav')
        self.assertEqual(page['page_type'], 'UNKNOWN')
        self.assertIsNone(page['page_type_confidence'])
        self.assertEqual(page['headings'][0]['text'], 'Heading')
        self.assertTrue((self.folder/'output'/'Discovery_Report.md').exists())
        self.assertIn('Canonical duplicate candidates (not merged)', (self.folder/'output'/'Discovery_Report.md').read_text(encoding='utf-8'))

    def test_resource_type_requires_observation(self):
        self.assertEqual(_resource_type('', b'', 'https://example.invalid/file.pdf'), 'UNKNOWN')
        self.assertEqual(_resource_type('application/pdf', b'%PDF-1.7', 'https://example.invalid/file'), 'DOCUMENT_RESOURCE')
        self.assertEqual(_resource_type('application/octet-stream', b'<html><title>x</title>', 'https://example.invalid/file'), 'WEB_PAGE')

    def test_media_presence_and_provenance_without_media_requests(self):
        Pages.visits = []
        Pages.mode = 'media'
        self.addCleanup(setattr, Pages, 'mode', 'normal')
        output = self.folder/'media-presence'
        result = _inspect_site(initialize('127.0.0.1'), output,
                               _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder)
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(Pages.visits, ['/', '/robots.txt', '/map.xml', '/sitemap-only'])
        self.assertEqual(result['budgets']['attempts'], 4)
        tables = {}
        for name in ('Asset_Inventory.csv','Occurrences.csv','Evidence.csv','Sources.csv','Relationships.csv'):
            with (output/'data'/name).open(encoding='utf-8-sig', newline='') as stream:
                tables[name] = list(csv.DictReader(stream))
        sources = tables['Sources.csv']
        source_status_counts = {status: sum(row['observation_status'] == status for row in sources)
                                for status in sorted({row['observation_status'] for row in sources})}
        self.assertGreater(source_status_counts.get('NOT_INSPECTED', 0), 0)
        self.assertEqual(sum(source_status_counts.values()), len(sources))
        report = (output/'Discovery_Report.md').read_text(encoding='utf-8')
        expected_statuses = ', '.join(f'{status}={count}' for status, count in source_status_counts.items())
        self.assertIn(f'Записей Source: {len(sources)}', report)
        self.assertIn(f'Статусы Source: {expected_statuses}', report)
        self.assertIn(f"HTTP(S)-попыток: {result['budgets']['attempts']}", report)
        self.assertIn('Успешных HTTP(S)-попыток: 4', report)
        self.assertIn('Неуспешных HTTP(S)-попыток: 0', report)
        assets = {r['asset_id']: r for r in tables['Asset_Inventory.csv']}
        media = [r for r in assets.values() if r['asset_category'] == 'MEDIA']
        self.assertEqual({kind:sum(r['asset_type'] == kind for r in media) for kind in ('IMAGE','AUDIO','VIDEO')},
                         {'IMAGE':5,'AUDIO':1,'VIDEO':2})
        occurrences = [r for r in tables['Occurrences.csv'] if r['asset_id'] in {a['asset_id'] for a in media}]
        self.assertEqual(len(occurrences), 8)
        self.assertEqual(sum(r['component']=='video' and r['usage_context']=='video poster' for r in occurrences), 1)
        self.assertEqual(sum(r['component']=='iframe' for r in occurrences), 1)
        same = [r for r in media if r['primary_url'] == self.base+'/assets/a.jpg?ver=1']
        self.assertEqual(len(same), 2)  # D-108: no merge without confirmed final URL.
        evidence = {r['evidence_id']:r for r in tables['Evidence.csv']}
        sources = {r['source_id']:r for r in tables['Sources.csv']}
        for occurrence in occurrences:
            record = evidence[json.loads(occurrence['evidence_ids_json'])[0]]
            self.assertEqual(sources[record['source_id']]['final_url'], self.base+'/')
            observed = json.loads(record['observation_result'])
            self.assertFalse(observed['resource_requested_by_extraction'])
            self.assertFalse(observed['resource_availability_checked_by_extraction'])
            self.assertTrue(all(c['attribute'] in {'src','srcset','poster','href'} for c in observed['candidates']))
            if occurrence['component'] == 'iframe':
                self.assertEqual(observed['candidates'][0]['title'], 'Video player')
        picture = next(r for r in occurrences if r['component'] == 'picture')
        candidates = json.loads(evidence[json.loads(picture['evidence_ids_json'])[0]]['observation_result'])['candidates']
        self.assertEqual([(c['attribute'],c['descriptor']) for c in candidates], [('srcset','1x'),('srcset','2x'),('src','')])
        responsive = next(r for r in occurrences if assets[r['asset_id']]['primary_url'].endswith('/assets/small.jpg'))
        variants = json.loads(evidence[json.loads(responsive['evidence_ids_json'])[0]]['observation_result'])['candidates']
        self.assertEqual([c['descriptor'] for c in variants], ['1x','2x'])
        self.assertEqual(variants[1]['url'], self.base+'/assets/large.jpg?ver=1,2')
        for kind in ('AUDIO', 'VIDEO'):
            placement = next(r for r in occurrences if r['component'] == kind.lower() and not r['usage_context'])
            candidates = json.loads(evidence[json.loads(placement['evidence_ids_json'])[0]]['observation_result'])['candidates']
            self.assertEqual([(c['element'],c['attribute']) for c in candidates], [(kind.lower(),'src'),('source','src')])
        poster = next(r for r in occurrences if r['usage_context'] == 'video poster')
        self.assertEqual(json.loads(evidence[json.loads(poster['evidence_ids_json'])[0]]['observation_result'])['candidates'][0]['attribute'], 'poster')
        external = [s for s in tables['Sources.csv'] if s['source_type'] == 'EXTERNAL_DEPENDENCY']
        self.assertEqual(len(external), 2)
        self.assertTrue(all(s['observation_status']=='NOT_INSPECTED' and not s['final_url'] for s in external))
        self.assertEqual(sum(r['relationship_type']=='EMBEDS' and r['target_object_id'] in {s['source_id'] for s in external}
                             for r in tables['Relationships.csv']), 2)
        for relation in tables['Relationships.csv']:
            if relation['target_object_id'] in {s['source_id'] for s in external}:
                record = evidence[json.loads(relation['evidence_ids_json'])[0]]
                self.assertEqual(sources[record['source_id']]['final_url'], self.base+'/')
        self.assertFalse(any('example.invalid' in (a['primary_url'] or '') for a in media))
        print('MEDIA_FIXTURE', json.dumps({'counts':{'IMAGE':5,'AUDIO':1,'VIDEO':2},
              'asset':media[0], 'occurrence':occurrences[0], 'srcset':variants,
              'external_sources':len(external)}, ensure_ascii=False))

    def test_media_presence_partial_preserves_records(self):
        Pages.visits = []
        Pages.mode = 'media'
        self.addCleanup(setattr, Pages, 'mode', 'normal')
        output = self.folder/'media-partial'
        result = _inspect_site(initialize('127.0.0.1'), output,
                               _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder,
                               _limits=replace(Limits(), attempts=1))
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(Pages.visits, ['/'])
        with (output/'data'/'Occurrences.csv').open(encoding='utf-8-sig', newline='') as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 8)
        self.assertIn('Status: PARTIAL', (output/'Discovery_Report.md').read_text(encoding='utf-8'))

    def test_media_inline_and_srcset_candidates_are_preserved(self):
        Pages.visits = []
        Pages.mode = 'media_data'
        self.addCleanup(setattr, Pages, 'mode', 'normal')
        output = self.folder/'media-data'
        result = _inspect_site(initialize('127.0.0.1'), output,
                               _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder)
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(Pages.visits, ['/', '/robots.txt', '/map.xml', '/sitemap-only'])
        with (output/'data'/'Asset_Inventory.csv').open(encoding='utf-8-sig', newline='') as stream:
            media = [row for row in csv.DictReader(stream) if row['asset_type']=='IMAGE']
        self.assertEqual(len(media), 3)
        self.assertIn('data:image/png;base64,BBBB', [row['primary_url'] for row in media])
        self.assertIn('blob:https://127.0.0.1/id', [row['primary_url'] for row in media])
        self.assertNotIn('blob:https://cdn.example.invalid/id', [row['primary_url'] for row in media])
        with (output/'data'/'Evidence.csv').open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        observations = [json.loads(row['observation_result']) for row in rows if row['observation_result'].startswith('{')]
        srcset = next(row['candidates'] for row in observations if len(row.get('candidates', [])) == 4)
        self.assertEqual([(c['url'],c['descriptor']) for c in srcset],
                         [('data:image/png;base64,AAAA','1x'),(self.base+'/assets/valid.png','2x'),
                          ('ftp://cdn.example.invalid/other.png','3x'),('ipfs://cdn.example.invalid/image','4x')])
        with (output/'data'/'Sources.csv').open(encoding='utf-8-sig', newline='') as stream:
            sources = list(csv.DictReader(stream))
        self.assertTrue(any(s['requested_url']=='ftp://cdn.example.invalid/other.png' and
                            s['observation_status']=='NOT_INSPECTED' for s in sources))
        self.assertTrue(any(s['requested_url']=='ipfs://cdn.example.invalid/image' and
                            s['observation_status']=='NOT_INSPECTED' for s in sources))
        self.assertTrue(any(s['requested_url']=='blob:https://cdn.example.invalid/id' and
                            s['observation_status']=='NOT_INSPECTED' for s in sources))

    def test_media_markup_direct_links_and_iframe_controls(self):
        parser = _MediaMarkup()
        parser.feed('<a href="/x.JPG?rev=3">image</a><a href="/a.mp3">audio</a>'
                    '<a href="/watch" type="video/mp4">video</a><a href="/ordinary">ordinary</a>'
                    '<a href="/conflict.jpg" type="text/html">conflicting type</a>'
                    '<iframe title="Video player" src="/player"></iframe><iframe title="Contact" src="/other"></iframe>'
                    '<iframe title="Video player" src="/still.jpg"></iframe>'
                    '<iframe title="Video player" type="text/html" src="/html-player"></iframe>'
                    '<a href="/generic.jpg" type="application/octet-stream">generic image</a>')
        self.assertEqual([item['kind'] for item in parser.observed], ['IMAGE','AUDIO','VIDEO','VIDEO','VIDEO','IMAGE'])
        self.assertEqual([item['component'] for item in parser.observed], ['a','a','a','iframe','iframe','a'])
        self.assertEqual(parser.observed[2]['candidates'][0]['type'], 'video/mp4')
        self.assertEqual(parser.observed[3]['candidates'][0]['title'], 'Video player')
        self.assertEqual(_srcset('/small.jpg 1x, /large.jpg?x=1,2 2x'),
                         [('/small.jpg','1x'),('/large.jpg?x=1,2','2x')])

    def test_media_direct_links_base_and_no_download(self):
        Pages.visits = []
        Pages.mode = 'media_direct'
        self.addCleanup(setattr, Pages, 'mode', 'normal')
        output = self.folder/'media-direct'
        result = _inspect_site(initialize('127.0.0.1'), output,
                               _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder)
        self.assertEqual(result['status'], 'PARTIAL')  # Existing robots policy blocks the discovered /direct/ links.
        self.assertEqual(Pages.visits, ['/', '/robots.txt', '/map.xml', '/sitemap-only'])
        with (output/'data'/'Asset_Inventory.csv').open(encoding='utf-8-sig', newline='') as stream:
            media = [row for row in csv.DictReader(stream) if row['asset_category'] == 'MEDIA']
        self.assertEqual([r['asset_type'] for r in media], ['IMAGE','AUDIO','VIDEO','VIDEO'])
        self.assertEqual(media[0]['primary_url'], self.base+'/assets/direct/image.jpg?rev=3')
        self.assertTrue(all('/assets/direct/' in r['primary_url'] for r in media))
        with (output/'data'/'Evidence.csv').open(encoding='utf-8-sig', newline='') as stream:
            records = {r['evidence_id']:r for r in csv.DictReader(stream)}
        player = next(r for r in media if 'play?id=1' in r['primary_url'])
        observed = json.loads(records[json.loads(player['evidence_ids_json'])[0]]['observation_result'])
        self.assertEqual(observed['candidates'][0]['type'], 'video/mp4')

    def test_media_confirmed_repeated_identity_without_fetch(self):
        run = initialize('127.0.0.1')
        ids = RunIds(run.run_id)
        base = 'https://127.0.0.1/'
        media_url = base+'shared.jpg'
        page_source = {'schema_version':'1.0','run_id':run.run_id,'source_id':ids.next('SRC'),
            'source_type':'WEB_PAGE','requested_url':base,'final_url':base,'normalized_final_url':base,
            'observation_status':'OBSERVED'}
        media_source = {'schema_version':'1.0','run_id':run.run_id,'source_id':ids.next('SRC'),
            'source_type':'MEDIA_RESOURCE','requested_url':media_url,'final_url':media_url,'normalized_final_url':media_url,
            'observation_status':'OBSERVED'}
        page = {'schema_version':'1.0','run_id':run.run_id,'asset_id':ids.next('AST'),
            'asset_category':'CONTENT','asset_type':'WEB_PAGE','primary_url':base,'observation_status':'OBSERVED',
            'evidence_ids_json':'["EVD-000001"]'}
        ev = {'schema_version':'1.0','run_id':run.run_id,'evidence_id':ids.next('EVD'),
            'source_id':page_source['source_id'],'observed_at':'2026-11-05T00:00:00Z','evidence_type':'HTTP_RESPONSE',
            'discovery_method':'STATIC_HTTP','observation_result':'HTML page observed',
            'technical_attributes_json':json.dumps({'attempts':[{'url':base,'http_status':200}],'redirects':[],'reason':'complete'})}
        body = self.folder/'media-identity.html'
        body.write_text('<img src="/shared.jpg"><img src="/shared.jpg">', encoding='utf-8')
        class FakeExecutor:
            def __init__(self):
                self.state = type('State', (), {'started':time.monotonic_ns(), 'hosts':{'127.0.0.1'}})()
                self.limits = Limits()
            def fetch(self, *args, **kwargs):
                raise AssertionError('extraction must not fetch')
        attempts = [{'url':media_url,'http_status':200,'reason':'complete','headers':{'content-type':'image/jpeg'}}]
        assets, evidence = [page], [ev]
        result = _process(run=run, executor=FakeExecutor(), ids=ids, sources=[page_source,media_source],
            assets=assets, evidence=evidence, homepage={'source':page_source,'evidence':ev,
                'http':{'attempts':[{'url':base,'http_status':200,'body':str(body)}]}},
            robot=None, add_candidate=lambda *args:None, internal=lambda url: url.startswith(base),
            observations=[], attempts=attempts, stop='', partial=False)
        media = [asset for asset in assets if asset['asset_type']=='IMAGE']
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]['primary_url'], media_url)
        self.assertEqual(len(result['occurrences']), 2)
        self.assertEqual({o['asset_id'] for o in result['occurrences']}, {media[0]['asset_id']})

    def test_asset_extraction_evidence_occurrence_and_no_requests(self):
        Pages.visits = []
        Pages.mode = 'assets'
        self.addCleanup(setattr, Pages, 'mode', 'normal')
        output = self.folder/'assets'
        result = _inspect_site(initialize('127.0.0.1'), output,
                               _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder)
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(Pages.visits, ['/', '/robots.txt', '/map.xml', '/sitemap-only'])
        self.assertEqual(result['budgets']['attempts'], 4)
        data = output/'data'
        tables = {}
        for name in ('Asset_Inventory.csv', 'Occurrences.csv', 'Evidence.csv', 'Sources.csv', 'Relationships.csv'):
            raw = (data/name).read_bytes()
            self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
            self.assertNotIn(b'\n', raw.replace(b'\r\n', b''))
            with (data/name).open(encoding='utf-8-sig', newline='') as stream:
                reader = csv.DictReader(stream)
                if name == 'Occurrences.csv':
                    self.assertEqual(reader.fieldnames, ['schema_version','run_id','occurrence_id','asset_id',
                        'placement_page_url','page_area','component','usage_context','observation_status','evidence_ids_json'])
                tables[name] = list(reader)
        assets = {row['asset_id']: row for row in tables['Asset_Inventory.csv']}
        occurrences = tables['Occurrences.csv']
        self.assertEqual(len(assets), 4)
        self.assertEqual(len(occurrences), 2)
        self.assertEqual({assets[row['asset_id']]['asset_type'] for row in occurrences}, {'CONTENT_BLOCK','NAVIGATION_STRUCTURE'})
        self.assertEqual({(r['page_area'], r['component'], r['usage_context']) for r in occurrences},
                         {('header','nav','Main menu'), ('main','section','Services')})
        evidence = {row['evidence_id']: row for row in tables['Evidence.csv']}
        sources = {row['source_id']: row for row in tables['Sources.csv']}
        for occurrence in occurrences:
            for eid in json.loads(occurrence['evidence_ids_json']):
                self.assertEqual(sources[evidence[eid]['source_id']]['final_url'], self.base+'/')
                self.assertEqual(evidence[eid]['evidence_type'], 'HTTP_RESPONSE')
                attrs = json.loads(evidence[eid]['technical_attributes_json'])
                if assets[occurrence['asset_id']]['asset_type'] == 'CONTENT_BLOCK':
                    self.assertEqual(evidence[eid]['observation_result'], 'Services Metal, reuse & repair.')
                    self.assertNotIn('page_area', attrs)
                    self.assertNotIn('usage_context', attrs)
                else:
                    links = json.loads(evidence[eid]['observation_result'])
                    self.assertEqual([(link['href'], link['text'], link['depth']) for link in links],
                                     [('#services', 'Services', 1), ('#top', 'Back', 2)])
        self.assertEqual(sum(r['relationship_type'] == 'CONTAINS' for r in tables['Relationships.csv']), 2)
        print('ASSET_FIXTURE', json.dumps({'assets': len(assets), 'occurrences': len(occurrences),
              'example': occurrences[0]}, ensure_ascii=False))

    def test_asset_extraction_partial_preserves_records(self):
        Pages.visits = []
        Pages.mode = 'assets'
        self.addCleanup(setattr, Pages, 'mode', 'normal')
        output = self.folder/'assets-partial'
        result = _inspect_site(initialize('127.0.0.1'), output,
            _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder,
            _limits=replace(Limits(), attempts=1))
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(Pages.visits, ['/'])
        with (output/'data'/'Occurrences.csv').open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 2)
        self.assertIn('Status: PARTIAL', (output/'Discovery_Report.md').read_text(encoding='utf-8'))

    def test_asset_repeated_confirmed_identity_has_two_occurrences(self):
        Pages.visits = []
        Pages.mode = 'assets_repeat'
        self.addCleanup(setattr, Pages, 'mode', 'normal')
        output = self.folder/'assets-repeat'
        result = _inspect_site(initialize('127.0.0.1'), output,
                               _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder)
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(Pages.visits, ['/', '/robots.txt', '/map.xml', '/sitemap-only'])
        with (output/'data'/'Asset_Inventory.csv').open(encoding='utf-8-sig', newline='') as stream:
            assets = list(csv.DictReader(stream))
        nav = [a for a in assets if a['asset_type'] == 'NAVIGATION_STRUCTURE']
        self.assertEqual(len(nav), 1)
        self.assertEqual(nav[0]['primary_url'], self.base+'/')
        with (output/'data'/'Occurrences.csv').open(encoding='utf-8-sig', newline='') as stream:
            occurrences = [r for r in csv.DictReader(stream) if r['asset_id'] == nav[0]['asset_id']]
        self.assertEqual(len(occurrences), 2)
        self.assertEqual({r['page_area'] for r in occurrences}, {'header', 'footer'})
        self.assertEqual({r['usage_context'] for r in occurrences}, {'Main menu', 'Footer menu'})
        with (output/'data'/'Evidence.csv').open(encoding='utf-8-sig', newline='') as stream:
            by_id = {r['evidence_id']: r for r in csv.DictReader(stream)}
        with (output/'data'/'Sources.csv').open(encoding='utf-8-sig', newline='') as stream:
            sources = {r['source_id']: r for r in csv.DictReader(stream)}
        self.assertEqual({sources[by_id[json.loads(o['evidence_ids_json'])[0]]['source_id']]['final_url']
                          for o in occurrences}, {self.base+'/', self.base+'/sitemap-only'})

    def test_asset_unverified_identifier_does_not_merge_or_fetch(self):
        Pages.visits = []
        Pages.mode = 'assets_unverified'
        self.addCleanup(setattr, Pages, 'mode', 'normal')
        output = self.folder/'assets-unverified'
        result = _inspect_site(initialize('127.0.0.1'), output,
                               _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder)
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(Pages.visits, ['/', '/robots.txt', '/map.xml', '/sitemap-only'])
        with (output/'data'/'Asset_Inventory.csv').open(encoding='utf-8-sig', newline='') as stream:
            nav = [r for r in csv.DictReader(stream) if r['asset_type'] == 'NAVIGATION_STRUCTURE']
        self.assertEqual(len(nav), 2)
        self.assertTrue(all(not r['primary_url'] for r in nav))

    def test_asset_markup_missing_attributes_and_boundaries(self):
        parser = _AssetMarkup()
        parser.feed('<main><section><h1>Services</h1><p>First.</p><p>Second.</p></section><nav><a href="/x">X</a></nav><section><p>Unbounded.</p></section></main>')
        self.assertEqual([x['kind'] for x in parser.observed], ['CONTENT_BLOCK','NAVIGATION_STRUCTURE'])
        self.assertTrue(all(not x['label'] for x in parser.observed))
        self.assertEqual(parser.observed[0]['text'], 'Services First. Second.')

    def test_asset_markup_identifies_faq_contact_and_breadcrumb(self):
        parser = _AssetMarkup()
        parser.feed('<main><details><summary>FAQ</summary><p>Answer.</p></details><address>Local office</address></main>'
                    '<ol aria-label="breadcrumb"><li><a href="/">Home</a></li></ol>')
        self.assertEqual([x['kind'] for x in parser.observed],
                         ['CONTENT_BLOCK','CONTENT_BLOCK','NAVIGATION_STRUCTURE'])
        self.assertEqual([x.get('text') for x in parser.observed[:2]], ['FAQ Answer.', 'Local office'])

    def test_asset_markup_preserves_inline_text(self):
        parser = _AssetMarkup()
        parser.feed('<nav><a href="/"><span>Ser</span>vices</a></nav>'
                    '<main><section><h1>Ser<span>vi</span>ces</h1><p>One <b>word</b>.</p><p>Next.</p></section></main>')
        self.assertEqual(''.join(parser.observed[0]['links'][0]['text']), 'Services')
        self.assertEqual(parser.observed[1]['text'], 'Services One word. Next.')

    def test_feature_lists_and_ctas_preserved_in_evidence(self):
        Pages.visits = []
        Pages.mode = 'features'
        self.addCleanup(setattr, Pages, 'mode', 'normal')
        output = self.folder/'feature-evidence'
        result = _inspect_site(initialize('127.0.0.1'), output,
                               _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder)
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(Pages.visits, ['/', '/robots.txt', '/map.xml', '/sitemap-only'])
        self.assertEqual(result['budgets']['attempts'], 4)
        data = output/'data'
        with (data/'Asset_Inventory.csv').open(encoding='utf-8-sig', newline='') as stream:
            assets = {row['asset_id']: row for row in csv.DictReader(stream)}
        with (data/'Occurrences.csv').open(encoding='utf-8-sig', newline='') as stream:
            occurrences = list(csv.DictReader(stream))
        self.assertEqual(len(occurrences), 2)
        self.assertEqual({assets[o['asset_id']]['asset_type'] for o in occurrences}, {'CONTENT_BLOCK','NAVIGATION_STRUCTURE'})
        with (data/'Evidence.csv').open(encoding='utf-8-sig', newline='') as stream:
            evidence = {row['evidence_id']: row for row in csv.DictReader(stream)}
        with (data/'Sources.csv').open(encoding='utf-8-sig', newline='') as stream:
            sources = {row['source_id']: row for row in csv.DictReader(stream)}
        block = next(o for o in occurrences if assets[o['asset_id']]['asset_type'] == 'CONTENT_BLOCK')
        record = evidence[json.loads(block['evidence_ids_json'])[0]]
        self.assertEqual(sources[record['source_id']]['final_url'], self.base+'/')
        observation = json.loads(record['observation_result'])
        self.assertEqual([node['type'] for node in observation['feature_lists']], ['ul','ol'])
        self.assertEqual([item['text'] for item in observation['feature_lists'][0]['items']], ['Fast','Careful'])
        self.assertEqual([item['text'] for item in observation['feature_lists'][0]['items'][0]['children'][0]['items']], ['Same day','Next day'])
        self.assertEqual([item['text'] for item in observation['feature_lists'][1]['items']], ['First','Second'])
        self.assertEqual(observation['ctas'], [{'element':'button','label':'Request quote'},
            {'element':'a','label':'Contact us','destination':self.base+'/#contact'}])
        self.assertNotIn('Read more', str(observation['ctas']))
        self.assertNotIn('Submit form', str(observation['ctas']))
        navigation = next(o for o in occurrences if assets[o['asset_id']]['asset_type'] == 'NAVIGATION_STRUCTURE')
        nav_evidence = evidence[json.loads(navigation['evidence_ids_json'])[0]]
        self.assertEqual(json.loads(nav_evidence['observation_result'])[0]['text'], 'Navigation')

    def test_feature_lists_and_ctas_survive_partial(self):
        Pages.visits = []
        Pages.mode = 'features'
        self.addCleanup(setattr, Pages, 'mode', 'normal')
        output = self.folder/'feature-partial'
        result = _inspect_site(initialize('127.0.0.1'), output,
                               _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder,
                               _limits=replace(Limits(), attempts=1))
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(Pages.visits, ['/'])
        with (output/'data'/'Evidence.csv').open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        observations = [json.loads(row['observation_result']) for row in rows if row['observation_result'].startswith('{')]
        self.assertEqual(observations[0]['feature_lists'][0]['items'][1]['text'], 'Careful')
        self.assertEqual(observations[0]['ctas'][0]['label'], 'Request quote')
        self.assertIn('Status: PARTIAL', (output/'Discovery_Report.md').read_text(encoding='utf-8'))

    def test_feature_markup_role_button_and_ordinary_links(self):
        parser = _AssetMarkup()
        parser.feed('<main><section aria-label="Actions"><h1>Actions</h1><div role="button">Open <b>details</b></div>'
                    '<a data-cta href="/request">Request</a><a href="/about">About</a>'
                    '<button style="display: none">Hidden</button><input role="button" value="Field"></section></main>')
        self.assertEqual(len(parser.observed), 1)
        self.assertEqual(parser.observed[0]['ctas'], [
            {'element':'div','label':'Open details','destination':''},
            {'element':'a','label':'Request','destination':'/request'}])

    def test_feature_filter_preserves_existing_hidden_navigation(self):
        parser = _AssetMarkup()
        parser.feed('<main><h1>Intro</h1><p>Text.</p><nav hidden><ul><li><a href="/x">Menu</a></li></ul></nav>'
                    '<ol aria-label="breadcrumb"><li><a href="/">Home</a></li></ol></main>')
        self.assertEqual([item['kind'] for item in parser.observed], ['NAVIGATION_STRUCTURE','NAVIGATION_STRUCTURE','CONTENT_BLOCK'])
        self.assertEqual(''.join(parser.observed[0]['links'][0]['text']), 'Menu')
        self.assertEqual(parser.observed[2]['feature_lists'], [])

    def test_occurrence_multiple_placements_and_reference_validation(self):
        output = self.folder/'occurrence-writer'
        output.mkdir(exist_ok=True)
        run = 'RUN-fixture'
        source = {'schema_version':'1.0','run_id':run,'source_id':'SRC-1','source_type':'WEB_PAGE',
                  'requested_url':'https://example.invalid/','final_url':'https://example.invalid/',
                  'normalized_final_url':'https://example.invalid/','observation_status':'OBSERVED'}
        evidence = {'schema_version':'1.0','run_id':run,'evidence_id':'EVD-1','source_id':'SRC-1',
                    'observed_at':'2026-11-05T00:00:00Z','evidence_type':'HTTP_RESPONSE',
                    'discovery_method':'STATIC_HTTP','observation_result':'observed','technical_attributes_json':'{}'}
        asset = {'schema_version':'1.0','run_id':run,'asset_id':'AST-1','asset_category':'CONTENT',
                 'asset_type':'CONTENT_BLOCK','primary_url':None,'observation_status':'OBSERVED','evidence_ids_json':'["EVD-1"]'}
        occurrences = [{'schema_version':'1.0','run_id':run,'occurrence_id':f'OCC-{i}', 'asset_id':'AST-1',
                        'placement_page_url':'https://example.invalid/', 'page_area':area, 'component':'section',
                        'usage_context':'Local, "fixture"', 'observation_status':'OBSERVED','evidence_ids_json':'["EVD-1"]'}
                       for i, area in enumerate(('main','aside'), 1)]
        write_csv(output, sources=[source], assets=[asset], evidence=[evidence], occurrences=occurrences)
        with (output/'Occurrences.csv').open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([r['page_area'] for r in rows], ['main','aside'])
        self.assertEqual(rows[0]['usage_context'], 'Local, "fixture"')
        self.assertEqual({r['asset_id'] for r in rows}, {'AST-1'})
        for key, value in [('asset_id','AST-missing'), ('evidence_ids_json','["EVD-missing"]')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                write_csv(output, sources=[source], assets=[asset], evidence=[evidence],
                          occurrences=[{**occurrences[0], key:value}])

    def test_r10_classification_preserved_in_evidence_and_report(self):
        Pages.visits = []
        Pages.mode = 'classification'
        self.addCleanup(setattr, Pages, 'mode', 'normal')
        output = self.folder/'classification'
        result = _inspect_site(initialize('127.0.0.1'), output,
                               _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder)
        self.assertEqual(result['status'], 'COMPLETE', result['observations'])
        with (output/'data'/'Evidence.csv').open(encoding='utf-8-sig', newline='') as stream:
            pages = [attrs['page'] for row in csv.DictReader(stream)
                     if 'page' in (attrs := json.loads(row['technical_attributes_json']))]
        page = next(page for page in pages if page['title'] == 'Company')
        self.assertEqual((page['page_type'], page['page_type_confidence']), ('INFORMATIONAL', 'HIGH'))
        self.assertTrue(all(page['page_type'] in {'HOME', 'LISTING', 'DETAIL', 'INFORMATIONAL', 'FUNCTIONAL', 'UNKNOWN'}
                            for page in pages))
        self.assertTrue(all(page['page_type_confidence'] is None for page in pages if page['page_type'] == 'UNKNOWN'))
        report = (output/'Discovery_Report.md').read_text(encoding='utf-8')
        self.assertIn(self.base+'/nav: INFORMATIONAL', report)
        self.assertEqual(Pages.visits, ['/', '/robots.txt', '/map.xml', '/nav', '/main', '/other', '/sitemap-only'])

    def test_robots_disallow_blocks_queued_page_and_continues(self):
        Pages.visits = []
        Pages.mode = 'robots_disallow'
        self.addCleanup(setattr, Pages, 'mode', 'normal')
        run = initialize('127.0.0.1')
        result = _inspect_site(run, self.folder/'robots-disallow',
                               _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder)
        self.assertIn(self.base+'/nav', run.candidates, result['observations'])
        self.assertEqual(run.url_states[self.base+'/nav']['state'], 'QUEUED')
        self.assertNotIn('/nav', Pages.visits)
        self.assertEqual(Pages.visits, ['/', '/robots.txt', '/map.xml', '/main', '/other', '/sitemap-only'])
        self.assertIn(('WEB_PAGE', self.base+'/nav', 'robots restriction'), result['observations'])
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(run.url_states[self.base+'/main']['state'], 'COMPLETED')
        self.assertEqual(run.url_states[self.base+'/sitemap-only']['state'], 'COMPLETED')

    def test_unused_sitemap_reserve_returns_capacity_to_general_queue(self):
        Pages.visits = []
        result = _inspect_site(initialize('127.0.0.1'), self.folder/'unused-reserve',
                               _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder,
                               _test_caps=(5, 2))
        # One Sitemap-only page uses half the reserve; /other receives the unused slot.
        self.assertEqual(Pages.visits, ['/', '/robots.txt', '/map.xml', '/nav', '/main', '/sitemap-only', '/other'], result['observations'])
        self.assertNotIn('/child', Pages.visits)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['stop_reason'], 'page_budget')
        data = self.folder/'unused-reserve'/'data'
        with (data/'Asset_Inventory.csv').open(encoding='utf-8-sig', newline='') as stream:
            assets = list(csv.DictReader(stream))
        pages = [row for row in assets if row['asset_type'] == 'WEB_PAGE']
        self.assertEqual(len(pages), 5)
        self.assertEqual(len({row['primary_url'] for row in pages}), 5)
        report = (self.folder/'unused-reserve'/'Discovery_Report.md').read_text(encoding='utf-8')
        self.assertIn('HTML pages: 5', report)
        self.assertIn('Sitemap-only pages used: 1', report)
        self.assertIn('Maximum crawl depth: 6', report)

    def test_description_and_hreflang_preserved_in_evidence(self):
        Pages.visits = []
        Pages.mode = 'metadata'
        self.addCleanup(setattr, Pages, 'mode', 'normal')
        output = self.folder/'metadata'
        result = _inspect_site(initialize('127.0.0.1'), output,
                               _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder)
        self.assertEqual(result['status'], 'COMPLETE', result['observations'])
        with (output/'data'/'Evidence.csv').open(encoding='utf-8-sig', newline='') as stream:
            attrs = [json.loads(row['technical_attributes_json']) for row in csv.DictReader(stream)]
        nav = next(row for row in attrs if row.get('page', {}).get('title') == 'Nav')
        page = nav['page']
        self.assertEqual(page['description'], 'Local fixture & metadata')
        self.assertEqual(page['hreflang'], [{'language': 'en-US', 'url': '/en/nav'},
                                          {'language': 'de', 'url': '/de/nav'},
                                          {'language': 'x-default', 'url': '/nav'}])
        self.assertEqual(page['canonical'], self.base+'/other')
        self.assertEqual(page['headings'], [{'level': 'h1', 'text': 'Heading'}])
        self.assertIn(self.base+'/child', nav['observed_links'])
        home = next(row['page'] for row in attrs if row.get('page', {}).get('title') == 'Home')
        self.assertEqual(home['description'], '')
        self.assertEqual(home['hreflang'], [])

    def test_t01_page_language_and_indexing_signals_preserved(self):
        Pages.visits = []
        Pages.mode = 't01_metadata'
        self.addCleanup(setattr, Pages, 'mode', 'normal')
        output = self.folder/'t01-metadata'
        result = _inspect_site(initialize('127.0.0.1'), output,
                               _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder)
        self.assertEqual(result['status'], 'COMPLETE', result['observations'])
        with (output/'data'/'Evidence.csv').open(encoding='utf-8-sig', newline='') as stream:
            evidence = list(csv.DictReader(stream))
        with (output/'data'/'Sources.csv').open(encoding='utf-8-sig', newline='') as stream:
            sources = list(csv.DictReader(stream))
        nav = next(row for row in evidence if json.loads(row['technical_attributes_json']).get('page', {}).get('title') == 'Nav')
        page = json.loads(nav['technical_attributes_json'])['page']
        self.assertEqual(page['language'], 'ru-RU')
        self.assertEqual(page['indexing_signals'], [
            {'name':'robots','content':'noindex, nofollow'},
            {'name':'googlebot','content':'noarchive'},
            {'name':'ROBOTS','content':'max-snippet:0'},
        ])
        source = next(item for item in sources if item['source_id'] == nav['source_id'])
        self.assertEqual(source['final_url'], self.base+'/nav')
        self.assertEqual(Pages.visits, ['/', '/robots.txt', '/map.xml', '/nav', '/main', '/other', '/child', '/sitemap-only'])

    def test_t01_missing_language_and_indexing_signals_are_not_inferred(self):
        Pages.visits = []
        Pages.mode = 'normal'
        self.addCleanup(setattr, Pages, 'mode', 'normal')
        output = self.folder/'t01-missing-metadata'
        result = _inspect_site(initialize('127.0.0.1'), output,
                               _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder)
        self.assertEqual(result['status'], 'COMPLETE', result['observations'])
        with (output/'data'/'Evidence.csv').open(encoding='utf-8-sig', newline='') as stream:
            evidence = list(csv.DictReader(stream))
        nav = next(row for row in evidence if json.loads(row['technical_attributes_json']).get('page', {}).get('title') == 'Nav')
        page = json.loads(nav['technical_attributes_json'])['page']
        self.assertIsNone(page['language'])
        self.assertEqual(page['indexing_signals'], [])
        self.assertEqual(Pages.visits, ['/', '/robots.txt', '/map.xml', '/nav', '/main', '/other', '/child', '/sitemap-only'])

    def test_t01_empty_language_is_not_inferred(self):
        Pages.visits = []
        Pages.mode = 't01_empty_language'
        self.addCleanup(setattr, Pages, 'mode', 'normal')
        output = self.folder/'t01-empty-language'
        result = _inspect_site(initialize('127.0.0.1'), output,
                               _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder)
        self.assertEqual(result['status'], 'COMPLETE', result['observations'])
        with (output/'data'/'Evidence.csv').open(encoding='utf-8-sig', newline='') as stream:
            evidence = list(csv.DictReader(stream))
        nav = next(row for row in evidence if json.loads(row['technical_attributes_json']).get('page', {}).get('title') == 'Nav')
        page = json.loads(nav['technical_attributes_json'])['page']
        self.assertIsNone(page['language'])
        self.assertEqual(page['indexing_signals'], [])
        self.assertEqual(Pages.visits, ['/', '/robots.txt', '/map.xml', '/nav', '/main', '/other', '/child', '/sitemap-only'])

    def test_t02_functional_assets_taxonomy_provenance_and_no_requests(self):
        Pages.visits = []
        Pages.mode = 'functional'
        self.addCleanup(setattr, Pages, 'mode', 'normal')
        output = self.folder/'functional-assets'
        result = _inspect_site(initialize('127.0.0.1'), output,
                               _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder)
        self.assertEqual(result['status'], 'COMPLETE', result['observations'])
        data = output/'data'
        with (data/'Asset_Inventory.csv').open(encoding='utf-8-sig', newline='') as stream:
            assets = list(csv.DictReader(stream))
        with (data/'Occurrences.csv').open(encoding='utf-8-sig', newline='') as stream:
            occurrences = list(csv.DictReader(stream))
        with (data/'Evidence.csv').open(encoding='utf-8-sig', newline='') as stream:
            evidence = list(csv.DictReader(stream))
        with (data/'Sources.csv').open(encoding='utf-8-sig', newline='') as stream:
            sources = list(csv.DictReader(stream))
        with (data/'Relationships.csv').open(encoding='utf-8-sig', newline='') as stream:
            relationships = list(csv.DictReader(stream))

        functional = [item for item in assets if item['asset_category'] == 'FUNCTIONAL']
        counts = {kind:sum(item['asset_type'] == kind for item in functional)
                  for kind in ('FORM','SEARCH','FILTER','CALCULATOR','INTERACTIVE_MAP','OTHER','UNKNOWN')}
        self.assertEqual(counts, {'FORM':2,'SEARCH':1,'FILTER':1,'CALCULATOR':1,
                                  'INTERACTIVE_MAP':1,'OTHER':1,'UNKNOWN':1})
        self.assertTrue(all(not item['primary_url'] for item in functional))
        self.assertEqual(len(functional), 8)  # The two same-page forms remain distinct Assets.
        functional_ids = {item['asset_id'] for item in functional}
        page_source = next(source for source in sources
                           if source['source_type'] == 'WEB_PAGE' and source['final_url'] == self.base+'/')
        evidence_by_id = {item['evidence_id']:item for item in evidence}
        function_occurrences = [item for item in occurrences if item['asset_id'] in functional_ids]
        self.assertEqual(len(function_occurrences), 8)
        for occurrence in function_occurrences:
            refs = json.loads(occurrence['evidence_ids_json'])
            self.assertEqual(len(refs), 1)
            self.assertEqual(occurrence['placement_page_url'], self.base+'/')
            self.assertEqual(occurrence['observation_status'], 'OBSERVED')
            self.assertEqual(evidence_by_id[refs[0]]['source_id'], page_source['source_id'])
        for asset in functional:
            refs = json.loads(asset['evidence_ids_json'])
            self.assertEqual(len(refs), 1)
            observed = json.loads(evidence_by_id[refs[0]]['observation_result'])['functional_component']
            self.assertEqual(observed['asset_type'], asset['asset_type'])
        form_asset = next(item for item in functional if item['asset_type'] == 'FORM')
        form_evidence = evidence_by_id[json.loads(form_asset['evidence_ids_json'])[0]]
        form_data = json.loads(form_evidence['observation_result'])['functional_component']
        self.assertTrue(any(field.get('name') == 'email' and field.get('required') is True
                            for field in form_data['controls']))
        self.assertTrue(any(child['text'] == 'Send' for child in form_data['child_labels']))
        contained = [item for item in relationships
                     if item['relationship_type'] == 'CONTAINS' and item['target_object_id'] in functional_ids]
        self.assertEqual(len(contained), len(functional))
        map_source = next(item for item in sources
                          if item['source_type'] == 'EXTERNAL_DEPENDENCY'
                          and item['requested_url'] == 'https://maps.example.invalid/embed')
        self.assertEqual(map_source['observation_status'], 'NOT_INSPECTED')
        self.assertEqual(map_source['final_url'], '')
        map_embed = next(item for item in relationships
                         if item['relationship_type'] == 'EMBEDS'
                         and item['target_object_id'] == map_source['source_id'])
        self.assertEqual(map_embed['source_object_type'], 'ASSET')
        self.assertEqual(map_embed['target_object_type'], 'SOURCE')
        self.assertEqual(map_embed['source_object_id'], next(asset['asset_id'] for asset in assets
            if asset['asset_type'] == 'WEB_PAGE' and asset['primary_url'] == self.base+'/'))
        self.assertNotIn('https://maps.example.invalid/embed', Pages.visits)
        self.assertEqual(Pages.visits, ['/', '/robots.txt', '/map.xml', '/nav', '/main', '/other', '/child', '/sitemap-only'])

    def test_t03_technical_resources_source_identity_and_no_requests(self):
        Pages.visits = []
        Pages.mode = 'technical'
        self.addCleanup(setattr, Pages, 'mode', 'normal')
        output = self.folder/'technical-resources'
        result = _inspect_site(initialize('127.0.0.1'), output,
                               _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder)
        self.assertEqual(result['status'], 'COMPLETE', result['observations'])
        data = output/'data'
        tables = {}
        for filename, columns in FIELDS.items():
            with (data/filename).open(encoding='utf-8-sig', newline='') as stream:
                reader = csv.reader(stream)
                self.assertEqual(tuple(next(reader)), columns)
            with (data/filename).open(encoding='utf-8-sig', newline='') as stream:
                tables[filename] = list(csv.DictReader(stream))

        sources = tables['Sources.csv']
        assets = tables['Asset_Inventory.csv']
        evidence = tables['Evidence.csv']
        relationships = tables['Relationships.csv']
        source_by_id = {item['source_id']:item for item in sources}
        evidence_by_id = {item['evidence_id']:item for item in evidence}
        asset_by_id = {item['asset_id']:item for item in assets}
        page_assets = {item['primary_url']:item for item in assets if item['asset_type'] == 'WEB_PAGE'}
        root_asset = page_assets[self.base+'/']
        other_asset = page_assets[self.base+'/other']
        resource_relationships = [item for item in relationships
            if item['relationship_type'] in {'USES','EMBEDS'} and item['target_object_id'] in source_by_id
            and source_by_id[item['target_object_id']]['source_type'] in {'TECHNICAL_RESOURCE','EXTERNAL_DEPENDENCY'}]
        declarations_by_source = {}
        for relation in resource_relationships:
            source = source_by_id[relation['target_object_id']]
            self.assertEqual(source['observation_status'], 'NOT_INSPECTED')
            self.assertEqual(source['final_url'], '')
            self.assertEqual(source['normalized_final_url'], '')
            for evidence_id in json.loads(relation['evidence_ids_json']):
                item = evidence_by_id[evidence_id]
                declaration = json.loads(item['technical_attributes_json'])['technical_resource_declaration']
                parent = next(s for s in sources if s['source_id'] == item['source_id'])
                self.assertEqual(parent['source_type'], 'WEB_PAGE')
                self.assertEqual(parent['final_url'], asset_by_id[relation['source_object_id']]['primary_url'])
                declarations_by_source.setdefault(source['source_id'], []).append((relation, declaration))

        shared_home = self.base+'/assets/shared?utm_source=home'
        shared_other = self.base+'/assets/shared?utm_source=other'
        self.assertEqual(normalize_url(shared_home), normalize_url(shared_other))
        shared = {source_id:items for source_id,items in declarations_by_source.items()
                  if any(normalize_url(d['resolved_url']) == normalize_url(shared_home) for _,d in items)}
        self.assertEqual(len(shared), 2)  # CSS and JavaScript share a URL but not a Source.
        shared_kinds = {next(d['resource_kind'] for _,d in items) for items in shared.values()}
        self.assertEqual(shared_kinds, {'CSS','JAVASCRIPT'})
        css_shared = next(items for items in shared.values() if items[0][1]['resource_kind'] == 'CSS')
        self.assertEqual({rel['source_object_id'] for rel,_ in css_shared},
                         {root_asset['asset_id'], other_asset['asset_id']})
        self.assertTrue(all(rel['relationship_type'] == 'USES' for rel,_ in css_shared))
        print_source = next(item for item in sources if item['requested_url'].endswith('/assets/print.css'))
        self.assertNotIn(print_source['source_id'], shared)
        self.assertEqual(declarations_by_source[print_source['source_id']][0][1]['resource_kind'], 'CSS')

        resource_kinds = {declaration['resource_kind'] for items in declarations_by_source.values()
                          for _,declaration in items}
        self.assertTrue({'CSS','JAVASCRIPT','FONT','manifest'}.issubset(resource_kinds))
        self.assertTrue(any(s['source_type'] == 'TECHNICAL_RESOURCE' for s in sources))
        external_js = next(s for s in sources if s['source_type'] == 'EXTERNAL_DEPENDENCY'
                           and s['requested_url'] == 'https://cdn.example.invalid/app.js')
        self.assertTrue(any(r['relationship_type'] == 'EMBEDS'
                            and r['target_object_id'] == external_js['source_id'] for r in resource_relationships))
        self.assertTrue(any(r['relationship_type'] == 'LINKS_TO'
                            and r['target_url'] == 'https://outside.example.invalid/reference'
                            for r in relationships))

        root_evidence = next(item for item in evidence
            if json.loads(item['technical_attributes_json']).get('page', {}).get('title') == 'Technical resources')
        unclassified = json.loads(root_evidence['technical_attributes_json'])['unclassified_technical_resources']
        self.assertEqual({item['value'] for item in unclassified}, {'unclear.bin','data.json'})
        self.assertFalse(any(source['requested_url'].endswith(('/unclear.bin','/data.json')) for source in sources))
        self.assertNotIn('https://cdn.example.invalid/app.js', Pages.visits)
        self.assertEqual(Pages.visits, ['/', '/robots.txt', '/map.xml', '/nav', '/main', '/other', '/child', '/sitemap-only'])

    def test_missing_optional_html_attribute_values(self):
        page = _Page()
        page.feed('<meta name><link rel><title>Safe</title>')
        self.assertEqual(page.title, 'Safe')

    def test_request_limit_preserves_partial_package(self):
        Pages.visits = []
        result = _inspect_site(initialize('127.0.0.1'), self.folder/'partial',
                               _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder,
                               _limits=replace(Limits(), attempts=4))
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['stop_reason'], 'request_limit')
        self.assertEqual(len(list((self.folder/'partial'/'data').glob('*.csv'))), 5)
        self.assertIn('Status: PARTIAL', (self.folder/'partial'/'Discovery_Report.md').read_text(encoding='utf-8'))

    def test_sitemap_reserve_keeps_a_page_slot(self):
        Pages.visits = []
        result = _inspect_site(initialize('127.0.0.1'), self.folder/'reserve',
                               _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder,
                               _test_caps=(3, 1))
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(Pages.visits, ['/', '/robots.txt', '/map.xml', '/nav', '/sitemap-only'])
        report = (self.folder/'reserve'/'Discovery_Report.md').read_text(encoding='utf-8')
        self.assertIn('Sitemap-only pages used: 1', report)

    def test_redirect_to_visited_url_reuses_result(self):
        Pages.visits = []
        Pages.mode = 'redirect'
        try:
            result = _inspect_site(initialize('127.0.0.1'), self.folder/'redirect',
                                   _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder)
            self.assertEqual(Pages.visits.count('/other'), 1)
            self.assertEqual(result['run'].url_states[self.base+'/other']['state'], 'COMPLETED')
            alias = next(s for s in result['sources'] if s['requested_url'].endswith('/alias'))
            with (self.folder/'redirect'/'data'/'Evidence.csv').open(encoding='utf-8-sig', newline='') as f:
                evidence = [json.loads(row['technical_attributes_json']) for row in csv.DictReader(f)]
            observations = [item for row in evidence for item in row.get('reused_redirects', [])]
            self.assertTrue(any(item['source_id'] == alias['source_id'] and item['redirects'][0]['status'] == 302
                                for item in observations))
        finally:
            Pages.mode = 'normal'

    def test_redirect_before_queued_url_fetches_and_inspects_once(self):
        Pages.visits = []
        Pages.mode = 'redirect_reverse'
        try:
            result = _inspect_site(initialize('127.0.0.1'), self.folder/'redirect-reverse',
                                   _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder)
            self.assertEqual(Pages.visits.count('/other'), 1)
            self.assertEqual(result['run'].url_states[self.base+'/other']['state'], 'COMPLETED')
            report = (self.folder/'redirect-reverse'/'Discovery_Report.md').read_text(encoding='utf-8')
            self.assertEqual(report.count(self.base+'/other: UNKNOWN'), 1)
        finally:
            Pages.mode = 'normal'

    def test_loop_and_retries_remain_independent(self):
        Pages.visits = []
        with Executor(self.base, cafile=str(self.cert), work_dir=self.folder) as engine:
            loop = engine.fetch(self.base+'/loop-a', redirect_states={})
            self.assertEqual(loop['reason'], 'redirect_loop')
            self.assertEqual(Pages.visits.count('/loop-a'), 1)
            self.assertEqual(Pages.visits.count('/loop-b'), 1)
            retry = engine.fetch(self.base+'/retry', redirect_states={})
            self.assertTrue(retry['complete'])
            self.assertEqual(Pages.visits.count('/retry'), 3)

    def test_redirect_to_in_progress_does_not_dispatch(self):
        Pages.visits = []
        with Executor(self.base, cafile=str(self.cert), work_dir=self.folder) as engine:
            result = engine.fetch(self.base+'/alias', redirect_states={self.base+'/other': {'state': 'IN_PROGRESS'}})
            self.assertEqual(result['reason'], 'redirect_in_progress')
            self.assertNotIn('/other', Pages.visits)

    def test_deferred_redirect_resolves_actual_target_without_reinspection(self):
        Pages.visits = []
        run = initialize('127.0.0.1')
        ids = RunIds(run.run_id)
        adapter = SourceAdapter(ids, '1.0')
        with Executor(self.base, cafile=str(self.cert), work_dir=self.folder) as engine:
            target_result = engine.fetch(self.base+'/other')
            target, history = adapter.adapt(target_result, source_type='WEB_PAGE', observation_status='OBSERVED')
            asset, _ = AssetIdentityRegistry(ids).register(normalized_final_url=target['normalized_final_url'],
                asset_type='WEB_PAGE', asset_category='CONTENT', confirmed_final_url=True, source_type='WEB_PAGE',
                observation_status='OBSERVED', schema_version='1.0', primary_url=target['final_url'])
            ev = attach_evidence(ids, target, asset, history, confirmed=True, observed_at='2026-11-04T00:00:00Z',
                                 evidence_type='HTTP_RESPONSE', discovery_method='STATIC_HTTP', observation_result='HTML page observed')
            inspected = _Page()
            inspected.feed(Path(target_result['attempts'][-1]['body']).read_bytes().decode('utf-8'))
            self.assertEqual(inspected.title, 'Page')
            states = {self.base+'/other': {'state': 'IN_PROGRESS'}}
            alias_result = engine.fetch(self.base+'/alias', redirect_states=states)
            alias, _ = adapter.adapt(alias_result, source_type='WEB_PAGE', observation_status='OBSERVED')
            alias2_result = engine.fetch(self.base+'/alias2', redirect_states=states)
            alias2, _ = adapter.adapt(alias2_result, source_type='WEB_PAGE', observation_status='OBSERVED')
            self.assertEqual(alias_result['reason'], 'redirect_in_progress')
            self.assertEqual(alias['final_url'], self.base+'/alias')
            pending = [{'url': self.base+'/alias', 'target': self.base+'/other', 'source_id': alias['source_id'],
                        'redirects': alias_result['redirects'], 'result': alias_result},
                       {'url': self.base+'/alias2', 'target': self.base+'/other', 'source_id': alias2['source_id'],
                        'redirects': alias2_result['redirects'], 'result': alias2_result}]
            self.assertFalse(_resolve_redirect_dependencies(pending, states))
            self.assertEqual(len(pending), 2)
            states[self.base+'/other'] = {'state': 'COMPLETED', 'result': target_result, 'source': target,
                                          'asset': asset, 'evidence': ev}
            with patch.object(_Page, 'feed', side_effect=AssertionError('reinspection')):
                self.assertFalse(_resolve_redirect_dependencies(pending, states))
            self.assertFalse(pending)
            self.assertEqual(states[self.base+'/alias']['state'], 'COMPLETED')
            self.assertEqual(ev['source_id'], target['source_id'])
            self.assertEqual({entry['source_id'] for entry in json.loads(ev['technical_attributes_json'])['reused_redirects']},
                             {alias['source_id'], alias2['source_id']})
            output = self.folder/'resolved-provenance'
            output.mkdir()
            write_csv(output, sources=[target, alias, alias2], assets=[asset], evidence=[ev])
            with (output/'Evidence.csv').open(encoding='utf-8-sig', newline='') as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row['source_id'], target['source_id'])
            self.assertEqual(len(json.loads(row['technical_attributes_json'])['reused_redirects']), 2)
            self.assertEqual(Pages.visits.count('/other'), 1)
            self.assertEqual(Pages.visits.count('/alias'), 1)
            self.assertEqual(Pages.visits.count('/alias2'), 1)

    def test_deferred_redirect_terminal_failure_keeps_actual_observations(self):
        Pages.visits = []
        ids = RunIds(initialize('127.0.0.1').run_id)
        adapter = SourceAdapter(ids, '1.0')
        with Executor(self.base, cafile=str(self.cert), work_dir=self.folder) as engine:
            target_result = engine.fetch(self.base+'/missing')
            target, _ = adapter.adapt(target_result, source_type='WEB_PAGE', observation_status='OBSERVED')
            states = {self.base+'/missing': {'state': 'IN_PROGRESS'}}
            alias_result = engine.fetch(self.base+'/alias-missing', redirect_states=states)
            alias, _ = adapter.adapt(alias_result, source_type='WEB_PAGE', observation_status='OBSERVED')
            pending = [{'url': self.base+'/alias-missing', 'target': self.base+'/missing',
                        'source_id': alias['source_id'], 'redirects': alias_result['redirects'], 'result': alias_result}]
            states[self.base+'/missing'] = {'state': 'TERMINAL_FAILURE', 'result': target_result, 'source': target}
            self.assertFalse(_resolve_redirect_dependencies(pending, states))
            self.assertEqual(states[self.base+'/alias-missing']['state'], 'TERMINAL_FAILURE')
            self.assertEqual(target_result['attempts'][-1]['http_status'], 404)
            self.assertEqual(alias_result['attempts'][-1]['http_status'], 302)
            self.assertEqual(Pages.visits.count('/missing'), 1)
            self.assertEqual(Pages.visits.count('/alias-missing'), 1)

    def test_unresolved_redirect_preserves_partial_package(self):
        Pages.visits = []
        Pages.mode = 'redirect_reverse'
        try:
            run = initialize('127.0.0.1')
            run.url_states[self.base+'/other'] = {'state': 'IN_PROGRESS'}
            result = _inspect_site(run, self.folder/'pending', _fixture_url=self.base+'/',
                                   _fixture_ca=str(self.cert), _work_dir=self.folder,
                                   _limits=replace(Limits(), attempts=4))
            self.assertEqual(result['status'], 'PARTIAL')
            self.assertEqual(result['stop_reason'], 'request_limit')
            self.assertNotIn('/other', Pages.visits)
            self.assertTrue(any('unresolved redirect' in item[2] for item in result['observations']))
            self.assertTrue(any('redirect_in_progress HTTP 302' in item[2] for item in result['observations']))
            self.assertEqual(len(list((self.folder/'pending'/'data').glob('*.csv'))), 5)
            with (self.folder/'pending'/'data'/'Sources.csv').open(encoding='utf-8-sig', newline='') as stream:
                self.assertTrue(any(row['requested_url'] == self.base+'/alias' for row in csv.DictReader(stream)))
            report = (self.folder/'pending'/'Discovery_Report.md').read_text(encoding='utf-8')
            self.assertIn('Status: PARTIAL', report)
            self.assertIn('redirect_in_progress HTTP 302', report)
        finally:
            Pages.mode = 'normal'

    def test_normalized_redirect_loop_does_not_dispatch_twice(self):
        Pages.visits = []
        with Executor(self.base, cafile=str(self.cert), work_dir=self.folder) as engine:
            result = engine.fetch(self.base+'/normalized-loop?utm_first=1', redirect_states={})
            self.assertEqual(result['reason'], 'redirect_loop')
            self.assertEqual(len(Pages.visits), 1)

    def test_sitemap_redirect_to_home_does_not_refetch_home(self):
        Pages.visits = []
        Pages.mode = 'sitemap_redirect_home'
        try:
            result = _inspect_site(initialize('127.0.0.1'), self.folder/'sitemap-home',
                                   _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder)
            self.assertEqual(Pages.visits.count('/'), 1)
            self.assertTrue(any(item['source_type'] == 'SITEMAP' for item in result['sources']))
        finally:
            Pages.mode = 'normal'

    def test_redirect_reuses_terminal_404_without_forcing_partial(self):
        Pages.visits = []
        Pages.mode = 'redirect_missing'
        try:
            result = _inspect_site(initialize('127.0.0.1'), self.folder/'missing-reuse',
                                   _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder)
            self.assertEqual(Pages.visits.count('/missing'), 1)
            self.assertEqual(result['status'], 'COMPLETE')
            self.assertEqual(result['run'].url_states[self.base+'/missing']['state'], 'TERMINAL_FAILURE')
        finally:
            Pages.mode = 'normal'

    def test_depth_six_does_not_fetch_depth_seven(self):
        Pages.visits = []
        Pages.mode = 'depth'
        try:
            result = _inspect_site(initialize('127.0.0.1'), self.folder/'depth',
                                   _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder)
            self.assertIn('/chain/6', Pages.visits)
            self.assertNotIn('/chain/7', Pages.visits)
            self.assertEqual(result['status'], 'PARTIAL')
        finally:
            Pages.mode = 'normal'

    def test_confirmed_document_and_missing_resource(self):
        Pages.visits = []
        Pages.mode = 'resources'
        try:
            result = _inspect_site(initialize('127.0.0.1'), self.folder/'resources',
                                   _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder)
            found = {s['requested_url'].rsplit('/', 1)[-1]: s['source_type'] for s in result['sources']}
            self.assertEqual(found['file.pdf'], 'DOCUMENT_RESOURCE')
            with (self.folder/'resources'/'data'/'Asset_Inventory.csv').open(encoding='utf-8-sig', newline='') as f:
                urls = [row['primary_url'] for row in csv.DictReader(f)]
            self.assertIn(self.base+'/file.pdf', urls)
            self.assertNotIn(self.base+'/missing', urls)
        finally:
            Pages.mode = 'normal'

    def test_page_inspection_byte_limit_preserves_output(self):
        Pages.visits = []
        result = _inspect_site(initialize('127.0.0.1'), self.folder/'bytes',
                               _fixture_url=self.base+'/', _fixture_ca=str(self.cert), _work_dir=self.folder,
                               _limits=replace(Limits(), run_base=400))
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertLessEqual(result['budgets']['retained'], 400)
        self.assertLessEqual(result['budgets']['decoded'], 400 + 65536)
        self.assertEqual(len(list((self.folder/'bytes'/'data').glob('*.csv'))), 5)


class R10ClassificationTests(unittest.TestCase):
    def classify(self, html, url='https://example.invalid/page', *, entry=False):
        page = _Page()
        page.feed(html)
        return _classify_page(page, url, is_entry=entry)

    def test_r10_home_high(self):
        html = '<title>Welcome</title><h1>Home</h1><p>Welcome to our website.</p><nav><a href="/about">About</a></nav>'
        self.assertEqual(self.classify(html, 'https://example.invalid/', entry=True), ('HOME', 'HIGH'))

    def test_r10_listing_high(self):
        html = '<title>Services</title><h1>Services</h1><p>Services available:</p><ul><li><a href="/a">A</a></li><li><a href="/b">B</a></li></ul>'
        self.assertEqual(self.classify(html), ('LISTING', 'HIGH'))

    def test_r10_detail_high(self):
        html = '<title>Product</title><article><h1>Product</h1><p>This product has a single primary subject.</p></article>'
        self.assertEqual(self.classify(html), ('DETAIL', 'HIGH'))

    def test_r10_informational_high(self):
        html = '<title>Company</title><h1>About company</h1><p>Company information and history.</p>'
        self.assertEqual(self.classify(html), ('INFORMATIONAL', 'HIGH'))

    def test_r10_functional_high(self):
        html = '<title>Calculator</title><h1>Calculator</h1><form><input name="amount"><button>Calculate</button><output>Result</output></form>'
        self.assertEqual(self.classify(html), ('FUNCTIONAL', 'HIGH'))

    def test_r10_unknown_insufficient_evidence(self):
        self.assertEqual(self.classify('<title>Page</title><h1>Text</h1><p>Some content.</p>'), ('UNKNOWN', None))

    def test_r10_unknown_conflicting_strong_signals(self):
        html = '<title>Services and product</title><article><h1>Services and product</h1><p>Product description.</p><ul><li><a href="/a">A</a></li><li><a href="/b">B</a></li></ul></article>'
        self.assertEqual(self.classify(html), ('UNKNOWN', None))

    def test_r10_low_specific_positive_evidence(self):
        html = '<title>Product</title><p>This product is available.</p>'
        self.assertEqual(self.classify(html), ('DETAIL', 'LOW'))

    def test_r10_medium_missing_title(self):
        html = '<article><h1>Service</h1><p>A description of the available work.</p></article>'
        self.assertEqual(self.classify(html), ('DETAIL', 'MEDIUM'))

    def test_r10_unknown_null_when_all_signals_missing(self):
        self.assertEqual(self.classify(''), ('UNKNOWN', None))

    def test_r10_missing_heading_keeps_tentative_confidence(self):
        self.assertEqual(self.classify('<title>Company</title><p>Company history.</p>'), ('INFORMATIONAL', 'LOW'))

    def test_r10_secondary_order_form_preserves_detail(self):
        html = '<title>Product</title><article><h1>Product</h1><p>Product description.</p></article><form><h2>Calculator</h2><input name="quantity"><button>Calculate</button></form>'
        self.assertEqual(self.classify(html), ('DETAIL', 'HIGH'))

    def test_r10_url_alone_does_not_classify(self):
        for path in ('/', '/services', '/product', '/company', '/calculator'):
            with self.subTest(path=path):
                self.assertEqual(self.classify('<p>Text.</p>', 'https://example.invalid'+path, entry=path == '/'), ('UNKNOWN', None))

    def test_r10_content_overrides_url(self):
        html = '<title>Company</title><h1>About company</h1><p>Company history.</p>'
        self.assertEqual(self.classify(html, 'https://example.invalid/products'), ('INFORMATIONAL', 'HIGH'))

    def test_r10_isolated_heading_does_not_classify(self):
        self.assertEqual(self.classify('<h1>Company</h1>'), ('UNKNOWN', None))

    def test_r10_navigation_and_repeated_chrome_do_not_define_purpose(self):
        html = '<nav><h1>Services</h1><ul><li><a href="/a">A</a></li><li><a href="/b">B</a></li></ul></nav><h1>Text</h1><p>Unspecified content.</p>'
        self.assertEqual(self.classify(html), ('UNKNOWN', None))

    def test_r10_navigation_and_url_only_corroborate_structure(self):
        html = '<title>Services</title><h1>Services</h1><nav><a href="/services">Current</a></nav><ul><li><a href="/a">A</a></li><li><a href="/b">B</a></li></ul>'
        self.assertEqual(self.classify(html, 'https://example.invalid/services'), ('LISTING', 'HIGH'))


if __name__ == '__main__':
    unittest.main()
