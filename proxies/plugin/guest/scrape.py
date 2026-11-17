"""Scrapes proxy lists inside the scraper microVMs. Module level imports stdlib only."""
import asyncio
import base64
import bisect
import html
import ipaddress
import json
import re
import time
from urllib.parse import urlsplit

_OCTET = r'(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)'
_IP = rf'{_OCTET}(?:\.{_OCTET}){{3}}'
# ip:port. A scheme and credentials before it (socks5://user:pass@1.2.3.4:1080) are matched
# separately by PREFIX_RE, only when the IP follows '/' or '@': trying them at every position
# made this scan 3-4x slower.
COLON_RE = re.compile(rf'(?<![\d.])({_IP})\s*:\s*(\d{{1,5}})(?!\d)')
# Credentials are length-bounded: unbounded [^...]+ backtracks quadratically on long minified JS.
PREFIX_RE = re.compile(r'(?:\b(socks5h?|socks4a?|https?)://)?(?:[^\s:@/<>"\']{1,64}:[^\s:@/<>"\']{1,64}@)?\Z', re.I)
PREFIX_WINDOW = 150
SCHEME_RE = re.compile(r'\b(socks5h?|socks4a?|https?)://\Z', re.I)
# ip and port in neighbouring table cells (tags were replaced by tabs)
CELL_RE = re.compile(rf'(?<![\d.])({_IP})[ \r\n]*\t[\t \r\n]*(\d{{1,5}})[ \r\n]*(?:\t|$)')
# base64 of "255.255.255.255:65535" is 28 chars; longer blobs are never an IP.
B64_RE = re.compile(r'["\']([A-Za-z0-9+/]{8,40}={0,2})["\']')
TAG_RE = re.compile(r'<[^>]*>')
SCRIPT_RE = re.compile(r'<(style)\b[^>]*>.*?</\1>', re.I | re.S)
HINTS = (('socks5', 'socks5'), ('socks4', 'socks4'), ('socks', 'socks5'), ('https', 'http'), ('http', 'http'), ('ssl', 'http'))
IP_KEYS = ('ip', 'ip_address', 'ipaddress', 'ipAddress', 'host', 'addr', 'address', 'server', 'proxy_ip')
PORT_KEYS = ('port', 'proxy_port', 'portnum')
PROTO_KEYS = ('protocol', 'protocols', 'type', 'proxy_type', 'scheme')


def url_hint(url):
    parts = urlsplit(url.lower())
    lowered = parts.path.rsplit('/', 1)[-1] + ' ' + parts.query
    for needle, hint in HINTS:
        if needle in lowered:
            return hint
    return None


def normalize_hint(value):
    if not value:
        return None
    value = str(value).lower()
    for needle, hint in HINTS:
        if needle in value:
            return hint
    return None


_IPV4_RE = re.compile(_IP)
# IPv4 ranges that are never a public proxy (ipaddress' non-global ranges plus multicast), as
# sorted [start, end] integers. Checking these directly is ~10x faster than ipaddress, which
# was most of the scraper's CPU on big lists.
_BLOCKED = sorted(
    (int(ipaddress.IPv4Network(net).network_address), int(ipaddress.IPv4Network(net).broadcast_address))
    for net in ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16',
                '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24', '192.168.0.0/16', '198.18.0.0/15',
                '198.51.100.0/24', '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4'))
_BLOCKED_STARTS = [start for start, _ in _BLOCKED]


def _global_v4(ip):
    a, b, c, d = ip.split('.')
    value = (int(a) << 24) | (int(b) << 16) | (int(c) << 8) | int(d)
    i = bisect.bisect_right(_BLOCKED_STARTS, value) - 1
    return i < 0 or value > _BLOCKED[i][1]


def valid(ip, port):
    try:
        port = int(port)
        if not 0 < port < 65536:
            return False
        if _IPV4_RE.fullmatch(ip):
            return _global_v4(ip)
        return ipaddress.ip_address(ip).is_global   # IPv6 from JSON APIs
    except (TypeError, ValueError):
        return False


def _walk_json(node, out):
    if isinstance(node, dict):
        lowered = {str(k).lower(): v for k, v in node.items()}
        ip = next((lowered[k.lower()] for k in IP_KEYS if k.lower() in lowered), None)
        port = next((lowered[k] for k in PORT_KEYS if k in lowered), None)
        if isinstance(ip, str) and port is not None:
            proto = next((lowered[k] for k in PROTO_KEYS if k in lowered), None)
            if isinstance(proto, list):
                proto = proto[0] if proto else None
            out.append((ip.strip(), str(port).strip(), normalize_hint(proto)))
        for value in node.values():
            if isinstance(value, (dict, list)):
                _walk_json(value, out)
    elif isinstance(node, list):
        for value in node:
            if isinstance(value, (dict, list)):
                _walk_json(value, out)


def _decode_b64(match):
    token = match.group(1)
    try:
        decoded = base64.b64decode(token + '=' * (-len(token) % 4), validate=True).decode('ascii')
    except Exception:
        return match.group(0)
    if re.fullmatch(rf'\s*{_IP}(?::\d{{1,5}})?\s*', decoded):
        return decoded
    return match.group(0)


def extract_proxies(text, url=''):
    """Return a sorted list of [ip, port, hint] found in any common proxy-list format."""
    default_hint = url_hint(url)
    found = {}

    def add(ip, port, hint):
        if valid(ip, port):
            key = (ip, int(port))
            if key not in found or (hint and not found[key]):
                found[key] = hint

    stripped = text.lstrip()
    if stripped[:1] in ('{', '['):
        try:
            items = []
            _walk_json(json.loads(text), items)
            for ip, port, hint in items:
                add(ip, port, hint or default_hint)
        except ValueError:
            pass

    text = B64_RE.sub(_decode_b64, text)
    for match in COLON_RE.finditer(text):
        start = match.start()
        scheme = None
        before = text[start - 1] if start else ''
        if before == '/':
            prefix = SCHEME_RE.search(text, max(0, start - 10), start)
            scheme = prefix and prefix.group(1)
        elif before == '@':
            prefix = PREFIX_RE.search(text, max(0, start - PREFIX_WINDOW), start)
            scheme = prefix and prefix.group(1)
        add(match.group(1), match.group(2), normalize_hint(scheme) or default_hint)
    if '<' in text:
        cells = html.unescape(TAG_RE.sub('\t', SCRIPT_RE.sub(' ', text)))
        for ip, port in CELL_RE.findall(cells):
            add(ip, port, default_hint)
    return sorted([ip, port, hint] for (ip, port), hint in found.items())


async def _fetch_all(urls, timeout, max_bytes):
    from curl_cffi.requests import AsyncSession

    async def one(session, url):
        started = time.monotonic()
        result = {'url': url, 'ok': False, 'status': None, 'error': None, 'proxies': []}
        try:
            response = await session.get(url, timeout=timeout, allow_redirects=True)
            result['status'] = response.status_code
            body = response.content[:max_bytes].decode(response.encoding or 'utf-8', errors='replace')
            if response.status_code >= 400:
                result['error'] = f'HTTP {response.status_code}'
            else:
                result['ok'] = True
            result['proxies'] = extract_proxies(body, url)
        except Exception as error:
            result['error'] = f'{error.__class__.__name__}: {error}'[:500]
        result['ms'] = int((time.monotonic() - started) * 1000)
        return result

    async with AsyncSession(impersonate='chrome', verify=False, max_clients=len(urls) or 1) as session:
        return await asyncio.gather(*(one(session, url) for url in urls))


def scrape_batch(urls, timeout=30, max_bytes=30_000_000):
    """Fetch every URL concurrently and extract proxies. Called by the executor."""
    return asyncio.run(_fetch_all(list(urls), timeout, max_bytes))
