"""Upload only the current confirmation excerpt, under the bound bot identity."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import wave


class FeishuMedia:
    def __init__(self,api,root):
        self.api=api
        self.root=Path(root)/hashlib.sha256(api.app_id.encode()).hexdigest()
        self.root.mkdir(parents=True,exist_ok=True)

    def _upload(self,kind,content,*,identity=None,**metadata):
        digest=hashlib.sha256(kind.encode()+(identity if identity is not None else content)).hexdigest()
        path=self.root/(digest+'.json')
        if path.exists():
            saved=json.loads(path.read_text())
            if saved.get('app_id')==self.api.app_id and saved.get('key'):
                return saved['key']
        key=self.api.upload_image(content) if kind=='image' else self.api.upload_audio(content,**metadata)
        # Cache contains only bot resource handles, never credentials or source bytes.
        with tempfile.NamedTemporaryFile(mode='w',dir=self.root,delete=False) as output:
            json.dump({'app_id':self.api.app_id,'key':key},output)
            temporary=Path(output.name)
        temporary.replace(path)
        return key

    def image(self,content):
        return self._upload('image',content)

    def audio(self,path):
        path=Path(path)
        identity=path.read_bytes()
        cached=self.root/(hashlib.sha256(b'audio'+identity).hexdigest()+'.json')
        if cached.exists():
            saved=json.loads(cached.read_text())
            if saved.get('app_id')==self.api.app_id and saved.get('key'):return saved['key']
        with wave.open(str(path),'rb') as source:
            duration=round(source.getnframes()*1000/source.getframerate())
        if not 0<duration<=11000:
            raise ValueError('confirmation_excerpt_duration_invalid')
        with tempfile.TemporaryDirectory(prefix='opus-',dir=self.root) as directory:
            target=Path(directory)/'confirmation.opus'
            result=subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(path),'-vn',
                '-c:a','libopus','-b:a','32k',str(target)],stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,timeout=20,check=False)
            if result.returncode!=0 or not target.is_file():
                raise ValueError('feishu_audio_conversion_failed')
            return self._upload('audio',target.read_bytes(),identity=identity,duration_ms=duration)
