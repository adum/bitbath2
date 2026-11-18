"""Serves the repository over HTTP for the replay viewer.

    python scripts/serve.py [port]

Then open http://localhost:8000/web/index.html, adding
?replay=/runs/<game>/replay.bbr to open a replay directly.

Unlike `python -m http.server`, this tells browsers to check every file for
changes before using a cached copy. Otherwise a browser can keep running an
old viewer or engine after `scripts/build_web.py` rebuilds it, and replay
games the engine now plays differently.
"""

import functools
import http.server
import os
import sys


class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    handler = functools.partial(Handler, directory=root)
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as server:
        print("Serving the viewer at http://localhost:%d/web/index.html" % port, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
