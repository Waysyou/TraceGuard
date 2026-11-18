"""Browser preconnects must not block the local workbench."""
from http.server import BaseHTTPRequestHandler
import socket
import threading
import unittest
import urllib.request

from traceguard.server import WorkbenchServer


class ConnectionTests(unittest.TestCase):
    def test_idle_connections_do_not_block_real_request(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ready")
            def log_message(self, *args):
                pass
        with WorkbenchServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            address = server.server_address
            idle = [socket.create_connection(address, timeout=1) for _ in range(3)]
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(f"http://127.0.0.1:{address[1]}/", timeout=2) as response:
                    self.assertEqual(response.read(), b"ready")
            finally:
                for connection in idle:
                    connection.close()
                server.shutdown()
                thread.join(timeout=3)

    def test_second_server_cannot_claim_same_port(self):
        class Handler(BaseHTTPRequestHandler):
            pass
        with WorkbenchServer(("127.0.0.1", 0), Handler) as first:
            with self.assertRaises(OSError):
                WorkbenchServer(first.server_address, Handler)
