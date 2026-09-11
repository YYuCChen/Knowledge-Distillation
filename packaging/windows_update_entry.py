import sys
from knowledge_distiller.v1.windows_update_installer import run,recover
if __name__ == '__main__':
    raise SystemExit(recover(sys.argv[2],int(sys.argv[3])) if sys.argv[1]=='--recover' else run(sys.argv[1]))
