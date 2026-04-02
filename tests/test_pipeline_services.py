"""
QA tests for t2v-service.py and sentiment-service.py stability fixes.

Tests cover:
  - sentiment POST handler: missing content-length, bad JSON, empty message
  - sentiment classifier lock (inference serialisation)
  - t2v-service startup validation (AFFECTS_DIR required, T2V_BIN checked)
  - t2v-service subscribe-in-on_connect pattern

Run from bushglue/:
    python3 -m pytest tests/test_pipeline_services.py -v
or:
    python3 tests/test_pipeline_services.py

No model files, hardware, or live services required.
"""
import io
import json
import os
import sys
import threading
import unittest
from http.server import HTTPServer
from unittest.mock import MagicMock, patch, call
import importlib.util

_BUSHGLUE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BUSHGLUE)


# ---------------------------------------------------------------------------
# Minimal stubs so sentiment-service imports without torch/transformers
# ---------------------------------------------------------------------------

def _load_sentiment_server_class():
    """
    Import only the Server class and related helpers from sentiment-service.py
    by stubbing out torch, transformers, paho, and bushutil at import time.
    Returns the module.
    """
    import types

    # Stub torch
    torch_stub = types.ModuleType("torch")
    torch_stub.set_num_threads = lambda n: None
    sys.modules["torch"] = torch_stub

    # Stub transformers
    transformers_stub = types.ModuleType("transformers")
    fake_classifier = MagicMock(return_value=[
        [{"label": "joy", "score": 0.9}, {"label": "anger", "score": 0.1}]
    ])
    transformers_stub.pipeline = MagicMock(return_value=fake_classifier)
    sys.modules["transformers"] = transformers_stub

    # Stub paho
    paho_stub = types.ModuleType("paho")
    paho_mqtt_stub = types.ModuleType("paho.mqtt")
    paho_client_stub = types.ModuleType("paho.mqtt.client")
    paho_client_stub.Client = MagicMock
    paho_client_stub.CallbackAPIVersion = MagicMock()
    sys.modules["paho"] = paho_stub
    sys.modules["paho.mqtt"] = paho_mqtt_stub
    sys.modules["paho.mqtt.client"] = paho_client_stub

    # Stub bushutil
    bushutil_stub = types.ModuleType("bushutil")
    bushutil_stub.get_mqtt_broker = lambda: "localhost"
    sys.modules["bushutil"] = bushutil_stub

    spec = importlib.util.spec_from_file_location(
        "sentiment_service",
        os.path.join(_BUSHGLUE, "sentiment-service.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Load module once for all tests
try:
    _sentiment_mod = _load_sentiment_server_class()
    _Server = _sentiment_mod.Server
    _SENTIMENT_AVAILABLE = True
except Exception as e:
    _SENTIMENT_AVAILABLE = False
    _SENTIMENT_IMPORT_ERROR = str(e)


# ---------------------------------------------------------------------------
# Fake HTTP request helper
# ---------------------------------------------------------------------------

class _FakeRequest:
    """Minimal stand-in for a socket that HTTPServer passes to the handler."""
    def __init__(self, body: bytes = b""):
        self._body = body
        self._sent = io.BytesIO()

    def makefile(self, mode, *args, **kwargs):
        if "r" in mode:
            # Build a minimal HTTP request line + headers
            return io.BytesIO(self._body)
        return self._sent

    def sendall(self, data):
        self._sent.write(data)


def _make_handler(body_bytes: bytes, path: str = "/") -> _Server:
    """
    Instantiate a Server handler with a fake request for testing do_POST.
    Bypasses __init__ to avoid needing a real socket/server object.
    """
    handler = _Server.__new__(_Server)
    handler.rfile = io.BytesIO(body_bytes)
    handler.wfile = io.BytesIO()
    handler.headers = {}
    handler.path = path
    handler.server = MagicMock()
    # resp() implementation needs these
    handler._headers_buffer = []

    def send_response(code, message=None):
        handler._response_code = code
    def send_header(k, v):
        pass
    def end_headers():
        pass
    def wfile_write(data):
        if isinstance(data, str):
            data = data.encode()
        handler.wfile.write(data)

    handler.send_response = send_response
    handler.send_header = send_header
    handler.end_headers = end_headers
    handler._response_code = None

    # Capture resp() output
    handler._resp_calls = []
    original_resp = _Server.resp
    def captured_resp(self, code, body):
        self._resp_calls.append((code, body))
        self._response_code = code
    handler.resp = lambda code, body: captured_resp(handler, code, body)

    return handler


@unittest.skipUnless(_SENTIMENT_AVAILABLE, f"sentiment-service import failed")
class TestSentimentPOSTHandler(unittest.TestCase):

    def test_missing_content_length_returns_400(self):
        handler = _make_handler(b'{"text": "hello"}')
        handler.headers = {}  # no content-length
        handler.do_POST()
        self.assertEqual(handler._response_code, 400)
        self.assertTrue(any("Content-Length" in str(r) for r in handler._resp_calls))

    def test_invalid_content_length_returns_400(self):
        handler = _make_handler(b'{"text": "hello"}')
        handler.headers = {"content-length": "not-a-number"}
        handler.do_POST()
        self.assertEqual(handler._response_code, 400)

    def test_invalid_json_returns_400(self):
        body = b"not json {"
        handler = _make_handler(body)
        handler.headers = {"content-length": str(len(body))}
        handler.do_POST()
        self.assertEqual(handler._response_code, 400)
        self.assertTrue(any("JSON" in str(r) for r in handler._resp_calls))

    def test_empty_message_returns_400(self):
        body = json.dumps({"other_key": "value"}).encode()
        handler = _make_handler(body)
        handler.headers = {"content-length": str(len(body))}
        handler.do_POST()
        self.assertEqual(handler._response_code, 400)

    def test_valid_text_key_returns_200(self):
        body = json.dumps({"text": "the fire speaks"}).encode()
        handler = _make_handler(body)
        handler.headers = {"content-length": str(len(body))}
        # Patch the classifier lock and classifier to return a fixed result
        with patch.object(_sentiment_mod, "_classifier_lock", threading.Lock()):
            with patch.object(_sentiment_mod, "classifier",
                              return_value=[[{"label": "joy", "score": 0.9}]]):
                handler.do_POST()
        self.assertEqual(handler._response_code, 200)

    def test_valid_affected_text_key_returns_200(self):
        body = json.dumps({"affected_text": "burning wilderness"}).encode()
        handler = _make_handler(body)
        handler.headers = {"content-length": str(len(body))}
        with patch.object(_sentiment_mod, "_classifier_lock", threading.Lock()):
            with patch.object(_sentiment_mod, "classifier",
                              return_value=[[{"label": "fear", "score": 0.8}]]):
                handler.do_POST()
        self.assertEqual(handler._response_code, 200)


@unittest.skipUnless(_SENTIMENT_AVAILABLE, f"sentiment-service import failed")
class TestClassifierLock(unittest.TestCase):

    def test_classifier_lock_exists(self):
        """_classifier_lock must be a threading.Lock (or RLock)."""
        lock = getattr(_sentiment_mod, "_classifier_lock", None)
        self.assertIsNotNone(lock, "_classifier_lock not found in sentiment-service")
        # Both Lock and RLock have acquire/release
        self.assertTrue(hasattr(lock, "acquire"))
        self.assertTrue(hasattr(lock, "release"))

    def test_http_handler_acquires_lock(self):
        """
        The POST handler must acquire _classifier_lock before calling classifier.
        Verify by checking that classifier is called while the lock is held.
        """
        calls_while_locked = []
        original_lock = _sentiment_mod._classifier_lock

        class _SpyLock:
            def __init__(self):
                self._lock = threading.Lock()
                self.held = False
            def acquire(self, *a, **kw):
                self._lock.acquire(*a, **kw)
                self.held = True
            def release(self):
                self.held = False
                self._lock.release()
            def __enter__(self):
                self.acquire()
                return self
            def __exit__(self, *a):
                self.release()

        spy = _SpyLock()

        def _spy_classifier(text):
            calls_while_locked.append(spy.held)
            return [[{"label": "joy", "score": 0.9}]]

        body = json.dumps({"text": "the flame"}).encode()
        handler = _make_handler(body)
        handler.headers = {"content-length": str(len(body))}

        with patch.object(_sentiment_mod, "_classifier_lock", spy):
            with patch.object(_sentiment_mod, "classifier", side_effect=_spy_classifier):
                handler.do_POST()

        self.assertTrue(all(calls_while_locked),
                        "classifier was called without holding _classifier_lock")


# ---------------------------------------------------------------------------
# t2v-service startup validation tests
# ---------------------------------------------------------------------------

class TestT2VServiceValidation(unittest.TestCase):

    def test_affects_dir_required(self):
        """Service must exit if AFFECTS_DIR is not set."""
        old = os.environ.pop("AFFECTS_DIR", None)
        try:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AFFECTS_DIR", None)
                # Re-evaluate the module-level check by reading the source
                src_path = os.path.join(_BUSHGLUE, "t2v-service.py")
                with open(src_path) as f:
                    source = f.read()
                self.assertIn("AFFECTS_DIR", source)
                self.assertIn("sys.exit", source)
                # Verify the guard appears near AFFECTS_DIR assignment
                idx_affects = source.index("AFFECTS_DIR = os.environ.get")
                idx_exit = source.index("sys.exit", idx_affects)
                # sys.exit should appear within 10 lines of the AFFECTS_DIR assignment
                snippet = source[idx_affects:idx_exit]
                self.assertLess(snippet.count('\n'), 10,
                    "sys.exit should be within ~10 lines of AFFECTS_DIR assignment")
        finally:
            if old is not None:
                os.environ["AFFECTS_DIR"] = old

    def test_t2v_bin_existence_check(self):
        """Service must validate T2V_BIN exists before starting subprocess."""
        src_path = os.path.join(_BUSHGLUE, "t2v-service.py")
        with open(src_path) as f:
            source = f.read()
        self.assertIn("os.path.isfile(T2V_BIN)", source,
            "t2v-service should check os.path.isfile(T2V_BIN) at startup")

    def test_subscribe_inside_on_connect(self):
        """
        subscribe() must be called inside on_connect, not directly after
        mqttc.connect(). This ensures resubscription after broker reconnects.
        """
        src_path = os.path.join(_BUSHGLUE, "t2v-service.py")
        with open(src_path) as f:
            source = f.read()

        # Find on_connect definition and verify subscribe is inside it
        import ast
        tree = ast.parse(source)
        on_connect_body = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "on_connect":
                on_connect_body = ast.get_source_segment(source, node)
                break

        self.assertIsNotNone(on_connect_body, "on_connect function not found in t2v-service.py")
        self.assertIn("subscribe", on_connect_body,
            "on_connect should call client.subscribe()")

    def test_watchdog_thread_present(self):
        """A watchdog thread must be started to detect t2v process death."""
        src_path = os.path.join(_BUSHGLUE, "t2v-service.py")
        with open(src_path) as f:
            source = f.read()
        self.assertIn("_watchdog", source,
            "t2v-service should have a _watchdog function to detect t2v process death")
        self.assertIn("t2v_proc.wait()", source,
            "_watchdog should call t2v_proc.wait() to detect process exit")

    def test_on_disconnect_callback_present(self):
        """on_disconnect must be wired up so reconnection is visible in logs."""
        src_path = os.path.join(_BUSHGLUE, "t2v-service.py")
        with open(src_path) as f:
            source = f.read()
        self.assertIn("on_disconnect", source)
        self.assertIn("mqttc.on_disconnect = on_disconnect", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
