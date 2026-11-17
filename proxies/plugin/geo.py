"""Country of an IP from the free DB-IP Lite country database (CC BY 4.0, https://db-ip.com).

Includes a small reader for the MaxMind DB format, so the plugin needs no packages.
"""
import gzip
import ipaddress
import os
import struct
import threading
import time
import urllib.request
from datetime import date

URL = 'https://download.db-ip.com/free/dbip-country-lite-{:%Y-%m}.mmdb.gz'
MAX_AGE = 32 * 86400
USER_AGENT = "Mozilla/5.0 (compatible; scraperack-proxies-plugin)"
METADATA_MARKER = b'\xab\xcd\xefMaxMind.com'


class Reader:
    """Read-only MaxMind DB (.mmdb) lookups."""

    def __init__(self, data):
        self.buf = data
        start = data.rfind(METADATA_MARKER)
        if start < 0:
            raise ValueError('not a MaxMind DB file')
        start += len(METADATA_MARKER)
        meta, _ = self._decode(start, start)
        self.node_count = meta['node_count']
        self.record_size = meta['record_size']
        if self.record_size not in (24, 28, 32):
            raise ValueError(f'unsupported record size {self.record_size}')
        self.ip_version = meta['ip_version']
        self.node_bytes = self.record_size // 4
        self.tree_size = self.node_bytes * self.node_count
        self.data_start = self.tree_size + 16
        self.ipv4_start = 0
        if self.ip_version == 6:   # IPv4 addresses live under 96 zero bits
            node = 0
            for _ in range(96):
                if node >= self.node_count:
                    break
                node = self._record(node, 0)
            self.ipv4_start = node

    def _record(self, node, bit):
        buf, offset = self.buf, node * self.node_bytes
        if self.record_size == 24:
            at = offset + bit * 3
            return int.from_bytes(buf[at:at + 3], 'big')
        if self.record_size == 28:
            middle = buf[offset + 3]
            if bit == 0:
                return ((middle & 0xF0) << 20) | int.from_bytes(buf[offset:offset + 3], 'big')
            return ((middle & 0x0F) << 24) | int.from_bytes(buf[offset + 4:offset + 7], 'big')
        at = offset + bit * 4
        return int.from_bytes(buf[at:at + 4], 'big')

    def get(self, ip):
        address = ipaddress.ip_address(ip)
        if address.version == 6 and self.ip_version == 4:
            return None
        packed, bits = address.packed, address.max_prefixlen
        node = self.ipv4_start if address.version == 4 else 0
        for i in range(bits):
            if node >= self.node_count:
                break
            node = self._record(node, (packed[i >> 3] >> (7 - (i & 7))) & 1)
        if node <= self.node_count:   # == node_count: no data for this address
            return None
        value, _ = self._decode(node - self.node_count + self.tree_size, self.data_start)
        return value

    def _decode(self, offset, base):
        buf = self.buf
        ctrl = buf[offset]
        offset += 1
        kind = ctrl >> 5
        if kind == 1:   # pointer, relative to the data section
            size = (ctrl >> 3) & 3
            low = ctrl & 7
            if size == 0:
                pointer = (low << 8) | buf[offset]
            elif size == 1:
                pointer = ((low << 16) | int.from_bytes(buf[offset:offset + 2], 'big')) + 2048
            elif size == 2:
                pointer = ((low << 24) | int.from_bytes(buf[offset:offset + 3], 'big')) + 526336
            else:
                pointer = int.from_bytes(buf[offset:offset + 4], 'big')
            value, _ = self._decode(base + pointer, base)
            return value, offset + size + 1
        if kind == 0:
            kind = 7 + buf[offset]
            offset += 1
        size = ctrl & 0x1F
        if size >= 29:
            extra = size - 28
            add = int.from_bytes(buf[offset:offset + extra], 'big')
            offset += extra
            size = (29, 285, 65821)[extra - 1] + add
        if kind == 2:
            return buf[offset:offset + size].decode(), offset + size
        if kind == 3:
            return struct.unpack('>d', buf[offset:offset + 8])[0], offset + 8
        if kind == 4:
            return bytes(buf[offset:offset + size]), offset + size
        if kind in (5, 6, 9, 10):
            return int.from_bytes(buf[offset:offset + size], 'big'), offset + size
        if kind == 8:
            return int.from_bytes(buf[offset:offset + size].rjust(4, b'\0'), 'big', signed=True), offset + size
        if kind == 7:
            out = {}
            for _ in range(size):
                key, offset = self._decode(offset, base)
                out[key], offset = self._decode(offset, base)
            return out, offset
        if kind == 11:
            out = []
            for _ in range(size):
                item, offset = self._decode(offset, base)
                out.append(item)
            return out, offset
        if kind == 14:
            return bool(size), offset
        if kind == 15:
            return struct.unpack('>f', buf[offset:offset + 4])[0], offset + 4
        raise ValueError(f'unsupported MaxMind DB type {kind}')


class Geo:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, 'dbip-country-lite.mmdb')
        self.reader = None
        self.lock = threading.Lock()

    def refresh(self):
        """Download a fresh database if missing or older than a month. Safe to call often."""
        stale = not os.path.exists(self.path) or time.time() - os.path.getmtime(self.path) > MAX_AGE
        if stale:
            today = date.today()
            previous = date(today.year - (today.month == 1), (today.month - 2) % 12 + 1, 1)
            for month in (today, previous):
                try:
                    # db-ip.com refuses Python's default User-Agent.
                    request = urllib.request.Request(URL.format(month), headers={"User-Agent": USER_AGENT})
                    with urllib.request.urlopen(request, timeout=120) as response:
                        data = gzip.decompress(response.read())
                    Reader(data)   # refuse a broken download
                    tmp = self.path + '.tmp'
                    with open(tmp, 'wb') as handle:
                        handle.write(data)
                    os.replace(tmp, self.path)
                    with self.lock:
                        self.reader = None
                    break
                except Exception as error:
                    print(f'geo download failed for {month:%Y-%m}: {error}', flush=True)
        with self.lock:
            if self.reader is None and os.path.exists(self.path):
                with open(self.path, 'rb') as handle:
                    self.reader = Reader(handle.read())

    def country(self, ip):
        reader = self.reader
        if not reader:
            return None
        try:
            record = reader.get(ip) or {}
            return (record.get('country') or {}).get('iso_code')
        except Exception:
            return None
