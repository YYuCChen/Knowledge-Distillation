import sys
import json
from pathlib import Path
from knowledge_distiller.v1.adapters.python_policy import check_current
from knowledge_distiller.v1.windows_update_installer import run,recover
if __name__ == '__main__':
    runtime = check_current()
    if len(sys.argv) == 3 and sys.argv[1] == '--runtime-report':
        Path(sys.argv[2]).write_text(json.dumps(dict(python=runtime, frozen=bool(getattr(sys, 'frozen', False)))), encoding='utf-8')
        raise SystemExit(0)
    raise SystemExit(recover(sys.argv[2],int(sys.argv[3])) if sys.argv[1]=='--recover' else run(sys.argv[1]))
