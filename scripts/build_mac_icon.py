"""Convert the user-supplied artwork to standard macOS icon sizes unchanged."""
from pathlib import Path
import subprocess
import tempfile

project = Path(__file__).resolve().parents[1]
source = project / 'packaging/assets/app-icon.png'
target = project / 'packaging/assets/KnowledgeDistiller-transparent.icns'
with tempfile.TemporaryDirectory(prefix='kd-icon-') as temporary:
    iconset = Path(temporary) / 'KnowledgeDistiller.iconset'
    iconset.mkdir()
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            pixels = size * scale
            name = f'icon_{size}x{size}' + ('@2x' if scale == 2 else '') + '.png'
            subprocess.run(['/usr/bin/sips', '-z', str(pixels), str(pixels), str(source),
                            '--out', str(iconset / name)], check=True, capture_output=True)
    subprocess.run(['/usr/bin/iconutil', '-c', 'icns', str(iconset), '-o', str(target)], check=True)
print(target)
