"""Render the existing installer SVG to reviewed ICO/ICNS assets on macOS.

Uses the existing Cocoa/Pillow build environment and native iconutil. It does
not alter the artwork or add an image generation dependency to either package.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile


def render(source, pixels):
    from AppKit import (NSImage, NSBitmapImageRep, NSGraphicsContext,
                        NSDeviceRGBColorSpace, NSCompositingOperationCopy, NSPNGFileType)
    artwork = NSImage.alloc().initWithContentsOfFile_(str(source))
    if artwork is None:
        raise ValueError('Cannot decode installer SVG')
    bitmap = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, pixels, pixels, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
    NSGraphicsContext.saveGraphicsState()
    try:
        NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(bitmap))
        artwork.drawInRect_fromRect_operation_fraction_(((0, 0), (pixels, pixels)),
            ((0, 0), artwork.size()), NSCompositingOperationCopy, 1.0)
    finally:
        NSGraphicsContext.restoreGraphicsState()
    return bytes(bitmap.representationUsingType_properties_(NSPNGFileType, {}))


def main():
    from PIL import Image
    project = Path(__file__).resolve().parents[1]
    source = project/'src/knowledge_distiller/v1/installer_assets/installer-logo.svg'
    assets = project/'packaging/assets'
    with tempfile.TemporaryDirectory(prefix='kd-installer-icon-') as temporary:
        iconset = Path(temporary)/'Installer.iconset'
        iconset.mkdir()
        master = Path(temporary)/'master.png'
        master.write_bytes(render(source, 1024))
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                name = f'icon_{size}x{size}' + ('@2x' if scale == 2 else '') + '.png'
                (iconset/name).write_bytes(render(source, size*scale))
        Image.open(master).save(assets/'installer-icon.ico', sizes=[(n,n) for n in (16,24,32,48,64,128,256)])
        subprocess.run(['/usr/bin/iconutil','-c','icns',str(iconset),'-o',str(assets/'installer-icon.icns')], check=True)
    manifest = {'source':source.relative_to(project).as_posix(),
                'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
                'method':'macOS NSImage SVG renderer; native iconutil; Pillow ICO',
                'files':{name:hashlib.sha256((assets/name).read_bytes()).hexdigest()
                         for name in ('installer-icon.ico','installer-icon.icns')}}
    (assets/'installer-icon.json').write_text(json.dumps(manifest, indent=2)+'\n')


if __name__ == '__main__':
    main()
