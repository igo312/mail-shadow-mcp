from __future__ import annotations

import http.server
import json
import os
import socketserver
import tempfile
import threading
import unittest


class _UnixHTTPServer(socketserver.UnixStreamServer):
    allow_reuse_address = True

    def get_request(self):
        request, _ = super().get_request()
        return request, ("127.0.0.1", 0)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        if self.path == "/containers/msm-test/json":
            body = json.dumps({"Id": "cid-test", "State": {"Running": True}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = b'{"message":"not found"}'
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class DockerApiInspectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmp.name, "docker.sock")
        self.server = _UnixHTTPServer(self.socket_path, _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def test_inspect_uses_unix_socket_api(self) -> None:
        from app.docker_cli import DockerClient

        client = DockerClient(binary="does-not-exist", socket_path=self.socket_path)
        info = client.inspect_container("msm-test")
        self.assertIsNotNone(info)
        self.assertEqual(info["Id"], "cid-test")
        self.assertIsNone(client.inspect_container("missing"))


if __name__ == "__main__":
    unittest.main()
