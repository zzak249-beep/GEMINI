"""
health_server.py — Responde en :PORT desde el primer segundo.
El HTTPServer se crea DENTRO del thread para no bloquear el hilo principal.
"""
import threading, logging, socket, time
from http.server import BaseHTTPRequestHandler, HTTPServer
import config as C

log = logging.getLogger(__name__)
_status = {"cycles": 0, "last": "starting", "errors": 0, "positions": 0, "symbols": 0}
_ready  = threading.Event()


class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (
            f"CVD Bot OK | cycles={_status['cycles']} "
            f"last={_status['last']} errors={_status['errors']} "
            f"positions={_status['positions']}/{C.MAX_POSITIONS} "
            f"symbols={_status['symbols']}"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _serve():
    try:
        srv = HTTPServer(("0.0.0.0", C.PORT), _H)
        _ready.set()
        log.info(f"Health server escuchando en :{C.PORT}")
        srv.serve_forever()
    except Exception as e:
        log.error(f"Health server error: {e}")
        _ready.set()  # desbloquear aunque falle


def update(last=None, error=False, positions=None, symbols=None):
    if last is not None:
        _status["last"] = last
        _status["cycles"] += 1
    if error:
        _status["errors"] += 1
    if positions is not None:
        _status["positions"] = positions
    if symbols is not None:
        _status["symbols"] = symbols


def start():
    t = threading.Thread(target=_serve, daemon=True, name="health-server")
    t.start()
    # Esperar hasta 3s a que el puerto esté abierto
    _ready.wait(timeout=3)
    log.info(f"Health server listo en :{C.PORT}")
