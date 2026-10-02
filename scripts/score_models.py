"""Score an existing comparison locally, without new model calls."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path

from scripts.model_compare.report import _write_private
from scripts.model_compare.scorecard import scoring_data, summarize_scores, write_scorecard


def _make_server(directory: Path, data: dict, reveal: dict, port: int) -> ThreadingHTTPServer:
    page = (directory / "scorecard.html").read_bytes()
    origin = f"http://127.0.0.1:{port}"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_content(self, status, content, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            if self.headers.get("Host") != f"127.0.0.1:{port}":
                self.send_content(403, b"Forbidden host", "text/plain")
            elif self.path != "/":
                # Serve only this page, never arbitrary files, secrets or reveal maps.
                self.send_content(404, b"Not found", "text/plain")
            else:
                self.send_content(200, page, "text/html; charset=utf-8")

        def do_POST(self):
            if (self.headers.get("Host") != f"127.0.0.1:{port}"
                    or self.headers.get("Origin") != origin):
                self.send_content(403, b"Forbidden origin", "text/plain")
                return
            if self.path != "/summary":
                self.send_content(404, b"Not found", "text/plain")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError("Invalid request length")
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError("Expected JSON")
                exported = json.loads(self.rfile.read(length))
                summary = summarize_scores(data, reveal, exported)
            except (ValueError, TypeError, KeyError):
                self.send_content(400, b"Invalid identified score export", "text/plain")
                return
            self.send_content(200, json.dumps(summary).encode(), "application/json")

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    port = server.server_port
    origin = f"http://127.0.0.1:{port}"
    return server


def serve_scorecard(directory: Path, data: dict, reveal: dict, port: int) -> None:
    server = _make_server(directory, data, reveal, port)
    origin = f"http://127.0.0.1:{server.server_port}"
    print(f"Scoring app ready: {origin}/", flush=True)
    print("Private, loopback-only; browser progress saves locally. Export a backup. No model calls.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Existing comparison output directory")
    parser.add_argument("--serve", action="store_true", help="Serve the private scoring app on loopback only")
    parser.add_argument("--port", type=int, default=8787, help="Loopback port, default 8787")
    parser.add_argument("--scores", type=Path, help="Summarize a previously exported identified score file")
    args = parser.parse_args()
    try:
        directory = args.directory.expanduser().resolve()
        cases = json.loads((directory / "cases.json").read_text())
        records = [json.loads(line) for line in (directory / "results.jsonl").read_text().splitlines() if line.strip()]
        reveal = json.loads((directory / "reveal.json").read_text())
        data = scoring_data(cases, records, reveal)
        if args.scores:
            summary = summarize_scores(data, reveal, json.loads(args.scores.read_text()))
            _write_private(directory / "score-summary.json", json.dumps(summary, indent=2) + "\n")
            print(json.dumps(summary, indent=2))
        else:
            write_scorecard(directory, cases, records, reveal, server_mode=args.serve)
            print(f"Private scorecard: {directory / 'scorecard.html'}", flush=True)
            if args.serve:
                if not 1 <= args.port <= 65535:
                    raise ValueError("Choose a port between 1 and 65535")
                serve_scorecard(directory, data, reveal, args.port)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(1, f"Scoring failed: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
