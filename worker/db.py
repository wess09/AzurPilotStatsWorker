"""Only JSON, small candidate crops and progress are durable; originals are not."""
import hashlib
import json
import re
import sqlite3
import threading
import time


HEX = re.compile(r"^[0-9a-f]{64}$")

# Pipeline stages, in order. Delivery keeps the historical name so existing
# databases and saved results stay compatible.
STAGES = ('pending', 'downloaded', 'verified', 'matched', 'recognized', 'delivery')


def validate_source(source, file_bytes=10 * 1024**2, group_bytes=500 * 1024**2):
    files = source["files"]
    meta = source["metadata"]
    if not HEX.fullmatch(source["sha256"]) or not 1 <= len(files) <= 1000 or len(files) != len(meta["file_sizes"]):
        raise ValueError("Invalid sample identity/count")
    total = 0
    digest = hashlib.sha256()
    for index, f in enumerate(files):
        if f["index"] != index or not HEX.fullmatch(f["sha256"]) or not 0 < f["size"] <= file_bytes or f["file_id"] <= 0 or f["size"] != meta["file_sizes"][index]:
            raise ValueError("Invalid source file")
        digest.update(bytes.fromhex(f["sha256"]))
        total += f["size"]
    if total > group_bytes or digest.hexdigest() != source["sha256"]:
        raise ValueError("Sample hash/size mismatch")
    if meta["project"] not in ("research", "drop"):
        raise ValueError("Unsupported project")


class Database:
    def __init__(self, path, file_bytes=10 * 1024**2, group_bytes=500 * 1024**2):
        self.file_bytes, self.group_bytes = file_bytes, group_bytes
        self.lock = threading.RLock()
        self._release_cache = None
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY,sha TEXT NOT NULL,task TEXT NOT NULL,source TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',result TEXT,candidates TEXT,retry_at REAL NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',UNIQUE(sha,task));
        CREATE TABLE IF NOT EXISTS commands(id INTEGER PRIMARY KEY,payload TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',result TEXT,reported INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        UPDATE commands SET state='pending' WHERE state='processing';
        """)
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(jobs)')}
        for name, definition in (('active', 'INTEGER NOT NULL DEFAULT 0'),
                                 ('attempts', 'INTEGER NOT NULL DEFAULT 0'),
                                 ('partial', 'TEXT'),
                                 ('snapshot', 'TEXT'),
                                 ('opencv_ms', 'INTEGER NOT NULL DEFAULT 0'),
                                 ('ocr_ms', 'INTEGER NOT NULL DEFAULT 0'),
                                 ('download_ms', 'INTEGER NOT NULL DEFAULT 0')):
            if name not in columns:
                self.db.execute(f'ALTER TABLE jobs ADD COLUMN {name} {definition}')
        with self.db:
            # Interrupted work is resumed from its durable stage. A job that
            # already stored a final result only needs its delivery retried.
            self.db.execute("UPDATE jobs SET active=0,state=CASE WHEN result IS NOT NULL THEN 'delivery' ELSE 'pending' END WHERE state='processing' OR active=1")
            # Engine removes originals on startup. Persisted intermediate jobs
            # must download again; their old files cannot still be assumed valid.
            self.db.execute("UPDATE jobs SET state='pending',active=0 WHERE state IN ('downloaded','verified','matched','recognized') AND result IS NULL")
            self.db.execute('CREATE INDEX IF NOT EXISTS jobs_active_sha ON jobs(sha) WHERE active=1')
            self.db.execute('CREATE INDEX IF NOT EXISTS jobs_stage ON jobs(state,retry_at)')

    def close(self):
        with self.lock:
            self.db.close()

    def accept(self, batch):
        if batch.get("type") != "batch_ready" or batch.get("protocol_version") != 1 or not re.fullmatch(r"[0-9a-f]{32}", batch.get("batch_id", "")) or not 1 <= len(batch.get("items", [])) <= 30:
            raise ValueError("Invalid batch")
        for source in batch["items"]:
            validate_source(source, self.file_bytes, self.group_bytes)
        payload = json.dumps(batch, separators=(",", ":"), sort_keys=True)
        with self.lock, self.db:
            previous = self.db.execute("SELECT payload FROM batches WHERE id=?", (batch["batch_id"],)).fetchone()
            if previous and previous[0] != payload:
                raise ValueError("Batch ID conflict")
            self.db.execute("INSERT OR IGNORE INTO batches VALUES(?,?)", (batch["batch_id"], payload))
            for source in batch["items"]:
                self.db.execute("INSERT OR IGNORE INTO jobs(sha,task,source) VALUES(?,'ingest',?)", (source["sha256"], json.dumps(source)))

    def claim(self):
        with self.lock, self.db:
            row = self.db.execute("SELECT j.* FROM jobs j WHERE j.active=0 AND j.state IN ('pending','delivery') AND j.retry_at<=? AND NOT EXISTS(SELECT 1 FROM jobs busy WHERE busy.sha=j.sha AND busy.active=1) ORDER BY CASE WHEN task LIKE 'command:%' THEN 0 ELSE 1 END,id LIMIT 1", (time.time(),)).fetchone()
            if row:
                self.db.execute("UPDATE jobs SET active=1,state=CASE WHEN state='pending' THEN 'processing' ELSE state END WHERE id=?", (row['id'],))
            return dict(row) if row else None

    def claim_stage(self, state):
        """Lease one job of a pipeline stage; every stage claims its own state."""
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT j.* FROM jobs j WHERE j.active=0 AND j.state=? AND j.retry_at<=? "
                "AND NOT EXISTS(SELECT 1 FROM jobs busy WHERE busy.sha=j.sha AND busy.active=1) "
                "ORDER BY CASE WHEN j.task LIKE 'command:%' THEN 0 ELSE 1 END,id LIMIT 1", (state, time.time())).fetchone()
            if row:
                self.db.execute("UPDATE jobs SET active=1 WHERE id=?", (row['id'],))
            return dict(row) if row else None

    def advance(self, job, state, partial=None):
        with self.lock, self.db:
            if partial is None:
                self.db.execute("UPDATE jobs SET state=?,active=0,error='',attempts=0 WHERE id=?", (state, job['id']))
            else:
                self.db.execute("UPDATE jobs SET state=?,active=0,error='',attempts=0,partial=? WHERE id=?",
                                (state, json.dumps(partial), job['id']))

    def unlease(self, job):
        with self.lock, self.db:
            self.db.execute('UPDATE jobs SET active=0 WHERE id=?', (job['id'],))

    def retry_stage(self, job, state, error):
        attempts = int(job.get('attempts') or 0) + 1
        delay = min(300, 10 * attempts)
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET state=?,active=0,attempts=?,retry_at=?,error=? WHERE id=?",
                            (state, attempts, time.time() + delay, str(error)[:2000], job['id']))

    def partial(self, job):
        return json.loads(job['partial']) if job.get('partial') else None

    def timed(self, job, column, milliseconds):
        if column not in ('download_ms', 'opencv_ms', 'ocr_ms'):
            raise ValueError('Invalid stage timing column')
        with self.lock, self.db:
            self.db.execute(f"UPDATE jobs SET {column}={column}+? WHERE id=?", (int(milliseconds), job['id']))

    def count_states(self, states):
        placeholders = ','.join('?' for _ in states)
        with self.lock:
            return self.db.execute(f"SELECT count(*) FROM jobs WHERE state IN ({placeholders})", tuple(states)).fetchone()[0]

    def stage_counts(self):
        with self.lock:
            return dict(self.db.execute("SELECT state,count(*) FROM jobs GROUP BY state"))

    def save_result(self, job, result, candidates, snapshot=None):
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET state='delivery',result=?,candidates=?,snapshot=?,error='',partial=NULL WHERE id=?",
                            (json.dumps(result), json.dumps(candidates), json.dumps(snapshot) if snapshot is not None else None, job["id"]))

    def finish(self, job):
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET state='done',active=0,candidates=NULL,error='' WHERE id=?", (job["id"],))

    def retry(self, job, error):
        with self.lock, self.db:
            state = "delivery" if self.db.execute("SELECT result FROM jobs WHERE id=?", (job["id"],)).fetchone()[0] else "pending"
            self.db.execute("UPDATE jobs SET state=?,active=0,retry_at=?,error=? WHERE id=?", (state, time.time() + 10, str(error)[:2000], job["id"]))

    def quarantine(self, job, message):
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET state='failed',active=0,error=? WHERE id=?", (message, job["id"]))

    def pending(self):
        with self.lock:
            return self.db.execute("SELECT count(*) FROM jobs WHERE state NOT IN ('done','failed')").fetchone()[0]

    def is_redundant_research_list(self, source):
        """Only discard mislabeled completion frames already submitted as lists."""
        meta = source["metadata"]
        if meta.get("project") != "research" or meta.get("scene") != "research_complete":
            return False
        wanted = {file["sha256"] for file in source["files"]}
        covered = set()
        with self.lock:
            rows = self.db.execute("SELECT source FROM jobs WHERE task='ingest' AND sha<>?", (source["sha256"],))
            for row in rows:
                other = json.loads(row[0])
                other_meta = other["metadata"]
                if (other_meta.get("project") == "research" and other_meta.get("scene") == "research_refresh"
                        and other_meta.get("device_id") == meta.get("device_id")
                        and other_meta.get("instance_id") == meta.get("instance_id")):
                    covered.update(file["sha256"] for file in other["files"])
                    if wanted <= covered:
                        return True
        return False

    def setting(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.lock, self.db:
            self.db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
            if key in ('release_id', 'templates', 'local_icons', 'template_bundle'):
                self._release_cache = None

    def release(self):
        with self.lock:
            if self._release_cache is None:
                bundle = self.setting("template_bundle")
                published = self.setting("templates", [])
                by_id = {entry["id"]: entry for entry in self.setting("local_icons", [])}
                by_id.update({entry["id"]: entry for entry in published})
                entries = list(by_id.values())
                options = {"match_thresholds": bundle["match_thresholds"],
                           "hash_overrides": {pixel_hash: bundle["mappings"].get(key, key)
                                              for pixel_hash, key in bundle.get("hash_overrides", {}).items()}} if bundle else {}
                options["known_icons"] = {entry["known_icon_sha256"]: entry["item_id"] for entry in entries
                                           if entry.get("known_icon_sha256")}
                self._release_cache = self.setting("release_id", 0), entries, options
            return self._release_cache[:2]

    def set_release(self, id_, templates):
        with self.lock, self.db:
            current = self.setting("release_id", 0)
            if type(id_) is not int or id_ < current:
                raise ValueError("Template revision cannot move backwards")
            bundle = None
            if isinstance(templates, dict):
                from .template_package import normalize_bundle, effective_templates
                bundle = normalize_bundle(templates, id_, self.setting("template_bundle"))
                if id_ == current and bundle != self.setting("template_bundle"):
                    raise ValueError("Template revision is immutable")
                templates = effective_templates(bundle)
            elif not isinstance(templates, list):
                raise ValueError("Invalid template release")
            for key, value in (("release_id", id_), ("templates", templates), ("template_bundle", bundle)):
                self.db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
            if id_ > current:
                self.db.execute("DELETE FROM settings WHERE key='local_icons'")
            self._release_cache = None

    def release_snapshot(self):
        with self.lock:
            self.release()
            return self._release_cache

    def cache_icon(self, candidate, receipt):
        from .template_package import ITEM, CLUSTER
        kind = receipt.get("match")
        item = receipt.get("item") if kind == "formal" else receipt.get("key")
        if kind not in ("formal", "cluster", "new"):
            return
        pixel_hash = candidate.get("metadata", {}).get("icon_sha256")
        if pixel_hash is None:
            return  # A durable candidate produced by an older parser.
        if receipt.get("local_key") != pixel_hash or not isinstance(item, str) or not ITEM.fullmatch(item):
            raise ValueError("Invalid icon acknowledgment")
        if (kind != "formal" and not CLUSTER.fullmatch(item)) or (kind == "formal" and CLUSTER.fullmatch(item)):
            raise ValueError("Invalid server cluster key")
        revision = receipt.get("revision")
        if type(revision) is not int or revision < 0:
            raise ValueError("Invalid icon acknowledgment revision")
        with self.lock, self.db:
            if revision != self.setting("release_id", 0):
                return
            cached = [entry for entry in self.setting("local_icons", []) if entry["id"] != candidate["id"]]
            cached.append({"id": candidate["id"], "item_id": item, "image": candidate["image"],
                           "scene": "all", "enabled": True,
                           "kind": "formal" if kind == "formal" else "temporary",
                           "known_icon_sha256": pixel_hash, "temporary_similarity": 0.95})
            self.db.execute("INSERT INTO settings VALUES('local_icons',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (json.dumps(cached[-512:]),))
            self._release_cache = None

    def update_reprocess_command(self, id_):
        with self.lock, self.db:
            counts = dict(self.db.execute("SELECT state,count(*) FROM jobs WHERE task=? GROUP BY state", (f"command:{id_}",)))
            total = sum(counts.values())
            finished = counts.get("done", 0) + counts.get("failed", 0)
            state = ("failed" if counts.get("failed") else "done") if total == finished else "running"
            self.db.execute("UPDATE commands SET state=?,result=?,reported=0 WHERE id=?", (state, json.dumps({"total": total, "finished": finished, "counts": counts}), id_))

    def sync_updates(self):
        with self.lock:
            return [{"id": r["id"], "state": r["state"], "result": json.loads(r["result"] or "{}")} for r in self.db.execute("SELECT * FROM commands WHERE state IN ('running','done','failed') AND reported=0 LIMIT 100")]

    def accept_commands(self, commands, reported):
        with self.lock, self.db:
            for cmd in commands:
                self.db.execute("INSERT OR IGNORE INTO commands(id,payload) VALUES(?,?)", (cmd["id"], json.dumps(cmd)))
            for update in reported:
                if update["state"] in ("done", "failed"):
                    self.db.execute("UPDATE commands SET reported=1 WHERE id=?", (update["id"],))

    def command(self):
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM commands WHERE state='pending' ORDER BY id LIMIT 1").fetchone()
            if row:
                self.db.execute("UPDATE commands SET state='running' WHERE id=?", (row["id"],))
            return dict(row) if row else None

    def command_result(self, id_, state, result):
        with self.lock, self.db:
            self.db.execute("UPDATE commands SET state=?,result=?,reported=0 WHERE id=?", (state, json.dumps(result), id_))

    def enqueue_reprocess(self, cmd):
        with self.lock, self.db:
            for source in cmd["payload"]["sources"]:
                validate_source(source, self.file_bytes, self.group_bytes)
                self.db.execute("INSERT OR IGNORE INTO jobs(sha,task,source) VALUES(?,?,?)", (source["sha256"], f"command:{cmd['id']}", json.dumps(source)))

    def enqueue_release(self, source, release):
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO jobs(sha,task,source) VALUES(?,?,?)", (source["sha256"], f"release:{release}", json.dumps(source)))
