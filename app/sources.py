"""Read collector-format target files; resolve execution credentials on the worker."""
import os
import re
from pathlib import Path

import yaml


def load_sources(path: str) -> dict[str, dict]:
    location = Path(path)
    if not location.exists():
        raise ValueError("Sources path does not exist")
    files = [location] if location.is_file() else sorted([*location.glob("*.yaml"), *location.glob("*.yml")])
    result = {}
    for file in files:
        doc = yaml.safe_load(file.read_text()) or {}
        if not isinstance(doc, dict) or not isinstance(doc.get("sources", []), list):
            raise ValueError("Expected collector YAML with a sources list")
        for item in doc.get("sources", []):
            source = {**doc.get("defaults", {}), **item}
            target_id = source.get("id")
            if not isinstance(target_id, str) or not target_id or target_id in result:
                raise ValueError("Source IDs must be nonempty and unique")
            result[target_id] = source
    return result


def execution_uri(source: dict) -> str:
    uri = source.get("autotune_uri") or source.get("db_uri") or source.get("conn_str")
    if not isinstance(uri, str) or not uri:
        raise ValueError("Target must define db_uri, conn_str or autotune_uri")

    def replace(match):
        name = match.group(1)
        if name not in os.environ:
            raise ValueError(f"Missing environment variable: {name}")
        return os.environ[name]

    return re.sub(r"\$\{([A-Za-z0-9_]+)\}", replace, uri)
