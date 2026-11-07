"""D-104 HTTP Executor candidate. No discovery, parsing, browser, or output model.

Derived from httpx_sync_process v2: spawn, sequential Pipe, parent watchdog,
sentinel confirmation, body-first flushed JSONL and confirmed-prefix recovery.
Callers consume retained files before close(); close removes the run directory.
"""

from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import json
import math
import multiprocessing
from multiprocessing.connection import wait
from pathlib import Path
import shutil
import ssl
import tempfile
import threading
import time
from urllib.parse import urljoin, urlsplit, urldefrag
import zlib

import httpx

from windows_job import Job
from url_normalizer import normalize_url

NS = 1_000_000_000
def resource_budget_key(url):
    return normalize_url(url)


@dataclass(frozen=True)
class Limits:
    attempt_target: float = 15
    attempt_maximum: float = 30
    run_target: float = 1800
    run_maximum: float = 1830
    resource_base: int = 10_000_000
    run_base: int = 100_000_000
    block: int = 65_536
    attempts: int = 1000
    reserve: float = 0.25  # Provisional: historical 32 ms max, pending current calibration.

    def __post_init__(self):
        ceilings = {"attempt_target": 15, "attempt_maximum": 30, "run_target": 1800, "run_maximum": 1830}
        for name, ceiling in ceilings.items():
            if getattr(self, name) > ceiling:
                raise ValueError("cannot raise approved " + name)
        for name in ("attempt_target", "attempt_maximum", "run_target", "run_maximum", "reserve"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(name)
        if not self.attempt_target < self.attempt_maximum or not self.run_target < self.run_maximum:
            raise ValueError("deadline order")
        if self.reserve >= min(self.attempt_maximum, self.run_maximum - self.run_target):
            raise ValueError("reserve")
        if any(type(getattr(self, name)) is not int for name in ("block", "attempts", "resource_base", "run_base")):
            raise ValueError("integer budgets required")
        if not (0 < self.block <= 65536 and 0 < self.attempts <= 1000
                and 0 < self.resource_base <= 10_000_000 and 0 < self.run_base <= 100_000_000):
            raise ValueError("budgets")


def deadlines(start, run_start, limits):
    return (start + round(limits.attempt_target * NS),
            min(start + round(limits.attempt_maximum * NS), run_start + round(limits.run_maximum * NS)))


def host_of(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("invalid HTTP URL")
    _ = parsed.port  # Reject malformed ports before admission.
    return parsed.hostname.lower().encode("idna").decode("ascii")


def boundary(url):
    host = host_of(url)
    return {host, host[4:] if host.startswith("www.") else "www." + host}


def retry_after(value, now_wall=None):
    if not value:
        return 0.0
    try:
        result = float(int(value))
    except OverflowError:
        return float("inf")  # Valid enormous wait: no further admission this run.
    except ValueError:
        try:
            result = parsedate_to_datetime(value).timestamp() - (time.time() if now_wall is None else now_wall)
        except (TypeError, ValueError, OverflowError):
            return 0.0
    return max(0.0, result) if math.isfinite(result) else 0.0


class Admission:
    """One run-wide counter and monotonic host/run scheduling, independent of Evidence."""
    def __init__(self, url, limits, clock=time.monotonic_ns, sleep=time.sleep):
        self.limits, self.clock, self.sleep = limits, clock, sleep
        self.started = clock()
        self.hosts = boundary(url)
        self.count = self.delivered = self.retained = self.uncertain_reserved = 0
        self.ready, self.last, self.crawl, self.stopped, self.errors, self.timeouts, self.seen429 = {}, {}, {}, {}, {}, {}, set()
        self.partial = False
        self.forbidden_urls = {}

    def check(self, url):
        host = host_of(url)
        if host not in self.hosts:
            return "host_boundary"
        if host in self.stopped:
            return self.stopped[host]
        if self.clock() >= self.started + round(self.limits.run_target * NS):
            return "run_target"
        if self.count >= self.limits.attempts:
            return "request_limit"
        if self.delivered + self.uncertain_reserved >= self.limits.run_base:
            return "run_bytes"
        return None

    def admit(self, url):
        host = host_of(url)
        while True:
            blocked = self.check(url)
            if blocked:
                return blocked
            now = self.clock()
            ready = max(self.ready.get(host, 0), self.last.get(host, -10**30) + round(max(1, self.crawl.get(host, 0)) * NS))
            if now >= ready:
                self.count += 1  # Before dispatch, never on redirects/retries as logical operations.
                self.last[host] = now
                return None
            self.sleep(min((ready-now)/NS, max(0, (self.started + round(self.limits.run_target*NS)-now)/NS), 0.1))

    def observe(self, host, result):
        status, error = result.get("http_status"), result.get("error_kind")
        self.errors[host] = self.errors.get(host, 0) + 1 if status and 500 <= status <= 599 else 0
        self.timeouts[host] = self.timeouts.get(host, 0) + 1 if error == "timeout" else 0
        delay = retry_after(result.get("headers", {}).get("retry-after"))
        if delay:
            delay = min(delay, self.limits.run_maximum)
            self.ready[host] = max(self.ready.get(host, 0), self.clock() + round(delay*NS))
        if status == 429:
            if host in self.seen429:
                self.stopped[host] = "repeated_429"
            self.seen429.add(host)
        if self.errors[host] >= 3:
            self.stopped[host] = "three_5xx"
        if self.timeouts[host] >= 3:
            self.stopped[host] = "three_timeouts"
        if status == 403:
            urls = self.forbidden_urls.setdefault(host, set())
            urls.add(result["url"])
            if len(urls) >= 2:
                # No section model exists in this component. Require caller access
                # assessment before any continuation; never infer an accessible section.
                self.stopped[host] = "access_review_required"


def _tls_error(exc):
    while exc is not None:
        if isinstance(exc, ssl.SSLCertVerificationError):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


class TargetReached(Exception):
    pass


def _decoded_blocks(response, block, target, send):
    """Bound gzip expansion; HTTPX iter_bytes can buffer a whole decoded raw chunk.

    Keep raw delivery and decoded delivery distinct. No independent raw-byte cap.
    Unsupported encodings fail closed without silently processing encoded bytes.
    """
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"", "identity", "gzip"}:
        raise ValueError("unsupported_content_encoding")
    def raw_blocks():
        raw_iterator = response.iter_raw(chunk_size=block)
        while True:
            now = time.monotonic_ns()
            if now >= target:
                raise TargetReached()
            send("read_start", time_ns=now)
            try:
                yield next(raw_iterator)
            except StopIteration:
                return
    if encoding != "gzip":
        yield from raw_blocks()
        return
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    for raw in raw_blocks():
        pending = raw
        while pending:
            data = decoder.decompress(pending, block)
            pending = decoder.unconsumed_tail
            if decoder.eof and decoder.unused_data:
                pending = decoder.unused_data
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            if data:
                yield data
    if not decoder.eof:
        raise ValueError("incomplete_gzip")


def _worker(connection, cafile):
    try:
        # The parent assigns the Job before sending boot. Parent death before this
        # barrier closes the Pipe; no HTTP request can have started.
        if connection.recv() != {"action": "boot"}:
            return
        context = ssl.create_default_context(cafile=cafile)
        with httpx.Client(verify=context, follow_redirects=False, trust_env=False,
                          timeout=None, headers={"Accept-Encoding": "gzip, identity"}) as client:
            connection.send({"event": "ready"})
            while True:
                cmd = connection.recv()
                if cmd["action"] == "stop":
                    break
                command_id = cmd["id"]
                def send(event, **data):
                    connection.send({"event": event, "id": command_id, **data})
                result = {"http_status": None, "headers": {}, "reason": "complete", "error_kind": None,
                          "delivered": 0, "retained": 0, "raw": 0, "response_closed": False}
                response = None
                try:
                    with Path(cmd["body"]).open("xb") as body, Path(cmd["metadata"]).open("x", encoding="utf-8") as meta:
                        with client.stream("GET", cmd["url"]) as response:
                            result["http_status"] = response.status_code
                            # Only policy-relevant headers travel through IPC.
                            result["headers"] = {key: response.headers[key] for key in
                                                 ("location", "retry-after", "content-type", "content-length", "content-encoding")
                                                 if key in response.headers}
                            send("headers", **result)
                            chunks = _decoded_blocks(response, cmd["block"], cmd["target"], send)
                            allowance_used = False
                            while True:
                                read_start = time.monotonic_ns()
                                if read_start >= cmd["target"]:
                                    result["reason"] = "attempt_target"
                                    break
                                at_base = result["delivered"] >= cmd["resource_base"] or result["delivered"] >= cmd["run_remaining"]
                                if at_base and allowance_used:
                                    result["reason"] = "byte_limit"
                                    break
                                if at_base:
                                    allowance_used = True
                                try:
                                    chunk = next(chunks)
                                except StopIteration:
                                    break
                                except TargetReached:
                                    result["reason"] = "attempt_target"
                                    break
                                result["delivered"] += len(chunk)
                                keep = min(len(chunk), max(0, cmd["resource_base"]-result["retained"]),
                                           max(0, cmd["run_remaining"]-result["retained"]),
                                           max(0, cmd.get("resource_retention", cmd["resource_base"])-result["retained"]))
                                if keep:
                                    body.write(chunk[:keep])
                                    body.flush()
                                    result["retained"] += keep
                                result["raw"] = response.num_bytes_downloaded
                                record = {"bytes_written": result["retained"], "delivered": result["delivered"],
                                          "raw": result["raw"], "time_ns": time.monotonic_ns()}
                                # A zero-retention allowance does not append a duplicate commit.
                                if keep:
                                    meta.write(json.dumps(record) + "\n")
                                    meta.flush()
                                send("block", **record)
                                if result["delivered"] > min(cmd["resource_base"], cmd["run_remaining"]):
                                    result["reason"] = "byte_limit"
                                    break
                        result["response_closed"] = response.is_closed
                except Exception as exc:
                    result["error_type"] = type(exc).__name__
                    result["error_kind"] = "tls" if _tls_error(exc) else "timeout" if isinstance(exc, httpx.TimeoutException) else "transport"
                    result["reason"] = "error"
                    result["response_closed"] = bool(response is not None and response.is_closed)
                if response is not None:
                    result["raw"] = response.num_bytes_downloaded
                send("result", **result, time_ns=time.monotonic_ns())
        connection.send({"event": "closed", "client_closed": client.is_closed})
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        connection.close()


def recover_prefix(body_path, metadata_path):
    """Validate commits, exclude tail in place; return metadata, never the whole body."""
    body, metadata = Path(body_path), Path(metadata_path)
    size = body.stat().st_size if body.exists() else 0
    confirmed, delivered, raw, issues = 0, 0, None, []
    if metadata.exists():
        with metadata.open("rb") as stream:
            while line := stream.readline(4097):
                if not line.endswith(b"\n"):
                    issues.append("incomplete_record")
                    break
                try:
                    record = json.loads(line)
                    value = record["bytes_written"]
                except (ValueError, TypeError, KeyError):
                    issues.append("invalid_record")
                    break
                if type(value) is not int or value <= confirmed:
                    issues.append("non_increasing_record")
                    break
                if value > size:
                    issues.append("metadata_exceeds_body")
                    break
                diagnostic = record.get("delivered", value)
                diagnostic_raw = record.get("raw")
                if (type(diagnostic) is not int or diagnostic < value or diagnostic < delivered or
                    (diagnostic_raw is not None and (type(diagnostic_raw) is not int or diagnostic_raw < 0))):
                    issues.append("invalid_diagnostics")
                    break
                confirmed = value
                delivered, raw = diagnostic, diagnostic_raw
    if size > confirmed:
        issues.append("unconfirmed_body_tail")
    if body.exists():
        with body.open("r+b") as stream:
            stream.truncate(confirmed)
    return {"body_bytes": size, "confirmed_bytes": confirmed, "confirmed_delivered": delivered,
            "confirmed_raw": raw, "discarded_tail": size-confirmed, "issues": issues}


class SiteWorker:
    """Baseline lifecycle with startup barrier and Windows parent-death containment."""
    def __init__(self, cafile=None):
        self.job, self.connection, self.process = Job(), None, None
        ctx = multiprocessing.get_context("spawn")
        parent, child = ctx.Pipe()
        self.connection = parent
        self.process = ctx.Process(target=_worker, args=(child, cafile))
        self.started = time.monotonic_ns()
        try:
            self.process.start()
            child.close()
            self.job.assign(self.process.pid)
            parent.send({"action": "boot"})
            if not parent.poll(10) or parent.recv().get("event") != "ready":
                raise RuntimeError("worker startup failed")
            self.ready = time.monotonic_ns()
        except BaseException:
            child.close()
            self.job.close()
            if self.process.pid is not None:
                if self.process.is_alive():
                    self.process.terminate()
                self.process.join(2)
            parent.close()
            raise

    def request(self, command, started, deadline, reserve):
        cutoff = deadline - round(reserve*NS)
        self.connection.send(command)
        events, last, headers = [], {}, {}
        while True:
            now = time.monotonic_ns()
            if now >= cutoff:
                called = time.monotonic_ns()
                self.process.terminate()
                signalled = bool(wait([self.process.sentinel], max(0, (deadline-time.monotonic_ns())/NS)))
                sentinel = time.monotonic_ns() if signalled else None
                if signalled:
                    self.process.join(0)
                joined = time.monotonic_ns()
                timely = signalled and sentinel < deadline and joined < deadline and not self.process.is_alive()
                if self.process.is_alive():
                    self.job.close()
                    self.process.join(2)
                return {**headers, **last, "reason": "timeout", "error_kind": "timeout", "forced": True,
                        "timely": timely, "started": started, "deadline": deadline, "terminate": called,
                        "sentinel": sentinel, "joined": joined, "events": events, "response_closed": False}
            ready = wait([self.connection, self.process.sentinel], (cutoff-now)/NS)
            if self.connection in ready:
                try:
                    msg = self.connection.recv()
                except EOFError:
                    msg = {"event": "eof"}
                if msg.get("id") != command["id"]:
                    raise RuntimeError("IPC sequence failure")
                if msg["event"] == "headers":
                    headers = msg
                elif msg["event"] == "block":
                    last = {"retained": msg["bytes_written"], "delivered": msg["delivered"], "raw": msg["raw"]}
                # Bounded diagnostic tail; JSONL carries all body commits.
                events.append(msg)
                if len(events) > 64:
                    del events[0]
                if msg["event"] == "result":
                    received = time.monotonic_ns()
                    return {**msg, "forced": False, "timely": received < deadline,
                            "started": started, "deadline": deadline, "received": received, "events": events}
            if self.process.sentinel in ready:
                self.process.join(0)
                return {**headers, **last, "forced": True, "reason": "worker_exit", "timely": False,
                        "error_kind": "worker_exit", "started": started, "deadline": deadline, "events": events}

    def close(self):
        closed = False
        try:
            if self.process.is_alive():
                self.connection.send({"action": "stop"})
                if self.connection.poll(2):
                    closed = self.connection.recv().get("client_closed", False)
                self.process.join(2)
        except (EOFError, OSError):
            pass
        finally:
            self.job.close()
            self.process.join(2)
            self.connection.close()
        if self.process.is_alive():
            raise RuntimeError("worker remains alive; cleanup blocked")
        result = {"alive": False, "client_closed": closed, "exitcode": self.process.exitcode}
        self.process.close()
        return result


class Executor:
    def __init__(self, site_url, *, limits=None, cafile=None, work_dir=None):
        self.limits = limits or Limits()
        self.state = Admission(site_url, self.limits)
        self.cafile = cafile
        self.folder = Path(tempfile.mkdtemp(prefix="candidate-", dir=work_dir or Path(__file__).parent))
        self.worker = None
        self.lock = threading.Lock()
        self.closed = False
        self.shutdowns = []
        self.resources = {}

    def _replace(self):
        if self.worker is not None:
            self.shutdowns.append(self.worker.close())
            self.worker = None
        self.worker = SiteWorker(self.cafile)

    def set_crawl_delay(self, host, seconds):
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("crawl delay")
        with self.lock:
            self.state.crawl[host] = max(self.state.crawl.get(host, 0), seconds)

    def restrict_host(self, host, reason):
        """Caller supplies confirmed CAPTCHA/access restriction; never bypass it."""
        if reason not in {"captcha", "access_restriction"}:
            raise ValueError("restriction")
        with self.lock:
            self.state.stopped[host] = reason

    def fetch(self, url, *, redirect_states=None):
        with self.lock:
            if self.closed:
                raise RuntimeError("executor closed")
            attempts, redirects, visited = [], [], set()
            reused_key = None
            current, retries = urldefrag(url)[0], 0
            reason = "complete"
            while True:
                host = host_of(current)
                key = resource_budget_key(current)
                budget = self.resources.setdefault(key, {"delivered": 0, "retained": 0, "uncertain": 0})
                remaining = self.limits.resource_base-budget["delivered"]-budget["uncertain"]
                if remaining <= 0:
                    reason = "resource_bytes"
                    break
                reason = self.state.check(current)
                if reason:
                    break
                # Worker startup consumes run time, but is not an HTTP attempt.
                if self.worker is None:
                    self._replace()
                reason = self.state.admit(current)
                if reason:
                    break
                started = time.monotonic_ns()
                target, deadline = deadlines(started, self.state.started, self.limits)
                ident = self.state.count
                body, meta = self.folder / f"{ident}.part", self.folder / f"{ident}.jsonl"
                command = {"action": "get", "id": ident, "url": current, "body": str(body), "metadata": str(meta),
                           "target": target, "block": self.limits.block, "resource_base": remaining,
                           "resource_retention": self.limits.resource_base-budget["retained"],
                           "run_remaining": self.limits.run_base-self.state.delivered-self.state.uncertain_reserved}
                result = self.worker.request(command, started, deadline, self.limits.reserve)
                # Anchor pacing to completion receipt as well as admission. This
                # conservative gap also covers variable spawn/IPC dispatch latency.
                self.state.last[host] = time.monotonic_ns()
                recovery = recover_prefix(body, meta)
                result.update(url=current, body=str(body), recovery=recovery)
                retained = recovery["confirmed_bytes"]
                delivered = max(result.get("delivered", 0), recovery["confirmed_delivered"])
                self.state.delivered += delivered
                self.state.retained += retained
                result["retained"] = retained
                result["delivered"] = delivered
                result["raw"] = max(result.get("raw", 0), recovery["confirmed_raw"] or 0)
                result["discarded"] = delivered-retained
                uncertain = self.limits.block if result["forced"] else 0
                if delivered > min(remaining, command["run_remaining"]):
                    uncertain = 0  # The crossing block is final; no subsequent read starts.
                uncertain = min(uncertain,
                                max(0, self.limits.resource_base + self.limits.block - budget["delivered"] - budget["uncertain"] - delivered),
                                max(0, self.limits.run_base + self.limits.block - self.state.delivered - self.state.uncertain_reserved))
                result["uncertain_decoded_upper"] = uncertain
                self.state.uncertain_reserved += result["uncertain_decoded_upper"]
                budget["delivered"] += delivered
                budget["retained"] += retained
                budget["uncertain"] += result["uncertain_decoded_upper"]
                result["resource_budget_key"] = key
                # A forced exit may interrupt one bounded decoded block before its
                # commit/ack. Reserve that upper bound; never label it measured.
                if recovery["issues"] and not result["forced"]:
                    result["timely"] = False  # Unexpected normal-result corruption: fail closed.
                attempts.append(result)
                if result["forced"]:
                    self.shutdowns.append(self.worker.close())
                    self.worker = None
                if not result["timely"] or result["reason"] == "worker_exit":
                    reason = "unsafe_worker_result"
                    self.closed = True  # No automatic recovery from a violated deadline/safety gate.
                    break
                self.state.observe(host, result)
                status = result.get("http_status")
                if host in self.state.stopped:
                    reason = self.state.stopped[host]
                    break
                if result["error_kind"] == "tls":
                    reason = "tls"
                    break
                if status in {301, 302, 303, 307, 308} and result.get("headers", {}).get("location"):
                    dest = urldefrag(urljoin(current, result["headers"]["location"]))[0]
                    redirects.append({"from": current, "to": dest, "status": status})
                    try:
                        outside = host_of(dest) not in self.state.hosts
                    except ValueError:
                        reason = "invalid_redirect"
                        break
                    if outside:
                        reason = "external_redirect"
                        break
                    visited.add(current)
                    dest_key = resource_budget_key(dest)
                    if dest in visited or (redirect_states is not None and dest_key in {resource_budget_key(item) for item in visited}):
                        reason = "redirect_loop"
                        break
                    if len(redirects) > 10:
                        reason = "redirect_limit"
                        break
                    if redirect_states is not None:
                        known = redirect_states.get(dest_key, {})
                        if known.get('state') in {'FETCHED', 'COMPLETED', 'TERMINAL_FAILURE'}:
                            reused_key = dest_key
                            reason = 'redirect_reused_failure' if known['state'] == 'TERMINAL_FAILURE' else 'redirect_reused'
                            break
                        if known.get('state') == 'IN_PROGRESS':
                            reused_key, reason = dest_key, 'redirect_in_progress'
                            break
                    current, retries = dest, 0
                    continue
                retryable = (status not in {401, 403, 404} and result["error_kind"] != "tls" and
                             (status == 429 or (status is not None and 500 <= status <= 599) or result["error_kind"] == "timeout"))
                if retryable and retries < 2:
                    retries += 1
                    self.state.ready[host] = max(self.state.ready.get(host, 0), time.monotonic_ns() + retries*NS)
                    continue
                reason = "retry_exhausted" if retryable else result["reason"]
                break
            complete = bool(attempts and (reason in {"complete", "redirect_reused"} or
                (reason == "redirect_reused_failure" and
                 redirect_states[reused_key].get('result', {}).get('complete', False))))
            if not complete:
                self.state.partial = True
            result = {"url": url, "attempts": attempts, "redirects": redirects, "reason": reason, "complete": complete,
                      "run_status": "PARTIAL" if self.state.partial else "COMPLETE"}
            if reused_key is not None:
                result['reused_key'] = reused_key
            return result

    def close(self):
        with self.lock:
            if self.worker is not None:
                self.shutdowns.append(self.worker.close())
                self.worker = None
            self.closed = True
            shutil.rmtree(self.folder)
            return {"temporary_removed": not self.folder.exists(), "shutdowns": self.shutdowns}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
