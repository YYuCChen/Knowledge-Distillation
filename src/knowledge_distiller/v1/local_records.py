"""Private atomic records inside the owning task's temporary retention scope."""
import json
import os
import tempfile
from pathlib import Path


def write_record(path, value):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise OSError('record_symlink')
    fd, name = tempfile.mkstemp(prefix=path.stem+'-', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)
