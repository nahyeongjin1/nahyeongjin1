#!/usr/bin/env python3
"""Render README.md through GitHub's own Markdown API and serve it locally.

Usage:
    scripts/preview.py [FILE] [--port PORT] [--no-open]

Requires the `gh` CLI to be authenticated (`gh auth status`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE = """<!DOCTYPE html>
<html lang="en" data-color-mode="auto" data-light-theme="light" data-dark-theme="dark">
<head>
<meta charset="utf-8">
<title>{title}</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.8.1/github-markdown.min.css">
<style>
  body {{ margin: 0; background: #f6f8fa; }}
  @media (prefers-color-scheme: dark) {{ body {{ background: #010409; }} }}
  .markdown-body {{ box-sizing: border-box; max-width: 1012px; margin: 0 auto;
                    padding: 32px 32px 96px; }}
  @media (max-width: 767px) {{ .markdown-body {{ padding: 16px; }} }}
</style>
</head>
<body>
<article class="markdown-body">{body}</article>
<script>
const KEY = "preview-scroll";
const saved = sessionStorage.getItem(KEY);
if (saved) window.scrollTo(0, parseInt(saved, 10));
addEventListener("scroll", () => sessionStorage.setItem(KEY, String(scrollY)));
let current = "{digest}";
setInterval(async () => {{
  try {{
    const next = await (await fetch("/digest", {{ cache: "no-store" }})).text();
    if (next && next !== current) location.reload();
  }} catch (e) {{ /* server stopped */ }}
}}, 1000);
</script>
</body>
</html>
"""


def render(path: Path) -> str:
    """Return GitHub-rendered HTML for `path`, or an error block on failure."""
    payload = json.dumps({"text": path.read_text(encoding="utf-8"), "mode": "gfm"})
    result = subprocess.run(
        ["gh", "api", "--method", "POST", "/markdown", "--input", "-"],
        input=payload,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return f"<h1>Render failed</h1><pre>{detail}</pre>"
    return result.stdout


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def make_handler(path: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            if self.path.startswith("/digest"):
                return self._send("text/plain", digest(path))
            if self.path not in ("/", "/index.html"):
                self.send_error(404)
                return
            page = PAGE.format(
                title=path.name, body=render(path), digest=digest(path)
            )
            self._send("text/html", page)

        def _send(self, content_type: str, body: str) -> None:
            raw = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args) -> None:
            pass

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", default="README.md", type=Path)
    parser.add_argument("--port", type=int, default=4000)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    if not args.file.is_file():
        print(f"no such file: {args.file}", file=sys.stderr)
        return 1

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.file))
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"previewing {args.file} at {url}  (Ctrl-C to stop)")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
