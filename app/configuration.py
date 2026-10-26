"""Validate configuration before atomically replacing files on disk."""
import os
import tempfile
from pathlib import Path


def replace_configuration(path: str, content: bytes, validate):
    destination = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=destination.suffix, delete=False) as file:
            temporary = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        validate(str(temporary))
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
