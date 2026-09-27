#!/usr/bin/env python3
"""Provision and reuse a local macOS code-signing identity without Apple enrollment.

Secrets stay in an owner-only directory and enter openssl/security through a file
or stdin, never command-line values or output. This does not add system trust.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys

def run(args, *, data=None):
    result = subprocess.run(args, input=data, capture_output=True, text=True)
    if result.returncode:
        # Tool output may contain password-bearing interactive input.
        raise RuntimeError(f"{Path(args[0]).name} {args[1]} operation failed (exit {result.returncode}); output suppressed")
    return result.stdout

def security_input(*args):
    # security's interactive interpreter accepts commands on stdin. Passing a
    # secret via -p/-P on the process command line would expose it to ps.
    result = subprocess.run(
        ["/usr/bin/security", "-i"],
        input=shlex.join(args) + "\n", text=True, capture_output=True,
    )
    output = result.stdout + result.stderr
    if result.returncode or re.search(r"(SecKeychain\w*|SecItem\w*):|security:.*(failed|error)", output, re.I):
        raise RuntimeError("Keychain operation failed; interactive output suppressed")

def private_write(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)

def certificate_fingerprint(cert):
    der = subprocess.run(
        ["/usr/bin/openssl", "x509", "-in", str(cert), "-outform", "DER"],
        capture_output=True, check=True,
    ).stdout
    return hashlib.sha1(der).hexdigest().upper()

def load_profile(directory):
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise RuntimeError("Signing directory must be owned by the build user and not a symlink")
    if directory.stat().st_mode & 0o077:
        raise RuntimeError("Signing directory must have permissions 0700")
    profile = json.loads((directory / "identity.json").read_text())
    if certificate_fingerprint(directory / "certificate.pem") != profile["certificate_sha1"]:
        raise RuntimeError("The persisted certificate no longer matches the pinned identity")
    for name in ["password", "identity.p12", "private-key.pem"]:
        path = directory / name
        if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise RuntimeError("Signing secret files must be owned by the build user with permissions 0600")
    return profile

def activate(directory, profile):
    password = (directory / "password").read_text().strip()
    keychain = str(directory / "signing.keychain-db")
    security_input("unlock-keychain", "-p", password, keychain)
    # Preserve the user's existing search list and default keychain.
    search = shlex.split(run(["/usr/bin/security", "list-keychains", "-d", "user"]))
    if keychain not in search:
        run(["/usr/bin/security", "list-keychains", "-d", "user", "-s", *search, keychain])
    identities = run(["/usr/bin/security", "find-identity", "-p", "codesigning", keychain])
    if profile["certificate_sha1"] not in identities:
        raise RuntimeError("The pinned certificate/private-key identity is unavailable in its keychain")

def create(directory, name):
    if directory.exists():
        if (directory / "identity.json").exists():
            return load_profile(directory)
        raise RuntimeError("Signing directory already exists without a completed profile; refusing to overwrite it")
    if not re.fullmatch(r"[A-Za-z0-9 ._-]{3,100}", name):
        raise RuntimeError("Use a 3–100 character ASCII signing name")
    directory.mkdir(mode=0o700, parents=True)
    password = secrets.token_hex(32)
    private_write(directory / "password", password + "\n")
    config = """[req]
distinguished_name=dn
prompt=no
x509_extensions=code_sign
[dn]
CN=SIGNING_NAME
[code_sign]
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=critical,codeSigning
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid
""".replace("SIGNING_NAME", name)
    private_write(directory / "certificate.cnf", config)
    passfile = "file:" + str(directory / "password")
    run(["/usr/bin/openssl", "req", "-new", "-x509", "-newkey", "rsa:3072",
         "-sha256", "-days", "3650", "-config", str(directory / "certificate.cnf"),
         "-keyout", str(directory / "private-key.pem"), "-out", str(directory / "certificate.pem"),
         "-passout", passfile])
    run(["/usr/bin/openssl", "pkcs12", "-export", "-inkey", str(directory / "private-key.pem"),
         "-in", str(directory / "certificate.pem"), "-out", str(directory / "identity.p12"),
         "-name", name, "-passin", passfile, "-passout", "stdin"], data=password + "\n")
    keychain = str(directory / "signing.keychain-db")
    security_input("create-keychain", "-p", password, keychain)
    security_input("unlock-keychain", "-p", password, keychain)
    security_input("import", str(directory / "identity.p12"), "-f", "pkcs12",
                   "-P", password, "-T", "/usr/bin/codesign", "-k", keychain)
    security_input("set-key-partition-list", "-S", "apple-tool:,apple:,codesign:",
                   "-s", "-k", password, keychain)
    run(["/usr/bin/security", "set-keychain-settings", "-t", "21600", "-u", keychain])
    profile = {"schema": 1, "name": name, "certificate_sha1": certificate_fingerprint(directory / "certificate.pem"),
               "mode": "self-signed", "keychain": keychain}
    private_write(directory / "identity.json", json.dumps(profile, indent=2) + "\n")
    # Keep a reusable helper outside volatile checkouts and never copy secrets
    # back into the repository or the artifact sync directory.
    shutil.copyfile(__file__, directory / "signing.py")
    os.chmod(directory / "signing.py", 0o700)
    return profile

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--name", default="Desktop Local Signing")
    parser.add_argument("--run", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise RuntimeError("Run this helper on the native Mac builder")
    os.umask(0o077)
    directory = args.directory.expanduser().absolute()
    profile = create(directory, args.name) if args.create else load_profile(directory)
    activate(directory, profile)
    if args.run:
        env = dict(os.environ)
        # This mode has no Apple notarization credentials. Avoid an inherited
        # Developer ID/PKCS12 configuration silently overriding the fixed key.
        for key in ["APPLE_CERTIFICATE", "APPLE_CERTIFICATE_PASSWORD", "APPLE_TEAM_ID",
                    "APPLE_ID", "APPLE_PASSWORD", "APPLE_API_KEY", "APPLE_API_ISSUER", "APPLE_API_KEY_PATH"]:
            env.pop(key, None)
        env.update(HUGAGENT_MACOS_SIGNING_MODE="self-signed",
                   APPLE_SIGNING_IDENTITY=profile["certificate_sha1"])
        return subprocess.call(args.run, env=env)
    print(json.dumps(profile, indent=2))
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        # Do not dump subprocess output or secret-bearing locals.
        print("Signing setup failed: " + str(error), file=sys.stderr)
        sys.exit(1)
