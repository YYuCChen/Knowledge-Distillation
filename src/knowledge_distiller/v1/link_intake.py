"""Shared desktop/Feishu link intake on the existing source adapters."""
import re
from urllib.parse import urlsplit
from .intake import links_in, platform_for_url
from .chrome import ChromeSessionError
from .bilibili import BilibiliSourceError


class LinkIntake:
    def __init__(self, store, settings, collections, errors):
        self.store, self.settings, self.collections, self.errors = store, settings, collections, errors

    def submit(self, value, *, receipt_key=None, durable=False):
        platform = platform_for_url(links_in(value)[0])
        if platform == 'weibo':
            from .weibo import weibo_identity, connection_authority
            matches = links_in(value)
            if len(matches) != 1:
                raise ValueError('请一次提交一条微博内容链接。')
            weibo_identity(matches[0])
            try:
                connection_authority(self.store.connection('weibo'))
            except ChromeSessionError as error:
                raise ValueError(self.errors.get(str(error), '请先连接微博。')) from error
            item_id = self.store.create_item(matches[0], receipt_key=receipt_key)
        elif platform == 'zhihu':
            from .zhihu import zhihu_identity, connection_authority
            matches = links_in(value)
            if len(matches) != 1:
                raise ValueError('请一次提交一条知乎内容链接。')
            zhihu_identity(matches[0])
            try:
                connection_authority(self.store.connection('zhihu'))
            except ChromeSessionError as error:
                raise ValueError(self.errors.get(str(error), '请先连接知乎。')) from error
            item_id = self.store.create_item(matches[0], receipt_key=receipt_key)
        elif platform == 'x':
            from .xpost import xpost_identity, connection_authority
            matches = links_in(value)
            if len(matches) != 1:
                raise ValueError('请一次提交一条 X 帖文链接。')
            xpost_identity(matches[0])
            try:
                connection_authority(self.store.connection('x'))
            except ChromeSessionError as error:
                raise ValueError(self.errors.get(str(error), '请先连接 X。')) from error
            item_id = self.store.create_item(matches[0], receipt_key=receipt_key)
        elif platform == 'xiaohongshu':
            from .xiaohongshu import xiaohongshu_input, connection_authority
            matches = links_in(value)
            if len(matches) != 1:
                raise ValueError('请一次提交一条小红书笔记链接。')
            xiaohongshu_input(matches[0])
            try:
                connection_authority(self.store.connection('xiaohongshu'))
            except ChromeSessionError as error:
                raise ValueError(self.errors.get(str(error), '请先连接小红书。')) from error
            from .xiaohongshu import preview_title, XiaohongshuSourceError
            try:
                title, authority = preview_title(self.store, self.settings.xiaohongshu, matches[0])
            except XiaohongshuSourceError as error:
                raise ValueError(self.errors.get(str(error), '暂时无法读取笔记信息，请稍后重试。')) from error
            item_id = self.store.create_item(matches[0], title=title, expected_authority=authority, receipt_key=receipt_key)
        elif platform == 'youtube':
            from .youtube import youtube_identity, connection_authority
            matches = links_in(value)
            if len(matches) != 1:
                raise ValueError("请一次提交一条 YouTube 视频链接。")
            _, canonical = youtube_identity(matches[0])
            try:
                connection_authority(self.store.connection('youtube'))
            except ChromeSessionError as error:
                raise ValueError(self.errors.get(str(error), "请先连接 YouTube。")) from error
            item_id = self.store.create_item(canonical, receipt_key=receipt_key)
        elif platform == 'bilibili':
            try:
                preview = self.collections.preview(links_in(value), submitted_text=value, durable=durable)
                if not preview.get('single_url'):
                    return preview
                self.collections.dismiss(preview['token'])
                item_id = self.store.create_item(preview['single_url'], title=preview['scopes'][0].members[0].title, receipt_key=receipt_key)
            except BilibiliSourceError as error:
                raise ValueError(self.errors.get(str(error), 'B 站来源读取未完成，请重试。')) from error
        elif platform == 'douyin':
            links = links_in(value)
            needs_scope = len(links)>1 or urlsplit(links[0]).hostname=='v.douyin.com' or not re.fullmatch(r'/(?:video|note|gallery|slides|article)/[0-9]+/?',urlsplit(links[0]).path)
            if needs_scope:
                preview = self.collections.preview(links, submitted_text=value, durable=durable)
                if not preview.get('single_url'):
                    return preview
                self.collections.dismiss(preview['token'])
                item_id = self.store.create_item(links[0], receipt_key=receipt_key)
            else:
                item_id = self.store.create_item(douyin_url(value), receipt_key=receipt_key)
        else:
            raise ValueError('暂不支持这个来源链接，请提交支持的平台链接或选择完整文本。')
        return item_id


def douyin_url(value: str) -> str:
    matches = links_in(value)
    if len(matches) != 1:
        raise ValueError("当前请提交一条抖音作品链接。")
    candidate = matches[0]
    parsed = urlsplit(candidate)
    host = (parsed.hostname or "").casefold()
    if host not in {"v.douyin.com", "www.douyin.com", "douyin.com"}:
        raise ValueError("当前请提交一条抖音作品链接。")
    if host != "v.douyin.com" and not re.fullmatch(r"/(?:video|note|gallery|slides|article)/[0-9]+/?", parsed.path):
        raise ValueError("抖音主页或集合需要在后续范围确认中处理。")
    return candidate
