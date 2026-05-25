"""
health_server.py — Healthcheck para Railway
CRÍTICO: escucha en 0.0.0.0 en el PORT que Railway asigna via env var.
El servidor arranca en su propio thread y señaliza cuando está listo.
"""
import os
import threading
import logging
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer

log = logging.getLogger(__name__)

# Leer PORT directamente de os.environ para no depender de config.py
# (que podría no estar importado todavía cuando esto se ejecuta)
_PORT   = int(os.environ.get("PORT", "8080"))
_status = {"cycles": 0, "last": "starting", "errors": 0, "positions": 0, "symbols": 0}
_ready  = threading.Event()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (
            f"OK cycles={_status['cycles']} "
            f"last={_status['last']} "
            f"errors={_status['errors']} "
            f"pos={_status['positions']} "
            f"syms={_status['symbols']}"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # silenciar logs de cada request


def _run_server():
    try:
        server = HTTPServer(("0.0.0.0", _PORT), _Handler)
        log.info(f"[health] Servidor HTTP escuchando en 0.0.0.0:{_PORT}")
        _ready.set()
        server.serve_forever()
    except OSError as e:
        log.error(f"[health] No se pudo abrir puerto {_PORT}: {e}")
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
    t = threading.Thread(target=_run_server, daemon=True, name="health-http")
    t.start()
    # Esperar hasta 5s a que el socket esté realmente abierto
    if _ready.wait(timeout=5):
        log.info(f"[health] Puerto {_PORT} confirmado abierto")
    else:
        log.warning(f"[health] Timeout esperando puerto {_PORT}")
