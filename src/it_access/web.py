"""Local request lab: a browser view over the existing intake services."""
import copy
from dataclasses import replace
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import parse_qs, urlsplit
import uuid

from .providers import ProviderConfig, ProviderError
from .runner import BusyError, WriterLock, capture_settings, run_jobs
from .state import StateError, StateStore


ASSETS = Path(__file__).with_name("web_assets")
_PUBLIC = ("document_id", "source_sha256", "attempt_id", "revision_id", "status", "owner", "next_action", "response_source", "extraction", "error", "created_at", "finished_at")


class WebError(ValueError):
    def __init__(self, status, message):
        super().__init__(message);self.status = status


def validate_browser_origin(value):
    if value is None:return None
    try:
        parsed = urlsplit(value)
        if (any(char.isspace() for char in value) or parsed.scheme not in ("http","https")
                or not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("","/") or parsed.query or parsed.fragment or "*" in value):
            raise ValueError("invalid origin")
        parsed.port
    except ValueError as error:
        raise StateError("Browser origin must be an HTTP(S) origin without credentials, a path, query or fragment.") from error
    return parsed.scheme + "://" + parsed.netloc


def is_local_request(host, origin, port, browser_origin=None):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {"http://" + item for item in hosts}
    if browser_origin:
        hosts.add(urlsplit(browser_origin).netloc)
        origins.add(browser_origin)
    return host in hosts and (origin is None or origin in origins)


class DemoApplication:
    def __init__(self, input_dir, database, mock_dir, config=None, *, browser_origin=None):
        self.browser_origin = validate_browser_origin(browser_origin)
        self.input_dir = Path(input_dir).resolve()
        if not self.input_dir.is_dir():raise StateError("The input directory does not exist.")
        self.database = Path(database).resolve()
        self.mock_dir = Path(mock_dir).resolve()
        self.config = config or ProviderConfig()
        self.csrf_token = secrets.token_urlsafe(32)
        self._jobs = {};self._threads = [];self._mutex = threading.Lock()

    def _path(self, document_id):
        if not isinstance(document_id, str) or Path(document_id).name != document_id or not document_id.endswith(".json"):
            raise WebError(404, "Choose a JSON file from the available requests.")
        path = (self.input_dir / document_id).resolve()
        if path.parent != self.input_dir or not path.is_file():
            raise WebError(404, "The selected request file is not available.")
        return path

    @staticmethod
    def _source(document_id, raw):
        raw_text = raw.decode("utf-8", errors="replace")
        try:
            data = json.loads(raw.decode("utf-8"))
            message = data.get("message", "") if isinstance(data, dict) else ""
            if not isinstance(message, str):message = ""
        except (ValueError, UnicodeError):data = None;message = ""
        metadata = data if isinstance(data, dict) else {}
        return {"document_id":document_id,"source_sha256":hashlib.sha256(raw).hexdigest(),
                "message":message,"raw_text":raw_text,"raw_json":data,
                "subject":metadata.get("subject") if isinstance(metadata.get("subject"), str) else "Untitled request",
                "channel":metadata.get("source_channel") if isinstance(metadata.get("source_channel"), str) else "export"}

    def source(self, document_id):
        return self._source(document_id, self._path(document_id).read_bytes())

    def files(self):
        result = []
        for path in sorted(self.input_dir.glob("*.json")):
            try:source = self.source(path.name)
            except (WebError, OSError):continue
            result.append({key:source[key] for key in ("document_id", "subject", "channel")})
        return result

    def start(self, document_id, provider, source_sha256, *, retry=False):
        if provider not in ("mock", "openai") or not isinstance(retry, bool):raise WebError(400, "Choose mock or live processing.")
        raw = self._path(document_id).read_bytes()
        if not isinstance(source_sha256, str) or len(source_sha256) != 64:raise WebError(400, "Reload the selected source before processing.")
        if hashlib.sha256(raw).hexdigest() != source_sha256:raise WebError(409, "This file changed. Select it again to load the latest input.")
        settings, adapter = capture_settings(replace(self.config, provider=provider), self.mock_dir, 1, 0)
        admission = WriterLock(self.database)
        admission.__enter__()
        identifier = uuid.uuid4().hex
        try:
            with self._mutex:
                completed = [key for key, value in self._jobs.items() if value["state"] != "processing"]
                for key in completed[:-99]:self._jobs.pop(key, None)
                self._jobs[identifier] = {"job_id":identifier,"document_id":document_id,"state":"processing","result":None,"reused":False}
                self._threads = [thread for thread in self._threads if thread.is_alive()]
                thread = threading.Thread(target=self._run, args=(identifier, document_id, raw, settings, adapter, retry, admission), daemon=True)
                self._threads.append(thread)
            thread.start()
        except BaseException:
            admission.__exit__(None,None,None)
            raise
        return identifier

    def _run(self, identifier, document_id, raw, settings, adapter, retry, admission):
        try:
            with StateStore(self.database) as store:
                store.recover_running()
                job = store.enqueue(document_id, raw, settings)
                if retry and not job.needs_processing and store.current(document_id)["status"] == "error":job = store.retry(document_id)
                reused = not job.needs_processing
                key = (settings["provider"], settings["mock_dir"], settings["endpoint"])
                run_jobs(store, [job], providers={key:adapter})
                row = store.current(document_id)
                result = {key:row[key] for key in _PUBLIC}
                completion = dict(state="complete",result=result,reused=reused)
        except Exception:
            completion = dict(state="error",error="Processing could not finish. Check the server configuration and try again.")
        finally:
            admission.__exit__(None,None,None)
        # A terminal task can immediately be followed by another processing request.
        with self._mutex:self._jobs[identifier].update(completion)

    def job(self, identifier):
        with self._mutex:
            if identifier not in self._jobs:raise WebError(404, "This processing result is no longer available. Run the request again.")
            return copy.deepcopy(self._jobs[identifier])

    def close(self):
        for thread in self._threads:thread.join(timeout=self.config.timeout_seconds + 2)

    def asset(self, path):
        names = {"/":("index.html","text/html; charset=utf-8"),"/assets/app.js":("app.js","text/javascript; charset=utf-8"),"/assets/app.css":("app.css","text/css; charset=utf-8")}
        if path not in names:raise WebError(404, "Page not found.")
        name, content_type = names[path]
        raw = (ASSETS / name).read_bytes()
        if path == "/":raw = raw.replace(b"{{CSRF_TOKEN}}", self.csrf_token.encode())
        return content_type, raw

    def dispatch(self, method, url, body=None, *, token=None):
        parsed = urlsplit(url)
        try:
            if method == "GET":
                if parsed.path == "/api/files":return 200,self.files()
                if parsed.path == "/api/config":return 200,{"default_provider":"mock","providers":["mock","openai"],"model":self.config.model}
                if parsed.path == "/api/source":return 200,self.source(parse_qs(parsed.query).get("document_id",[None])[0])
                if parsed.path.startswith("/api/jobs/"):return 200,self.job(parsed.path[len("/api/jobs/"):])
            elif method == "POST" and parsed.path == "/api/process":
                if not isinstance(token,str) or not secrets.compare_digest(token,self.csrf_token):raise WebError(403,"Reload this page before starting a request.")
                if not isinstance(body,dict):raise WebError(400,"Request body must be a JSON object.")
                identifier = self.start(body.get("document_id"),body.get("provider"),body.get("source_sha256"),retry=body.get("retry",False))
                return 202,{"job_id":identifier}
            raise WebError(404,"Endpoint not found.")
        except WebError as error:return error.status,{"error":str(error)}
        except BusyError as error:return 409,{"error":str(error)}
        except (ProviderError,StateError,OSError):return 503,{"error":"The local server could not prepare this request. Check its input and provider configuration."}


class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _send(self, status, content_type, raw):
        # Rejected POST bodies need not be consumed. Close the connection so their
        # unread bytes cannot be parsed as a subsequent HTTP request.
        self.close_connection = True
        self.send_response(status)
        for key,value in {
            "Content-Type":content_type,"Content-Length":str(len(raw)),"Cache-Control":"no-store",
            "Connection":"close",
            "X-Content-Type-Options":"nosniff","Referrer-Policy":"no-referrer",
            "Content-Security-Policy":"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        }.items():self.send_header(key,value)
        self.end_headers()
        try:self.wfile.write(raw)
        except (BrokenPipeError,ConnectionResetError):pass

    def _json(self, status, payload):
        self._send(status,"application/json; charset=utf-8",json.dumps(payload,ensure_ascii=False).encode())

    def _local(self):
        return is_local_request(self.headers.get("Host"),self.headers.get("Origin"),self.server.server_port,self.server.app.browser_origin)

    def do_GET(self):
        if not self._local():self._json(403,{"error":"Use the local server address."});return
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            self._json(*self.server.app.dispatch("GET",self.path));return
        try:content_type,raw = self.server.app.asset(path)
        except WebError as error:self._json(error.status,{"error":str(error)});return
        self._send(200,content_type,raw)

    def do_POST(self):
        if not self._local():self._json(403,{"error":"Use the local server address."});return
        if self.headers.get("Content-Type","").split(";")[0].strip().lower() != "application/json":
            self._json(415,{"error":"Send a JSON request."});return
        try:
            length = int(self.headers.get("Content-Length","0"))
            if length < 1 or length > 4096:raise ValueError("body size")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError,UnicodeError):self._json(400,{"error":"Invalid JSON request body."});return
        self._json(*self.server.app.dispatch("POST",self.path,body,token=self.headers.get("X-CSRF-Token")))


def serve(input_dir, database, mock_dir, *, config=None, port=8765, browser_origin=None):
    if not 0 <= port <= 65535:raise StateError("Web port must be between 0 and 65535.")
    app = DemoApplication(input_dir,database,mock_dir,config,browser_origin=browser_origin)
    server = ThreadingHTTPServer(("127.0.0.1",port),RequestHandler)
    server.daemon_threads = True;server.app = app
    print(f"Northstar Request Lab: http://127.0.0.1:{server.server_port}",flush=True)
    print("Mock mode is the default. Press Ctrl+C to stop the local server.",flush=True)
    try:server.serve_forever()
    finally:server.server_close();app.close()
