"""Independent frozen installer; its archive is outside the app being replaced."""
import sys
import json
from pathlib import Path
from knowledge_distiller.v1.adapters.python_policy import check_current
from knowledge_distiller.v1.update_installer import run

if __name__ == '__main__':
    runtime = check_current()
    if len(sys.argv) == 3 and sys.argv[1] == '--runtime-report':
        Path(sys.argv[2]).write_text(json.dumps(dict(python=runtime, frozen=bool(getattr(sys, 'frozen', False)))), encoding='utf-8')
        raise SystemExit(0)
    if sys.argv[1:] == ['--probe']:
        print('ready')
        raise SystemExit(0)
    if len(sys.argv) != 2:
        raise SystemExit(2)
    raise SystemExit(run(sys.argv[1]))
