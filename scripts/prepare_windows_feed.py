"""Sign Windows release assets and an appcast on Mac; never publish or export keys."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET
from knowledge_distiller.v1.updates import parse_feed, version_key

NAMESPACE='http://www.andymatuschak.org/xml-namespaces/sparkle'
S='{'+NAMESPACE+'}'


def sign_file(path,sdk,account):
    # Sparkle appends the signed-feed trailer when signing an XML appcast.
    return subprocess.check_output([str(Path(sdk)/'bin/sign_update'),'--account',account,'-p',str(path)],text=True).strip()


def prepare(*,full,delta,version,from_version,notes,output,sdk,account,public_key,signer=sign_file,product_version='1.1'):
    full,delta,output=Path(full),Path(delta),Path(output)
    if version_key(version)<=version_key(from_version):
        raise ValueError('Target build must be newer than the delta baseline')
    if output.exists():
        raise ValueError('Use a new appcast output path')
    if output.name!='appcast-windows.xml':
        raise ValueError('Windows feed must be appcast-windows.xml')
    for path in (full,delta):
        if not path.is_file() or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,180}',path.name):
            raise ValueError('Invalid release asset: '+path.name)
    ET.register_namespace('sparkle',NAMESPACE)
    rss=ET.Element('rss',version='2.0');channel=ET.SubElement(rss,'channel')
    ET.SubElement(channel,'title').text='知识蒸馏器 Windows 更新'
    item=ET.SubElement(channel,'item')
    ET.SubElement(item,'title').text='知识蒸馏器 V'+product_version
    ET.SubElement(item,S+'version').text=version
    ET.SubElement(item,S+'shortVersionString').text=product_version
    ET.SubElement(item,'description').text=notes
    def enclosure(parent,path,**extra):
        ET.SubElement(parent,'enclosure',{'url':path.name,'length':str(path.stat().st_size),
                                         'type':'application/octet-stream',S+'edSignature':signer(path,sdk,account),**extra})
    enclosure(item,full)
    enclosure(ET.SubElement(item,S+'deltas'),delta,**{S+'deltaFrom':from_version})
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_bytes(ET.tostring(rss,encoding='utf-8',xml_declaration=True)+b'\n')
    signer(output,sdk,account)
    release=parse_feed(output.read_bytes(),public_key,from_version)
    if not release or release['version']!=version:
        raise ValueError('Signed Windows feed failed verification')
    return {'feed':str(output),'version':version,'from_version':from_version,
            'full':release['full'],'selected':release['selected'],'published':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--full',type=Path,required=True)
    p.add_argument('--delta',type=Path,required=True)
    p.add_argument('--version',required=True)
    p.add_argument('--from-version',required=True)
    p.add_argument('--product-version',default='1.1')
    p.add_argument('--notes',type=Path,required=True)
    p.add_argument('--sdk',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--account',default='knowledge-distiller-updates')
    p.add_argument('--update-config',type=Path,default=Path(__file__).resolve().parents[1]/'packaging/update_config.json')
    a=p.parse_args()
    public_key=json.loads(a.update_config.read_text())['public_key']
    print(json.dumps(prepare(full=a.full,delta=a.delta,version=a.version,from_version=a.from_version,
                             notes=a.notes.read_text(encoding='utf-8'),output=a.output,sdk=a.sdk,account=a.account,
                             public_key=public_key,product_version=a.product_version),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
