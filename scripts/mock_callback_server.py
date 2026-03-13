from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


class CallbackHandler(BaseHTTPRequestHandler):
    output_dir = Path(".runtime/mock_callback")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        text = raw_body.decode("utf-8", errors="replace")

        self.output_dir.mkdir(parents=True, exist_ok=True)
        target = self.output_dir / "last_callback.json"
        target.write_text(text, encoding="utf-8")

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = {"raw": text}

        print(json.dumps(parsed, ensure_ascii=False, indent=2))
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')


def main() -> None:
    parser = argparse.ArgumentParser(description="Local callback receiver for DocSense integration tests")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), CallbackHandler)
    print(f"Mock callback server listening on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
