"""Compose existing source-browser runners; never claims native Dock evidence."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--case',choices=['protocol','slow-open'],required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--data-dir',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists() or args.data_dir.exists():raise ValueError('fresh output and data directory required')
    args.output.mkdir(parents=True)
    root=Path(__file__).resolve().parents[1]
    runner=root/'tests/v1/desktop'/('run_browser_protocol.py' if args.case=='protocol' else 'slow_open_browser.py')
    session='kd-quality-'+uuid.uuid4().hex
    process=None
    try:
        with (args.output/'fixture.stdout.log').open('wb') as stdout,(args.output/'fixture.stderr.log').open('wb') as stderr:
            process=subprocess.Popen([sys.executable,str(root/'tests/v1/desktop/browser_fixture.py'),
                '--data-dir',str(args.data_dir)],stdout=stdout,stderr=stderr,cwd=root)
            deadline=time.monotonic()+20
            while not (args.data_dir/'server.json').is_file():
                if process.poll() is not None:raise RuntimeError('fixture exited before ready')
                if time.monotonic()>deadline:raise TimeoutError('fixture readiness timeout')
                time.sleep(.05)
            port=json.loads((args.data_dir/'server.json').read_text())['port']
            # slow_open requires a new output; preserve wrapper logs separately.
            destination=args.output/'journey'
            with (args.output/'runner.stdout.log').open('wb') as runout,(args.output/'runner.stderr.log').open('wb') as runerr:
                result=subprocess.run([sys.executable,str(runner),'--port',str(port),'--session',session,
                    '--output',str(destination)],cwd=root,stdout=runout,stderr=runerr,timeout=240)
            for path in destination.iterdir():
                if path.is_file() and not path.is_symlink():(args.output/path.name).write_bytes(path.read_bytes())
            return result.returncode
    finally:
        # This session name is generated here and cannot target a user session.
        try:subprocess.run(['agent-browser','--session',session,'close'],capture_output=True,timeout=10)
        except (OSError,subprocess.TimeoutExpired):pass
        if process is not None and process.poll() is None:
            process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait()


if __name__=='__main__':raise SystemExit(main())
