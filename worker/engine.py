import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time

from .clients import Pan, Statistics, WebDAV
from .pool import RecognizerPool
from .adaptive import Adaptive

log = logging.getLogger(__name__)

MAX_STAGE_ATTEMPTS = 5


def template_fields(template):
    """Comparable identity of one published template, without the image bytes."""
    image = template.get('image') or ''
    if isinstance(image, str) and len(image) > 200:
        image = hashlib.sha256(image.encode()).hexdigest()
    return (template.get('item_id'), template.get('scene'), template.get('enabled'), image,
            template.get('kind'), template.get('match_thresholds'))


def template_snapshot(templates, options=None):
    snapshot = []
    for template in templates:
        item = {key: template.get(key) for key in ('item_id', 'scene', 'enabled', 'kind', 'match_thresholds')}
        item['id'] = template['id']
        item['image'] = template_fields(template)[3]
        snapshot.append(item)
    for pixel_hash, item in (options or {}).get('hash_overrides', {}).items():
        snapshot.append({'id': pixel_hash, 'item_id': item, 'scene': 'all', 'enabled': True,
                         'image': '', 'kind': 'hash_override', 'match_thresholds': None})
    return snapshot


def needs_template_retry(result, previous, current):
    if result.get('candidate_ids') or any(d == 'missing_template' or d.startswith(('missing_template:', 'unmapped_cluster:'))
                                          for d in result.get('diagnostics', [])):
        return True
    used = result.get('used_templates', [])
    if not used:
        return False
    if previous is None:
        # A saved delivery from an older process has no in-memory snapshot.
        return True
    old = {t['id']: template_fields(t) for t in previous}
    new = {t['id']: template_fields(t) for t in current}
    return any(old.get(id_) != new.get(id_) for id_ in used)


class Engine:
    def match_icons(self, body):
        """Bounded stateless work in the existing native process environment."""
        with tempfile.TemporaryDirectory(prefix="icons-", dir=self.work) as directory:
            request, output = Path(directory) / "request.json", Path(directory) / "result.json"
            request.write_text(json.dumps({**body, "operation": "match_icons", "alas_root": str(self.config.alas_root)}), encoding="utf-8")
            self.recognizers.recognize(request, output, "cn", timeout=60)
            result = json.loads(output.read_text(encoding="utf-8"))
            if "results" not in result:
                raise ValueError("Icon matching failed")
            return result

    def __init__(self, config, db):
        self.config = config
        self.db = db
        self.stats = Statistics(config)
        self.pan = WebDAV(config) if config.webdav_url else Pan(config)
        self.work = config.data / "work"
        self.work.mkdir(exist_ok=True)
        # A worker is deliberately single-process. No other process may share
        # this work directory or SQLite DB. Stale originals are not retained.
        for path in self.work.iterdir():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        self.tuning = Adaptive(config)
        self.download_workers = self.tuning.maximum['download']
        self.verify_workers = self.tuning.maximum['verify']
        self.recognize_slots = self.tuning.maximum['recognize']
        self.deliver_workers = self.tuning.maximum['deliver']
        self.download_ahead = self.tuning.ahead
        self.recognize_processes = self.recognize_slots
        self.recognizers = RecognizerPool(self.recognize_processes, config.ocr_threads, stage='recognize')
        self.workers = self.recognize_processes
        self.executors = {name: ThreadPoolExecutor(max_workers=limit, thread_name_prefix=name)
                          for name, limit in self.tuning.maximum.items()}
        self.executor = self.executors['recognize']  # Inline rollback path.
        self.scheduler = ThreadPoolExecutor(max_workers=1, thread_name_prefix='leases')
        self.management = ThreadPoolExecutor(max_workers=3, thread_name_prefix='control')
        self.disk_lock = threading.Lock()
        self.reservations = {}
        self.work_limit = config.work_max_bytes or max(config.group_bytes, min(2 * 1024**3, shutil.disk_usage(self.work).free // 4))
        self.memory_jobs = set()
        self.memory_work = None
        self.memory_limit = 0
        shm = Path('/dev/shm')
        if config.memory_cache_bytes >= 0 and shm.is_dir():
            try:
                usage = shutil.disk_usage(shm)
                self.memory_limit = min(config.memory_cache_bytes or min(512 * 1024**2, self.tuning.resources.memory // 10),
                                        usage.total * 3 // 4, max(0, usage.free - 8 * 1024**2))
                identity = hashlib.sha256(str(config.data).encode()).hexdigest()[:12]
                self.memory_work = shm / ('azurpilot-' + identity)
                self.memory_work.mkdir(exist_ok=True)
                for child in self.memory_work.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
            except OSError:
                self.memory_work, self.memory_limit = None, 0
        self.active = dict.fromkeys(self.tuning.maximum, 0)
        self.wakeups = []
        self.pan.on_throttle = self.tuning.throttled
        log.info('Pipeline staged: download=%s verify=%s recognize_slots=%s recognize_processes=%s '
                 'deliver=%s download_ahead=%s pipeline=%s',
                 self.download_workers, self.verify_workers, self.recognize_slots,
                 self.recognize_processes, self.deliver_workers, self.download_ahead, config.pipeline)

    # ---- shared helpers -------------------------------------------------
    def source(self, job):
        return json.loads(job["source"])

    def workdir(self, job):
        with self.disk_lock:
            root = self.memory_work if job['id'] in self.memory_jobs else self.work
        return root / str(job["id"])

    def files(self, job):
        source = self.source(job)
        directory = self.workdir(job)
        return [directory / f"{file['index']}.png" for file in source["files"]]

    def cleanup(self, job):
        root = getattr(self, 'work', None)
        if root is None:
            return
        for directory in {self.workdir(job), self.work / str(job['id'])}:
            shutil.rmtree(directory, ignore_errors=True)
        with self.disk_lock:
            self.reservations.pop(job['id'], None)
            self.memory_jobs.discard(job['id'])

    def reserve_download(self, job):
        size = sum(file['size'] for file in self.source(job)['files'])
        with self.disk_lock:
            reserved = sum(self.reservations.values())
            if (reserved + size > self.work_limit
                    or shutil.disk_usage(self.work).free - reserved - size < 128 * 1024**2):
                return False
            cached = sum(value for id_, value in self.reservations.items() if id_ in self.memory_jobs)
            if (self.memory_work is not None and cached + size <= self.memory_limit
                    and self.tuning.pressure['memory'] < .8
                    and shutil.disk_usage(self.memory_work).free - cached - size >= 8 * 1024**2):
                self.memory_jobs.add(job['id'])
            self.reservations[job['id']] = size
            return True

    def performance(self):
        result = self.tuning.snapshot()
        with self.disk_lock:
            result['work_reserved'] = sum(self.reservations.values())
            result['memory_cached'] = sum(value for id_, value in self.reservations.items() if id_ in self.memory_jobs)
            result['memory_cache_limit'] = self.memory_limit
        result.update(work_limit=self.work_limit, active=dict(self.active),
                      resident_processes=len(self.recognizers.processes))
        return result

    def fail(self, job, state, stage, error):
        attempts = int(job.get('attempts') or 0) + 1
        if attempts >= MAX_STAGE_ATTEMPTS and stage != 'deliver':
            self.db.quarantine(job, f'{stage}: {error}')
            self.cleanup(job)
            log.error('stage=%s quarantined job=%s attempts=%s error=%s',
                      stage, job['id'], attempts, str(error)[:300])
        else:
            self.db.retry_stage(job, state, f'{stage}: {error}')
            log.warning('stage=%s retry job=%s attempts=%s error=%s',
                        stage, job['id'], attempts, str(error)[:300])
        return False

    def run_recognizer(self, request, job, stage):
        # Only PNG bytes consume the bounded shared-memory cache. Template
        # packages and candidate JSON may be much larger than a screenshot.
        directory = self.work / str(job['id'])
        directory.mkdir(parents=True, exist_ok=True)
        input_path = directory / f'{stage}.input.json'
        output_path = directory / f'{stage}.output.json'
        input_path.write_text(json.dumps(request), encoding='utf-8')
        started = time.monotonic()
        self.recognizers.recognize(input_path, output_path, self.source(job)['metadata']['server'],
                                   timeout=int(os.getenv('RECOGNITION_TIMEOUT', '600')))
        if output_path.stat().st_size > 8 * 1024**2:
            raise ValueError("Recognition output is too large")
        parsed = json.loads(output_path.read_text(encoding="utf-8"))
        input_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
        return parsed, time.monotonic() - started

    # ---- stages ---------------------------------------------------------
    def stage_download(self, job):
        source = self.source(job)
        directory = self.workdir(job)
        directory.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        try:
            for file in source["files"]:
                path = directory / f"{file['index']}.png"
                one = time.monotonic()
                self.pan.download(file, path)
                log.info('stage=download sha=%s job=%s index=%s bytes=%s seconds=%.3f transport=%s',
                         source["sha256"], job['id'], file["index"], file["size"], time.monotonic() - one,
                         "webdav" if self.config.webdav_url else "openapi")
            self.db.timed(job, 'download_ms', (time.monotonic() - started) * 1000)
            # Both download transports validate every byte before returning.
            # Do not re-read and hash the same PNG a second time in another pool.
            self.db.advance(job, 'verified')
            log.info('stage=download complete sha=%s job=%s files=%s seconds=%.3f',
                     source["sha256"], job['id'], len(source["files"]), time.monotonic() - started)
        except Exception as error:
            self.cleanup(job)
            return self.fail(job, 'pending', 'download', error)

    def stage_verify(self, job):
        source = self.source(job)
        directory = self.workdir(job)
        started = time.monotonic()
        try:
            total = 0
            for file in source["files"]:
                path = directory / f"{file['index']}.png"
                # hashlib.file_digest hashes in C over the file object: one call,
                # no Python chunk loop and no GIL-only work while OCR saturates
                # the CPU. The old loop took over a second under load.
                with path.open("rb") as handle:
                    digest = hashlib.file_digest(handle, "sha256").hexdigest()
                size = path.stat().st_size
                if size != file["size"] or digest != file["sha256"]:
                    raise ValueError(f"index {file['index']} hash/size mismatch")
                total += size
            self.db.advance(job, 'verified')
            log.info('stage=verify sha=%s job=%s files=%s bytes=%s seconds=%.3f',
                     source["sha256"], job['id'], len(source["files"]), total, time.monotonic() - started)
        except Exception as error:
            self.cleanup(job)
            return self.fail(job, 'pending', 'verify', error)

    def stage_recognize(self, job):
        source = self.source(job)
        release_id, templates, options = self.db.release_snapshot()
        request = {"source": source, "files": [str(path) for path in self.files(job)],
                   "alas_root": str(self.config.alas_root), "templates": templates,
                   **options}
        try:
            parsed, elapsed = self.run_recognizer(request, job, 'recognize')
            if parsed.get("skipped_frames"):
                log.warning("Commission frames skipped sha=%s details=%s", source["sha256"],
                            json.dumps(parsed["skipped_frames"], ensure_ascii=False))
            if ({"no_complete_reward_frame", "unknown_research_project_or_series"}
                    <= set(parsed["diagnostics"]) and self.db.is_redundant_research_list(source)):
                parsed = {"status": "invalid", "events": [], "diagnostics": ["duplicate_research_list_capture"],
                          "candidates": [], "used_templates": []}
            overrides = parsed.get("metadata_overrides", {})
            if set(overrides) - {"project", "scene"} or any(not isinstance(v, str) or len(v) > 128 for v in overrides.values()):
                raise ValueError("Invalid inferred metadata")
            source["metadata"].update(overrides)
            # Network requests belong to delivery, never occupy an OCR slot.
            # Revision 0 is a local, durable "not allocated yet" marker.
            revision = 0
            result = {"sha256": source["sha256"], "revision": revision, "status": parsed["status"],
                      "parser_version": "azurpilot-adapter-v4",
                      "template_release": release_id, "source": source,
                      "events": parsed["events"], "diagnostics": parsed["diagnostics"],
                      "candidate_ids": sorted({c["id"] for c in parsed["candidates"]}),
                      "used_templates": parsed["used_templates"]}
            self.db.timed(job, 'ocr_ms', elapsed * 1000)
            self.db.save_result(job, result, parsed["candidates"], template_snapshot(templates, options))
            self.cleanup(job)
            # Hand the lease to the delivery stage; it is a separate worker.
            self.db.advance(job, 'delivery')
            log.info('stage=recognize sha=%s job=%s status=%s events=%s candidates=%s revision=%s seconds=%.3f',
                     source["sha256"], job['id'], parsed["status"], len(parsed["events"]),
                     len(parsed["candidates"]), revision, elapsed)
        except Exception as error:
            self.cleanup(job)
            return self.fail(job, 'pending', 'recognize', error)

    def deliver(self, job, source, result, candidates, snapshot):
        references = []
        for candidate in candidates:
            receipt = self.stats.post("/internal/v1/template-candidates", candidate)
            self.db.cache_icon(candidate, receipt)
            if receipt.get("need_screenshot") and receipt.get("key"):
                references.append({"key": receipt["key"], "candidate_id": candidate["id"],
                                   "sample_sha256": source["sha256"],
                                   "file_index": candidate["metadata"]["file_index"],
                                   "frame_index": candidate["metadata"]["frame_index"]})
        ack = self.stats.post("/internal/v1/statistics/results", {"results": [result]})["receipts"][0]
        if ack["status"] in ("accepted", "duplicate"):
            for reference in references:
                # Originals are already on 123Pan. Register a bounded preview
                # reference after C knows the source, without duplicating bytes.
                self.stats.post("/internal/v1/unknown-screenshots", reference)
            self.db.finish(job)
            self.cleanup(job)
            log.info('stage=deliver sha=%s job=%s revision=%s status=%s receipt=%s',
                     source["sha256"], job['id'], result["revision"], result["status"], ack["status"])
            active, current_templates, current_options = self.db.release_snapshot()
            if active != result["template_release"] and needs_template_retry(result, snapshot, template_snapshot(current_templates, current_options)):
                # Publication may race an in-flight first result, before C
                # knows that source. It must not miss the new template pass.
                self.db.enqueue_release(source, active)
        elif ack["status"] == "retry":
            raise RuntimeError(ack.get("error", "C temporarily unavailable"))
        else:
            self.db.quarantine(job, json.dumps(ack, ensure_ascii=False))
            log.error('stage=deliver rejected sha=%s receipt=%s error=%s', source["sha256"], ack["status"], ack.get("error", ""))
            return False

    def stage_deliver(self, job):
        source = self.source(job)
        snapshot = json.loads(job["snapshot"]) if job.get("snapshot") else None
        try:
            result = json.loads(job["result"])
            candidates = json.loads(job["candidates"] or "[]")
        except (TypeError, ValueError) as error:
            return self.fail(job, 'delivery', 'deliver', error)
        try:
            if not result.get('revision'):
                result['revision'] = self.stats.revision(source['sha256']) + 1
                # Persist before sending so a lost HTTP response reuses the
                # same revision instead of adding another statistics version.
                self.db.save_result(job, result, candidates, snapshot)
            return self.deliver(job, source, result, candidates, snapshot)
        except Exception as error:
            return self.fail(job, 'delivery', 'deliver', error)
        finally:
            if job["task"].startswith("command:"):
                self.db.update_reprocess_command(int(job["task"].split(":")[1]))

    # ---- compatibility: single-shot recognition for template tests ------
    def recognize(self, source, templates=None, recognition_options=None):
        with tempfile.TemporaryDirectory(dir=self.work) as temporary:
            directory = Path(temporary)
            paths = []
            for file in source["files"]:
                path = directory / f"{file['index']}.png"
                self.pan.download(file, path)
                paths.append(str(path))
            request = {"source": source, "files": paths, "alas_root": str(self.config.alas_root),
                       "templates": self.db.setting("templates", []) if templates is None else templates,
                       **(recognition_options or {})}
            input_path = directory / "input.json"
            output_path = directory / "output.json"
            input_path.write_text(json.dumps(request), encoding="utf-8")
            started = time.monotonic()
            self.recognizers.recognize(input_path, output_path, source['metadata']['server'],
                                       timeout=int(os.getenv('RECOGNITION_TIMEOUT', '600')))
            if output_path.stat().st_size > 8 * 1024**2:
                raise ValueError("Recognition output is too large")
            parsed = json.loads(output_path.read_text(encoding="utf-8"))
            log.info("OCR completed sha=%s status=%s seconds=%.3f diagnostics=%s", source["sha256"],
                     parsed["status"], time.monotonic() - started, len(parsed["diagnostics"]))
            return parsed

    def process(self, job):
        """Single-lane path used by PIPELINE=inline and template trials."""
        try:
            source = self.source(job)
            snapshot = json.loads(job["snapshot"]) if job.get("snapshot") else None
            if job["state"] != "delivery":
                revision = self.stats.revision(source["sha256"]) + 1
                release_id, templates, options = self.db.release_snapshot()
                parsed = (self.recognize(source, templates, recognition_options=options) if any(options.values())
                          else self.recognize(source, templates))
                if parsed.get("skipped_frames"):
                    log.warning("Commission frames skipped sha=%s details=%s", source["sha256"],
                                json.dumps(parsed["skipped_frames"], ensure_ascii=False))
                if ({"no_complete_reward_frame", "unknown_research_project_or_series"}
                        <= set(parsed["diagnostics"]) and self.db.is_redundant_research_list(source)):
                    parsed = {"status": "invalid", "events": [], "diagnostics": ["duplicate_research_list_capture"],
                              "candidates": [], "used_templates": []}
                overrides = parsed.get("metadata_overrides", {})
                if set(overrides) - {"project", "scene"} or any(not isinstance(v, str) or len(v) > 128 for v in overrides.values()):
                    raise ValueError("Invalid inferred metadata")
                source["metadata"].update(overrides)
                result = {"sha256": source["sha256"], "revision": revision, "status": parsed["status"],
                          "parser_version": "azurpilot-adapter-v4", "template_release": release_id,
                          "source": source, "events": parsed["events"], "diagnostics": parsed["diagnostics"],
                          "candidate_ids": sorted({c["id"] for c in parsed["candidates"]}),
                          "used_templates": parsed["used_templates"]}
                snapshot = template_snapshot(templates, options)
                self.db.save_result(job, result, parsed["candidates"], snapshot)
                candidates = parsed["candidates"]
            else:
                result = json.loads(job["result"])
                candidates = json.loads(job["candidates"] or "[]")
            self.deliver(job, source, result, candidates, snapshot)
        except Exception as exc:
            log.exception("Recognition/delivery will retry")
            self.db.retry(job, exc)
        finally:
            if job["task"].startswith("command:"):
                self.db.update_reprocess_command(int(job["task"].split(":")[1]))

    def synchronize(self):
        updates = self.db.sync_updates()
        reply = self.stats.post("/internal/v1/worker/sync", {"worker_id": self.config.worker_id,
                                                             "release_id": self.db.setting("release_id", 0),
                                                             "pending": self.db.pending(), "updates": updates})
        release = reply.get("release_id") or 0
        if release != self.db.setting("release_id", 0):
            base = self.db.setting("template_bundle")
            templates = (self.stats.release(release, base["revision"]) if base and self.config.template_public_key
                         else self.stats.release(release)) if release else []
            # Atomic snapshot: no task can observe half a package.
            self.db.set_release(release, templates)
        self.db.accept_commands(reply["commands"], updates)

    def control(self, row):
        cmd = json.loads(row["payload"])
        try:
            if cmd["action"] == "reprocess":
                self.db.enqueue_reprocess(cmd)
                self.db.update_reprocess_command(cmd["id"])
            elif cmd["action"] == "test":
                reports = []
                for source in cmd["payload"]["sources"]:
                    parsed = self.recognize(source, cmd["payload"]["templates"])
                    reports.append({"sha256": source["sha256"], "status": parsed["status"],
                                    "events": parsed["events"], "diagnostics": parsed["diagnostics"]})
                self.db.command_result(cmd["id"], "done", {"reports": reports})
            else:
                raise ValueError("Unsupported command")
        except Exception as exc:
            self.db.command_result(cmd["id"], "failed", {"error": str(exc)})

    # ---- scheduler ------------------------------------------------------
    def stage(self, loop, name, state, slots, handler, budget=None):
        wake = asyncio.Event()
        self.wakeups.append(wake)
        running = set()

        async def execute(function, *args):
            future = loop.run_in_executor(self.executors[name], function, *args)
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                await future  # Drain leases/results before closing SQLite or HTTP.
                raise

        async def consume(job):
            started = time.monotonic()
            self.active[name] += 1
            succeeded = False
            try:
                succeeded = await execute(handler, job) is not False
            except Exception:
                log.exception('stage=%s worker failed unexpectedly', name)
                if name in ('download', 'verify', 'recognize'):
                    self.cleanup(job)
                await loop.run_in_executor(self.scheduler, self.fail, job,
                                           'pending' if name != 'deliver' else state,
                                           name, 'unexpected worker failure')
            finally:
                self.active[name] -= 1
                self.tuning.observe(name, time.monotonic() - started,
                                    len(self.source(job)['files']), failed=not succeeded)
                for event in self.wakeups:
                    event.set()

        async def dispatch():
            try:
                while True:
                    wake.clear()
                    ready = len(running) < self.tuning.limit(name)
                    if name == 'download':
                        ready = ready and self.tuning.download_ready()
                    job = None
                    try:
                        if ready and budget is not None:
                            ready = await loop.run_in_executor(self.scheduler, budget)
                        if ready:
                            job = await loop.run_in_executor(self.scheduler, self.db.claim_stage, state)
                    except Exception:
                        log.exception('stage=%s claim failed', name)
                        await asyncio.sleep(1)
                        continue
                    if job:
                        if name == 'download' and not await loop.run_in_executor(self.scheduler, self.reserve_download, job):
                            await loop.run_in_executor(self.scheduler, self.db.unlease, job)
                        else:
                            task = asyncio.create_task(consume(job))
                            running.add(task)
                            def finished(done):
                                running.discard(done)
                                wake.set()
                            task.add_done_callback(finished)
                            continue
                    try:
                        await asyncio.wait_for(wake.wait(), timeout=.25)
                    except asyncio.TimeoutError:
                        pass
            finally:
                # Finish only already-running work, without pre-leasing a
                # second queue of jobs that can starve other stages.
                await asyncio.gather(*running, return_exceptions=True)

        return [asyncio.create_task(dispatch())]

    async def run(self):
        loop = asyncio.get_running_loop()
        if self.config.pipeline == "inline":
            return await self.run_inline(loop)

        async def control_worker():
            while True:
                command = await loop.run_in_executor(self.management, self.db.command)
                if command:
                    future = loop.run_in_executor(self.management, self.control, command)
                    try:
                        await asyncio.shield(future)
                    except asyncio.CancelledError:
                        await future
                        raise
                else:
                    await asyncio.sleep(0.25)

        async def management():
            while True:
                try:
                    sync = loop.run_in_executor(self.management, self.synchronize)
                    try:
                        await asyncio.shield(sync)
                    except asyncio.CancelledError:
                        await sync  # Finish HTTP activity before closing clients.
                        raise
                except Exception:
                    log.exception("C control sync failed")
                await asyncio.sleep(30)

        def ahead():
            return self.db.count_states(('downloaded', 'verified')) < self.download_ahead

        async def reporter():
            report_at = 0
            while True:
                counts = await loop.run_in_executor(self.scheduler, self.db.stage_counts)
                await loop.run_in_executor(self.management, self.tuning.adjust, counts)
                if self.tuning.pressure['memory'] >= .85:
                    await loop.run_in_executor(self.management, self.recognizers.trim_idle,
                                               self.tuning.limit('recognize'))
                for event in self.wakeups:
                    event.set()
                if time.monotonic() >= report_at:
                    log.info('stage=depth counts=%s performance=%s', counts, self.performance())
                    report_at = time.monotonic() + 30
                await asyncio.sleep(5)

        tasks = [asyncio.create_task(management()), asyncio.create_task(control_worker()),
                 asyncio.create_task(reporter())]
        tasks += self.stage(loop, 'download', 'pending', self.download_workers, self.stage_download, budget=ahead)
        tasks += self.stage(loop, 'verify', 'downloaded', self.verify_workers, self.stage_verify)
        tasks += self.stage(loop, 'recognize', 'verified', self.recognize_slots, self.stage_recognize)
        tasks += self.stage(loop, 'deliver', 'delivery', self.deliver_workers, self.stage_deliver)
        try:
            await asyncio.shield(asyncio.gather(*tasks))
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for executor in (*self.executors.values(), self.scheduler, self.management):
                executor.shutdown(wait=True)
            self.recognizers.close()
            self.stats.client.close()
            self.pan.client.close()

    async def run_inline(self, loop):
        """Previous one-lane pipeline, kept as a rollback switch (PIPELINE=inline)."""
        async def execute(function, *args):
            future = loop.run_in_executor(self.executor, function, *args)
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                await future
                raise

        async def consume():
            while True:
                job = await loop.run_in_executor(None, self.db.claim)
                if job:
                    await execute(self.process, job)
                else:
                    await asyncio.sleep(0.2)

        async def management():
            while True:
                try:
                    sync = loop.run_in_executor(None, self.synchronize)
                    try:
                        await asyncio.shield(sync)
                    except asyncio.CancelledError:
                        await sync
                        raise
                except Exception:
                    log.exception("C control sync failed")
                await asyncio.sleep(30)

        tasks = [asyncio.create_task(management())]
        tasks.extend(asyncio.create_task(consume()) for _ in range(max(4, self.deliver_workers)))
        try:
            await asyncio.shield(asyncio.gather(*tasks))
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for executor in (*self.executors.values(), self.scheduler, self.management):
                executor.shutdown(wait=True)
            self.recognizers.close()
            self.stats.client.close()
            self.pan.client.close()
