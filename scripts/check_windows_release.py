"""Exercise an actual frozen executable with disposable data and no dev PATH."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--samples', type=Path, required=True)
    parser.add_argument('--documents', action='store_true')
    args = parser.parse_args()
    app, output, samples = args.app.resolve(), args.output.resolve(), args.samples.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error('Use a new disposable output directory')
    output.mkdir(parents=True, exist_ok=True)
    data = output / '中文 用户数据'
    env = {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'CONDA', 'VIRTUAL_ENV', 'KNOWLEDGE_DISTILLER'))}
    env.update(PATH=str(Path(os.environ['SystemRoot']) / 'System32'),
               LOCALAPPDATA=str(output / 'LocalAppData'),
               HF_HOME=str(output / 'empty-hf-cache'), HF_HUB_OFFLINE='1',
               PADDLE_PDX_CACHE_HOME=str(output / 'empty-paddle-cache'))
    command = [str(app / 'KnowledgeDistiller.exe'), '--data-dir', str(data), '--no-open']
    report = {'app': str(app), 'developer_path': False, 'checks': {}}
    diagnostic = output / 'runtime.json'
    with (output / 'process.log').open('wb') as log:
        check = command + ['--check-runtime', str(diagnostic), '--check-offline']
        if args.documents:
            check += ['--check-ocr-image', str(samples / 'ocr.png'), '--check-pdf', str(samples / 'native.pdf'),
                      '--check-epub', str(samples / 'sample.epub')]
        result = subprocess.run(check, cwd=output, env=env, stdout=log, stderr=log, timeout=900)
        assert result.returncode == 0, ('runtime failed', result.returncode, diagnostic)
        report['checks']['runtime'] = json.loads(diagnostic.read_text(encoding='utf-8'))
        original_database = None
        for launch in range(2):
            process = subprocess.Popen(command + ['--smoke-seconds', '25'], cwd=output, env=env,
                                       stdout=log, stderr=log)
            try:
                state = data / '.desktop-instance.json'
                deadline = time.monotonic() + 20
                while not state.is_file():
                    assert process.poll() is None, ('launcher exited', process.returncode)
                    assert time.monotonic() < deadline, 'startup timeout'
                    time.sleep(.1)
                instance = json.loads(state.read_text(encoding='utf-8'))
                base = f"http://127.0.0.1:{instance['port']}"
                for route in ('/', '/topics', '/insights', '/settings'):
                    with urllib.request.urlopen(base + route, timeout=10) as response:
                        assert response.status == 200
                        assert '知识蒸馏器' in response.read().decode('utf-8')
                second = subprocess.run(command, cwd=output, env=env, stdout=log, stderr=log, timeout=15)
                assert second.returncode == 0
                assert json.loads(state.read_text(encoding='utf-8')) == instance
                assert process.wait(timeout=40) == 0
                assert not state.exists()
                database = data / 'knowledge.sqlite3'
                assert database.is_file()
                if original_database is None:
                    original_database = database.stat().st_ino
                else:
                    assert database.stat().st_ino == original_database
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=15)
        report['checks']['lifecycle'] = 'four pages, single instance, two launches, same database, clean exit'
    report['ok'] = True
    (output / 'release-check.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(output / 'release-check.json')


if __name__ == '__main__':
    main()
