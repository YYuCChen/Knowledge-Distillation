"""Ordered user image messages, downloaded only with the bound bot identity."""
import base64
import hashlib
from io import BytesIO
import json
from PIL import Image
from .file_sources import SubmittedSource
from .source_parsing import ParsedSource, ParsedMedia, SourceReadError


def blocks(kind, content):
    data=json.loads(content)
    if kind=='image':
        result=[{'tag':'img','image_key':data['image_key']}]
    elif kind=='post':
        if 'content' not in data:
            data=data.get('zh_cn') or data.get('en_us') or next(iter(data.values()))
        result=[]
        if data.get('title'):result.append({'tag':'text','text':data['title']+'\n'})
        for row in data['content']:
            for entry in row:
                if entry['tag']=='text':result.append({'tag':'text','text':entry['text']})
                elif entry['tag']=='a':result.append({'tag':'text','text':entry['text']+' '+entry['href']})
                elif entry['tag']=='img':result.append({'tag':'img','image_key':entry['image_key']})
                else:raise ValueError('图片消息含当前无法完整读取的内容，请分开发送图片和正文。')
            result.append({'tag':'text','text':'\n'})
    else:return None
    if not any(b['tag']=='img' for b in result):raise ValueError('请发送含原图的图片或图文消息。')
    if len(result)>1000 or sum(b['tag']=='img' for b in result)>50:raise ValueError('图片消息过大，请分批发送。')
    for b in result:
        field='image_key' if b['tag']=='img' else 'text'
        if not isinstance(b[field],str) or (field=='image_key' and not b[field]):raise ValueError('图片消息格式无法读取。')
    return result


def prepare(app_id, message_id, entries, api):
    values=[]
    for entry in entries:
        if entry['tag']=='text':values.append(entry);continue
        content=api.download_message_image(message_id,entry['image_key'])
        if not content or len(content)>20*1024*1024:raise ValueError('图片为空或超过20MB，请重新发送。')
        with Image.open(BytesIO(content)) as image:
            mime=Image.MIME.get(image.format)
            image.verify()
        if mime not in {'image/png','image/jpeg','image/webp','image/bmp','image/tiff'}:raise ValueError('图片格式不受支持，请发送 PNG、JPEG 或 WebP 原图。')
        values.append({**entry,'mime_type':mime,'content':base64.b64encode(content).decode()})
    content=json.dumps({'app_id':app_id,'message_id':message_id,'blocks':values},ensure_ascii=False,sort_keys=True).encode()
    return SubmittedSource('image',hashlib.sha256(content).hexdigest(),'飞书图片',content,{})


def parse(source, ocr):
    if hashlib.sha256(source.content).hexdigest()!=source.source_key:raise SourceReadError('image_snapshot_mismatch')
    data=json.loads(source.content)
    media=[];members=[];native=''
    for entry in data['blocks']:
        if entry['tag']=='text':native+=entry['text'];continue
        key=f'image-{len(media)+1}'
        content=base64.b64decode(entry['content'],validate=True)
        native+=f'〔图片 {len(media)+1}〕'
        media.append(ParsedMedia(key,entry['mime_type'],content))
        members.append({'member_id':key,'mime_type':entry['mime_type'],'content':content,'sha256':hashlib.sha256(content).hexdigest()})
    from .image_source import image_source_fact
    fact,lineage=image_source_fact(native,members,ocr,inline_images=True)
    return ParsedSource(fact.snapshot,{'source_title':'飞书图片','message_id':data['message_id'],
        'app_id':data['app_id'],'content_scope':{'description':'用户在已绑定飞书会话投递的图片与文字'}},lineage,tuple(media),fact.uncertainties)
