"""
HTTP requests shared by the venue clients.

get_json() reads the public catalogs, retrying on network errors.
send_json() carries the signed trading calls. It keeps connections open
between calls, since a new TLS connection costs several round trips that
an order racing a stale quote cannot spare, and it never retries, since
sending an order twice would trade twice.
"""

import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from common.paths import NAME

USER_AGENT = f"{NAME} research (github.com/ryubears/{NAME})"
SEND_TIMEOUT = 10       # Seconds a trading call may take before its outcome is unknown.
IDLE_SECONDS = 20       # A kept connection idle longer than this is closed rather than reused, before the server closes it under us.


def get_json(url, params=None, retries=3):
    """
    GET a URL and parse the JSON body. Retries a few times on network errors.
    """
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


class RequestFailed(Exception):
    """
    A request the server answered with an error status, with the status and the body it sent.
    """

    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class Connections:
    """
    Open HTTPS connections per host, handed out one caller at a time and
    kept for the next call while they are fresh. Safe to use from threads.
    """

    def __init__(self, idle_seconds=IDLE_SECONDS):
        self.idle_seconds = idle_seconds
        self.idle = {}          # host maps to [(connection, monotonic time it was last used)].
        self.lock = threading.Lock()

    def take(self, host, timeout):
        with self.lock:
            idle = self.idle.setdefault(host, [])
            while idle:
                conn, used = idle.pop()
                if time.monotonic() - used < self.idle_seconds:
                    conn.timeout = timeout
                    if conn.sock is not None:
                        conn.sock.settimeout(timeout)
                    return conn
                conn.close()
        return http.client.HTTPSConnection(host, timeout=timeout)

    def give_back(self, host, conn):
        with self.lock:
            self.idle.setdefault(host, []).append((conn, time.monotonic()))


CONNECTIONS = Connections()


def send_json(method, url, headers=None, body=None, timeout=SEND_TIMEOUT):
    """
    Send one request with an optional JSON body over a kept connection and
    parse the JSON answer. Raises RequestFailed when the server answers with
    an error status, and the connection's own error when there is no answer,
    in which case a request that changes something may or may not have.
    """
    parts = urllib.parse.urlsplit(url)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    data = json.dumps(body).encode() if body is not None else None
    sent_headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
    if data is not None:
        sent_headers["Content-Type"] = "application/json"
    conn = CONNECTIONS.take(parts.netloc, timeout)
    try:
        conn.request(method, path, body=data, headers=sent_headers)
        resp = conn.getresponse()
        text = resp.read().decode()
    except BaseException:
        conn.close()
        raise
    if resp.will_close:
        conn.close()
    else:
        CONNECTIONS.give_back(parts.netloc, conn)
    if resp.status >= 400:
        raise RequestFailed(resp.status, text)
    return json.loads(text) if text.strip() else {}
