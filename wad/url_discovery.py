"""Bounded one-level URL discovery using the existing run-scoped executor."""
import ipaddress
from datetime import datetime, timezone
import platform
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree
import httpx

from wad.discovery import _observe_page, Executor, RunIds, SourceAdapter, write_csv
from wad.run import DiscoveryRun
from executor import host_of
from url_normalizer import normalize_url


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.base = ''
        self.mimes = {}

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'base' and not self.base:
            self.base = next((v for k, v in attrs if k.lower() == 'href' and v), '')
        if tag.lower() == 'a':
            links = [value for name, value in attrs if name.lower() == 'href' and value]
            self.links.extend(links)
            mime = next((value for name, value in attrs if name.lower() == 'type' and value), '')
            for link in links:
                if mime:
                    self.mimes[link] = mime


def discover_urls(run, output_dir):
    """Inspect the homepage and public source documents, not candidate pages."""
    return _discover_urls(run, output_dir)


def _discover_urls(run, output_dir, *, _fixture_url=None, _fixture_ca=None, _work_dir=None, _limits=None,
                   _page_processor=None, _document_hint=None):
    if not isinstance(run, DiscoveryRun):
        raise TypeError('DiscoveryRun required')
    url = run.initial_url
    if _fixture_url is not None:
        parsed = urlsplit(_fixture_url)
        try:
            valid = ipaddress.ip_address(parsed.hostname).is_loopback
        except (ValueError, TypeError):
            valid = False
        if not valid or parsed.hostname != run.host or parsed.scheme != 'https' or not _fixture_ca:
            raise ValueError('fixture requires matching loopback host, HTTPS and test CA')
        url = _fixture_url
    elif _fixture_ca is not None or _limits is not None:
        raise ValueError('test options require a loopback fixture')
    if run.candidates:
        raise ValueError('run already contains candidates')
    started_at = datetime.now(timezone.utc).isoformat()
    output = Path(output_dir)
    data_dir = output / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    ids = RunIds(run.run_id)
    adapter = SourceAdapter(ids, '1.0')
    sources, assets, evidence, observations, attempts = [], [], [], [], []
    partial, stop = False, ''
    exhausted = {'run_target', 'run_bytes', 'request_limit', 'unsafe_worker_result'}
    candidate_limit, sitemap_limit = 10_000, 20
    skipped = 0
    sitemap_provenance = {}
    with Executor(run.initial_url, cafile=_fixture_ca, work_dir=_work_dir, limits=_limits) as executor:
        def internal(value):
            try:
                return host_of(value) in executor.state.hosts
            except (ValueError, UnicodeError):
                return False

        def add(value, base, source_id):
            nonlocal partial, stop, skipped
            absolute = urljoin(base, value)
            if not internal(absolute):
                return
            try:
                key = normalize_url(absolute)
            except (ValueError, UnicodeError):
                return
            if key not in run.candidates:
                if len(run.candidates) >= candidate_limit:
                    skipped += 1
                    partial, stop = True, 'url_candidate_limit'
                    return
                run.candidates[key] = set()
            run.candidate_urls.setdefault(key, absolute)
            run.candidates[key].add(source_id)

        def fetch_document(address, kind):
            nonlocal partial, stop
            key = normalize_url(address)
            cached = run.url_states.get(key, {})
            if cached.get('state') in {'FETCHED', 'COMPLETED', 'TERMINAL_FAILURE'} and cached.get('result'):
                result = cached['result']
            else:
                result = executor.fetch(address, redirect_states=run.url_states)
                attempts.extend(result['attempts'])
            received = [a for a in result['attempts'] if type(a.get('http_status')) is int]
            if result['reason'] in exhausted:
                partial, stop = True, result['reason']
            elif result['reason'] == 'resource_bytes':
                partial, stop = True, result['reason']
            elif result['reason'] == 'byte_limit':
                partial = True
                stop = ('run_bytes' if executor.state.delivered + executor.state.uncertain_reserved >= executor.limits.run_base
                        else 'resource_bytes')
            if not received:
                observations.append((kind, address, 'no response'))
                return None, None, None
            source, _ = adapter.adapt(result, source_type=kind,
                                      observation_status='OBSERVED' if result['complete'] else 'PARTIALLY_OBSERVED')
            sources.append(source)
            reused = run.url_states.get(result.get('reused_key'), {})
            body_result = reused.get('result', result)
            body_attempts = [a for a in body_result['attempts'] if type(a.get('http_status')) is int]
            final = body_attempts[-1] if body_attempts else received[-1]
            good = result['complete'] and final['http_status'] == 200
            observed_status = received[-1]['http_status']
            observations.append((kind, source['final_url'], 'HTTP ' + str(observed_status) +
                                 (' reused response' if reused else ' complete' if good else ' incomplete')))
            body = Path(final['body']).read_bytes() if good else None
            for item in result['attempts']:
                run.url_states[normalize_url(item['url'])] = {'state': 'COMPLETED' if good else 'TERMINAL_FAILURE',
                                                              'result': body_result, 'source': source}
            return source, body, result

        parts = urlsplit(url)
        fallback = urlunsplit(('http', parts.netloc, parts.path, parts.query, ''))
        homepage = _observe_page(run, data_dir, _fixture_url=_fixture_url, _fixture_ca=_fixture_ca,
                                 _work_dir=_work_dir, _executor=executor, _ids=ids, _fallback_url=fallback,
                                 _redirect_states=run.url_states)
        hr = homepage['http']
        attempts.extend(hr['attempts'])
        if hr['reason'] in exhausted:
            partial, stop = True, hr['reason']
        elif hr['reason'] == 'resource_bytes':
            partial, stop = True, hr['reason']
        home_source = homepage['source']
        for item in hr['attempts']:
            run.url_states[normalize_url(item['url'])] = {'state': 'COMPLETED' if hr['complete'] else 'TERMINAL_FAILURE',
                                                        'result': hr, 'source': home_source,
                                                        'asset': homepage['asset'], 'evidence': homepage['evidence']}
        if home_source['final_url'] is not None:
            sources.append(home_source)
            observations.append(('WEB_PAGE', home_source['final_url'], homepage['reason']))
        else:
            observations.append(('WEB_PAGE', url, homepage['reason']))
        if not hr['complete']:
            partial, stop = True, stop or hr['reason']
        if homepage['asset'] is not None:
            assets.append(homepage['asset'])
            evidence.append(homepage['evidence'])
            attempt = next(a for a in reversed(hr['attempts']) if a.get('url') == home_source['final_url'] and a.get('http_status') == 200)
            parser = _Links()
            parser.feed(Path(attempt['body']).read_bytes().decode('utf-8', errors='replace'))
            for link in parser.links:
                link_base = home_source['final_url']
                if _document_hint is not None and parser.base and _document_hint(link, parser.mimes.get(link, '')):
                    try:
                        candidate_base = urljoin(link_base, parser.base)
                        if urlsplit(candidate_base).scheme in {'http', 'https'}:
                            link_base = candidate_base
                    except (ValueError, UnicodeError):
                        pass
                add(link, link_base, home_source['source_id'])
        else:
            partial = True
            stop = stop or homepage['reason']
        robots_url = urljoin(home_source['final_url'] or url, '/robots.txt')
        robots_source = robots_body = None
        if not stop or stop not in exhausted:
            robots_source, robots_body, _ = fetch_document(robots_url, 'ROBOTS_TXT')
        sitemap_urls = []
        robot = RobotFileParser()
        if robots_body is not None:
            robot.set_url(robots_url)
            robot.parse(robots_body.decode('utf-8', errors='replace').splitlines())
            agent = f'python-httpx/{httpx.__version__}'
            delay = robot.crawl_delay(agent)
            if delay is not None:
                executor.set_crawl_delay(run.host, delay)
            sitemap_urls = [(value, robots_source['source_id']) for value in (robot.site_maps() or [])]
        seen = set()
        completed_sitemaps = 0
        while sitemap_urls and not (stop in exhausted):
            value, discovered_by = sitemap_urls.pop(0)
            address = urljoin(robots_url, value)
            if not internal(address) or not robot.can_fetch(agent, address):
                continue
            try:
                key = normalize_url(address)
            except (ValueError, UnicodeError):
                continue
            sitemap_provenance.setdefault(key, set()).add(discovered_by)
            if key in seen:
                continue
            if len(seen) >= sitemap_limit:
                partial, stop = True, 'sitemap_limit'
                skipped += 1 + len(sitemap_urls)
                break
            seen.add(key)
            src, body, _ = fetch_document(address, 'SITEMAP')
            if body is None:
                continue
            try:
                if b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
                    raise ElementTree.ParseError('DTD not supported')
                root = ElementTree.fromstring(body)
            except ElementTree.ParseError:
                observations[-1] = ('SITEMAP', address, 'invalid XML')
                continue
            tag = root.tag.rsplit('}', 1)[-1]
            if tag not in {'urlset', 'sitemapindex'}:
                observations[-1] = ('SITEMAP', address, 'unsupported XML')
                continue
            completed_sitemaps += 1
            for item in root:
                if item.tag.rsplit('}', 1)[-1] != ('url' if tag == 'urlset' else 'sitemap'):
                    continue
                for child in item:
                    if child.tag.rsplit('}', 1)[-1] == 'loc' and child.text:
                        if tag == 'urlset':
                            add(child.text.strip(), src['final_url'], src['source_id'])
                        else:
                            sitemap_urls.append((urljoin(src['final_url'], child.text.strip()), src['source_id']))
        relationships, occurrences, extra_report = [], [], []
        if _page_processor is not None:
            result = _page_processor(run=run, executor=executor, ids=ids, sources=sources, assets=assets,
                                     evidence=evidence, homepage=homepage, robot=robot if robots_body is not None else None,
                                     add_candidate=add, internal=internal, observations=observations, attempts=attempts,
                                     stop=stop, partial=partial)
            relationships, extra_report = result['relationships'], result['report']
            occurrences = result['occurrences']
            partial, stop = partial or result['partial'], stop or result['stop']
        state = executor.state
        budgets = {'attempts': state.count, 'decoded': state.delivered, 'retained': state.retained,
                   'uncertain': state.uncertain_reserved}
        result_status = 'PARTIAL' if partial and sources else 'FAILED' if not sources else 'COMPLETE'
        write_csv(data_dir, sources=sources, assets=assets, evidence=evidence, relationships=relationships, occurrences=occurrences)
        successful_requests = sum(type(a.get('http_status')) is int and 200 <= a['http_status'] < 400
                                  and a.get('reason') == 'complete' for a in attempts)
        source_status_counts = {}
        for source in sources:
            status = source.get('observation_status') or 'UNKNOWN'
            source_status_counts[status] = source_status_counts.get(status, 0) + 1
        source_status_summary = ', '.join(f'{status}={count}'
                                          for status, count in sorted(source_status_counts.items())) or 'нет'
        lines = ['# Discovery Report', '', f'Run: {run.run_id}', f'Status: {result_status}',
                 f'Начальный домен: {run.host}',
                 f'Конечный домен главной страницы: {urlsplit(home_source["final_url"]).hostname if home_source["final_url"] else "не подтверждён"}',
                 f'Начало UTC: {started_at}', f'Завершение UTC: {datetime.now(timezone.utc).isoformat()}',
                 f'Среда: {platform.system()}, Python {platform.python_version()}; статический HTTP, браузер не используется',
                 f'Записей Source: {len(sources)}',
                 f'Статусы Source: {source_status_summary}',
                 f'HTTP(S)-попыток: {len(attempts)}',
                 f'Успешных HTTP(S)-попыток: {successful_requests}',
                 f'Неуспешных HTTP(S)-попыток: {len(attempts) - successful_requests}',
                 f'Успешных документов Sitemap: {completed_sitemaps}',
                 f'Уникальных обнаруженных URL: {len(run.candidates)}',
                 f'Причина остановки: {stop or "нет"}', f'Исчерпанный бюджет: {stop if stop in exhausted | {"resource_bytes", "sitemap_limit", "url_candidate_limit"} else "нет"}',
                 f'Использованные бюджеты: {budgets}',
                 f'Пределы: запросы={executor.limits.attempts}, байты запуска={executor.limits.run_base}, '
                 f'байты ресурса={executor.limits.resource_base}, Sitemap={sitemap_limit}, URL={candidate_limit}',
                 f'Исключено при ограничениях: {skipped}',
                 ('Границы: статическая инспекция внутренних страниц.' if _page_processor is not None else
                  'Границы: только главная страница, robots.txt и обнаруженные Sitemap; найденные страницы не посещались.'),
                 'Browser Runtime: NOT_INSPECTED.', '', '## Документы-источники']
        lines.extend(f'- {kind}: {address} — {outcome}' for kind, address, outcome in observations)
        lines.extend(extra_report)
        (output / 'Discovery_Report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return {'run': run, 'status': result_status, 'sources': sources, 'observations': observations,
                'sitemaps': completed_sitemaps, 'sitemap_provenance': sitemap_provenance,
                'budgets': budgets, 'stop_reason': stop, 'relationships': relationships}
