"""Observe actual mount/RBAC separation without exporting any credential value."""

import json
import subprocess
from pathlib import Path


def probe(container, code):
    result = subprocess.run(
        ["docker", "exec", container, "python", "-c", code],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def main():
    mounts = {}
    code = "import json;from pathlib import Path;print(json.dumps({p:Path(p).exists() for p in ['/run/cloudops-reader/client.json','/run/cloudops-writer/client.json','/var/lib/cloudops-executor/maintenance.sock','/run/cloudops-dispatch/key']}))"
    for service in ("api", "worker"):
        value = probe(f"cloudops-e01-https-{service}-1", code)
        assert (
            value["/run/cloudops-reader/client.json"]
            and not value["/run/cloudops-writer/client.json"]
            and not value["/var/lib/cloudops-executor/maintenance.sock"]
        )
        assert value["/run/cloudops-dispatch/key"] == (service == "worker")
        mounts[service] = value
    common = """import json,os
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request
from urllib.error import HTTPError
from cloudops.features.workloads.kubernetes import Kubernetes
k=Kubernetes(os.environ.get('EXECUTOR_KUBE_CONFIG'))
entries=json.loads(Path(os.environ.get('EXECUTOR_ALLOWLIST',os.environ.get('WORKLOADS_FILE'))).read_text())
w=SimpleNamespace(**entries[0]); d=k.deployment(w)
headers={'Authorization':'Bearer '+k.token,'Content-Type':'application/json-patch+json'}
def denied(path,patch=None):
    request=Request(k.server+path,headers=headers,data=json.dumps(patch).encode() if patch is not None else None,method='PATCH' if patch is not None else 'GET')
    try:
        k.opener.open(request,timeout=8)
        raise AssertionError('Unexpected credential grant')
    except HTTPError as error:
        assert error.code==403,error.code
        return error.code
result={'secret_read':denied('/api/v1/namespaces/demo/secrets')}
"""
    reader = probe(
        "cloudops-e01-https-api-1",
        common
        + "result['deployment_patch']=denied(k.path(w.namespace,w.name),[{'op':'test','path':'/metadata/uid','value':w.uid},{'op':'test','path':'/metadata/resourceVersion','value':d['metadata']['resourceVersion']},{'op':'replace','path':'/spec/replicas','value':d['spec']['replicas']}]);print(json.dumps(result))",
    )
    writer = probe(
        "cloudops-e01-https-executor-1",
        common
        + "result['other_target_patch']=denied(k.path(w.namespace,'not-allowlisted-e01'),[{'op':'test','path':'/metadata/uid','value':w.uid}]);print(json.dumps(result))",
    )
    Path("tmp/e01-credential-boundaries.json").write_text(
        json.dumps({"mounts": mounts, "reader_denials": reader, "writer_denials": writer}, indent=2)
    )
    print(
        "PASS: API/worker have no writer credential or maintenance socket; API has no dispatch key. Real Kubernetes denies reader patches, reader/writer Secret reads and writer patches outside the named target."
    )


if __name__ == "__main__":
    main()
