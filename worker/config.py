from dataclasses import dataclass, field
from pathlib import Path
import os


@dataclass(frozen=True)
class Config:
    data: Path
    worker_token: str
    c_url: str
    c_token: str
    worker_id: str
    pan_id: str
    pan_secret: str
    pan_base: str
    alas_root: Path
    file_bytes: int = 10 * 1024**2
    group_bytes: int = 500 * 1024**2
    uploader_url: str = ""
    webdav_url: str = ""
    webdav_username: str = field(default="", repr=False)
    webdav_password: str = field(default="", repr=False)
    recognition_workers: int = 0
    ocr_threads: int = 1
    prefetch_jobs: int = 0
    # Stage pipeline: every stage owns its workers and its queue.
    download_workers: int = 0
    verify_workers: int = 0
    ocr_slots: int = 0
    deliver_workers: int = 0
    download_ahead: int = 0
    ocr_processes: int = 0
    pipeline: str = "staged"
    auto_tune: bool = True
    work_max_bytes: int = 0
    memory_cache_bytes: int = 0
    template_public_key: str = ""

    def __post_init__(self):
        if self.template_public_key:
            import base64
            if len(base64.b64decode(self.template_public_key, validate=True)) != 32:
                raise ValueError("TEMPLATE_PUBLIC_KEY must be base64 Ed25519 public key")
        if not 0 <= self.recognition_workers <= 32 or not 1 <= self.ocr_threads <= 16 or not 0 <= self.prefetch_jobs <= 32:
            raise ValueError('Invalid recognition concurrency')
        if not 0 <= self.download_workers <= 256 or not 0 <= self.verify_workers <= 32:
            raise ValueError('Invalid download/verify concurrency')
        if not 0 <= self.ocr_slots <= 128 or not 0 <= self.deliver_workers <= 32:
            raise ValueError('Invalid ocr/delivery concurrency')
        if not 0 <= self.ocr_processes <= 64:
            raise ValueError('Invalid recognizer process count')
        if not 0 <= self.download_ahead <= 1024:
            raise ValueError('Invalid download-ahead group limit')
        if self.work_max_bytes < 0 or (self.work_max_bytes and self.work_max_bytes < self.group_bytes):
            raise ValueError('Work budget must fit at least one maximum-size group')
        if self.memory_cache_bytes < -1:
            raise ValueError('Invalid memory cache limit')
        if self.pipeline not in ('staged', 'inline'):
            raise ValueError('PIPELINE must be staged or inline')
        if self.file_bytes < 8 or self.group_bytes < self.file_bytes:
            raise ValueError("Invalid file/group limits")
        if self.uploader_url:
            from urllib.parse import urlparse
            url = urlparse(self.uploader_url)
            if url.scheme not in ("ws", "wss") or not url.hostname or url.username or url.password:
                raise ValueError("UPLOADER_WS_URL requires ws:// or wss:// without embedded credentials")
        if self.webdav_url:
            from urllib.parse import urlparse
            url = urlparse(self.webdav_url)
            if (url.scheme != "https" or not url.hostname or url.username or url.password
                    or url.query or url.fragment or not self.webdav_username or not self.webdav_password):
                raise ValueError("WebDAV requires an HTTPS directory URL and separate username/password")

    @classmethod
    def load(cls):
        required = ["WORKER_TOKEN", "STATISTICS_URL", "STATISTICS_TOKEN", "ALAS_ROOT"]
        required += ["WEBDAV_USERNAME", "WEBDAV_PASSWORD"] if os.getenv("WEBDAV_URL") else ["PAN_CLIENT_ID", "PAN_CLIENT_SECRET"]
        missing = [k for k in required if not os.getenv(k)]
        if missing:
            raise ValueError("Missing configuration: " + ", ".join(missing))
        return cls(
            Path(os.getenv("DATA_DIR", "data")).resolve(),
            os.environ["WORKER_TOKEN"],
            os.environ["STATISTICS_URL"].rstrip("/"),
            os.environ["STATISTICS_TOKEN"],
            os.getenv("WORKER_ID", "recognizer-1"),
            os.getenv("PAN_CLIENT_ID", ""),
            os.getenv("PAN_CLIENT_SECRET", ""),
            os.getenv("PAN_API_BASE", "https://open-api.123pan.com"),
            Path(os.environ["ALAS_ROOT"]).resolve(),
            int(os.getenv("MAX_FILE_BYTES", str(10 * 1024**2))),
            int(os.getenv("MAX_GROUP_BYTES", str(500 * 1024**2))),
            os.getenv("UPLOADER_WS_URL", ""),
            os.getenv("WEBDAV_URL", "").rstrip("/"),
            os.getenv("WEBDAV_USERNAME", ""),
            os.getenv("WEBDAV_PASSWORD", ""),
            int(os.getenv('RECOGNITION_WORKERS', '0')),
            int(os.getenv('OCR_THREADS', '1')),
            int(os.getenv('PREFETCH_JOBS', '0')),
            int(os.getenv('DOWNLOAD_WORKERS', '0')),
            int(os.getenv('VERIFY_WORKERS', '0')),
            int(os.getenv('OCR_SLOTS', '0')),
            int(os.getenv('DELIVER_WORKERS', '0')),
            int(os.getenv('DOWNLOAD_AHEAD', '0')),
            int(os.getenv('OCR_PROCESSES', '0')),
            os.getenv('PIPELINE', 'staged'),
            os.getenv('AUTO_TUNE', 'true').lower() in ('1', 'true', 'yes'),
            int(os.getenv('WORK_MAX_BYTES', '0')),
            int(os.getenv('MEMORY_CACHE_BYTES', '0')),
            os.getenv('TEMPLATE_PUBLIC_KEY', ''),
        )
