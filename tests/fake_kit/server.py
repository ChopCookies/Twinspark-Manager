"""A stand-in for a kit's inference server (tests/test_kit_end_to_end.py and the CI systemd job).

It answers the three calls TwinSpark makes through a kit route: /health, /v1/models and /v1/chat/completions.
The chat reply reports the Authorization header it received: TwinSpark's gateway must send a kit none.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "Fake-Kit-Model"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, doc):
        body = json.dumps(doc).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        elif self.path == "/v1/models":
            self._send(200, {"object": "list", "data": [{"id": MODEL, "object": "model"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        doc = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        if self.path != "/v1/chat/completions":
            return self._send(404, {"error": "not found"})
        self._send(200, {
            "id": "fake-1", "object": "chat.completion", "model": doc.get("model"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "hello from the kit"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            "seen_authorization": self.headers.get("authorization"),
        })

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer((sys.argv[1], int(sys.argv[2])), Handler).serve_forever()
