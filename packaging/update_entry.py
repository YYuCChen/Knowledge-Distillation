"""Independent frozen installer; its archive is outside the app being replaced."""
import sys
from knowledge_distiller.v1.update_installer import run

if __name__ == '__main__':
    if sys.argv[1:] == ['--probe']:
        print('ready')
        raise SystemExit(0)
    if len(sys.argv) != 2:
        raise SystemExit(2)
    raise SystemExit(run(sys.argv[1]))
