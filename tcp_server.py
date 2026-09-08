"""
TCP bridge (Python side): a simple threaded TCP server that accepts a
connection from the mGBA Lua bridge script and pushes button commands to
it. Tolerant of no client being connected -- the tracker should never
crash or block because mGBA hasn't loaded bridge.lua yet.
"""

import logging
import socket
import threading

import config

log = logging.getLogger(__name__)


class CommandServer:
    def __init__(self, host: str = config.TCP_HOST, port: int = config.TCP_PORT):
        self.host = host
        self.port = port
        self._server_socket: socket.socket | None = None
        self._clients: list[socket.socket] = []
        self._clients_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._accept_thread: threading.Thread | None = None

    def start(self):
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self.host, self.port))
        self._server_socket.listen(1)
        self._server_socket.settimeout(1.0)
        log.info("TCP command server listening on %s:%d", self.host, self.port)

        self._stop_event.clear()
        self._accept_thread = threading.Thread(target=self._accept_loop, name="TCPAccept", daemon=True)
        self._accept_thread.start()

    def stop(self):
        self._stop_event.set()
        if self._accept_thread:
            self._accept_thread.join(timeout=2)
        with self._clients_lock:
            for c in self._clients:
                try:
                    c.close()
                except OSError:
                    pass
            self._clients.clear()
        if self._server_socket:
            try:
                self._server_socket.close()
            except OSError:
                pass

    def send_command(self, command: str):
        """Send a command string (e.g. 'UP') to all connected clients."""
        payload = (command.strip() + "\n").encode("utf-8")
        with self._clients_lock:
            dead = []
            for client in self._clients:
                try:
                    client.sendall(payload)
                except OSError as exc:
                    log.info("Client disconnected while sending (%s)", exc)
                    dead.append(client)
            for d in dead:
                self._clients.remove(d)
                try:
                    d.close()
                except OSError:
                    pass

        if not self._clients:
            log.debug("No mGBA client connected; dropped command %r", command)

    @property
    def client_connected(self) -> bool:
        with self._clients_lock:
            return len(self._clients) > 0

    def _accept_loop(self):
        while not self._stop_event.is_set():
            try:
                client, addr = self._server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            log.info("mGBA client connected from %s", addr)
            with self._clients_lock:
                self._clients.append(client)
