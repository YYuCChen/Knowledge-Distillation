"""Prepare local, signed Sparkle assets. This script never uploads or publishes."""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import subprocess
import xml.etree.ElementTree as ET

p = argparse.ArgumentParser()
p.add_argument('--app', type=Path, required=True)
p.add_argument('--previous', type=Path)
p.add_argument('--output', type=Path, required=True)
p.add_argument('--sdk', type=Path, required=True)
p.add_argument('--notes', type=Path, required=True)
p.add_argument('--account', default='knowledge-distiller-updates')
p.add_argument('--test-key', type=Path)
p.add_argument('--testing', action='store_true')
a = p.parse_args()
if a.output.exists() and any(a.output.iterdir()): p.error('输出目录必须为空')
a.output.mkdir(parents=True, exist_ok=True)
info = plistlib.loads((a.app/'Contents/Info.plist').read_bytes())
if not a.testing and (info.get('KDUpdateTestDataRoot') or a.test_key): p.error('测试包/测试私钥不能作为正式发布内容')
if not info.get('SUPublicEDKey') or not info.get('SURequireSignedFeed'): p.error('应用未配置签名更新')
subprocess.run(['codesign', '--verify', '--deep', '--strict', str(a.app)], check=True)
version = info['CFBundleVersion']
namespace = 'http://www.andymatuschak.org/xml-namespaces/sparkle'
ET.register_namespace('sparkle', namespace)
s = '{'+namespace+'}'
rss = ET.Element('rss', version='2.0'); channel = ET.SubElement(rss, 'channel')
ET.SubElement(channel, 'title').text = '知识蒸馏器更新'
item = ET.SubElement(channel, 'item')
ET.SubElement(item, 'title').text = info['CFBundleShortVersionString']
ET.SubElement(item, s+'version').text = version
ET.SubElement(item, s+'shortVersionString').text = info['CFBundleShortVersionString']
ET.SubElement(item, s+'minimumSystemVersion').text = '14.0'
ET.SubElement(item, 'description').text = a.notes.read_text()
keyargs = ['--ed-key-file', str(a.test_key)] if a.test_key else ['--account', a.account]

def sign(path):
    return subprocess.check_output([str(a.sdk/'bin/sign_update'), *keyargs, '-p', str(path)], text=True).strip()

def enclosure(parent, path, **extra):
    signature = sign(path)
    subprocess.run([str(a.app/'Contents/MacOS/update-verify'), info['SUPublicEDKey'], signature, str(path)], check=True)
    return ET.SubElement(parent, 'enclosure', {'url':path.name, 'length':str(path.stat().st_size),
        'type':'application/octet-stream', s+'edSignature':signature, **extra})
archive = a.output/f'KnowledgeDistiller-{version}-macOS-arm64.zip'
subprocess.run(['ditto','-c','-k','--sequesterRsrc','--keepParent',str(a.app),str(archive)],check=True)
enclosure(item, archive)
if a.previous:
    previous = plistlib.loads((a.previous/'Contents/Info.plist').read_bytes())
    if tuple(map(int, previous['CFBundleVersion'].split('.'))) >= tuple(map(int, version.split('.'))): p.error('构建号必须高于差量基线')
    if previous['CFBundleIdentifier'] != info['CFBundleIdentifier']: p.error('差量基线应用身份不同')
    delta = a.output/f'KnowledgeDistiller-{previous["CFBundleVersion"]}-{version}.delta'
    subprocess.run([str(a.sdk/'bin/BinaryDelta'), 'create', '--version=4', str(a.previous), str(a.app), str(delta)], check=True)
    enclosure(ET.SubElement(item, s+'deltas'), delta, **{s+'deltaFrom':previous['CFBundleVersion']})
feed = a.output/'appcast.xml'
feed.write_bytes(ET.tostring(rss, encoding='utf-8', xml_declaration=True)+b'\n')
sign(feed)
assets = []
for file in sorted(a.output.iterdir()):
    if file.is_file():
        h=hashlib.sha256()
        with file.open('rb') as stream:
            while chunk:=stream.read(1024*1024): h.update(chunk)
        assets.append({'name':file.name,'bytes':file.stat().st_size,'sha256':h.hexdigest()})
(a.output/'manifest.json').write_text(json.dumps({'version':version,'product_version':info['CFBundleShortVersionString'],'manual_update_only':info.get('KDManualUpdateOnly', True),'testing':a.testing,'assets':assets},indent=2))
print(json.dumps(assets, indent=2))
