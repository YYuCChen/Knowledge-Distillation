"""Mandatory size gate after packaging and immediately before Release upload."""
import argparse
import json
from pathlib import Path

POLICY = json.loads((Path(__file__).resolve().parents[1]/'packaging/release_policy.json').read_text())


def check_size(size, *, windows_full=False):
    limits = {'github_asset':POLICY['github_asset_max_exclusive']}
    if windows_full:
        limits['windows_full'] = POLICY['windows_full_max_exclusive']
    if size < 0 or any(size >= limit for limit in limits.values()):
        raise ValueError(f'Release asset size {size} violates strict limits {limits}')
    return {'bytes':size, 'limits_exclusive':limits,
            'remaining_bytes':{name:limit-1-size for name,limit in limits.items()}}


def check_files(paths):
    records = []
    for path in paths:
        windows_full = path.name.endswith('-Windows-x64.zip') and not path.name.endswith('.delta.zip')
        records.append(dict(name=path.name, **check_size(path.stat().st_size,windows_full=windows_full)))
    return records


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('assets',type=Path,nargs='+')
    p.add_argument('--report',type=Path)
    args=p.parse_args()
    records=check_files(args.assets)
    value=json.dumps({'ok':True,'assets':records},indent=2)
    if args.report:args.report.write_text(value,encoding='utf-8')
    print(value)
