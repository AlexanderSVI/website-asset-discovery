"""Local safety verification only. Run with -B; no public requests or browsers.

Evidence is emitted as JSON lines. Slow cases use actual 15/30-second settings.
Clock-controlled tests are explicitly named. Parent-crash tests have an external
Job supervisor and retained OS worker handles, independent of active_children().
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import multiprocessing
from pathlib import Path
import platform
import socket
import ssl
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest

from executor import Admission, Executor, Limits, NS, SiteWorker, boundary, deadlines, host_of, recover_prefix, retry_after
from windows_job import Job, open_process_for_wait

EVENTS = []
LOCK = threading.Lock()
COUNTS = {}
ACTIVE = 0
MAX_ACTIVE = 0


def emit(kind, **data):
    print(json.dumps({"kind": kind, **data}), flush=True)


class Fixture(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_GET(self):
        global ACTIVE, MAX_ACTIVE
        with LOCK:
            ACTIVE += 1
            MAX_ACTIVE = max(MAX_ACTIVE, ACTIVE)
            COUNTS[self.path] = COUNTS.get(self.path, 0) + 1
            count = COUNTS[self.path]
            EVENTS.append({"path": self.path, "event": "start", "ns": time.monotonic_ns()})
        try:
            self.serve(count)
        except (ConnectionError, OSError):
            pass
        finally:
            with LOCK:
                ACTIVE -= 1
                EVENTS.append({"path": self.path, "event": "end", "ns": time.monotonic_ns()})

    def serve(self, count):
        path = self.path.split("?")[0]
        status, headers, length = 200, {}, 8
        data = b"abcdefgh"
        if path.startswith("/status/"):
            status = int(path.split("/")[2])
        elif path.startswith("/retry/"):
            status = 503 if count < 3 else 200
        elif path.startswith("/retry-after/"):
            status = 503 if count == 1 else 200
            headers["Retry-After"] = "2"
        elif path.startswith("/429/"):
            status = 429
            headers["Retry-After"] = "1"
        elif path.startswith("/redirect/"):
            n = int(path.split("/")[2])
            if n:
                status, headers = 302, {"Location": f"/redirect/{n-1}"}
        elif path == "/loop":
            status, headers = 302, {"Location": "/loop"}
        elif path == "/external":
            status, headers = 302, {"Location": "http://example.invalid/uninspected"}
        elif path.startswith("/bytes/"):
            length = int(path.split("/")[2])
            data = None
        elif path.startswith("/gzip/"):
            data = gzip.compress(b"z" * int(path.split("/")[2]))
            length = len(data)
            headers["Content-Encoding"] = "gzip"
        elif path.startswith("/hang/") or path.startswith("/target/"):
            length = 12
            data = b"abcd"
        self.send_response(status)
        self.send_header("Content-Length", str(length))
        self.send_header("Connection", "close")
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if data is None:
            chunk = b"x" * 65536
            while length:
                part = chunk[:min(length, len(chunk))]
                self.wfile.write(part)
                length -= len(part)
        else:
            self.wfile.write(data)
        self.wfile.flush()
        if path.startswith("/target/"):
            time.sleep(16)
            self.wfile.write(b"efgh")
            self.wfile.flush()
        if path.startswith("/hang/") or path.startswith("/target/"):
            self.connection.settimeout(35)
            try:
                event = "EOF" if self.connection.recv(1) == b"" else "unexpected_input"
            except (ConnectionResetError, ConnectionAbortedError):
                event = "RESET"
            except socket.timeout:
                event = "observation_timeout"
            with LOCK:
                EVENTS.append({"path": self.path, "event": event, "ns": time.monotonic_ns()})


class Clock:
    def __init__(self):
        self.now = 0
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.now += max(1, round(seconds*NS))


def crash_controller(connection, url, folder):
    # Outer supervisor assigns this controller to its own kill-on-close job
    # before opening the startup barrier. It retains the worker OS handle.
    if connection.recv() != "go":
        return
    engine = Executor(url, work_dir=folder, limits=replace(Limits(), block=4))
    engine._replace()
    connection.send({"pid": engine.worker.process.pid, "folder": str(engine.folder)})
    engine.fetch(url + "/hang/parent-crash")
    engine.close()


def certificate(folder):
    # Existing dependency only; generate an ephemeral test identity, not real credentials.
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "WAD synthetic loopback fixture")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
            .not_valid_after(now+timedelta(hours=1)).add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    certpath, keypath = folder/"synthetic-cert.pem", folder/"synthetic-key.pem"
    certpath.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keypath.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return certpath, keypath


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="verification-", dir=Path(__file__).parent)
        cls.folder = Path(cls.temp.name)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)
        cls.temp.cleanup()
        emit("fixture_cleanup", thread_alive=cls.thread.is_alive(), temporary_removed=not cls.folder.exists(),
             remaining_children=[p.pid for p in multiprocessing.active_children()])

    def engine(self, **kwargs):
        return Executor(self.base, work_dir=self.folder, **kwargs)

    def check_attempt(self, result, retained=8):
        self.assertEqual(len(result["attempts"]), 1, result)
        a = result["attempts"][0]
        self.assertTrue(a["timely"], a)
        self.assertEqual(a["retained"], retained)
        self.assertEqual(Path(a["body"]).stat().st_size, retained)
        self.assertFalse(a["recovery"]["issues"])
        return a

    def test_01_runtime_lifecycle(self):
        emit("runtime", executable=sys.executable, python=sys.version, windows=platform.win32_ver(),
             os_build=platform.version(), httpx=__import__("httpx").__version__, spawn=multiprocessing.get_all_start_methods())
        e = self.engine()
        try:
            a = self.check_attempt(e.fetch(self.base+"/normal/runtime"))
            self.assertEqual(Path(a["body"]).read_bytes(), b"abcdefgh")
            self.assertTrue(a["response_closed"])
            self.assertTrue(e.worker.process.is_alive())
            emit("spawn_loopback", startup_ns=e.worker.ready-e.worker.started, pid=e.worker.process.pid)
        finally:
            cleanup = e.close()
        self.assertTrue(cleanup["temporary_removed"])
        self.assertTrue(cleanup["shutdowns"][-1]["client_closed"])
        self.assertFalse(multiprocessing.active_children())
        emit("normal_cleanup", **cleanup)

    def test_02_calibrate_termination(self):
        samples = []
        for i in range(8):
            worker = SiteWorker()
            body, meta = self.folder/f"cal-{i}.part", self.folder/f"cal-{i}.jsonl"
            start = time.monotonic_ns()
            cmd = {"action": "get", "id": i, "url": self.base+f"/hang/cal-{i}", "body": str(body),
                   "metadata": str(meta), "target": start+150_000_000, "block": 4,
                   "resource_base": 100, "run_remaining": 1000}
            try:
                r = worker.request(cmd, start, start+750_000_000, .25)
                self.assertTrue(r["forced"] and r["timely"], r)
                samples.append(r["sentinel"]-r["terminate"])
            finally:
                worker.close()
        emit("reserve_calibration", samples_ns=samples, maximum_ns=max(samples), provisional_reserve_ns=250_000_000,
             interpretation="observed samples, not a worst-case guarantee")

    def test_03_sequential_twenty_and_rate(self):
        global MAX_ACTIVE
        with LOCK:
            MAX_ACTIVE = 0
        with self.engine() as e:
            for i in range(20):
                a = self.check_attempt(e.fetch(self.base+f"/normal/series-{i}"))
                self.assertTrue(a["response_closed"])
            self.assertEqual(e.state.count, 20)
        starts = [v["ns"] for v in EVENTS if v["event"] == "start" and v["path"].startswith("/normal/series-")]
        gaps = [b-a for a,b in zip(starts, starts[1:])]
        self.assertEqual(len(starts), 20)
        self.assertGreaterEqual(min(gaps), NS)
        self.assertEqual(MAX_ACTIVE, 1)
        emit("sequential_twenty", attempts=20, fixture_max_active=MAX_ACTIVE, minimum_start_gap_ns=min(gaps))

    def test_04_concurrent_callers_serialized(self):
        with self.engine() as e:
            results, errors = [], []
            def fetch(i):
                try:
                    results.append(e.fetch(self.base+f"/normal/thread-{i}"))
                except BaseException as error:
                    errors.append(repr(error))
            threads = [threading.Thread(target=fetch, args=(i,)) for i in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(15)
            self.assertFalse(any(t.is_alive() for t in threads))
            self.assertFalse(errors)
            self.assertEqual(len(results), 3)
            self.assertEqual(e.state.count, 3)

    def test_05_target_real_15_30(self):
        with self.engine(limits=replace(Limits(), block=4)) as e:
            r = e.fetch(self.base+"/target/real")
            a = self.check_attempt(r)
            self.assertEqual(r["reason"], "attempt_target")
            reads = [x["time_ns"]-a["started"] for x in a["events"] if x["event"] == "read_start"]
            self.assertEqual(len(reads), 2)
            self.assertTrue(all(v < 15*NS for v in reads))
            self.assertGreaterEqual(a["received"]-a["started"], 15*NS)
            self.assertTrue(a["response_closed"])
            emit("real_target_crossing", read_starts_ns=reads, completed_ns=a["received"]-a["started"], retained=a["retained"])

    def test_06_three_real_stalls_and_replacement(self):
        # Each run has one eligible attempt, to measure exactly three stalls rather than automatic retry chains.
        for i in range(3):
            with self.engine(limits=replace(Limits(), block=4, attempts=1)) as e:
                r = e.fetch(self.base+f"/hang/production-{i}")
                a = self.check_attempt(r, 4)
                self.assertTrue(a["forced"])
                self.assertFalse(a["response_closed"])
                self.assertEqual(Path(a["body"]).read_bytes(), b"abcd")
                self.assertIsNone(e.worker)
                self.assertEqual(e.state.uncertain_reserved,4)
                self.assertEqual(a["uncertain_decoded_upper"],4)
                self.assertLess(a["joined"], a["deadline"])
                emit("real_stall", repetition=i+1, elapsed_ns=a["joined"]-a["started"],
                     terminate_latency_ns=a["sentinel"]-a["terminate"], margin_ns=a["deadline"]-a["joined"],
                     retained=a["retained"], normal_python_cleanup=False)
            with self.engine() as replacement:
                self.check_attempt(replacement.fetch(self.base+f"/normal/replacement-{i}"))
            self.assertFalse(multiprocessing.active_children())

    def test_07_production_run_constants_clock_controlled(self):
        c, limits = Clock(), Limits()
        s = Admission(self.base, limits, c, c.sleep)
        self.assertEqual((limits.run_target, limits.run_maximum, limits.attempt_target, limits.attempt_maximum), (1800,1830,15,30))
        c.now = 1799*NS
        self.assertIsNone(s.admit(self.base))
        target, maximum = deadlines(c.now, s.started, limits)
        self.assertEqual((target, maximum), (1814*NS,1829*NS))
        c.now = 1800*NS
        self.assertEqual(s.admit(self.base), "run_target")
        self.assertEqual(deadlines(1810*NS,0,limits)[1],1830*NS)
        emit("simulated_production_run", target_ns=1800*NS, maximum_ns=1830*NS, new_attempt_at_target="rejected")

    def test_08_short_run_earliest_deadline_process(self):
        limits = replace(Limits(), run_target=2, run_maximum=3, block=4)
        with self.engine(limits=limits) as e:
            r = e.fetch(self.base+"/hang/short-run")
            a = self.check_attempt(r, 4)
            self.assertTrue(a["forced"])
            self.assertEqual(a["deadline"],e.state.started+3*NS)
            self.assertLess(a["joined"], e.state.started+3*NS)
            self.assertEqual(r["reason"],"run_target")
            emit("short_run_actual_process", elapsed_from_run_ns=a["joined"]-e.state.started, maximum_ns=3*NS)

    def test_09_production_resource_budgets(self):
        for size in (10_000_000,10_065_536,10_065_537):
            with self.engine() as e:
                a = self.check_attempt(e.fetch(self.base+f"/bytes/{size}?resource"),10_000_000)
                self.assertLessEqual(a["delivered"],10_065_536)
                self.assertEqual(e.state.retained,10_000_000)
                self.assertEqual(a["discarded"],a["delivered"]-10_000_000)
                # Raw bytes are diagnostic: HTTPX may buffer beyond yielded blocks.
                self.assertIsInstance(a["raw"], int)
                self.assertGreaterEqual(a["raw"], 0)
                if size == 10_000_000:
                    self.assertEqual(a["delivered"], size)
                    self.assertEqual(a["reason"], "complete")
                else:
                    self.assertEqual(a["reason"], "byte_limit")
                emit("resource_boundary", response_bytes=size, delivered=a["delivered"], retained=a["retained"], raw=a["raw"],reason=a["reason"])

    def test_10_exact_base_extra_block(self):
        for size in (65536,65540,131072,131073):
            with self.engine(limits=replace(Limits(),resource_base=65536)) as e:
                a = self.check_attempt(e.fetch(self.base+f"/bytes/{size}?extra"),65536)
                self.assertEqual(a["delivered"],min(size,131072))
                self.assertLessEqual(a["discarded"],65536)
                emit("exact_base_allowance", size=size,delivered=a["delivered"],discarded=a["discarded"])

    def test_11_production_run_bytes(self):
        with self.engine() as e:
            for i in range(10):
                a = self.check_attempt(e.fetch(self.base+f"/bytes/10000000?run-{i}"),10_000_000)
            self.assertEqual(e.state.delivered,100_000_000)
            self.assertEqual(e.state.retained,100_000_000)
            self.assertEqual(e.fetch(self.base+"/normal/after-run-bytes")["reason"],"run_bytes")
            self.assertEqual(e.state.count,10)
            emit("production_run_bytes",delivered=e.state.delivered,retained=e.state.retained,attempts=e.state.count)

    def test_12_run_allowance_and_gzip(self):
        with self.engine(limits=replace(Limits(),run_base=65536)) as e:
            a = self.check_attempt(e.fetch(self.base+"/bytes/131073?run-extra"),65536)
            self.assertEqual(a["delivered"],131072)
            self.assertEqual(a["discarded"],65536)
            self.assertEqual(e.fetch(self.base+"/normal/run-full")["reason"],"run_bytes")
        # Actual production run base at a final-attempt boundary, after nine real resources.
        with self.engine() as e:
            for i in range(9):
                self.check_attempt(e.fetch(self.base+f"/bytes/10000000?run-allow-{i}"),10_000_000)
            a = self.check_attempt(e.fetch(self.base+"/bytes/10065536?run-last"),10_000_000)
            self.assertLessEqual(e.state.delivered,100_065_536)
            self.assertGreater(e.state.delivered,100_000_000)
            self.assertEqual(e.state.retained,100_000_000)
            self.assertEqual(e.fetch(self.base+"/normal/blocked-run-allow")["attempts"],[])
            emit("production_run_allowance",delivered=e.state.delivered,retained=e.state.retained)
        with self.engine() as e:
            a = self.check_attempt(e.fetch(self.base+"/gzip/1000000"),1_000_000)
            self.assertEqual(a["delivered"],1_000_000)
            self.assertEqual(a["raw"],len(gzip.compress(b"z"*1_000_000)))
            self.assertEqual(hashlib.sha256(Path(a["body"]).read_bytes()).digest(),hashlib.sha256(b"z"*1_000_000).digest())
            emit("gzip_counters",decoded=a["delivered"],raw=a["raw"])

    def test_13_bounded_streaming_memory(self):
        # In-process decoder observation avoids parent-only RSS claims about a worker.
        from executor import _decoded_blocks
        import httpx
        class Stream(httpx.SyncByteStream):
            def __iter__(self):
                compressor = __import__("zlib").compressobj(wbits=31)
                for _ in range(1600):
                    encoded = compressor.compress(b"x"*65536)
                    if encoded:
                        yield encoded
                yield compressor.flush()
        response = httpx.Response(200,headers={"content-encoding":"gzip"},stream=Stream())
        tracemalloc.start()
        total = 0
        largest = 0
        try:
            for block in _decoded_blocks(response,65536,time.monotonic_ns()+30*NS,lambda *a,**k:None):
                total += len(block)
                largest = max(largest,len(block))
            _,peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
            response.close()
        self.assertEqual(total,104_857_600)
        self.assertLessEqual(largest,65536)
        self.assertLess(peak,4_000_000)
        emit("decoder_memory",decoded_total=total,largest_block=largest,tracemalloc_peak=peak,
             scope="Python allocations in bounded decoder, not full OS RSS")

    def test_14_redirects(self):
        for path,expected,count in (("/redirect/1","complete",2),("/loop","redirect_loop",1),
                                    ("/redirect/10","complete",11),("/redirect/11","redirect_limit",11),
                                    ("/external","external_redirect",1)):
            with self.engine() as e:
                r = e.fetch(self.base+path)
                self.assertEqual(r["reason"],expected,r)
                self.assertEqual(e.state.count,count)
                self.assertEqual(len(r["attempts"]),count)
                emit("redirect",path=path,reason=r["reason"],attempts=count,chain=r["redirects"])

    def test_15_retry_and_stops(self):
        for path,expected,count in (("/retry/success","complete",3),("/status/503","three_5xx",3),
                                    ("/429/stop","repeated_429",2)):
            with self.engine() as e:
                r = e.fetch(self.base+path)
                self.assertEqual(r["reason"],expected,r)
                self.assertEqual(e.state.count,count)
                starts = [a["started"] for a in r["attempts"]]
                if count == 3:
                    self.assertGreaterEqual(starts[1]-starts[0],NS)
                    self.assertGreaterEqual(starts[2]-starts[1],2*NS)
                if expected != "complete":
                    self.assertEqual(e.fetch(self.base+"/normal/never-probe")["attempts"],[])
                    self.assertEqual(e.state.count,count)
                emit("retry_stop",path=path,attempts=count,reason=r["reason"],starts=starts)

    def test_16_retry_after_and_crawl_delay(self):
        with self.engine() as e:
            e.set_crawl_delay("127.0.0.1",1.5)
            a = e.fetch(self.base+"/normal/crawl-a")
            b = e.fetch(self.base+"/normal/crawl-b")
            self.assertGreaterEqual(b["attempts"][0]["started"]-a["attempts"][0]["received"],1_500_000_000)
            r = e.fetch(self.base+"/retry-after/once")
            self.assertEqual(len(r["attempts"]),2)
            gap = r["attempts"][1]["started"]-r["attempts"][0]["received"]
            self.assertGreaterEqual(gap,2*NS)
            emit("crawl_retry_after",crawl_delay=1.5,retry_after_gap_ns=gap)
        self.assertEqual(retry_after("3"),3)
        date = datetime.now(timezone.utc)+timedelta(seconds=5)
        self.assertGreater(retry_after(format_datetime(date)),3)
        self.assertEqual(retry_after("bad"),0)
        self.assertEqual(retry_after("9"*512),float("inf"))

    def test_17_three_timeouts(self):
        with self.engine(limits=replace(Limits(),attempt_target=.15,attempt_maximum=.75,block=4)) as e:
            r = e.fetch(self.base+"/hang/three-timeouts")
            self.assertEqual(r["reason"],"three_timeouts",r)
            self.assertEqual(len(r["attempts"]),3)
            self.assertEqual(e.fetch(self.base+"/normal/timeout-no-probe")["attempts"],[])
            self.assertEqual(e.state.count,3)
            emit("three_timeouts",attempts=3,workers_replaced=len(e.shutdowns),reason=r["reason"])

    def test_18_no_retry_restrictions(self):
        with self.engine() as e:
            for status in (401,403,404):
                r = e.fetch(self.base+f"/status/{status}")
                self.assertEqual(len(r["attempts"]),1)
            e.restrict_host("127.0.0.1","captcha")
            self.assertEqual(e.fetch(self.base+"/normal/captcha-blocked")["attempts"],[])
            self.assertEqual(e.state.count,3)

    def test_19_hosts(self):
        self.assertEqual(boundary("https://sub.example.invalid/a"),{"sub.example.invalid","www.sub.example.invalid"})
        self.assertEqual(boundary("http://www.example.invalid"),{"www.example.invalid","example.invalid"})
        for url in ("file:///etc/passwd","http://user:pass@127.0.0.1", "http://127.0.0.1:bad"):
            with self.assertRaises(ValueError):
                host_of(url)
        c = Clock()
        s = Admission("https://sub.example.invalid",Limits(),c,c.sleep)
        for host in ("sub.example.invalid","www.sub.example.invalid"):
            self.assertIsNone(s.admit("http://"+host))
        for host in ("example.invalid","sibling.example.invalid","deep.sub.example.invalid"):
            self.assertEqual(s.admit("https://"+host),"host_boundary")
        with self.engine() as e:
            self.assertEqual(e.fetch("http://example.invalid")["attempts"],[])
            self.assertEqual(e.state.count,0)

    def test_20_exact_1000_clock_controlled_and_dispatch(self):
        c=Clock()
        s=Admission(self.base,Limits(),c,c.sleep)
        for i in range(1000):
            self.assertIsNone(s.admit(self.base),i)
        self.assertEqual(s.count,1000)
        self.assertEqual(s.admit(self.base),"request_limit")
        self.assertEqual(s.count,1000)
        # One actual loopback dispatch at 1000 and no 1001; prior 999 are injected state.
        with self.engine() as e:
            e.state.count=999
            self.check_attempt(e.fetch(self.base+"/normal/1000th"))
            self.assertEqual(e.state.count,1000)
            self.assertEqual(e.fetch(self.base+"/normal/1001st")["attempts"],[])
            self.assertEqual(e.state.count,1000)
        self.assertEqual(COUNTS.get("/normal/1000th"),1)
        self.assertIsNone(COUNTS.get("/normal/1001st"))
        emit("accounting_1000",simulated_admissions=1000,simulated_rejection=1001,
             real_boundary_dispatch=1000,real_1001_arrivals=0,prior_999="injected for boundary integration only")

    def test_21_tls(self):
        cert,key=certificate(self.folder)
        server=ThreadingHTTPServer(("127.0.0.1",0),Fixture)
        ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert,key)
        server.socket=ctx.wrap_socket(server.socket,server_side=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        url=f"https://127.0.0.1:{server.server_port}"
        try:
            with Executor(url,cafile=str(cert),work_dir=self.folder) as e:
                a=self.check_attempt(e.fetch(url+"/normal/tls-valid"))
                self.assertTrue(a["response_closed"])
            with Executor(url,work_dir=self.folder) as e:
                r=e.fetch(url+"/normal/tls-invalid")
                self.assertEqual(r["reason"],"tls",r)
                self.assertEqual(len(r["attempts"]),1)
                self.assertEqual(e.state.count,1)
            emit("tls",trusted_local_certificate="PASS",untrusted_certificate="rejected",certificate_retries=0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
            cert.unlink()
            key.unlink()

    def test_22_recovery_corruption(self):
        cases=[(b"abcdefgh",b'{"bytes_written":4}\n{"bytes_written":8}\n',8,[]),
               (b"abcdef",b'{"bytes_written":4}\n',4,["unconfirmed_body_tail"]),
               (b"abcdefgh",b'{"bytes_written":4}\n{"bytes',4,["incomplete_record","unconfirmed_body_tail"]),
               (b"abcd",b'{"bytes_written":4}\n{"bytes_written":8}\n',4,["metadata_exceeds_body"]),
               (b"abcdefgh",b'{"bytes_written":4}\n{"bytes_written":4}\n',4,["non_increasing_record","unconfirmed_body_tail"]),
               (b"abcd",b'{"bytes_written":true}\n',0,["non_increasing_record","unconfirmed_body_tail"]),
               (b"abcd",b'not-json\n',0,["invalid_record","unconfirmed_body_tail"])]
        for i,(data,meta,expected,issues) in enumerate(cases):
            bp,mp=self.folder/f"corrupt-{i}.part",self.folder/f"corrupt-{i}.jsonl"
            bp.write_bytes(data)
            mp.write_bytes(meta)
            r=recover_prefix(bp,mp)
            self.assertEqual(r["confirmed_bytes"],expected)
            self.assertEqual(r["issues"],issues)
            self.assertEqual(bp.read_bytes(),data[:expected])
        self.assertEqual(recover_prefix(self.folder/"absent",self.folder/"absent-meta")["confirmed_bytes"],0)
        emit("synthetic_recovery",cases=len(cases)+1,result="PASS",power_loss_durability="not claimed")

    def test_23_parent_crash_external_supervisor(self):
        ctx=multiprocessing.get_context("spawn")
        parent,child=ctx.Pipe()
        controller=ctx.Process(target=crash_controller,args=(child,self.base,str(self.folder)))
        supervisor=Job()
        k,handle=None,None
        try:
            controller.start()
            child.close()
            supervisor.assign(controller.pid)
            parent.send("go")
            self.assertTrue(parent.poll(15))
            info=parent.recv()
            k,handle=open_process_for_wait(info["pid"])
            end=time.monotonic()+10
            while time.monotonic()<end and not COUNTS.get("/hang/parent-crash"):
                time.sleep(.02)
            self.assertEqual(COUNTS.get("/hang/parent-crash"),1)
            called=time.monotonic_ns()
            controller.terminate()
            controller.join(3)
            self.assertFalse(controller.is_alive())
            # The supervisor job is still OPEN. Worker exit must come from the
            # candidate controller job's handle closing at controller death.
            status=k.WaitForSingleObject(handle,3000)
            observed=time.monotonic_ns()
            self.assertEqual(status,0,"worker survived parent crash")
            emit("parent_crash",controller_pid=controller.pid,worker_pid=info["pid"],worker_exit_observed_ns=observed-called,
                 supervisor_job_still_open=True,worker_wait_status=status)
        finally:
            supervisor.close()  # Guaranteed bounded external containment even on assertion failure.
            if controller.pid is not None:
                controller.join(3)
                if controller.is_alive():
                    controller.terminate()
                    controller.join(3)
                controller.close()
            if handle:
                k.CloseHandle(handle)
            parent.close()
            child.close()
        self.assertFalse(multiprocessing.active_children())

    def test_24_failed_startup_cleanup(self):
        with self.assertRaises((RuntimeError,EOFError)):
            SiteWorker(str(self.folder/"missing-ca.pem"))
        self.assertFalse(multiprocessing.active_children())

    def test_25_crash_accounting_boundaries(self):
        c=Clock()
        s=Admission(self.base,Limits(),c,c.sleep)
        s.delivered=99_934_464
        s.uncertain_reserved=65_536
        self.assertEqual(s.check(self.base),"run_bytes")
        self.assertEqual(s.count,0)
        s.delivered=0
        s.uncertain_reserved=0
        s.observe("127.0.0.1",{"http_status":429,"headers":{"retry-after":"9"*512}})
        self.assertEqual(s.admit(self.base),"run_target")
        self.assertEqual(s.count,0)
        bp,mp=self.folder/"diagnostic.part",self.folder/"diagnostic.jsonl"
        bp.write_bytes(b"abcdefgh")
        mp.write_text('{"bytes_written":4,"delivered":4,"raw":4}\n{"bytes_written":8,"delivered":7}\n')
        r=recover_prefix(bp,mp)
        self.assertEqual(r["confirmed_bytes"],4)
        self.assertEqual(r["confirmed_delivered"],4)
        self.assertEqual(r["issues"],["invalid_diagnostics","unconfirmed_body_tail"])
        emit("crash_accounting",known_decoded=99_934_464,uncertain_upper_bound=65_536,
             admission="blocked at combined run base",diagnostic_corruption="rejected")

    def test_26_overrides_cannot_raise_safety_limits(self):
        defaults=Limits()
        for name in ("attempt_target","attempt_maximum","run_target","run_maximum",
                     "resource_base","run_base","block","attempts"):
            with self.assertRaises(ValueError,msg=name):
                replace(defaults,**{name:getattr(defaults,name)+1})
        for name in ("block","attempts","resource_base","run_base"):
            with self.assertRaises(ValueError,msg=name):
                replace(defaults,**{name:1.5})
        for value in (float("inf"),float("nan"),0,-1):
            with self.assertRaises(ValueError):
                replace(defaults,attempt_target=value)
        self.assertEqual(defaults,Limits())
        replace(defaults,attempt_target=.15,attempt_maximum=.75,run_target=2,run_maximum=3,block=4)
        emit("configuration_limits",higher_limits="rejected",invalid_types="rejected",shortened_test_limits="accepted")

    def test_27_repeated_403_requires_access_review(self):
        with self.engine() as e:
            first=e.fetch(self.base+"/status/403?page-a")
            self.assertEqual(len(first["attempts"]),1)
            repeated=e.fetch(self.base+"/status/403?page-a")
            self.assertEqual(len(repeated["attempts"]),1)
            second=e.fetch(self.base+"/status/403?page-b")
            self.assertEqual(len(second["attempts"]),1)
            self.assertEqual(second["reason"],"access_review_required")
            self.assertEqual(e.fetch(self.base+"/normal/access-probe-forbidden")["attempts"],[])
            self.assertEqual(e.state.count,3)
            emit("repeated_403",distinct_forbidden_urls=2,automatic_probes=0,reason=second["reason"],
                 section_assessment="future caller; no section inferred")

    def test_28_resource_keys(self):
        from executor import resource_budget_key
        self.assertEqual(resource_budget_key('HTTP://Example.COM:80/A?x=1&UTM_source=a&x=2&gClId=b#frag'),
                         'http://example.com/A?x=1&x=2')
        self.assertEqual(resource_budget_key('http://example.com/A?x=1&x=2'),
                         resource_budget_key('http://example.com/A?x=1&mtm_test=z&x=2'))
        self.assertNotEqual(resource_budget_key('http://example.com/?ref=one'), resource_budget_key('http://example.com/?ref=two'))
        self.assertEqual(resource_budget_key('http://example.com/%41/?q=a%2Fb&q=z&fbclid=a'), 'http://example.com/%41/?q=a%2Fb&q=z')

    def test_29_cumulative_retries_and_fetch(self):
        from executor import resource_budget_key
        with self.engine(limits=replace(Limits(),resource_base=12,block=4)) as e:
            url=self.base+'/retry/cumulative'
            r=e.fetch(url)
            self.assertEqual(len(r['attempts']),2)
            self.assertEqual(r['reason'],'resource_bytes')
            b=e.resources[resource_budget_key(url)]
            self.assertEqual(b['delivered'],16)
            self.assertEqual(b['retained'],12)
            self.assertEqual(e.fetch(url+'?utm_source=test')['attempts'],[])
            other=e.fetch(self.base+'/normal/independent')
            self.assertEqual(len(other['attempts']),1)
            self.assertEqual(e.state.delivered,24)

    def test_30_redirect_resource_keys(self):
        from executor import resource_budget_key
        with self.engine() as e:
            r=e.fetch(self.base+'/redirect/1')
            self.assertEqual(len(r['attempts']),2)
            self.assertEqual(e.resources[resource_budget_key(self.base+'/redirect/1')]['delivered'],8)
            self.assertEqual(e.resources[resource_budget_key(self.base+'/redirect/0')]['delivered'],8)
            self.assertEqual(e.state.count,2)

    def test_31_forced_resource_reservation(self):
        from executor import resource_budget_key
        limits=replace(Limits(),resource_base=12,block=4,attempt_target=.15,attempt_maximum=.75,attempts=1)
        with self.engine(limits=limits) as e:
            url=self.base+'/hang/resource-reservation'
            r=e.fetch(url)
            a=r['attempts'][0]
            b=e.resources[resource_budget_key(url)]
            self.assertEqual((a['delivered'],a['uncertain_decoded_upper']),(4,4))
            self.assertEqual((b['delivered'],b['retained'],b['uncertain']),(4,4,4))
            self.assertEqual((e.state.delivered,e.state.uncertain_reserved),(4,4))


if __name__ == "__main__":
    multiprocessing.freeze_support()
    unittest.main(verbosity=2)
