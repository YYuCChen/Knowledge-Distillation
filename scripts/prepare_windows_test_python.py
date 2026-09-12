"""Create an explicit disposable UTF-8 launcher from the current 3.11 build env."""
import argparse
from pathlib import Path
import runpy
import shutil
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    runpy.run_path(str(project/'src/knowledge_distiller/v1/adapters/python_policy.py'))['check_current']()
    if sys.platform != 'win32':
        parser.error('Windows only')
    if args.output.exists():
        parser.error('Use a new disposable output directory')
    args.output.mkdir(parents=True)
    # Copy the complete verified environment so this helper never patches the
    # build interpreter or links to a historical environment.
    shutil.copytree(Path(sys.prefix), args.output/'environment')
    import win32api
    target = args.output/'environment/Scripts/python.exe'
    handle = win32api.BeginUpdateResource(str(target), False)
    win32api.UpdateResource(handle, 24, 1, (project/'packaging/windows.manifest').read_bytes(), 0)
    win32api.EndUpdateResource(handle, False)
    print(target)


if __name__ == '__main__':
    main()
