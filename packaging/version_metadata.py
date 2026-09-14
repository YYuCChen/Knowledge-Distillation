"""Render native version metadata from the existing candidate request values."""
from pathlib import Path
import runpy


def native_versions(product, build):
    return runpy.run_path(str(Path(__file__).with_name('mac_signing.py')))['version_info'](product, build)


def windows_version_resource(product, build):
    native_versions(product, build)
    file_version = tuple(map(int, build.split('.')))
    product_version = tuple(map(int, product.split('.')))
    product_version += (0,) * (4 - len(product_version))
    if any(value > 65535 for value in file_version + product_version):
        raise ValueError('Windows version components must fit unsigned 16-bit fields')
    strings = {'FileDescription': 'Knowledge Distiller Installer',
               'FileVersion': build, 'ProductVersion': product,
               'ProductName': 'Knowledge Distiller',
               'InternalName': 'KnowledgeDistillerInstaller',
               'OriginalFilename': 'KnowledgeDistillerInstaller.exe'}
    entries = ',\n'.join(f'StringStruct({key!r}, {value!r})' for key, value in strings.items())
    return f'''VSVersionInfo(
 ffi=FixedFileInfo(filevers={file_version!r}, prodvers={product_version!r},
  mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
 kids=[StringFileInfo([StringTable('040904B0', [{entries}])]),
       VarFileInfo([VarStruct('Translation', [1033, 1200])])])\n'''
