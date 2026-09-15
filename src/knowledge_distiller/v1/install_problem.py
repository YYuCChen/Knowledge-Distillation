"""Resource-aware errors without exposing signed URL credentials in UI/logs."""
from dataclasses import dataclass, asdict
import re
from urllib.parse import urlsplit, urlunsplit
import httpx

from .updates import UpdateError

_ROLES={'manifest':'发行信息','base':'应用基础文件','docling':'文档识别组件','delta':'目标更新文件',
        'candidate':'安装内容','data':'知识与设置位置','shortcut':'桌面入口'}
_ACTIONS={'not_found':'请重新检查发行资源，或补齐同一发行的离线文件。',
 'auth':'请检查资源访问权限后重试。','server':'下载服务暂时不可用，请稍后重试。',
 'network':'网络连接中断或超时，请检查网络后重试。','size':'文件大小不符，已拒绝使用；请重新取得同一发行文件。',
 'hash':'文件内容校验失败，已拒绝使用；请重新取得同一发行文件。',
 'resume_identity':'续传身份不符，已拒绝使用本次响应；请重新检查资源后重试。',
 'signature':'真实性校验失败，请取得可信发行文件；不要关闭校验。',
 'schema':'发行信息格式无效，请检查发行文件与安装器版本。',
 'protocol':'发行协议不兼容，请使用适用的安装器。',
 'tree':'安装内容校验失败，请重新检查同一发行文件。',
 'version':'目标版本不符，请重新检查发行信息。',
 'path':'请核对完整路径、目录归属与访问权限。','space':'请在所选位置腾出足够空间后重试。',
 'cancelled':'准备已停止，已验证下载和恢复材料保留。','io':'请检查文件权限或占用，恢复材料已保留。'}


def redact(text):
    def clean(match):
        try:
            u=urlsplit(match.group(0));return urlunsplit((u.scheme,u.hostname or '',u.path,'',''))
        except ValueError:return '[URL]'
    return re.sub(r'https?://[^\s\"\'<>]+',clean,str(text))


@dataclass
class InstallProblem(UpdateError):
    stage:str
    resource_role:str
    asset_name:str
    expected_identity:str
    category:str
    http_status:int|None=None
    retryable:bool=False
    accepted:bool=False
    data_state:str='unchanged'
    detail_ref:str=''

    def __str__(self):
        role=_ROLES.get(self.resource_role,self.resource_role)
        lead=('当前发布尚未提供此平台的发行信息。' if self.resource_role=='manifest' and self.category=='not_found'
              else role+'不可取得。' if self.category=='not_found' else role+'需处理。')
        safety=('安装已接受，新程序和新数据保留。' if self.accepted else
                '已恢复原程序和安装前数据。' if self.data_state=='restored' else
                '程序未被替换，已有有效缓存保留。' if self.data_state=='unchanged' else
                '安装恢复状态待核实，请使用恢复入口，勿重新安装覆盖。')
        return lead+_ACTIONS[self.category]+safety

    def to_dict(self):return asdict(self)


def problem_from(error, *, stage, role, asset=None, accepted=False, data_state='unchanged'):
    if isinstance(error,InstallProblem):return error
    category='io';status=None
    text=str(error)
    if isinstance(error,httpx.HTTPStatusError):
        status=error.response.status_code
        category='not_found' if status==404 else 'auth' if status in (401,403) else 'server' if status>=500 else 'protocol'
    elif isinstance(error,httpx.RequestError):category='network'
    elif isinstance(error,InterruptedError):category='cancelled'
    elif isinstance(error,FileNotFoundError):category='not_found'
    else:
        # These are the existing validated domain failures; no download/cache
        # algorithm changes are needed to add the resource context.
        for needles,kind in [(['续传'],'resume_identity'),(['空间'],'space'),(['大小','过大'],'size'),(['签名'],'signature'),
            (['协议','Python','运行时','不兼容','契约'],'protocol'),(['版本'],'version'),
            (['内容校验','已变化'],'hash' if role!='candidate' else 'tree'),(['最终程序','程序树','基线'],'tree'),
            (['清单','格式','字段'],'schema'),(['目录','路径','位置'],'path')]:
            if any(part in text for part in needles):category=kind;break
    asset=asset or {}
    name=urlsplit(asset.get('url','')).path.rsplit('/',1)[-1]
    return InstallProblem(stage,role,name,asset.get('sha256',''),category,status,
        category in {'network','server','io'},accepted,data_state,redact(text))
