"""Tests proxies inside the tester microVMs. Module level imports stdlib only."""
import asyncio
import json
import random
import re
import time

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36',
      'Accept': '*/*'}
# Headers a proxy adds that reveal a proxy is in use.
PROXY_HEADERS = {'via', 'x-forwarded-for', 'forwarded', 'x-real-ip', 'proxy-connection', 'x-proxy-id',
                 'client-ip', 'x-client-ip', 'forwarded-for', 'x-forwarded', 'x-originating-ip',
                 'x-proxy-connection', 'proxy-agent', 'x-bluecoat-via', 'x-forwarded-host', 'x-forwarded-server'}
AZENV_RE = re.compile(r'^\s*([A-Z][A-Z0-9_]+)\s*=\s*(.*?)\s*$', re.M)
IP_RE = re.compile(r'\d{1,3}(?:\.\d{1,3}){3}')
MAX_BODY = 65536
# ENETUNREACH: our own network is failing, not the proxy. (EHOSTUNREACH is common for dead proxies.)
NET_ERRNOS = {101}


def parse_judge(text):
    """Return (remote_ip_field, {lower header: value}) or None if this isn't a judge response."""
    try:
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get('headers'), dict) and data.get('origin'):
            return str(data['origin']), {k.lower(): str(v) for k, v in data['headers'].items()}
    except ValueError:
        pass
    pairs = dict(AZENV_RE.findall(re.sub(r'<[^>]+>', '\n', text)))
    if pairs.get('REMOTE_ADDR'):
        headers = {k[5:].lower().replace('_', '-'): v for k, v in pairs.items() if k.startswith('HTTP_')}
        return pairs['REMOTE_ADDR'], headers
    return None


def classify(origin, headers, real_ip):
    blob = origin + ' ' + ' '.join(headers.values())
    if real_ip and real_ip in blob:
        return 'transparent'
    if ',' in origin or PROXY_HEADERS.intersection(headers):
        return 'anonymous'
    return 'elite'


def exit_ip_of(origin):
    ips = IP_RE.findall(origin)
    return ips[-1] if ips else None


def _cache_dns():
    """The microVM resolver is slow (~0.5s); judge hosts are the only names looked up, so cache them."""
    import socket
    if getattr(socket.getaddrinfo, '_cached', False):
        return
    original = socket.getaddrinfo
    cache = {}

    def getaddrinfo(*args, **kwargs):
        key = (args, tuple(sorted(kwargs.items())))
        if key not in cache:
            cache[key] = original(*args, **kwargs)
        return cache[key]

    getaddrinfo._cached = True
    socket.getaddrinfo = getaddrinfo


def _verified_context():
    """A certificate-checking SSL context, with certifi's CAs when installed (curl-cffi brings it)."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


async def _read(response):
    return (await response.content.read(MAX_BODY)).decode('utf-8', errors='replace')


def is_forged_certificate(error):
    """Whether an HTTPS request through a proxy failed on the certificate: the proxy (or
    something behind it) intercepts TLS. Other TLS failures (a closed tunnel, a timeout) are
    just a proxy that can't do HTTPS."""
    import ssl
    import aiohttp
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (aiohttp.ClientConnectorCertificateError, ssl.SSLCertVerificationError)):
            return True
        if 'CERTIFICATE_VERIFY_FAILED' in str(error):
            return True
        error = error.__cause__ or error.__context__
    return False


async def _test_all(items, judges, https_urls, timeout, concurrency, rate):
    import aiohttp
    from aiohttp_socks import ProxyConnector

    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    except Exception:
        pass
    _cache_dns()
    from concurrent.futures import ThreadPoolExecutor
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(32))

    client_timeout = aiohttp.ClientTimeout(total=timeout, sock_connect=min(timeout, 6))
    # The HTTPS check verifies the site's certificate: a proxy that intercepts TLS (its own
    # self-signed or unknown CA) breaks every real client, so it doesn't count as HTTPS.
    https_ssl = _verified_context()

    async def direct_ip():
        """Our own egress IP via a judge, or None if no judge is reachable without a proxy."""
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as direct:
            for judge in judges:
                try:
                    async with direct.get(judge, headers=UA) as response:
                        parsed = parse_judge(await _read(response))
                        if parsed:
                            return exit_ip_of(parsed[0])
                except Exception:
                    continue
        return None

    # Our own egress IP, to spot transparent proxies.
    real_ip = await direct_ip()

    async def through(session, proxy_url):
        judge = random.choice(judges)
        started = time.monotonic()
        async with session.get(judge, headers=UA, proxy=proxy_url, allow_redirects=False) as response:
            text = await _read(response)
        parsed = parse_judge(text)
        if not parsed:
            raise ValueError('not a judge response')
        latency = int((time.monotonic() - started) * 1000)
        https, https_latency, intercepts = False, None, False
        started = time.monotonic()
        try:
            async with session.get(random.choice(https_urls), headers=UA, proxy=proxy_url,
                                   allow_redirects=False, ssl=https_ssl) as response:
                await _read(response)
                https = response.status < 400
                https_latency = int((time.monotonic() - started) * 1000)
        except Exception as error:
            intercepts = is_forged_certificate(error)
        origin, headers = parsed
        return {'latency_ms': latency, 'https': https, 'https_latency_ms': https_latency if https else None,
                'intercepts': intercepts, 'exit_ip': exit_ip_of(origin),
                'anonymity': classify(origin, headers, real_ip)}

    http_session = aiohttp.ClientSession(
        timeout=client_timeout,
        connector=aiohttp.TCPConnector(limit=0, force_close=True, ssl=False))

    async def try_http(ip, port):
        return await through(http_session, f'http://{ip}:{port}')

    async def try_socks(ip, port, version):
        connector = ProxyConnector.from_url(f'{version}://{ip}:{port}', rdns=version == 'socks5', ssl=False, limit=0)
        async with aiohttp.ClientSession(timeout=client_timeout, connector=connector) as session:
            return await through(session, None)

    sem = asyncio.Semaphore(concurrency)
    pacer = _Pacer(rate)
    stats = {'connect_ok': 0, 'connect_fail': 0, 'net_errors': 0}

    async def port_open(ip, port):
        """One plain TCP connect. Most dead proxies stop here, costing a single connection."""
        await pacer.wait()
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), min(timeout, 6))
            writer.close()
            stats['connect_ok'] += 1
            return True
        except OSError as error:
            if error.errno in NET_ERRNOS:
                stats['net_errors'] += 1
            stats['connect_fail'] += 1
            return False
        except Exception:
            stats['connect_fail'] += 1
            return False

    async def check(item):
        pid, ip, port, hint, known = item
        protocols = [known] if known else ['http', 'socks5', 'socks4']
        preference = [p for p in [known, hint, 'http', 'socks5', 'socks4'] if p in protocols]
        async with sem:
            if not await port_open(ip, port):
                return {'id': pid, 'ip': ip, 'ok': False}
            tasks = {p: asyncio.ensure_future(try_http(ip, port) if p == 'http' else try_socks(ip, port, p))
                     for p in protocols}
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        for protocol in dict.fromkeys(preference):
            task = tasks[protocol]
            if not task.exception():
                return {'id': pid, 'ip': ip, 'ok': True, 'protocol': protocol, **task.result()}
        return {'id': pid, 'ip': ip, 'ok': False}

    canary = {'checks': 0, 'failures': [], 'done': False}

    async def watch_network():
        """Direct connections to well-known hosts every few seconds while testing.

        Dead is final, so a batch tested while our own uplink was blocked (the ISP rejects or
        drops new connections for a while) must be thrown away, not saved.
        """
        targets = (('1.1.1.1', 443), ('8.8.8.8', 53))
        while not canary['done']:
            for host, port in targets:
                canary['checks'] += 1
                try:
                    _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
                    writer.close()
                except Exception as error:
                    canary['failures'].append(f'{host}:{port} {error.__class__.__name__}')
            await asyncio.sleep(3)

    if real_ip is None:
        await http_session.close()
        return {'real_ip': None, 'inconclusive': 'no judge reachable directly', 'stats': stats, 'results': []}
    watcher = asyncio.ensure_future(watch_network())
    try:
        results = await asyncio.gather(*(check(item) for item in items), return_exceptions=True)
    finally:
        canary['done'] = True
        await http_session.close()
        await asyncio.gather(watcher, return_exceptions=True)
    stats['canary_checks'] = canary['checks']
    attempted = stats['connect_ok'] + stats['connect_fail']
    problem = None
    if canary['failures']:
        problem = f"direct canary connections failed during the batch: {', '.join(canary['failures'][:3])}"
    elif attempted and stats['net_errors'] > max(5, attempted * 0.05):
        problem = f"{stats['net_errors']} of {attempted} connects failed with network unreachable"
    elif await direct_ip() is None:
        problem = 'judges stopped answering directly during the batch'
    if problem:
        return {'real_ip': real_ip, 'stats': stats, 'results': [], 'inconclusive': problem}
    out = []
    for item, result in zip(items, results):
        out.append(result if isinstance(result, dict) else {'id': item[0], 'ip': item[1], 'ok': False})
    return {'real_ip': real_ip, 'stats': stats, 'results': out}


class _Pacer:
    """Spaces out new connections to `rate` per second, so NAT/conntrack tables are not flooded."""

    def __init__(self, rate):
        self.interval = 1.0 / rate if rate and rate > 0 else 0.0
        self.next_at = 0.0
        self.lock = asyncio.Lock()

    async def wait(self):
        if not self.interval:
            return
        async with self.lock:
            now = time.monotonic()
            delay = self.next_at - now
            self.next_at = max(now, self.next_at) + self.interval
        if delay > 0:
            await asyncio.sleep(delay)


def test_batch(items, judges, https_urls, timeout=10, concurrency=60, rate=40):
    """items: [[id, ip, port, hint, known_protocol], ...]. Called by the executor.

    https_urls: a list of HTTPS URLs (or one URL); each check uses one at random.
    rate: new proxy connections per second in this microVM.
    """
    urls = [https_urls] if isinstance(https_urls, str) else [u for u in https_urls if u]
    return asyncio.run(_test_all([tuple(i) for i in items], list(judges), urls, timeout, concurrency, rate))
