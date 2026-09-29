"""Safely unpack a verified runtime cache for application smoke tests."""
import os
from pathlib import Path
import sys
import tarfile


def extract(archive, destination):
    destination = str(Path(destination).resolve())
    if os.name == "nt" and not destination.startswith("\\\\?\\"):
        destination = ("\\\\?\\UNC\\" + destination[2:]
                       if destination.startswith("\\\\")
                       else "\\\\?\\" + destination)
    with tarfile.open(archive) as bundle:
        bundle.extractall(destination, filter="data")


if __name__ == "__main__":
    extract(*sys.argv[1:])
