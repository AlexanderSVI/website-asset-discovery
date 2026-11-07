"""Run initialization; network discovery is not implemented here."""
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from uuid import uuid4


@dataclass(frozen=True)
class DiscoveryRun:
    run_id: str
    host: str
    initial_url: str
    candidates: dict = field(default_factory=dict, compare=False, repr=False)
    candidate_urls: dict = field(default_factory=dict, compare=False, repr=False)
    url_states: dict = field(default_factory=dict, compare=False, repr=False)


def initialize(domain: str) -> DiscoveryRun:
    if not isinstance(domain, str) or not domain or domain != domain.strip():
        raise ValueError('domain required without surrounding whitespace')
    parsed = urlsplit('https://' + domain)
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment or parsed.port is not None):
        raise ValueError('provide a domain, without scheme, port, path, or credentials')
    host = parsed.hostname.encode('idna').decode('ascii').lower()
    labels = host.split('.')
    if (len(host) > 253 or any(not label or len(label) > 63 or label[0] == '-' or label[-1] == '-'
            or any(not (char.isascii() and (char.isalnum() or char == '-')) for char in label)
            for label in labels)):
        raise ValueError('invalid domain')
    return DiscoveryRun('RUN-' + str(uuid4()), host, 'https://' + host)
