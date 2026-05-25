import threading, logging
from http.server import BaseHTTPRequestHandler, HTTPServer
import config as C

log = logging.getLogger(__name__)
_status = {"cycles": 0, "last": "—", "errors": 0}

class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (f"CVD Bot OK | cycles={_status['cycles']} "
                f"last={_status['last']} errors={_status['errors']}").encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

def update(last=None, error=False):
    if last: _status["last"] = last; _status["cycles"] += 1
    if error: _status["errors"] += 1

def start():
    t = threading.Thread(target=HTTPServer(("0.0.0.0", C.PORT), _H).serve_forever, daemon=True)
    t.start()
    log.info(f"Health server :{ C.PORT}")
