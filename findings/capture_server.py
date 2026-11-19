"""Local fake OpenAI-compatible endpoint: records each request body and answers with a fixed tool-less reply."""
import json, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
OUT = sys.argv[1]
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length", 0)); body = self.rfile.read(n)
        with open(OUT, "ab") as f: f.write(body + b"\n")
        resp = {"id": "x", "object": "chat.completion", "created": int(time.time()), "model": "capture",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 1, "total_tokens": 1}}
        b = json.dumps(resp).encode(); self.send_response(200); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        b = json.dumps({"data": [{"id": "capture"}]}).encode(); self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", 18099), H).serve_forever()
