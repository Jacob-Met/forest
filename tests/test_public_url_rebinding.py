"""Public literature retrieval must connect only to the IP checked by the validator."""
import socket
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from research.literature.browser import read_page
from research.literature.sources import download_pdf


@pytest.mark.parametrize('format', ['page', 'pdf', 'screenshot', 'screenshot-safe'])
def test_public_url_dns_rebinding_cannot_reach_private_service(tmp_path, monkeypatch, format):
    """A public first answer and a loopback second answer must not reach loopback."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests_seen.append(self.path)
            host_headers.append(self.headers.get('Host'))
            if self.path == '/style.css':
                body = b'body { background: white; color: black }'
                content_type = 'text/css'
            elif format == 'pdf':
                body = b'%PDF-1.4\n%%EOF\n'
                content_type = 'application/pdf'
            else:
                body = (b'<!doctype html><title>TEST PUBLIC</title>'
                        b'<link rel="stylesheet" href="/style.css"><p>content</p>')
                content_type = 'text/html'
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.timeout = 2
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    public_ip = '93.184.216.34'
    dns_lookups = []
    connections = []
    requests_seen = []
    host_headers = []
    getaddrinfo = socket.getaddrinfo
    connect = socket.socket.connect

    def controlled_dns(host, requested_port, *args, **kwargs):
        if host == 'rebinding.invalid':
            dns_lookups.append(host)
            ip = public_ip if len(dns_lookups) <= (2 if format == 'screenshot-safe' else 1) else '127.0.0.1'
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                     '', (ip, requested_port))]
        return getaddrinfo(host, requested_port, *args, **kwargs)

    def connect_to_fixture(self, address):
        if isinstance(address, tuple) and address[1] == port and address[0] in (public_ip, '127.0.0.1'):
            connections.append(address[0])
            return connect(self, ('127.0.0.1', port))
        return connect(self, address)

    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                 'http_proxy', 'https_proxy', 'all_proxy'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(socket, 'getaddrinfo', controlled_dns)
    monkeypatch.setattr(socket.socket, 'connect', connect_to_fixture)
    try:
        url = f'http://rebinding.invalid:{port}/source' + ('.pdf' if format == 'pdf' else '')
        if format == 'pdf':
            assert download_pdf(url, tmp_path / 'source.pdf')
        else:
            result = read_page(url, tmp_path, screenshot=format.startswith('screenshot'))
            assert result['title'] == 'TEST PUBLIC'
            if format.startswith('screenshot'):
                assert Path(result['screenshot_path']).stat().st_size > 1000
                if format == 'screenshot-safe':
                    assert '/style.css' in requests_seen, 'Chromium did not load the pinned stylesheet'
                else:
                    assert '/style.css' not in requests_seen, 'Chromium loaded stylesheet after DNS rebinding'
        assert dns_lookups, 'the public name was never validated'
        assert connections and set(connections) == {public_ip}, f'connected to an unvalidated address: {connections}'
        assert host_headers and all(h == f'rebinding.invalid:{port}' for h in host_headers), host_headers
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize('private', ['127.0.0.1', '10.1.2.3', '::1'])
def test_public_url_rejects_mixed_dns_answers(monkeypatch, private):
    """One private address poisons the entire DNS answer set."""
    from research.literature.sources import _public_url
    def mixed_answers(*_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 443)),
            (socket.AF_INET6 if ':' in private else socket.AF_INET,
             socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (private, 443)),
        ]
    monkeypatch.setattr(socket, 'getaddrinfo', mixed_answers)
    with pytest.raises(ValueError, match='Private/network-local'):
        _public_url('https://mixed-dns.example/source.pdf')
