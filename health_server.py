import threading, logging
from http.server import BaseHTTPRequestHandler, HTTPServer
import config as C

log = logging.getLogger(__name__)
_status = {"cycles": 0, "last": "—", "errors": 0, "positions": 0, "symbols": 0}

class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (
            f"CVD Bot OK | cycles={_status['cycles']} "
            f"last={_status['last']} errors={_status['errors']} "
            f"positions={_status['positions']}/{C.MAX_POSITIONS} "
            f"symbols={_status['symbols']}"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

def update(last=None, error=False, positions=None, symbols=None):
    if last:      _status["last"] = last; _status["cycles"] += 1
    if error:     _status["errors"] += 1
    if positions is not None: _status["positions"] = positions
    if symbols   is not None: _status["symbols"]   = symbols

def start():
    t = threading.Thread(target=HTTPServer(("0.0.0.0", C.PORT), _H).serve_forever, daemon=True)
    t.start()
    log.info(f"Health server :{C.PORT}")
