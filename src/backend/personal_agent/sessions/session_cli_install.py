"""Install trusted CLI assets in existing permanent computers without rebuilding them."""

import base64
import hashlib
import json
from pathlib import Path
import shlex

from personal_agent.computer.sandbox import SANDBOX_PYTHON, install_once

SCRIPT = r"""import base64,json,os,pathlib,shlex,sys,uuid
sources=json.loads(base64.b64decode(sys.argv[1]));digest=sys.argv[2]
base=pathlib.Path('/opt/hugagent-cli/releases')/digest
base.mkdir(parents=True,exist_ok=True)
package=base/'hugagent_cli';package.mkdir(exist_ok=True)
for name,content in sources.items():
 fd=os.open(package/name,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o644)
 with os.fdopen(fd,'wb') as output: output.write(base64.b64decode(content))
 os.chmod(package/name,0o644)
launcher='/usr/local/bin/.hugagent-'+uuid.uuid4().hex
with open(launcher,'x') as output:
 code='import sys;sys.path.insert(0,'+repr(str(base))+');from hugagent_cli.main import main;raise SystemExit(main())'
 output.write('#!/bin/sh\nexec {python} -I -c '+shlex.quote(code)+' "$@"\n')
os.chmod(launcher,0o755);os.replace(launcher,'/usr/local/bin/hugagent')
print('CLI ready')
"""

async def ensure_cli(provider, box, uid):
    import hugagent_cli

    sources = {
        path.name: base64.b64encode(path.read_bytes()).decode()
        for path in Path(hugagent_cli.__file__).parent.glob("*.py")
    }
    bundle = json.dumps(sources, sort_keys=True)
    digest = hashlib.sha256(("isolated-loader-v1:" + bundle).encode()).hexdigest()
    command = (
        SANDBOX_PYTHON + " -I -c "
        + shlex.quote(SCRIPT.replace("{python}", SANDBOX_PYTHON))
        + " "
        + shlex.quote(base64.b64encode(bundle.encode()).decode())
        + " "
        + digest
    )
    await install_once(provider, box, uid, "cli", digest, command)
