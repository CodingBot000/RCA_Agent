"""Explicit private output only; never returns payloads or paths as public summaries."""

import json
import os
import stat
import uuid
from pathlib import Path


def write_artifact(directory, data):
    directory = Path(directory)
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = os.lstat(directory)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("private output directory must be owned and 0700")
    path = directory / (uuid.uuid4().hex + ".json")
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
    if len(encoded) > 262144:
        raise ValueError("artifact too large")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(encoded)
        out.flush()
        os.fsync(out.fileno())
