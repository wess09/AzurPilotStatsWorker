import hashlib
import logging
import time
import threading
import re
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse, urljoin

import httpx

log = logging.getLogger(__name__)

# 123 Pan drops connections under load. Retry the same original in place so a
# single timeout cannot fail a whole group and re-download every image.
TRANSIENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteError,
             httpx.RemoteProtocolError, httpx.ProxyError)


class Retryable(Exception):
    """Transient cloud response; the same original download is attempted again."""

    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


def retry_after(response):
    value = response.headers.get('Retry-After', '')
    try:
        return max(0, float(value))
    except ValueError:
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return 10


class DownloadGate:
    def init_gate(self):
        self.gate_lock = threading.Lock()
        self.gate_until = 0.0
        self.on_throttle = None

    def wait_gate(self):
        while True:
            with self.gate_lock:
                delay = self.gate_until - time.monotonic()
            if delay <= 0:
                return
            time.sleep(min(delay, 1))

    def check_response(self, response):
        if response.status_code == 429:
            delay = max(1, retry_after(response))
            with self.gate_lock:
                self.gate_until = max(self.gate_until, time.monotonic() + delay)
            if self.on_throttle:
                self.on_throttle(delay)
            raise Retryable('cloud download HTTP 429', delay)


def retrying(operation, attempts=3):
    delay = 0.25
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except TRANSIENT + (Retryable,) as exc:
            if attempt == attempts:
                raise
            # Signed download URLs must never reach the logs, so log the type.
            log.warning('download retry attempt=%s error=%s', attempt, type(exc).__name__)
            time.sleep(max(delay, getattr(exc, 'retry_after', 0)))
            delay *= 2


class Statistics:
    def __init__(self, config):
        self.template_public_key = getattr(config, "template_public_key", "")
        self.client = httpx.Client(base_url=config.c_url, headers={"Authorization": "Bearer " + config.c_token}, timeout=60)

    def post(self, path, data):
        r = self.client.post(path, json=data)
        r.raise_for_status()
        return r.json()

    def revision(self, sha):
        r = self.client.get("/internal/v1/statistics/results/" + sha)
        if r.status_code == 404:
            return 0
        r.raise_for_status()
        return int(r.json()["revision"])

    def release(self, id_, base_revision=0):
        # Signed deltas are optional during a rolling upgrade. A missing base or
        # an active-revision race falls back to the immutable full package.
        if self.template_public_key and base_revision:
            r = self.client.get("/api/v3/get/templates/delta", params={"from": base_revision})
            if r.status_code == 200:
                if len(r.content) > 32 * 1024**2:
                    raise ValueError("Template package exceeds size limit")
                if r.json().get("revision") == id_:
                    return self._release_response(r, id_)
            elif r.status_code not in (400, 404, 503):
                r.raise_for_status()
        r = self.client.get(f"/internal/v1/template-releases/{id_}", headers={"Accept": "application/vnd.azurpilot.template-bundle+json;version=1"})
        return self._release_response(r, id_)

    def _release_response(self, r, id_):
        r.raise_for_status()
        if len(r.content) > 32 * 1024**2 or hashlib.sha256(r.content).hexdigest() != r.headers.get("X-Content-SHA256"):
            raise ValueError("Template package checksum/size mismatch")
        if self.template_public_key:
            from .template_package import verify_signature
            verify_signature(r.content, id_, r.headers.get("X-Content-Signature", ""), self.template_public_key)
        return r.json()


class Pan(DownloadGate):
    def __init__(self, config):
        self.config = config
        self.client = httpx.Client(timeout=120, follow_redirects=False)
        self.token = None
        self.expires = 0
        self.last_download = 0
        self.control_lock = threading.RLock()
        self.init_gate()

    def access(self):
        with self.control_lock:
            return self._access()

    def _access(self):
        if self.token and self.expires > time.time():
            return self.token
        r = self.client.post(self.config.pan_base + "/api/v1/access_token", headers={"Platform": "open_platform"}, json={"clientID": self.config.pan_id, "clientSecret": self.config.pan_secret})
        r.raise_for_status()
        data = r.json()
        if data["code"] != 0:
            raise RuntimeError("123Pan authentication failed: " + str(data.get("message")))
        self.token = data["data"]["accessToken"]
        # Conservative cache, never acquire a token per screenshot.
        self.expires = time.time() + 1800
        return self.token

    def download(self, file, path: Path):
        with self.control_lock:
            url = self.download_url(file)
        retrying(lambda: self._download(url, file, path))

    def _download(self, url, file, path: Path):
        self.wait_gate()
        digest = hashlib.sha256()
        size = 0
        try:
            # Control credentials never accompany the CDN request.
            with self.client.stream("GET", url, follow_redirects=True) as response, path.open("wb") as dst:
                self.check_response(response)
                if response.status_code in (429, 500, 502, 503, 504):
                    raise Retryable(f"download HTTP {response.status_code}")
                response.raise_for_status()
                for block in response.iter_bytes(1 << 20):
                    size += len(block)
                    if size > file["size"] or size > self.config.file_bytes:
                        raise ValueError("Downloaded file exceeds expected size")
                    digest.update(block)
                    dst.write(block)
            if size != file["size"] or digest.hexdigest() != file["sha256"]:
                raise ValueError("Downloaded file hash/size mismatch")
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def download_url(self, file):
        wait = 0.25 - (time.monotonic() - self.last_download)
        if wait > 0:
            time.sleep(wait)
        self.last_download = time.monotonic()
        r = self.client.get(self.config.pan_base + "/api/v1/file/download_info", headers={"Platform": "open_platform", "Authorization": "Bearer " + self.access()}, params={"fileId": file["file_id"]})
        if r.status_code == 401:
            self.token = None
        r.raise_for_status()
        data = r.json()
        if data["code"] == 401:
            self.token = None
        if data["code"] != 0:
            raise RuntimeError("123Pan download: " + str(data.get("message")))
        url = data["data"]["downloadUrl"]
        if urlparse(url).scheme != "https":
            raise ValueError("123Pan returned a non-HTTPS download URL")
        return url


class WebDAV(DownloadGate):
    """A stores immutable originals as <sha256>.png in this WebDAV directory."""

    def __init__(self, config):
        self.config = config
        self.base_url = config.webdav_url.rstrip("/") + "/"
        self.origin = httpx.URL(self.base_url)
        self.auth = httpx.BasicAuth(config.webdav_username, config.webdav_password)
        self.client = httpx.Client(timeout=httpx.Timeout(60, connect=10), follow_redirects=False,
                                   limits=httpx.Limits(max_connections=256, max_keepalive_connections=64))
        self.init_gate()

    def download(self, file, path: Path):
        if not re.fullmatch(r"[0-9a-f]{64}", file["sha256"]):
            raise ValueError("Invalid original hash")
        retrying(lambda: self._download(self.base_url + file["sha256"] + ".png", file, path))

    def _download(self, url, file, path: Path):
        self.wait_gate()
        digest = hashlib.sha256()
        size = 0
        try:
            for _ in range(6):
                target = httpx.URL(url)
                if target.scheme != "https" or target.userinfo:
                    raise ValueError("WebDAV redirect must use HTTPS without URL credentials")
                same_origin = (target.scheme, target.host, target.port) == (self.origin.scheme, self.origin.host, self.origin.port)
                with self.client.stream("GET", target, auth=self.auth if same_origin else None) as response:
                    self.check_response(response)
                    if response.is_redirect:
                        location = response.headers.get("Location")
                        if not location:
                            raise ValueError("WebDAV redirect has no location")
                        url = urljoin(str(target), location)
                        continue
                    if response.status_code in (429, 502, 503, 504):
                        raise Retryable(f"WebDAV download HTTP {response.status_code}")
                    if response.status_code != 200:
                        # Signed CDN query strings and account credentials must
                        # never become part of a persisted job error/traceback.
                        raise RuntimeError(f"WebDAV download HTTP {response.status_code}")
                    with path.open("wb") as dst:
                        for block in response.iter_bytes(1 << 20):
                            size += len(block)
                            if size > file["size"] or size > self.config.file_bytes:
                                raise ValueError("Downloaded file exceeds expected size")
                            digest.update(block)
                            dst.write(block)
                    if size != file["size"] or digest.hexdigest() != file["sha256"]:
                        raise ValueError("Downloaded file hash/size mismatch")
                    return
            raise ValueError("Too many WebDAV redirects")
        except BaseException:
            path.unlink(missing_ok=True)
            raise
