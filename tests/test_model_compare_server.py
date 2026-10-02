"""A browser's speculative idle socket must not starve real page requests."""

import http.client
import socket
import threading

from scripts.score_models import _make_server


def test_idle_browser_connection_does_not_block_page_loading(tmp_path):
    page = b"<p>ready to score</p>"
    (tmp_path / "scorecard.html").write_bytes(page)
    server = _make_server(tmp_path, {}, {}, 0)
    accepted = threading.Event()
    process_request = server.process_request

    def observe_accept(request, address):
        accepted.set()
        return process_request(request, address)

    server.process_request = observe_accept
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    idle = socket.create_connection(server.server_address, timeout=2)
    client = http.client.HTTPConnection(*server.server_address, timeout=2)
    try:
        assert accepted.wait(2), "First connection must be accepted before the real request"
        # Deliberately send nothing on the first socket, like Chrome preconnect.
        client.request("GET", "/")
        response = client.getresponse()
        assert response.status == 200
        assert response.read() == page
    finally:
        client.close()
        idle.close()
        server.shutdown()
        server.server_close()
        worker.join(2)
    assert not worker.is_alive()
