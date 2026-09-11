"""Present source declarations without inventing authors or publication times."""
from datetime import datetime
from html import escape
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit


PLATFORMS={'douyin':'抖音','bilibili':'B 站','youtube':'YouTube','xiaohongshu':'小红书',
           'x':'X','weibo':'微博','zhihu':'知乎','direct_text':'直接文本','markdown':'Markdown','pdf':'PDF','epub':'EPUB'}


def source_header(kind,metadata,url):
    authors=metadata.get('authors') or [metadata.get('author')]
    if not isinstance(authors,list):authors=[authors]
    names=[]
    for author in authors:
        name=(author.get('display_name') or author.get('name')) if isinstance(author,dict) else author
        if isinstance(name,str) and name.strip() and name.strip() not in names:names.append(name.strip())
    left='<span>'+escape(PLATFORMS.get(kind,kind))+'</span>'
    if names:left+='<span>'+escape('、'.join(names))+'</span>'
    right=''
    if isinstance(url,str) and urlsplit(url).scheme in {'http','https'}:
        right='<a href="'+escape(url,quote=True)+'" rel="noopener noreferrer">查看原帖 ↗</a>'
    published=metadata.get('published_at')
    if isinstance(published,str):
        try:
            try:value=datetime.fromisoformat(published.replace('Z','+00:00'))
            except ValueError:value=parsedate_to_datetime(published)
            if value.tzinfo is not None:
                right+='<time datetime="'+escape(value.isoformat(),quote=True)+'">'+value.astimezone().strftime('%Y年%m月%d日 %H:%M')+'</time>'
        except ValueError:pass
    return ['<div class="kd-source-meta"><div class="kd-source-meta-left">'+left+
            '</div><div class="kd-source-meta-right">'+right+'</div></div>','']
