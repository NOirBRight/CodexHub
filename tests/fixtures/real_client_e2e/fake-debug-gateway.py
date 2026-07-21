import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path


parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--bad-health", action="store_true")
args = parser.parse_args()
settings = json.loads(
    (Path(os.environ["CODEXHUB_RUNTIME_HOME"]) / "proxy" / "settings.json").read_text()
)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health" or args.bad_health:
            self.send_response(503)
            self.end_headers()
            return
        payload = b'{"ok":true,"build":"fixture","features":[]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format, *_args):
        return


class Server(ThreadingHTTPServer):
    allow_reuse_address = True


if args.port != int(settings["proxy_port"]):
    raise SystemExit(2)
Server(("127.0.0.1", args.port), Handler).serve_forever()
