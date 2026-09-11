"""Compatibility entry point for the existing Zhihu foreground connection."""
from .foreground_session import PlatformForegroundSession


class ZhihuForegroundSession(PlatformForegroundSession):
    def __init__(self, store, root, **kwargs):
        super().__init__(store, root, 'zhihu', **kwargs)
