"""Install the trusted desktop driver in existing permanent computers without rebuilding them."""

import base64
import hashlib
import shlex
from pathlib import Path

from personal_agent.device_link import driver

from personal_agent.computer.sandbox import SANDBOX_PYTHON, install_once

CONTENT = Path(driver.__file__).read_bytes()
DIGEST = hashlib.sha256(CONTENT).hexdigest()
DRIVER_PATH = "/opt/personal-agent/driver-" + DIGEST[:16] + ".py"
# Root owns the file. The desktop user can run it but cannot change it.
INSTALL = ("import base64,os,sys;p=sys.argv[2];t=p+'.tmp'\n"
           "open(t,'wb').write(base64.b64decode(sys.argv[1]));os.chmod(t,0o755);os.replace(t,p)\n")

async def ensure_driver(provider, box, uid):
    """Return the root-owned driver path for the current backend driver version."""
    command = (SANDBOX_PYTHON + " -I -c " + shlex.quote(INSTALL) + " "
               + shlex.quote(base64.b64encode(CONTENT).decode()) + " " + shlex.quote(DRIVER_PATH))
    await install_once(provider, box, uid, "driver", DIGEST, command)
    return DRIVER_PATH
