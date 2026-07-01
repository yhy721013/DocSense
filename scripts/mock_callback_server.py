from __future__ import annotations

import argparse
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.utils.callback_client import save_callback_history_payload  # noqa: E402

from dotenv import load_dotenv


logger = logging.getLogger(__name__)


class CallbackHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        # 只处理 /llm/callback 路径的请求
        if self.path != "/llm/callback":
            self.send_response(404)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'{"error": "Not Found"}')
            return
            
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        text = raw_body.decode("utf-8", errors="replace")

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = {"raw": text}

        target = save_callback_history_payload(parsed)
        logger.info("Callback history saved to %s", target)
        logger.info("Callback payload:\n%s", json.dumps(parsed, ensure_ascii=False, indent=2))
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s [%(name)s:%(lineno)d] %(message)s",
    )
    load_dotenv(override=True)
    parser = argparse.ArgumentParser(description="Local callback receiver for DocSense integration tests")
    parser.add_argument("--host", default=os.getenv("MOCK_CALLBACK_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MOCK_CALLBACK_PORT", "9000")))
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), CallbackHandler)
    logger.info("Mock callback server listening on http://%s:%s", args.host, args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
