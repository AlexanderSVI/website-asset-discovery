"""One-page CP-02.2 integration with the existing D-108 foundation."""
import ipaddress
import sys
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

_FOUNDATION = Path(__file__).resolve().parents[1] / 'runtime' / 'executor'
if str(_FOUNDATION) not in sys.path:
    sys.path.insert(0, str(_FOUNDATION))

from executor import Executor
from run_ids import RunIds
from source_adapter import SourceAdapter
from asset_identity import AssetIdentityRegistry
from foundation_output import attach_evidence, write_csv

from wad.run import DiscoveryRun


def observe_page(run: DiscoveryRun, output_dir):
    """Fetch the initialized HTTPS URL; write the single-page records."""
    return _observe_page(run, output_dir)


def _observe_page(run, output_dir, *, _fixture_url=None, _fixture_ca=None, _work_dir=None,
                  _executor=None, _ids=None, _fallback_url=None, _redirect_states=None):
    """Private local-test seam; override is restricted to the run's loopback IP."""
    if not isinstance(run, DiscoveryRun):
        raise TypeError('DiscoveryRun required')
    url = run.initial_url
    if _fixture_url is not None:
        parsed = urlsplit(_fixture_url)
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except (TypeError, ValueError):
            loopback = False
        if (parsed.scheme != 'https' or not loopback or parsed.hostname != run.host
                or parsed.username is not None or parsed.password is not None or _fixture_ca is None):
            raise ValueError('fixture must use the initialized loopback host, HTTPS and a test CA')
        url = _fixture_url
    elif _fixture_ca is not None:
        raise ValueError('test CA requires a loopback fixture')
    ids = _ids if _ids is not None else RunIds(run.run_id)
    adapter = SourceAdapter(ids, '1.0')
    registry = AssetIdentityRegistry(ids)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    context = nullcontext(_executor) if _executor is not None else Executor(run.initial_url, cafile=_fixture_ca, work_dir=_work_dir)
    with context as executor:
        result = executor.fetch(url, redirect_states=_redirect_states)
        if (_fallback_url is not None and result['attempts'] and
                all(a.get('http_status') is None for a in result['attempts']) and
                result['attempts'][-1].get('error_kind') == 'transport'):
            initial = result
            result = executor.fetch(_fallback_url, redirect_states=_redirect_states)
            result['url'] = initial['url']
            result['attempts'] = initial['attempts'] + result['attempts']
            result['redirects'] = initial['redirects'] + result['redirects']
        has_response = any(type(item.get('http_status')) is int for item in result['attempts'])
        status = ('INACCESSIBLE' if not has_response else
                  'OBSERVED' if result['complete'] else 'PARTIALLY_OBSERVED')
        source, history = adapter.adapt(result, source_type='WEB_PAGE', observation_status=status)
        if source['final_url'] is None:
            return {'run': run, 'source': source, 'asset': None, 'evidence': None,
                    'http': result, 'reason': 'no_http_response'}
        final_attempt = next(item for item in reversed(result['attempts'])
                             if item.get('url') == source['final_url'] and type(item.get('http_status')) is int)
        mime = final_attempt.get('headers', {}).get('content-type', '').split(';', 1)[0].strip().lower()
        if final_attempt['http_status'] != 200 or mime != 'text/html':
            return {'run': run, 'source': source, 'asset': None, 'evidence': None,
                    'http': result, 'reason': 'not_200_html'}
        if not result['complete'] and not final_attempt.get('retained', 0):
            return {'run': run, 'source': source, 'asset': None, 'evidence': None,
                    'http': result, 'reason': 'no_confirmed_html_body'}
        if source['normalized_final_url'] is None:
            return {'run': run, 'source': source, 'asset': None, 'evidence': None,
                    'http': result, 'reason': 'invalid_final_url'}
        asset, _ = registry.register(normalized_final_url=source['normalized_final_url'],
                                     asset_type='WEB_PAGE', asset_category='CONTENT',
                                     confirmed_final_url=True, source_type='WEB_PAGE',
                                     observation_status=status, schema_version='1.0',
                                     primary_url=source['final_url'])
        evidence = attach_evidence(ids, source, asset, history, confirmed=True,
                                   observed_at=datetime.now(timezone.utc).isoformat(),
                                   evidence_type='HTTP_RESPONSE', discovery_method='STATIC_HTTP',
                                   observation_result='HTML page observed')
        write_csv(output, sources=[source], assets=[asset], evidence=[evidence])
        return {'run': run, 'source': source, 'asset': asset, 'evidence': evidence,
                'http': result, 'reason': 'observed'}
