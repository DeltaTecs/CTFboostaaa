"""Small JSON transport: no proxy inheritance, redirects, or implicit retries."""
import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Redirects are disabled for integration probes")


def local_url(value):
    url = urllib.parse.urlsplit(value)
    try:
        loopback = ipaddress.ip_address(url.hostname or "").is_loopback
    except ValueError:
        loopback = False
    if (url.scheme != "http" or not loopback or url.username or url.password
            or url.path not in ("", "/") or url.query or url.fragment):
        raise ValueError("HexStrike assessment endpoint must be an HTTP loopback IP URL")
    return value.rstrip("/")


def request_json(url, payload=None, headers=None, timeout=30):
    data = None if payload is None else json.dumps(payload).encode()
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=request_headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # Do not include request headers or provider response bodies in logs.
        raise RuntimeError(f"HTTP {error.code} from integration endpoint") from None
