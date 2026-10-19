"""Run INSIDE a dedicated Docker 26.1.3 daemon, never production.

The caller supplies cached runtime images and the read-only /source mount.
See internal design docs for the invocation.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading
import time
import uuid

import docker

BACKEND_IMAGE = "hugagent-backend:latest"
SMALL_IMAGE = "python:3.12-alpine"
CONTROLLER_IMAGE = "hugagent-opensandbox-server:shared-rollback-e2e-254"
LABEL = "hugagent.egress-e2e"
CLIENT = r"""
import http.client,json,subprocess
for i in range(14):
    subprocess.run(["python","-c","print('local tool completed')"],
                   check=True,stdout=subprocess.DEVNULL)
c=http.client.HTTPConnection("GATEWAY",18092,timeout=5)
c.request("POST","/v1/desktop/capability/gateway/models/fixture/responses",
          json.dumps({"model":"fixture","stream":True,"input":"synthetic"}),
          {"Content-Type":"application/json"})
r=c.getresponse()
assert r.status==200,r.status
events=[]
while True:
    line=r.readline()
    if not line:break
    if line.startswith(b"data: "): events.append(json.loads(line[6:]))
assert [e["sequence"] for e in events]==list(range(60)),events
assert events[-1]["type"]=="response.completed",events[-1]
print(json.dumps({"local_tools":14,"ordered_events":len(events),"completed":True}))
"""


class Upstream(BaseHTTPRequestHandler):
    started = threading.Event()
    release = threading.Event()

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if json.loads(body).get("input") == "disconnect-before-headers":
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.started.set()
        try:
            for i in range(59):
                event = {"sequence": i, "type": "response.output_text.delta"}
                self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
                self.wfile.flush()
                time.sleep(0.1)
            while not self.release.wait(0.2):
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
            self.wfile.write(b'data: {"sequence":59,"type":"response.completed"}\n\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    """Verify the rollback against real controller APIs on a private Docker 26 daemon."""
    client = docker.from_env()
    assert client.version()["Version"] == "26.1.3", "Use the private production-version daemon"
    project = "shared-" + uuid.uuid4().hex[:10]
    containers, sandbox_ids = [], []
    network = client.networks.create(project + "_main", labels={LABEL: project})
    server = ThreadingHTTPServer(("0.0.0.0", 18091), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = Path("/evidence") / (project + ".toml")
    config.write_text(
        '[server]\nhost="0.0.0.0"\nport=8080\n'
        'api_key="synthetic-controller-key-254-00000000"\n'
        '[runtime]\ntype="docker"\nexecd_image="opensandbox/execd:v1.0.17"\n'
        '[store]\ntype="sqlite"\npath="/tmp/sandboxes.db"\n'
        '[docker]\nnetwork_mode="' + network.name + '"\n'
        '[ingress]\nmode="direct"\n'
    )
    import httpx

    def box(name, service, image, command, *, controller=False):
        volumes = ({str(config): {"bind": "/etc/opensandbox/config.toml", "mode": "ro"},
                    "/var/run/docker.sock": {"bind": "/var/run/docker.sock"}}
                   if controller else {"/source": {"bind": "/fixture/repo/src/backend", "mode": "ro"}})
        kwargs = {} if controller else {"entrypoint": ["python"]}
        result = client.containers.run(
            image, command, name=project + "-" + name, network=network.name, detach=True,
            environment={"PYTHONPATH": "/fixture/repo/src/backend",
                         "DATABASE_URL": "sqlite:////tmp/e2e.sqlite", "AUTH_MODE": "mock",
                         "MEM0_ENABLED": "false"},
            labels={"com.docker.compose.project": project,
                    "com.docker.compose.service": service, LABEL: project},
            volumes=volumes, **kwargs,
        )
        containers.append(result)
        result.reload()
        return result

    def ip(container):
        container.reload()
        return container.attrs["NetworkSettings"]["Networks"][network.name]["IPAddress"]

    def endpoint_snapshot(container):
        container.reload()
        return {name: info["IPAddress"] for name, info in
                container.attrs["NetworkSettings"]["Networks"].items()}

    def route(container):
        result = container.exec_run(["python", "-c",
            "import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
            "s.connect(('198.18.0.1',18091)); print(s.getsockname()[0])"])
        assert result.exit_code == 0, result.output
        return result.output.decode().strip()

    def ready(url, container):
        with httpx.Client(timeout=1) as http:
            for _ in range(150):
                try:
                    if http.get(url).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
        raise AssertionError(container.logs().decode()[-4000:])

    http = httpx.Client(timeout=45, headers={
        "OPEN-SANDBOX-API-KEY": "synthetic-controller-key-254-00000000"})
    try:
        controller = box("controller", "opensandbox", CONTROLLER_IMAGE, None, controller=True)
        backend = box("backend", "backend", BACKEND_IMAGE,
                      ["/fixture/repo/src/backend/tests/sandbox/fixtures/egress_gateway.py"])
        desktop = box("desktop", "desktop", SMALL_IMAGE, ["-c", "import time; time.sleep(600)"])
        controller_url = "http://" + ip(controller) + ":8080"
        ready(controller_url + "/health", controller)
        gateway = ip(backend)
        ready("http://" + gateway + ":18092/health", backend)
        snapshots = {b.id: endpoint_snapshot(b) for b in (backend, controller)}
        routes = {b.id: route(b) for b in (backend, controller)}
        initial_ids = {n.id for n in client.networks.list(names=[project])}

        def stable():
            for b in (backend, controller):
                assert endpoint_snapshot(b) == snapshots[b.id], "Shared service endpoints changed"
                assert route(b) == routes[b.id], "Default egress changed"
            assert {n.id for n in client.networks.list(names=[project])} == initial_ids

        def create_sandbox():
            response = http.post(controller_url + "/sandboxes", json={
                "image": {"uri": BACKEND_IMAGE},
                "timeout": 120, "entrypoint": ["/bin/sh", "-c", "sleep 600"],
                "resourceLimits": {"cpu": "0.5", "memory": "512Mi"},
                "metadata": {"hugagent-user-id": "synthetic-other-user"}})
            assert response.status_code < 300, response.text
            sid = response.json()["id"]
            sandbox_ids.append(sid)
            container = client.containers.get("sandbox-" + sid)
            assert set(container.attrs["NetworkSettings"]["Networks"]) == {network.name}
            assert container.attrs["HostConfig"]["PortBindings"], "Prior direct mode not restored"
            stable()
            return sid, container

        def command(container, text):
            address = "http://" + ip(container) + ":44772/command"
            for _ in range(100):
                try:
                    response = http.post(address, json={"command": text, "background": False})
                    if response.status_code < 300:
                        return response.text
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            raise AssertionError("execd command did not succeed")

        with ThreadPoolExecutor() as pool:
            future = pool.submit(desktop.exec_run, ["python", "-c", CLIENT.replace("GATEWAY", gateway)])
            assert Upstream.started.wait(10), "Model gateway never reached fixture upstream"
            try:
                for _ in range(3):
                    sid, sandbox = create_sandbox()
                    assert "rollback-ok" in command(sandbox, "printf rollback-ok")
                    result = http.delete(controller_url + "/sandboxes/" + sid)
                    assert result.status_code == 204, result.text
                    sandbox_ids.remove(sid)
                    stable()
            finally:
                Upstream.release.set()
            result = future.result(timeout=10)
            assert result.exit_code == 0, result.output
            print(result.output.decode().strip(), flush=True)

        # Keep a real sandbox across a controller restart, then execute on it again.
        sid, sandbox = create_sandbox()
        assert "retained" in command(sandbox, "printf retained > /tmp/rollback-marker; cat /tmp/rollback-marker")
        original = sandbox.id
        controller.restart()
        ready(controller_url + "/health", controller)
        response = http.get(controller_url + "/sandboxes/" + sid)
        assert response.status_code == 200, response.text
        assert client.containers.get("sandbox-" + sid).id == original
        assert "retained" in command(sandbox, "cat /tmp/rollback-marker")
        stable()
        with httpx.Client(timeout=5) as gateway_http:
            result = gateway_http.post(
                "http://" + gateway + ":18092/v1/desktop/capability/gateway/models/fixture/responses",
                json={"input": "disconnect-before-headers", "stream": True})
            assert result.status_code == 502, result.text
        print(json.dumps({"docker": client.version()["Version"], "controller_cycles": 3,
                          "sandbox_commands": True, "controller_restart_retains_files": True,
                          "stable_endpoints_and_egress": True, "gateway_failure": 502,
                          "network_isolation": False}), flush=True)
    finally:
        Upstream.release.set()
        for sid in sandbox_ids:
            try:
                http.delete(controller_url + "/sandboxes/" + sid)
            except httpx.HTTPError:
                pass
        http.close()
        server.shutdown()
        for container in reversed(containers):
            container.remove(force=True)
        network.reload()
        for cid in list((network.attrs.get("Containers") or {})):
            client.containers.get(cid).remove(force=True)
        network.remove()
        config.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
