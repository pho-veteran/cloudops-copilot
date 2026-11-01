import asyncio
import base64
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from sqlalchemy import select
from cloudops.features.workloads.models import Workload
from cloudops.features.workloads.observation_service import claim, observe_direct
from cloudops.features.workloads.observation_reader import ObservationReader
from cloudops.shared.database import sessions
from cloudops.shared.config import settings

if not __debug__ or urlsplit(settings().database_url).path != "/cloudops28_lab":
    raise RuntimeError("Use unoptimized Python and the separate cloudops28_lab fixture database.")

with tempfile.TemporaryDirectory(prefix="e02-read-isolation-") as directory:
    directory = Path(directory)
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(directory / "key.pem"),
            "-out",
            str(directory / "cert.pem"),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    closed = []
    connected = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            connected.append(1)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "100000")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    import time

                    time.sleep(0.2)
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                closed.append(1)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(directory / "cert.pem", directory / "key.pem")
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = {
        "server": f"https://localhost:{server.server_port}",
        "ca_data": base64.b64encode((directory / "cert.pem").read_bytes()).decode(),
        "token": "noncredential-probe-token",
    }
    path = directory / "client.json"
    path.write_text(json.dumps(config))
    os.environ["KUBE_READ_CONFIG"] = str(path)
    with sessions()() as db:
        targets = {
            row.name: row.id
            for row in db.scalars(
                select(Workload).where(Workload.active, Workload.cluster == "e02-local-k3s")
            )
        }

    async def main():
        tasks = [
            asyncio.create_task(
                observe_direct(targets[name], "scheduled", reader=ObservationReader())
            )
            for name in ["checkout", "e02-ready", "e02-recreate"]
        ]
        for _ in range(80):
            if len(connected) == 3:
                break
            await asyncio.sleep(0.05)
        if len(connected) != 3:
            results = await asyncio.gather(*tasks)
            raise RuntimeError(
                json.dumps(
                    {
                        "connected": len(connected),
                        "outcomes": [(r.outcome, r.reason) for r in results],
                    }
                )
            )
        assert claim(targets["e02-config"], "scheduled") is None
        print(
            json.dumps({"ready": True, "scheduled_leases": 3, "fourth_scheduled_denied": True}),
            flush=True,
        )
        results = await asyncio.gather(*tasks)
        await asyncio.sleep(0.5)
        print(
            json.dumps({"outcomes": [r.reason for r in results], "closed": len(closed)}), flush=True
        )

    try:
        asyncio.run(main())
    finally:
        server.shutdown()
        server.server_close()
