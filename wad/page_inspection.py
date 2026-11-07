"""Bounded static page inspection using the CP-02.3 executor session."""
from datetime import datetime, timezone
from html.parser import HTMLParser
import heapq
import itertools
import json
import re
import time
from functools import partial as bind
from pathlib import Path
from urllib.parse import urljoin
from urllib.parse import urlsplit
import httpx

from wad.url_discovery import _discover_urls
from executor import host_of
from source_adapter import SourceAdapter
from asset_identity import AssetIdentityRegistry
from foundation_output import attach_evidence
from url_normalizer import normalize_url


def _resolve_redirect_dependencies(pending, states):
    """Resolve only dependencies whose target reached a terminal state."""
    changed = True
    incomplete_failure = False
    while changed:
        changed = False
        for dependency in pending[:]:
            target = states.get(dependency['target'], {})
            if target.get('state') not in {'COMPLETED', 'TERMINAL_FAILURE'}:
                continue
            if target.get('evidence') is not None:
                attrs = json.loads(target['evidence']['technical_attributes_json'])
                attrs.setdefault('reused_redirects', []).append({'source_id': dependency['source_id'],
                                                                 'redirects': dependency['redirects']})
                target['evidence']['technical_attributes_json'] = json.dumps(attrs, ensure_ascii=False)
            if target['state'] == 'TERMINAL_FAILURE' and not target.get('result', {}).get('complete', False):
                incomplete_failure = True
            states[dependency['url']] = {'state': target['state'], 'dependency': dependency['target'],
                                         'result': dependency['result']}
            pending.remove(dependency)
            changed = True
    return incomplete_failure


_DOCUMENT_MIMES = {
    'application/pdf': 'PDF',
    'application/msword': 'DOC',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'DOCX',
    'application/vnd.ms-excel': 'XLS',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'XLSX',
}
_DOCUMENT_EXTENSIONS = {'.pdf', '.doc', '.docx', '.xls', '.xlsx'}


def _document_format(mime, sample):
    if sample.startswith(b'%PDF-'):
        return 'PDF'
    return _DOCUMENT_MIMES.get((mime or '').split(';', 1)[0].strip().lower())


def _document_candidate(url, mime=''):
    try:
        return Path(urlsplit(url).path).suffix.lower() in _DOCUMENT_EXTENSIONS or (mime or '').split(';', 1)[0].strip().lower() in _DOCUMENT_MIMES
    except (ValueError, UnicodeError):
        return False


class _TechnicalResourceMarkup(HTMLParser):
    """Read only explicit technical-resource declarations from obtained HTML."""
    _void = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
    _javascript_types = {'module','text/javascript','application/javascript',
                         'text/ecmascript','application/ecmascript'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.base = ''
        self.items = []
        self.unclassified = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'base' and not self.base and attrs.get('href'):
            self.base = attrs['href']
        resource = None
        if tag == 'link' and attrs.get('href'):
            rel_raw = attrs.get('rel') or ''
            rel = set(rel_raw.casefold().split())
            as_raw = attrs.get('as') or ''
            as_kind = as_raw.casefold()
            kind = None
            if 'stylesheet' in rel or ('preload' in rel and as_kind == 'style'):
                kind = 'CSS'
            elif 'modulepreload' in rel or ('preload' in rel and as_kind == 'script'):
                kind = 'JAVASCRIPT'
            elif 'preload' in rel and as_kind == 'font':
                kind = 'FONT'
            elif 'manifest' in rel:
                # Keep the explicitly declared rel token as the resource kind.
                kind = 'manifest'
            resource = {'element':'link', 'attribute':'href', 'value':attrs['href'],
                        'rel':rel_raw, 'as':as_raw, 'declared_type':attrs.get('type') or ''}
            resource['resource_kind'] = kind
        elif tag == 'script' and attrs.get('src'):
            declared_type = (attrs.get('type') or '').split(';', 1)[0].strip().casefold()
            kind = 'JAVASCRIPT' if not declared_type or declared_type in self._javascript_types else None
            resource = {'element':'script', 'attribute':'src', 'value':attrs['src'],
                        'declared_type':attrs.get('type') or '', 'resource_kind':kind}
        if resource:
            (self.items if resource['resource_kind'] else self.unclassified).append(resource)
        if tag not in self._void:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            del self.stack[index:]


class _DocumentMarkup(HTMLParser):
    """Capture directly declared document placements from existing HTML."""
    _void = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.items, self.anchor = [], [], None
        self.base = ''

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'base' and not self.base and attrs.get('href'):
            self.base = attrs['href']
        attribute = {'a':'href', 'embed':'src', 'object':'data', 'iframe':'src'}.get(tag)
        if attribute and attrs.get(attribute) and _document_candidate(attrs[attribute], attrs.get('type')):
            item = {'value':attrs[attribute], 'mime':attrs.get('type') or '', 'component':tag,
                    'area':next((x for x in reversed(self.stack) if x in {'main','nav','header','footer','aside'}), ''),
                    'context':attrs.get('aria-label') or attrs.get('title') or '', 'text':[]}
            self.items.append(item)
            if tag == 'a':
                self.anchor = item
        if tag not in self._void:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag == 'a':
            self.anchor = None
        if tag in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            del self.stack[index:]

    def handle_data(self, data):
        if self.anchor is not None:
            self.anchor['text'].append(data)


def _resource_type(mime, sample, final_url):
    """Use response content and MIME; a suffix alone is not a type proof."""
    suffix = Path(urlsplit(final_url).path).suffix.lower()
    prefix = sample.lstrip().lower()
    if sample.startswith(b'%PDF-') or (mime == 'application/pdf' and suffix == '.pdf'):
        return 'DOCUMENT_RESOURCE'
    if mime.startswith(('image/', 'audio/', 'video/')):
        return 'MEDIA_RESOURCE'
    if mime == 'text/html' or (mime in ('', 'application/octet-stream') and
                               (prefix.startswith(b'<!doctype html') or prefix.startswith(b'<html'))):
        return 'WEB_PAGE'
    return 'UNKNOWN'


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.description, self.canonical = '', '', ''
        self.language = None
        self.indexing_signals = []
        self.headings, self.hreflang, self.links = [], [], []
        self._title = False
        self._heading = None
        self.primary_headings = []
        self._primary_heading = False
        self._regions = []
        self.content = []
        self.paragraphs = 0
        self.articles = 0
        self.controls = set()
        self.collections = []
        self._collections = []
        self._excluded = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'html':
            language = attrs.get('lang')
            if language is not None and language.strip():
                self.language = language
        if tag in {'nav', 'header', 'footer', 'script', 'style', 'template'}:
            self._excluded.append(tag)
        if not self._excluded:
            if tag == 'p':
                self.paragraphs += 1
            if tag == 'article':
                self.articles += 1
            if tag in {'form', 'input', 'select', 'textarea', 'button', 'output'}:
                self.controls.add(tag)
            if tag in {'ul', 'ol'}:
                self._collections.append({'items': 0, 'links': set()})
            if self._collections:
                if tag == 'li':
                    self._collections[-1]['items'] += 1
                if tag == 'a' and attrs.get('href'):
                    self._collections[-1]['links'].add(attrs['href'])
        if tag in ('nav', 'main'):
            self._regions.append(tag)
        if tag == 'title':
            self._title = True
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self._heading = [tag, '']
            self._primary_heading = not self._excluded
        if tag == 'meta' and (attrs.get('name') or '').lower() == 'description':
            self.description = attrs.get('content', '')
        if tag == 'meta' and (attrs.get('name') or '').casefold() in {'robots', 'googlebot'}:
            if 'content' in attrs:
                self.indexing_signals.append({'name': attrs['name'], 'content': attrs['content']})
        if tag == 'link':
            rel = (attrs.get('rel') or '').lower().split()
            if 'canonical' in rel:
                self.canonical = attrs.get('href', '')
            if 'alternate' in rel and attrs.get('hreflang') and attrs.get('href'):
                self.hreflang.append({'language': attrs['hreflang'], 'url': attrs['href']})
        if tag == 'a' and attrs.get('href'):
            self.links.append((attrs['href'], 'nav' if 'nav' in self._regions else 'main' if 'main' in self._regions else 'other'))

    def handle_endtag(self, tag):
        if not self._excluded and tag in {'ul', 'ol'} and self._collections:
            self.collections.append(self._collections.pop())
        if tag in self._excluded:
            self._excluded.remove(tag)
        if tag in ('nav', 'main') and tag in self._regions:
            self._regions.remove(tag)
        if tag == 'title':
            self._title = False
        if self._heading and tag == self._heading[0]:
            self.headings.append({'level': tag, 'text': self._heading[1].strip()})
            if self._primary_heading:
                self.primary_headings.append(self.headings[-1])
            self._heading = None

    def handle_data(self, data):
        if not self._excluded and not self._title and not self._heading:
            self.content.append(data)
        if self._title:
            self.title += data
        if self._heading:
            self._heading[1] += data


def _purpose_signals(text):
    """Recognize explicit purpose words, not URL-derived page types."""
    vocabulary = {
        'HOME': r'home|welcome|главная|добро пожаловать',
        'LISTING': r'services|products|catalog|catalogue|collection|directory|услуги|товары|каталог',
        'DETAIL': r'service|product|item|услуга|товар',
        'INFORMATIONAL': r'about|company|contact|faq|help|policy|terms|компания|контакты|о компании',
        'FUNCTIONAL': r'calculator|calculate|search|filter|registration|login|калькулятор|рассчитать|поиск',
    }
    return {kind for kind, words in vocabulary.items()
            if re.search(r'(?<!\w)(?:' + words + r')(?!\w)', text.casefold())}


def _classify_page(page, url, *, is_entry=False):
    """Return one tentative primary purpose and qualitative D-056 confidence."""
    title = _purpose_signals(page.title)
    headings = _purpose_signals(' '.join(h['text'] for h in page.primary_headings if h['level'] == 'h1'))
    content = _purpose_signals(' '.join(page.content))
    primary = title | headings
    has_content = any(text.strip() for text in page.content)
    # Two distinct linked entries establish recurrence, not a confidence score.
    recurring = any(c['items'] > 1 and len(c['links']) > 1 for c in page.collections) or page.articles > 1
    form = ('form' in page.controls and bool(page.controls & {'input', 'select', 'textarea'})
            and bool(page.controls & {'button', 'output'}))
    navigation = [href for href, region in page.links if region == 'nav']
    structure = {
        'HOME': is_entry and bool(navigation) and has_content,
        'LISTING': recurring and has_content,
        'DETAIL': page.articles == 1 and page.paragraphs > 0 and has_content,
        'INFORMATIONAL': bool(headings & {'INFORMATIONAL'}) and page.paragraphs > 0 and has_content,
        'FUNCTIONAL': form and has_content,
    }
    strong = {kind for kind, supported in structure.items() if supported and kind in primary}
    if len(strong) > 1:
        return 'UNKNOWN', None
    if strong:
        kind = next(iter(strong))
        # URL and navigation only corroborate an already established purpose.
        url_support = kind in _purpose_signals(urlsplit(url).path.replace('-', ' ').replace('_', ' '))
        nav_support = any(urlsplit(urljoin(url, href)).path == urlsplit(url).path for href in navigation)
        consistent = kind in title and kind in headings
        if consistent and (kind in content or (url_support and nav_support)):
            return kind, 'HIGH'
        return kind, 'MEDIUM'
    weak = primary & content
    weak.discard('HOME')  # Entry identity needs content and navigation, never a path alone.
    if not form:
        weak.discard('FUNCTIONAL')
    if len(weak) == 1 and has_content:
        return next(iter(weak)), 'LOW'
    return 'UNKNOWN', None


class _FunctionalMarkup(HTMLParser):
    """Collect statically identified functional components without interacting with them."""
    _void = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
    _controls = {'button', 'input', 'output', 'select', 'textarea'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.active = None
        self.hidden = []
        self.text_targets = []
        self.observed = []

    @staticmethod
    def _labels(attrs):
        return ' '.join(value for value in (attrs.get('aria-label'), attrs.get('title')) if value).strip()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        depth = len(self.stack)
        style = re.sub(r'\s+', '', (attrs.get('style') or '').lower())
        hidden = ('hidden' in attrs or (attrs.get('aria-hidden') or '').lower() == 'true'
                  or 'display:none' in style or 'visibility:hidden' in style)
        if hidden and tag not in self._void:
            self.hidden.append(depth)
        suppressed = bool(self.hidden) or tag in {'script', 'style', 'template'} or any(
            item in {'script', 'style', 'template'} for item in self.stack)
        role = (attrs.get('role') or '').casefold()
        labels = self._labels(attrs)

        if not suppressed:
            if self.active is not None:
                if tag in self._controls:
                    control = {'element': tag}
                    for key in ('type', 'name', 'aria-label', 'title', 'placeholder'):
                        if attrs.get(key):
                            control[key] = attrs[key]
                    if 'required' in attrs:
                        control['required'] = True
                    self.active['controls'].append(control)
                    if tag in {'button', 'output'}:
                        self.text_targets.append({'depth': depth, 'tag': tag, 'value': []})
                elif tag in {'label', 'legend'}:
                    self.text_targets.append({'depth': depth, 'tag': tag, 'value': []})
            elif tag == 'iframe' and self._has_word(labels, 'map'):
                self.observed.append({'asset_type': 'INTERACTIVE_MAP', 'element': tag,
                    'area': next((x for x in reversed(self.stack) if x in {'main','header','footer','aside'}), ''),
                    'label': labels, 'controls': [], 'child_labels': [], 'declared_source': attrs.get('src')})
            elif tag == 'form' or tag == 'search' or role in {'search', 'application'}:
                area = next((x for x in reversed(self.stack) if x in {'main','header','footer','aside'}), '')
                self.active = {'tag': tag, 'role': role, 'depth': depth, 'area': area,
                    'label': labels, 'controls': [], 'child_labels': []}
                if tag == 'button' or tag in {'output'}:
                    self.text_targets.append({'depth': depth, 'tag': tag, 'value': []})

        if tag not in self._void:
            self.stack.append(tag)

    @staticmethod
    def _has_word(value, word):
        return bool(re.search(r'(?<!\w)' + re.escape(word) + r'(?!\w)', (value or '').casefold()))

    def handle_endtag(self, tag):
        if tag not in self.stack:
            return
        position = len(self.stack) - 1 - self.stack[::-1].index(tag)
        for target in self.text_targets[:]:
            if target['tag'] == tag and target['depth'] >= position:
                text = ' '.join(''.join(target['value']).split())
                if text and self.active is not None:
                    self.active['child_labels'].append({'element': tag, 'text': text})
                self.text_targets.remove(target)
        if self.active is not None and self.active['tag'] == tag and self.active['depth'] == position:
            item = self.active
            labels = item['label'].casefold()
            control_types = {control.get('type', '').casefold() for control in item['controls']}
            if item['role'] == 'search' or item['tag'] == 'search' or 'search' in labels or 'search' in control_types:
                kind = 'SEARCH'
            elif 'filter' in labels:
                kind = 'FILTER'
            elif 'calculator' in labels:
                kind = 'CALCULATOR'
            elif item['role'] == 'application' and self._has_word(item['label'], 'map'):
                kind = 'INTERACTIVE_MAP'
            elif item['role'] == 'application' and self._has_word(item['label'], 'color picker'):
                kind = 'OTHER'
            elif item['role'] == 'application' and item['controls']:
                kind = 'UNKNOWN'
            elif item['tag'] == 'form' and any(c['element'] in {'input','select','textarea'} for c in item['controls']):
                kind = 'FORM'
            else:
                kind = None
            if kind and kind not in {'INTERACTIVE_MAP'} and not item['controls']:
                kind = None
            if kind:
                self.observed.append({'asset_type': kind, 'element': item['tag'], 'area': item['area'],
                    'label': item['label'], 'controls': item['controls'], 'child_labels': item['child_labels'],
                    'declared_source': None})
            self.active = None
            self.text_targets = []
        self.hidden = [depth for depth in self.hidden if depth < position]
        del self.stack[position:]

    def handle_data(self, data):
        if self.text_targets:
            self.text_targets[-1]['value'].append(data)


class _AssetMarkup(HTMLParser):
    """Collect directly observed semantic blocks and navigation from one body."""
    _void = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.active = []
        self.observed = []
        self.hidden = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        nav_active = any(item['kind'] == 'NAVIGATION_STRUCTURE' for item in self.active)
        role = (attrs.get('role') or '').lower()
        label = attrs.get('aria-label') or ''
        identifier = (attrs.get('itemid') or '') if 'itemscope' in attrs else ''
        is_nav = tag == 'nav' or role == 'navigation' or (tag in {'ol', 'ul'} and 'breadcrumb' in label.lower())
        style = re.sub(r'\s+', '', (attrs.get('style') or '').lower())
        is_hidden = ('hidden' in attrs or (attrs.get('aria-hidden') or '').lower() == 'true'
                     or 'display:none' in style or 'visibility:hidden' in style)
        suppressed = (((bool(self.hidden) or is_hidden) and not (nav_active or is_nav))
                      or tag in {'script', 'style', 'template', 'form'}
                      or any(item in {'script', 'style', 'template', 'form'} for item in self.stack))
        if is_hidden and tag not in self._void:
            self.hidden.append(len(self.stack))
        if not suppressed and is_nav and not nav_active:
            area = next((item for item in reversed(self.stack) if item in {'header', 'main', 'footer', 'aside'}), '')
            self.active.append({'tag': tag, 'kind': 'NAVIGATION_STRUCTURE', 'area': area, 'label': label, 'identifier': identifier,
                                'heading': [], 'paragraph': [], 'links': [], 'depth': len(self.stack)})
        elif not suppressed and not nav_active and tag in {'main', 'section', 'article', 'details', 'address'}:
            area = tag if tag == 'main' else next((item for item in reversed(self.stack) if item in {'main', 'header', 'footer', 'aside'}), '')
            self.active.append({'tag': tag, 'kind': 'CONTENT_BLOCK', 'area': area, 'label': label, 'identifier': identifier,
                                'heading': [], 'paragraph': [], 'links': [], 'depth': len(self.stack), 'child': False,
                                'feature_lists': [], 'list_stack': [], 'ctas': [], 'active_ctas': []})
        if not suppressed and not nav_active and not is_nav:
            classes = set((attrs.get('class') or '').lower().split())
            is_cta = (tag not in {'input', 'select', 'textarea'} and
                      (tag == 'button' or role == 'button' or
                       (tag == 'a' and ('data-cta' in attrs or bool(classes & {'cta', 'call-to-action'})))))
            for item in self.active:
                if item['kind'] != 'CONTENT_BLOCK':
                    continue
                if tag in {'ul', 'ol'}:
                    node = {'type': tag, 'items': []}
                    if item['list_stack'] and item['list_stack'][-1]['current'] is not None:
                        item['list_stack'][-1]['current']['children'].append(node)
                    else:
                        item['feature_lists'].append(node)
                    item['list_stack'].append({'node': node, 'current': None, 'depth': len(self.stack)})
                elif tag == 'li' and item['list_stack']:
                    entry = {'text': [], 'children': []}
                    item['list_stack'][-1]['node']['items'].append(entry)
                    item['list_stack'][-1]['current'] = entry
                if is_cta:
                    item['active_ctas'].append({'element': tag, 'label': [], 'destination': attrs.get('href') or attrs.get('formaction') or '', 'depth': len(self.stack)})
        if not suppressed and not nav_active and tag in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'summary', 'p', 'br'}:
            for item in self.active:
                if item['kind'] == 'CONTENT_BLOCK':
                    field = 'heading' if tag in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'summary'} else 'paragraph'
                    item[field].append(' ')
        if not suppressed and tag == 'a' and attrs.get('href'):
            for item in self.active:
                if item['kind'] == 'NAVIGATION_STRUCTURE':
                    link = {'href': attrs['href'], 'depth': sum(x in {'ul', 'ol'} for x in self.stack[item['depth']:]), 'text': []}
                    item['links'].append(link)
                    item['active_link'] = link
        if tag not in self._void:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag not in self.stack:
            return
        if tag == 'a':
            for item in self.active:
                if item['kind'] == 'NAVIGATION_STRUCTURE':
                    item.pop('active_link', None)
        position = len(self.stack) - 1 - self.stack[::-1].index(tag)
        for item in self.active:
            if item['kind'] != 'CONTENT_BLOCK':
                continue
            if tag == 'li' and item['list_stack']:
                item['list_stack'][-1]['current'] = None
            while item['list_stack'] and item['list_stack'][-1]['depth'] >= position:
                item['list_stack'].pop()
            for cta in item['active_ctas'][:]:
                if cta['depth'] >= position:
                    item['active_ctas'].remove(cta)
                    cta['label'] = ' '.join(''.join(cta['label']).split())
                    if cta['label']:
                        item['ctas'].append({key: cta[key] for key in ('element', 'label', 'destination')})
        self.hidden = [depth for depth in self.hidden if depth < position]
        for item in self.active[:]:
            if item['depth'] >= position:
                self.active.remove(item)
                heading = ' '.join(''.join(item['heading']).split())
                paragraph = ' '.join(''.join(item['paragraph']).split())
                if item['kind'] == 'NAVIGATION_STRUCTURE' and item['links']:
                    self.observed.append(item)
                elif (item['kind'] == 'CONTENT_BLOCK' and not item['child'] and
                      ((item['tag'] == 'main' and (heading or paragraph or item['feature_lists'] or item['ctas'])) or
                       (item['tag'] == 'address' and paragraph) or
                       (item['tag'] in {'section', 'article', 'details'} and (heading or item['label']) and (paragraph or item['feature_lists'] or item['ctas'])))):
                    item['text'] = ' '.join(part for part in (heading, paragraph) if part)
                    self.observed.append(item)
                    for parent in self.active:
                        if parent['kind'] == 'CONTENT_BLOCK':
                            parent['child'] = True
        del self.stack[position:]

    def handle_data(self, data):
        if any(tag in {'script', 'style', 'template', 'form'} for tag in self.stack):
            return
        navigation = [item for item in self.active if item['kind'] == 'NAVIGATION_STRUCTURE']
        if navigation:
            for item in navigation:
                if item.get('active_link') is not None:
                    item['active_link']['text'].append(data)
            return
        if self.hidden:
            return
        for item in self.active:
            if item['kind'] != 'CONTENT_BLOCK':
                continue
            if item['list_stack'] and item['list_stack'][-1]['current'] is not None:
                item['list_stack'][-1]['current']['text'].append(data)
            for cta in item['active_ctas']:
                cta['label'].append(data)
            if any(tag in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'summary'} for tag in self.stack[item['depth']:]):
                item['heading'].append(data)
            elif 'p' in self.stack[item['depth']:] or item['tag'] == 'address':
                item['paragraph'].append(data)


def _srcset(value):
    """Parse URL tokens and descriptors without splitting commas inside URL tokens."""
    result, pos = [], 0
    while pos < len(value):
        while pos < len(value) and (value[pos].isspace() or value[pos] == ','):
            pos += 1
        start = pos
        while pos < len(value) and not value[pos].isspace():
            pos += 1
        token = value[start:pos]
        if not token:
            break
        if token.endswith(','):
            result.append((token.rstrip(','), ''))
            continue
        while pos < len(value) and value[pos].isspace():
            pos += 1
        start = pos
        while pos < len(value) and value[pos] != ',':
            pos += 1
        result.append((token, value[start:pos].strip()))
    return [(url, descriptor) for url, descriptor in result if url]


def _media_type(value, mime=''):
    """Recognize direct file references from explicit MIME or a small extension set."""
    mime = (mime or '').split(';', 1)[0].strip().lower()
    for prefix, kind in (('image/', 'IMAGE'), ('audio/', 'AUDIO'), ('video/', 'VIDEO')):
        if mime.startswith(prefix):
            return kind
    if mime and mime != 'application/octet-stream':
        return None  # Conflicting explicit MIME is not a clear media indicator.
    try:
        suffix = Path(urlsplit(value).path).suffix.lower()
    except ValueError:
        return None
    return next((kind for kind, extensions in {
        'IMAGE': {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.avif'},
        'AUDIO': {'.mp3', '.wav', '.ogg', '.m4a', '.aac', '.flac'},
        'VIDEO': {'.mp4', '.webm', '.mov', '.m4v', '.ogv'},
    }.items() if suffix in extensions), None)


class _MediaMarkup(HTMLParser):
    """Read media declarations from an existing body; never fetch resources."""
    _void = _AssetMarkup._void

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.groups, self.observed = [], [], []
        self.base = ''

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'base' and not self.base and attrs.get('href'):
            self.base = attrs['href']
        area = next((x for x in reversed(self.stack) if x in {'main', 'header', 'footer', 'aside', 'nav'}), '')
        if tag == 'picture':
            self.groups.append({'tag': tag, 'kind': 'IMAGE', 'area': area, 'component': 'picture',
                                'context': '', 'candidates': [], 'depth': len(self.stack)})
        elif tag in {'audio', 'video'}:
            self.groups.append({'tag': tag, 'kind': tag.upper(), 'area': area, 'component': tag,
                                'context': '', 'candidates': [], 'depth': len(self.stack)})
            if attrs.get('src'):
                self.groups[-1]['candidates'].append({'element': tag, 'attribute': 'src', 'value': attrs['src'], 'descriptor': ''})
            if tag == 'video' and attrs.get('poster'):
                self.observed.append({'kind': 'IMAGE', 'area': area, 'component': 'video', 'context': 'video poster',
                                      'candidates': [{'element': 'video', 'attribute': 'poster', 'value': attrs['poster'], 'descriptor': ''}]})
        elif tag == 'img':
            candidates = []
            if attrs.get('src'):
                candidates.append({'element': 'img', 'attribute': 'src', 'value': attrs['src'], 'descriptor': ''})
            candidates.extend({'element': 'img', 'attribute': 'srcset', 'value': url, 'descriptor': desc}
                              for url, desc in _srcset(attrs.get('srcset') or ''))
            picture = next((g for g in reversed(self.groups) if g['tag'] == 'picture'), None)
            if picture is not None:
                picture['candidates'].extend(candidates)
            elif candidates:
                self.observed.append({'kind': 'IMAGE', 'area': area, 'component': 'img', 'context': '', 'candidates': candidates})
        elif tag == 'source' and self.groups:
            group = self.groups[-1]
            if group['tag'] == 'picture':
                candidates = [{'element': 'source', 'attribute': 'srcset', 'value': url, 'descriptor': desc}
                              for url, desc in _srcset(attrs.get('srcset') or '')]
                if attrs.get('type'):
                    for candidate in candidates:
                        candidate['type'] = attrs['type']
                group['candidates'].extend(candidates)
            elif group['tag'] in {'audio', 'video'} and attrs.get('src'):
                candidate = {'element': 'source', 'attribute': 'src', 'value': attrs['src'], 'descriptor': ''}
                if attrs.get('type'):
                    candidate['type'] = attrs['type']
                group['candidates'].append(candidate)
        elif tag == 'a' and attrs.get('href'):
            kind = _media_type(attrs['href'], attrs.get('type'))
            if kind:
                candidate = {'element': 'a', 'attribute': 'href', 'value': attrs['href'], 'descriptor': ''}
                if attrs.get('type'):
                    candidate['type'] = attrs['type']
                self.observed.append({'kind': kind, 'area': area, 'component': 'a', 'context': '',
                                      'candidates': [candidate]})
        elif tag == 'iframe' and attrs.get('src'):
            label = ' '.join((attrs.get('title') or '', attrs.get('aria-label') or '')).lower()
            hinted = _media_type(attrs['src'], attrs.get('type'))
            mime = (attrs.get('type') or '').split(';', 1)[0].strip().lower()
            if hinted == 'VIDEO' or (hinted is None and mime in {'', 'text/html'} and re.search(r'\bvideo\b', label) and re.search(r'\b(player|embed)\b', label)):
                candidate = {'element': 'iframe', 'attribute': 'src', 'value': attrs['src'], 'descriptor': ''}
                candidate.update({key: attrs[key] for key in ('title', 'aria-label', 'type') if attrs.get(key)})
                self.observed.append({'kind': 'VIDEO', 'area': area, 'component': 'iframe', 'context': 'video embed',
                                      'candidates': [candidate]})
        if tag not in self._void:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag not in self.stack:
            return
        position = len(self.stack) - 1 - self.stack[::-1].index(tag)
        for group in self.groups[:]:
            if group['depth'] >= position:
                self.groups.remove(group)
                if group['candidates']:
                    self.observed.append(group)
        del self.stack[position:]


def inspect_site(run, output_dir):
    """Inspect discovered internal pages within the existing run budgets."""
    return _inspect_site(run, output_dir)


def _inspect_site(run, output_dir, *, _test_caps=None, **test_options):
    if _test_caps is not None and not test_options.get('_fixture_url'):
        raise ValueError('test caps require a loopback fixture')
    return _discover_urls(run, output_dir, _page_processor=bind(_process, _test_caps=_test_caps),
                          _document_hint=_document_candidate, **test_options)


def _process(*, run, executor, ids, sources, assets, evidence, homepage, robot,
             add_candidate, internal, observations, attempts, stop, partial, _test_caps=None):
    registry = AssetIdentityRegistry(ids)
    for asset in assets:
        source = homepage['source']
        registry.assets.append(asset)
        registry._identities[(run.run_id, source['normalized_final_url'], 'WEB_PAGE')] = asset
    adapter = SourceAdapter(ids, '1.0')
    source_types = {item['source_id']: item['source_type'] for item in sources}
    for item in sources:
        if item['source_type'] in {'ROBOTS_TXT', 'SITEMAP'} and item.get('final_url'):
            for address in (item['requested_url'], item['final_url']):
                run.url_states.setdefault(normalize_url(address), {'state': 'COMPLETED', 'source': item})
    queue, visited, scheduled = [], set(), {}
    order = itertools.count()
    relationships, occurrences, page_rows, canonical_rows, pending_redirects = [], [], [], [], []
    external_media_sources = {}
    technical_sources_by_key = {}
    technical_relationships_by_key = {}
    document_refs, document_candidates = {}, set()
    maximum_depth = 6
    max_pages, sitemap_reserve = 500, 100
    if _test_caps is not None:
        max_pages, sitemap_reserve = _test_caps
        if not (1 <= max_pages <= 500 and 0 <= sitemap_reserve <= min(100, max_pages)):
            raise ValueError('test caps must not increase limits')
    initial_candidates = len(run.candidates)
    sitemap_used, pages, general_pages = 0, 0, 0
    agent = f'python-httpx/{httpx.__version__}'

    def enqueue(url, depth, priority, sitemap_only):
        if run.url_states.get(url, {}).get('state') in {'FETCHED', 'COMPLETED', 'TERMINAL_FAILURE', 'IN_PROGRESS'}:
            return
        previous = scheduled.get(url)
        if previous is not None and previous <= (priority, depth if depth is not None else 99):
            return
        key = (priority, depth if depth is not None else 99)
        scheduled[url] = key
        run.url_states.setdefault(url, {'state': 'QUEUED'})
        heapq.heappush(queue, (key[0], key[1], next(order), url, depth, sitemap_only))

    def add_relation(asset, ev, kind, target):
        relationships.append({'schema_version': '1.0', 'run_id': run.run_id,
                              'relationship_id': ids.next('REL'), 'source_object_type': 'ASSET',
                              'source_object_id': asset['asset_id'], 'relationship_type': kind,
                              'target_object_type': 'SOURCE', 'target_object_id': '', 'target_url': target,
                              'confirmation_status': 'CONFIRMED', 'evidence_ids_json': json.dumps([ev['evidence_id']])})

    def emit_document_placements(key, document):
        for placement in document_refs.get(key, []):
            if placement['emitted']:
                continue
            occurrences.append({'schema_version':'1.0', 'run_id':run.run_id, 'occurrence_id':ids.next('OCC'),
                'asset_id':document['asset_id'], 'placement_page_url':placement['page'],
                'page_area':placement['area'], 'component':placement['component'],
                'usage_context':placement['context'], 'observation_status':'OBSERVED',
                'evidence_ids_json':json.dumps([placement['evidence']['evidence_id']])})
            placement['emitted'] = True

    def discover_documents(body, base, source, ev, page, depth, attrs):
        nonlocal partial, stop
        if time.monotonic_ns() >= executor.state.started + round(executor.limits.run_target * 1_000_000_000):
            partial, stop = True, 'run_target'
            return {}
        parser = _DocumentMarkup()
        parser.feed(body.decode('utf-8', errors='replace'))
        anchor_targets = {}
        doc_base = base
        if parser.base:
            try:
                candidate_base = urljoin(base, parser.base)
                if urlsplit(candidate_base).scheme in {'http','https'}:
                    doc_base = candidate_base
            except ValueError:
                pass
        for item in parser.items:
            if time.monotonic_ns() >= executor.state.started + round(executor.limits.run_target * 1_000_000_000):
                partial, stop = True, 'run_target'
                break
            try:
                target = urljoin(doc_base, item['value'])
                parsed = urlsplit(target)
                if parsed.scheme not in {'http','https'} or parsed.username is not None or parsed.password is not None:
                    continue
                key = normalize_url(target)
            except (ValueError, UnicodeError):
                continue
            if item['component'] == 'a':
                anchor_targets[item['value']] = target
            observed = {'url':target, 'element':item['component'],
                        'attribute':{'a':'href','embed':'src','object':'data','iframe':'src'}[item['component']],
                        'value':item['value']}
            if item['mime']:
                observed['declared_mime'] = item['mime']
            attrs.setdefault('document_references', []).append(observed)
            if not internal(target):
                if item['component'] == 'a':
                    add_relation(page, ev, 'LINKS_TO', target)
                else:
                    ext = external_media_sources.get(target)
                    if ext is None:
                        ext = {'schema_version':'1.0','run_id':run.run_id,'source_id':ids.next('SRC'),
                            'source_type':'EXTERNAL_DEPENDENCY','requested_url':target,'final_url':None,
                            'normalized_final_url':None,'observation_status':'NOT_INSPECTED'}
                        external_media_sources[target] = ext
                        sources.append(ext)
                    relationships.append({'schema_version':'1.0','run_id':run.run_id,
                        'relationship_id':ids.next('REL'),'source_object_type':'ASSET','source_object_id':page['asset_id'],
                        'relationship_type':'EMBEDS','target_object_type':'SOURCE','target_object_id':ext['source_id'],
                        'target_url':target,'confirmation_status':'CONFIRMED','evidence_ids_json':json.dumps([ev['evidence_id']])})
                continue
            if item['component'] != 'a':
                add_relation(page, ev, 'EMBEDS', target)
            document_candidates.add(key)
            context = item['context'] or ' '.join(''.join(item['text']).split())
            document_refs.setdefault(key, []).append({'page':base, 'area':item['area'], 'component':item['component'],
                'context':context, 'evidence':ev, 'emitted':False})
            known = run.url_states.get(key, {})
            if (known.get('asset') or {}).get('asset_type') == 'DOCUMENT':
                emit_document_placements(key, known['asset'])
            add_candidate(target, base, source['source_id'])
            if key in run.candidates and (depth is None or depth < maximum_depth):
                enqueue(key, None if depth is None else depth + 1, 2, False)
        return anchor_targets

    def inspect(asset, ev, source, body, depth):
        nonlocal partial, stop
        parser = _Page()
        parser.feed(body.decode('utf-8', errors='replace'))
        base = source['final_url']
        page_type, confidence = _classify_page(parser, base, is_entry=depth == 0)
        attrs = json.loads(ev['technical_attributes_json'])
        attrs['page'] = {'title': parser.title.strip(), 'description': parser.description,
                         'canonical': urljoin(base, parser.canonical) if parser.canonical else '',
                         'hreflang': parser.hreflang, 'headings': parser.headings,
                         'language': parser.language, 'indexing_signals': parser.indexing_signals,
                         'page_type': page_type, 'page_type_confidence': confidence}
        attrs['observed_links'] = []
        page_rows.append((base, depth, page_type))
        markup = _AssetMarkup()
        markup.feed(body.decode('utf-8', errors='replace'))
        functional = _FunctionalMarkup()
        functional.feed(body.decode('utf-8', errors='replace'))
        document_targets = discover_documents(body, base, source, ev, asset, depth, attrs)
        for item in markup.observed:
            if time.monotonic_ns() >= executor.state.started + round(executor.limits.run_target * 1_000_000_000):
                partial, stop = True, 'run_target'
                break
            support = dict(ev)
            support['evidence_id'] = ids.next('EVD')
            navigation_links = []
            if item['kind'] == 'CONTENT_BLOCK':
                def clean_lists(lists):
                    return [{'type': node['type'], 'items': [
                        {'text': ' '.join(''.join(entry['text']).split()), 'children': clean_lists(entry['children'])}
                        for entry in node['items']]} for node in lists]
                if item['feature_lists'] or item['ctas']:
                    ctas = []
                    for cta in item['ctas']:
                        observed = {'element': cta['element'], 'label': cta['label']}
                        if cta['destination']:
                            observed['destination'] = urljoin(base, cta['destination'])
                        ctas.append(observed)
                    support['observation_result'] = json.dumps({'text': item['text'],
                        'feature_lists': clean_lists(item['feature_lists']), 'ctas': ctas}, ensure_ascii=False)
                else:
                    support['observation_result'] = item['text']
            else:
                navigation_links = [{'href': link['href'], 'url': urljoin(base, link['href']),
                                     'depth': link['depth'], 'text': ' '.join(''.join(link['text']).split())}
                                    for link in item['links']]
                support['observation_result'] = json.dumps(navigation_links, ensure_ascii=False)
            if item['identifier']:
                support_attrs = json.loads(support['technical_attributes_json'])
                support_attrs['declared_identifier'] = item['identifier']
                support['technical_attributes_json'] = json.dumps(support_attrs, ensure_ascii=False)
            evidence.append(support)
            # An explicit microdata identifier is usable only when its final URL
            # was already observed in this run; never fetch or merge on markup similarity.
            confirmed_url = None
            if item['identifier']:
                try:
                    declared = urljoin(base, item['identifier'])
                    if urlsplit(declared).scheme in {'http', 'https'} and not urlsplit(declared).fragment:
                        key = normalize_url(declared)
                        for known in sources:
                            final = known.get('final_url')
                            if not final or known['observation_status'] != 'OBSERVED' or normalize_url(final) != key:
                                continue
                            if any(e['source_id'] == known['source_id'] and
                                   any(a.get('url') == final and a.get('http_status') == 200
                                       for a in json.loads(e['technical_attributes_json']).get('attempts', []))
                                   for e in evidence):
                                confirmed_url = final
                                break
                except (ValueError, UnicodeError):
                    pass
            component, created = registry.register(normalized_final_url=normalize_url(confirmed_url) if confirmed_url else None,
                asset_type=item['kind'], asset_category='CONTENT' if item['kind'] == 'CONTENT_BLOCK' else 'NAVIGATION',
                confirmed_final_url=bool(confirmed_url), source_type='WEB_PAGE', observation_status='OBSERVED',
                schema_version='1.0', primary_url=confirmed_url)
            refs = json.loads(component['evidence_ids_json'])
            if support['evidence_id'] not in refs:
                refs.append(support['evidence_id'])
            component['evidence_ids_json'] = json.dumps(refs)
            if created:
                assets.append(component)
            occurrences.append({'schema_version': '1.0', 'run_id': run.run_id, 'occurrence_id': ids.next('OCC'),
                'asset_id': component['asset_id'], 'placement_page_url': base, 'page_area': item['area'],
                'component': item['tag'], 'usage_context': item['label'], 'observation_status': 'OBSERVED',
                'evidence_ids_json': json.dumps([support['evidence_id']])})
            relationships.append({'schema_version': '1.0', 'run_id': run.run_id,
                'relationship_id': ids.next('REL'), 'source_object_type': 'ASSET', 'source_object_id': asset['asset_id'],
                'relationship_type': 'CONTAINS', 'target_object_type': 'ASSET', 'target_object_id': component['asset_id'],
                'target_url': '', 'confirmation_status': 'CONFIRMED', 'evidence_ids_json': json.dumps([support['evidence_id']])})
            if item['kind'] == 'NAVIGATION_STRUCTURE':
                for link in navigation_links:
                    target = link['url']
                    if urlsplit(target).scheme not in {'http', 'https'}:
                        continue
                    add_relation(component, support, 'LINKS_TO', target)
        for item in functional.observed:
            if time.monotonic_ns() >= executor.state.started + round(executor.limits.run_target * 1_000_000_000):
                partial, stop = True, 'run_target'
                break
            # D-037/D-094 classify an externally hosted map as a dependency,
            # not as an owned Functional Asset. Preserve its declaration and
            # existing Source/Relationship mapping without requesting it.
            if item['asset_type'] == 'INTERACTIVE_MAP' and item.get('declared_source'):
                try:
                    external_url = urljoin(base, item['declared_source'])
                    parsed = urlsplit(external_url)
                    if parsed.username is not None or parsed.password is not None:
                        continue
                    if parsed.scheme in {'http','https'} and not internal(external_url):
                        ext = external_media_sources.get(external_url)
                        if ext is None:
                            ext = {'schema_version':'1.0','run_id':run.run_id,'source_id':ids.next('SRC'),
                                'source_type':'EXTERNAL_DEPENDENCY','requested_url':external_url,'final_url':None,
                                'normalized_final_url':None,'observation_status':'NOT_INSPECTED'}
                            external_media_sources[external_url] = ext
                            sources.append(ext)
                        attrs.setdefault('functional_component_dependencies', []).append({
                            'element':item['element'], 'attribute':'src', 'declared_url':external_url,
                            'label':item['label']})
                        relationships.append({'schema_version':'1.0','run_id':run.run_id,
                            'relationship_id':ids.next('REL'),'source_object_type':'ASSET',
                            'source_object_id':asset['asset_id'],'relationship_type':'EMBEDS',
                            'target_object_type':'SOURCE','target_object_id':ext['source_id'],
                            'target_url':external_url,'confirmation_status':'CONFIRMED',
                            'evidence_ids_json':json.dumps([ev['evidence_id']])})
                        continue
                except (ValueError, UnicodeError):
                    continue
            functional_data = {'asset_type':item['asset_type'], 'element':item['element'],
                               'label':item['label'], 'controls':item['controls'],
                               'child_labels':item['child_labels']}
            if item.get('declared_source'):
                try:
                    functional_data['declared_source'] = urljoin(base, item['declared_source'])
                except ValueError:
                    pass
            support = dict(ev)
            support['evidence_id'] = ids.next('EVD')
            support['observation_result'] = json.dumps({'functional_component':functional_data}, ensure_ascii=False)
            support_attrs = json.loads(support['technical_attributes_json'])
            support_attrs['functional_component'] = functional_data
            support['technical_attributes_json'] = json.dumps(support_attrs, ensure_ascii=False)
            evidence.append(support)
            component, created = registry.register(normalized_final_url=None,
                asset_type=item['asset_type'], asset_category='FUNCTIONAL', confirmed_final_url=False,
                source_type='WEB_PAGE', observation_status='OBSERVED', schema_version='1.0', primary_url=None)
            refs = json.loads(component['evidence_ids_json'])
            if support['evidence_id'] not in refs:
                refs.append(support['evidence_id'])
            component['evidence_ids_json'] = json.dumps(refs)
            if created:
                assets.append(component)
            occurrences.append({'schema_version':'1.0', 'run_id':run.run_id, 'occurrence_id':ids.next('OCC'),
                'asset_id':component['asset_id'], 'placement_page_url':base, 'page_area':item['area'],
                'component':item['element'], 'usage_context':item['label'], 'observation_status':'OBSERVED',
                'evidence_ids_json':json.dumps([support['evidence_id']])})
            relationships.append({'schema_version':'1.0','run_id':run.run_id,
                'relationship_id':ids.next('REL'),'source_object_type':'ASSET','source_object_id':asset['asset_id'],
                'relationship_type':'CONTAINS','target_object_type':'ASSET','target_object_id':component['asset_id'],
                'target_url':'','confirmation_status':'CONFIRMED','evidence_ids_json':json.dumps([support['evidence_id']])})
            declared_source = functional_data.get('declared_source')
            if declared_source:
                try:
                    parsed = urlsplit(declared_source)
                    if parsed.scheme in {'http','https'} and parsed.username is None and parsed.password is None:
                        external = not internal(declared_source)
                        ext = external_media_sources.get(declared_source) if external else None
                        if external and ext is None:
                            ext = {'schema_version':'1.0','run_id':run.run_id,'source_id':ids.next('SRC'),
                                'source_type':'EXTERNAL_DEPENDENCY','requested_url':declared_source,'final_url':None,
                                'normalized_final_url':None,'observation_status':'NOT_INSPECTED'}
                            external_media_sources[declared_source] = ext
                            sources.append(ext)
                        relationships.append({'schema_version':'1.0','run_id':run.run_id,
                            'relationship_id':ids.next('REL'),'source_object_type':'ASSET','source_object_id':component['asset_id'],
                            'relationship_type':'EMBEDS','target_object_type':'SOURCE',
                            'target_object_id':ext['source_id'] if ext else '', 'target_url':declared_source,
                            'confirmation_status':'CONFIRMED','evidence_ids_json':json.dumps([support['evidence_id']])})
                except (ValueError, UnicodeError):
                    pass
        media = _MediaMarkup()
        media.feed(body.decode('utf-8', errors='replace'))
        media_base = base
        if media.base:
            try:
                candidate_base = urljoin(base, media.base)
                if urlsplit(candidate_base).scheme in {'http', 'https'}:
                    media_base = candidate_base
            except ValueError:
                pass
        for item in media.observed:
            if time.monotonic_ns() >= executor.state.started + round(executor.limits.run_target * 1_000_000_000):
                partial, stop = True, 'run_target'
                break
            resolved = []
            for candidate in item['candidates']:
                try:
                    address = urljoin(media_base, candidate['value'])
                    parsed = urlsplit(address)
                    if parsed.username is not None or parsed.password is not None:
                        continue
                    resolved.append({**candidate, 'url': address})
                except ValueError:
                    continue
            if not resolved:
                continue
            support = dict(ev)
            support['evidence_id'] = ids.next('EVD')
            support['observation_result'] = json.dumps({'media_type': item['kind'], 'component': item['component'],
                'candidates': resolved, 'resource_requested_by_extraction': False,
                'resource_availability_checked_by_extraction': False}, ensure_ascii=False)
            evidence.append(support)
            def local_reference(address):
                parsed = urlsplit(address)
                if parsed.scheme == 'data':
                    return True
                if parsed.scheme == 'blob':
                    return internal(parsed.path)
                return internal(address)

            def external_reference(address):
                parsed = urlsplit(address)
                if parsed.scheme == 'blob':
                    return not local_reference(address)
                return parsed.scheme != 'data' and bool(parsed.hostname) and not local_reference(address)

            external_urls = list(dict.fromkeys(c['url'] for c in resolved if external_reference(c['url'])))
            for address in external_urls:
                external = external_media_sources.get(address)
                if external is None:
                    external = {'schema_version': '1.0', 'run_id': run.run_id, 'source_id': ids.next('SRC'),
                        'source_type': 'EXTERNAL_DEPENDENCY', 'requested_url': address, 'final_url': None,
                        'normalized_final_url': None, 'observation_status': 'NOT_INSPECTED'}
                    external_media_sources[address] = external
                    sources.append(external)
                relationships.append({'schema_version': '1.0', 'run_id': run.run_id,
                    'relationship_id': ids.next('REL'), 'source_object_type': 'ASSET', 'source_object_id': asset['asset_id'],
                    'relationship_type': 'LINKS_TO' if item['component'] == 'a' else 'EMBEDS',
                    'target_object_type': 'SOURCE', 'target_object_id': external['source_id'], 'target_url': address,
                    'confirmation_status': 'CONFIRMED', 'evidence_ids_json': json.dumps([support['evidence_id']])})
            internal_candidates = [c for c in resolved if local_reference(c['url'])]
            if not internal_candidates:
                continue
            preferred = next((c for c in internal_candidates if c['element'] == 'img' and c['attribute'] == 'src'), internal_candidates[0])
            address = preferred['url']
            confirmed_url = None
            try:
                key = normalize_url(address)
                for known in sources:
                    final = known.get('final_url')
                    if not final or known['observation_status'] != 'OBSERVED' or normalize_url(final) != key:
                        continue
                    for attempt in attempts:
                        mime = (attempt.get('headers', {}).get('content-type') or '').split(';', 1)[0].lower()
                        if (attempt.get('url') == final and attempt.get('http_status') == 200 and
                                attempt.get('reason') == 'complete' and mime.startswith(item['kind'].lower()+'/')):
                            confirmed_url = final
                            break
                    if confirmed_url:
                        break
            except (ValueError, UnicodeError):
                pass
            component, created = registry.register(normalized_final_url=normalize_url(confirmed_url) if confirmed_url else None,
                asset_type=item['kind'], asset_category='MEDIA', confirmed_final_url=bool(confirmed_url),
                source_type='MEDIA_RESOURCE', observation_status='OBSERVED', schema_version='1.0',
                primary_url=confirmed_url or address)
            refs = json.loads(component['evidence_ids_json'])
            if support['evidence_id'] not in refs:
                refs.append(support['evidence_id'])
            component['evidence_ids_json'] = json.dumps(refs)
            if created:
                assets.append(component)
            occurrences.append({'schema_version':'1.0','run_id':run.run_id,'occurrence_id':ids.next('OCC'),
                'asset_id':component['asset_id'],'placement_page_url':base,'page_area':item['area'],
                'component':item['component'],'usage_context':item['context'],'observation_status':'OBSERVED',
                'evidence_ids_json':json.dumps([support['evidence_id']])})
            relationships.append({'schema_version':'1.0','run_id':run.run_id,'relationship_id':ids.next('REL'),
                'source_object_type':'ASSET','source_object_id':asset['asset_id'],
                'relationship_type':'LINKS_TO' if item['component']=='a' else 'EMBEDS',
                'target_object_type':'ASSET','target_object_id':component['asset_id'],'target_url':'',
                'confirmation_status':'CONFIRMED','evidence_ids_json':json.dumps([support['evidence_id']])})
        if parser.canonical:
            canonical = urljoin(base, parser.canonical)
            add_relation(asset, ev, 'CANONICAL_OF', canonical)
            try:
                if normalize_url(base) != normalize_url(canonical):
                    canonical_rows.append((base, canonical))
            except (ValueError, UnicodeError):
                pass
        for href, region in parser.links:
            if time.monotonic_ns() >= executor.state.started + round(executor.limits.run_target * 1_000_000_000):
                partial, stop = True, 'run_target'
                break
            target = document_targets.get(href) or urljoin(base, href)
            attrs['observed_links'].append(target)
            if not internal(target):
                continue
            try:
                normalized = normalize_url(target)
            except (ValueError, UnicodeError):
                continue
            add_relation(asset, ev, 'LINKS_TO', target)
            add_candidate(target, base, source['source_id'])
            if normalized in run.candidates and (depth is None or depth < maximum_depth):
                enqueue(normalized, None if depth is None else depth + 1, {'nav': 0, 'main': 1, 'other': 2}[region], False)
            elif depth is not None and depth >= maximum_depth and normalized not in visited:
                partial, stop = True, stop or 'crawl_depth'

        technical_markup = _TechnicalResourceMarkup()
        technical_markup.feed(body.decode('utf-8', errors='replace'))
        technical_base = base
        if technical_markup.base:
            try:
                candidate_base = urljoin(base, technical_markup.base)
                if urlsplit(candidate_base).scheme in {'http','https'}:
                    technical_base = candidate_base
            except ValueError:
                pass
        if technical_markup.unclassified:
            attrs.setdefault('unclassified_technical_resources', [])
        for item in technical_markup.unclassified:
            try:
                target = urljoin(technical_base, item['value'])
                parsed = urlsplit(target)
                if (parsed.scheme not in {'http','https'} or parsed.username is not None
                        or parsed.password is not None):
                    continue
                attrs['unclassified_technical_resources'].append({**item, 'resolved_url':target})
            except (ValueError, UnicodeError):
                attrs['unclassified_technical_resources'].append(item)
        for item in technical_markup.items:
            if time.monotonic_ns() >= executor.state.started + round(executor.limits.run_target * 1_000_000_000):
                partial, stop = True, 'run_target'
                break
            try:
                target = urljoin(technical_base, item['value'])
                parsed = urlsplit(target)
                if (parsed.scheme not in {'http','https'} or parsed.username is not None
                        or parsed.password is not None):
                    attrs.setdefault('unclassified_technical_resources', []).append({**item, 'resolved_url':target})
                    continue
                normalized_requested = normalize_url(target)
            except (ValueError, UnicodeError):
                attrs.setdefault('unclassified_technical_resources', []).append(item)
                continue

            kind = item['resource_kind']
            identity_key = (normalized_requested, kind)
            resource_source = technical_sources_by_key.get(identity_key)
            if resource_source is None:
                source_type = 'TECHNICAL_RESOURCE' if internal(target) else 'EXTERNAL_DEPENDENCY'
                resource_source = {'schema_version':'1.0','run_id':run.run_id,'source_id':ids.next('SRC'),
                    'source_type':source_type,'requested_url':target,'final_url':None,
                    'normalized_final_url':None,'observation_status':'NOT_INSPECTED'}
                technical_sources_by_key[identity_key] = resource_source
                sources.append(resource_source)

            declaration = {**item, 'resolved_url':target}
            resource_evidence = dict(ev)
            resource_evidence['evidence_id'] = ids.next('EVD')
            resource_evidence['observation_result'] = json.dumps(
                {'technical_resource_declaration':declaration}, ensure_ascii=False)
            resource_attributes = dict(attrs)
            resource_attributes['technical_resource_declaration'] = declaration
            resource_evidence['technical_attributes_json'] = json.dumps(resource_attributes, ensure_ascii=False)
            evidence.append(resource_evidence)
            asset_refs = json.loads(asset['evidence_ids_json'])
            if resource_evidence['evidence_id'] not in asset_refs:
                asset_refs.append(resource_evidence['evidence_id'])
                asset['evidence_ids_json'] = json.dumps(asset_refs)

            relation_type = 'USES' if resource_source['source_type'] == 'TECHNICAL_RESOURCE' else 'EMBEDS'
            relation_key = (asset['asset_id'], resource_source['source_id'], relation_type)
            relation = technical_relationships_by_key.get(relation_key)
            if relation is None:
                relation = {'schema_version':'1.0','run_id':run.run_id,'relationship_id':ids.next('REL'),
                    'source_object_type':'ASSET','source_object_id':asset['asset_id'],
                    'relationship_type':relation_type,'target_object_type':'SOURCE',
                    'target_object_id':resource_source['source_id'],'target_url':target,
                    'confirmation_status':'CONFIRMED','evidence_ids_json':'[]'}
                technical_relationships_by_key[relation_key] = relation
                relationships.append(relation)
            relation_refs = json.loads(relation['evidence_ids_json'])
            relation_refs.append(resource_evidence['evidence_id'])
            relation['evidence_ids_json'] = json.dumps(relation_refs)
        ev['technical_attributes_json'] = json.dumps(attrs, ensure_ascii=False)

    if assets and homepage['evidence']:
        source = homepage['source']
        visited.add(source['normalized_final_url'])
        pages = 1
        general_pages = 1
        attempt = next(a for a in reversed(homepage['http']['attempts']) if a.get('url') == source['final_url'] and a.get('http_status') == 200)
        inspect(assets[0], homepage['evidence'], source, Path(attempt['body']).read_bytes(), 0)
        for item in homepage['http']['attempts']:
            run.url_states[normalize_url(item['url'])] = {'state': 'COMPLETED', 'source': source,
                                                           'asset': assets[0], 'evidence': homepage['evidence']}
    for url, provenance in list(run.candidates.items()):
        if _document_candidate(url):
            document_candidates.add(url)
        only_sitemap = bool(provenance) and all(source_types.get(x) == 'SITEMAP' for x in provenance)
        if url not in visited and url not in scheduled:
            enqueue(url, None if only_sitemap else 1, 3 if only_sitemap else 2, only_sitemap)

    terminal = {'run_target', 'run_bytes', 'request_limit', 'unsafe_worker_result'}
    deferred = []
    while (queue or deferred) and stop not in terminal:
        if time.monotonic_ns() >= executor.state.started + round(executor.limits.run_target * 1_000_000_000):
            partial, stop = True, 'run_target'
            break
        if not queue:
            for entry in deferred:
                heapq.heappush(queue, entry)
            deferred.clear()
        priority, rank, _, url, depth, sitemap_only = heapq.heappop(queue)
        if url in visited or scheduled.get(url) != (priority, rank) or run.url_states.get(url, {}).get('state') in {'FETCHED', 'COMPLETED', 'TERMINAL_FAILURE'}:
            continue
        if pages >= max_pages:
            partial, stop = True, stop or 'page_budget'
            if url not in document_candidates:
                continue
        pending_sitemap = any(entry[5] and entry[3] not in visited for entry in queue)
        if not sitemap_only and general_pages >= max_pages - sitemap_reserve and pending_sitemap and sitemap_used < sitemap_reserve:
            deferred.append((priority, rank, next(order), url, depth, sitemap_only))
            continue
        if sitemap_only and sitemap_used >= sitemap_reserve and url not in document_candidates:
            partial, stop = True, stop or 'sitemap_page_reserve'
            continue
        if robot is not None and not robot.can_fetch(agent, url):
            is_document = url in document_candidates
            observations.append(('UNKNOWN' if is_document else 'WEB_PAGE', url, 'robots restriction'))
            if not is_document:
                partial = True
            continue
        visited.add(url)
        run.url_states[url] = {'state': 'IN_PROGRESS'}
        prior_executor_partial = executor.state.partial
        result = executor.fetch(run.candidate_urls.get(url, url), redirect_states=run.url_states)
        attempts.extend(result['attempts'])
        reused_key = result.get('reused_key')
        if reused_key is not None:
            previous = run.url_states[reused_key]
            received = [a for a in result['attempts'] if type(a.get('http_status')) is int]
            alias = None
            if received:
                alias, _ = adapter.adapt(result, source_type='DOCUMENT_RESOURCE' if url in document_candidates else 'WEB_PAGE', observation_status='OBSERVED')
                sources.append(alias)
                observations.append(('WEB_PAGE', alias['requested_url'],
                                     result['reason'] + ' HTTP ' + str(received[-1]['http_status']) + ' → ' + reused_key))
            if alias is not None:
                pending_redirects.append({'url': url, 'target': reused_key, 'source_id': alias['source_id'],
                                          'redirects': result['redirects'], 'result': result})
                if result['reason'] == 'redirect_in_progress':
                    run.url_states[url] = {'state': 'IN_PROGRESS', 'dependency': reused_key, 'result': result}
                    if not prior_executor_partial and all(a.get('reason') == 'complete' for a in result['attempts']):
                        executor.state.partial = False
                elif _resolve_redirect_dependencies(pending_redirects, run.url_states):
                    partial, stop = True, stop or 'redirect_reused_failure'
                if (previous.get('asset') or {}).get('asset_type') == 'DOCUMENT':
                    emit_document_placements(url, previous['asset'])
            continue
        if result['reason'] in terminal or result['reason'] in {'byte_limit', 'resource_bytes'}:
            partial, stop = True, result['reason']
        received = [a for a in result['attempts'] if type(a.get('http_status')) is int]
        if not received:
            run.url_states[url] = {'state': 'TERMINAL_FAILURE', 'result': result}
            if _resolve_redirect_dependencies(pending_redirects, run.url_states):
                partial, stop = True, stop or result['reason']
            observations.append(('WEB_PAGE', url, result['reason']))
            continue
        for item in received:
            run.url_states[normalize_url(item['url'])] = {'state': 'FETCHED', 'result': result}
        source, history = adapter.adapt(result, source_type='WEB_PAGE',
            observation_status='OBSERVED' if result['complete'] else 'PARTIALLY_OBSERVED')
        final = next(a for a in reversed(received) if a['url'] == source['final_url'])
        mime = final.get('headers', {}).get('content-type', '').split(';', 1)[0].strip().lower()
        body_path = Path(final['body'])
        with body_path.open('rb') as stream:
            sample = stream.read(512)
        source['source_type'] = _resource_type(mime, sample, source['final_url'])
        doc_format = _document_format(mime, sample)
        if doc_format:
            source['source_type'] = 'DOCUMENT_RESOURCE'
        sources.append(source)
        observations.append((source['source_type'], source['final_url'], 'HTTP ' + str(final['http_status'])))
        if doc_format and final['http_status'] in {200, 206} and source['normalized_final_url']:
            status = 'PARTIALLY_OBSERVED' if final['http_status'] == 206 or not result['complete'] else 'OBSERVED'
            source['observation_status'] = status
            document, created = registry.register(normalized_final_url=source['normalized_final_url'],
                asset_type='DOCUMENT', asset_category='DOCUMENT', confirmed_final_url=True,
                source_type='DOCUMENT_RESOURCE', observation_status=status, schema_version='1.0', primary_url=source['final_url'])
            if created:
                assets.append(document)
            doc_ev = attach_evidence(ids, source, document, history, confirmed=True,
                observed_at=datetime.now(timezone.utc).isoformat(), evidence_type='HTTP_RESPONSE',
                discovery_method='STATIC_HTTP', observation_result='Document HTTP response observed')
            doc_attrs = json.loads(doc_ev['technical_attributes_json'])
            doc_attrs['document'] = {'format':doc_format, 'http_status':final['http_status'],
                'response_complete':bool(result['complete'] and final['http_status'] == 200)}
            if mime:
                doc_attrs['document']['mime'] = mime
            if 'content-length' in final.get('headers', {}):
                doc_attrs['document']['declared_content_length'] = final['headers']['content-length']
            for field in ('retained','delivered','raw'):
                if field in final:
                    doc_attrs['document'][field] = final[field]
            doc_ev['technical_attributes_json'] = json.dumps(doc_attrs, ensure_ascii=False)
            evidence.append(doc_ev)
            for attempt in result['attempts']:
                key = normalize_url(attempt['url'])
                run.url_states[key] = {'state':'COMPLETED', 'result':result, 'source':source,
                                       'asset':document, 'evidence':doc_ev}
                emit_document_placements(key, document)
            if _resolve_redirect_dependencies(pending_redirects, run.url_states):
                partial, stop = True, stop or 'redirect_reused_failure'
            continue
        if pages >= max_pages and source['source_type'] == 'WEB_PAGE':
            run.url_states[url] = {'state':'TERMINAL_FAILURE', 'result':result, 'source':source}
            continue
        if not result['complete'] or final['http_status'] != 200 or source['source_type'] != 'WEB_PAGE' or not source['normalized_final_url']:
            for item in result['attempts']:
                run.url_states[normalize_url(item['url'])] = {'state': 'TERMINAL_FAILURE', 'result': result, 'source': source}
            if _resolve_redirect_dependencies(pending_redirects, run.url_states):
                partial, stop = True, stop or result['reason']
            continue
        asset, created = registry.register(normalized_final_url=source['normalized_final_url'],
            asset_type='WEB_PAGE', asset_category='CONTENT', confirmed_final_url=True, source_type='WEB_PAGE',
            observation_status='OBSERVED', schema_version='1.0', primary_url=source['final_url'])
        if created:
            assets.append(asset)
        ev = attach_evidence(ids, source, asset, history, confirmed=True,
            observed_at=datetime.now(timezone.utc).isoformat(), evidence_type='HTTP_RESPONSE',
            discovery_method='STATIC_HTTP', observation_result='HTML page observed')
        evidence.append(ev)
        pages += 1
        sitemap_used += bool(sitemap_only)
        general_pages += not sitemap_only
        inspect(asset, ev, source, Path(final['body']).read_bytes(), depth)
        for item in result['attempts']:
            run.url_states[normalize_url(item['url'])] = {'state': 'COMPLETED', 'result': result,
                                                          'source': source, 'asset': asset, 'evidence': ev}
        if _resolve_redirect_dependencies(pending_redirects, run.url_states):
            partial, stop = True, stop or 'redirect_reused_failure'
    if _resolve_redirect_dependencies(pending_redirects, run.url_states):
        partial, stop = True, stop or 'redirect_reused_failure'
    if pending_redirects:
        partial, stop = True, stop or 'redirect_in_progress'
        for item in pending_redirects:
            observations.append(('WEB_PAGE', item['url'], 'unresolved redirect → ' + item['target']))
    report = ['', '## Static page inspection', f'HTML pages: {pages}',
              f'Discovered normalized URLs: {len(run.candidates)}',
              f'New URLs found during inspection: {len(run.candidates) - initial_candidates}',
              f'Sitemap-only pages used: {sitemap_used}',
              f'Maximum crawl depth: {maximum_depth}', f'Relationships: {len(relationships)}',
              'Browser-dependent areas: NOT_INSPECTED.', 'Page classifications:']
    report.extend(f'- {url}: {kind}; depth={depth if depth is not None else "unknown"}' for url, depth, kind in page_rows)
    report.append('Canonical duplicate candidates (not merged):')
    report.extend(f'- {source} → {target}' for source, target in canonical_rows)
    return {'relationships': relationships, 'occurrences': occurrences, 'report': report, 'partial': partial, 'stop': stop}
