"""Fetch the pinned upstream codec into an explicit build cache, never PATH."""
import argparse
import hashlib
from pathlib import Path
import sys
import urllib.request
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from knowledge_distiller.v1.windows_binary_patch import HASHES

ASSETS = {
    'windows64':('hdiffpatch_v5.1.3_bin_windows64.zip','77f141386e5d8f785c1c846e10fbbc19b6c05aa00e3f59cc44670fb3f0e2ae94'),
    'macos':('hdiffpatch_v5.1.3_bin_macos.zip','9e9d7318db1ea5607dbdde41e6614cfe5b73c589034b38ee91a79f4ca80a940c'),
}


def prepare(root, platform):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    name,expected=ASSETS[platform]
    archive=root/name
    if not archive.is_file() or hashlib.sha256(archive.read_bytes()).hexdigest()!=expected:
        temporary=root/(name+'.download')
        try:
            with urllib.request.urlopen('https://github.com/sisong/HDiffPatch/releases/download/v5.1.3/'+name,timeout=60) as source:
                data=source.read(4*1024**2+1)
            if len(data)>4*1024**2 or hashlib.sha256(data).hexdigest()!=expected:
                raise RuntimeError('HDiffPatch archive checksum mismatch')
            temporary.write_bytes(data);temporary.replace(archive)
        finally:temporary.unlink(missing_ok=True)
    destination=root/platform;destination.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        for tool in ('hdiffz','hpatchz'):
            tool += '.exe' if platform=='windows64' else ''
            data=z.read(platform+'/'+tool)
            if hashlib.sha256(data).hexdigest()!=HASHES[tool]:
                raise RuntimeError('HDiffPatch executable checksum mismatch')
            target=destination/tool
            if target.is_symlink():raise RuntimeError('Codec cache must not contain links')
            if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest()!=HASHES[tool]:
                target.write_bytes(data)
                if platform=='macos':target.chmod(0o755)
    return destination


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache',type=Path,required=True)
    parser.add_argument('--platform',choices=tuple(ASSETS),required=True)
    args=parser.parse_args()
    print(prepare(args.cache,args.platform))
