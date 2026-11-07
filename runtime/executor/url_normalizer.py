"""Pure URL normalization under D-096, D-106 and D-107."""
from urllib.parse import urlsplit, unquote_plus

TRACKING = frozenset('gclid dclid gbraid wbraid gclsrc gad_source gad_campaignid _ga _gl srsltid fbclid igshid msclkid yclid ysclid mc_cid mc_eid ttclid twclid li_fat_id epik s_kwcid ef_id _hsenc _hsmi __hssc __hstc hsctatracking pk_campaign pk_kwd pk_source pk_medium pk_content pk_cid pk_keyword sc_cid mkt_tok vero_id vero_conv'.split())


def normalize_url(url):
    """Preserve the previous Executor normalizer's accepted input and output."""
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username is not None or p.password is not None:
        raise ValueError("invalid HTTP URL")
    _ = p.port
    p.hostname.lower().encode("idna")
    scheme = p.scheme.lower()
    host = p.hostname.lower()
    if ':' in host:
        host = '[' + host + ']'
    port = p.port
    authority = host + ((':' + str(port)) if port is not None and port != (80 if scheme == 'http' else 443) else '')
    parts = []
    for item in p.query.split('&'):
        name = unquote_plus(item.split('=', 1)[0]).lower()
        if name.startswith(('utm_', 'mtm_')) or name in TRACKING:
            continue
        parts.append(item)
    query = '&'.join(parts)
    return scheme + '://' + authority + p.path + (('?' + query) if query else '')
