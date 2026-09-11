"""User-authorized foreground readers preserve connection identity and generation."""
from .chrome import ChromeSessionError, DouyinConnection
from .opencli_session import read_opencli
from .platform_sessions import PLATFORMS, PlatformOwnedSession


class PlatformForegroundSession:
    def __init__(self, store, root, platform, *, owned=None, reader=read_opencli):
        self.platform = platform
        self.store = store
        self.owned = owned or PlatformOwnedSession(store, root, platform)
        self.reader = reader

    def verify(self):
        # The user explicitly connects from Settings. Chrome/extension approval
        # remains interactive; this application never clicks approval controls.
        result = self.reader(self.platform, self.platform, PLATFORMS[self.platform][1], None)
        context = result.get('contextId')
        if result.get('loggedIn') is not True or not isinstance(context, str) or not context or ':' in context:
            raise ChromeSessionError(self.platform + '_browser_unavailable')
        return DouyinConnection(None, 'foreground:' + context)

    def read(self, url, context=None):
        row = self.store.connection(self.platform)
        if row is None or row['state'] != 'connected' or not row['browser_context']:
            raise ChromeSessionError(self.platform + '_login_required')
        current = row['browser_context']
        generation = row['generation']
        if context is not None and context != current:
            raise ChromeSessionError(self.platform + '_connection_changed')
        if current.startswith('owned:'):
            return self.owned.read(url, current)
        native_context = current.removeprefix('foreground:')
        try:
            result = self.reader(self.platform, self.platform, url, native_context)
        except ChromeSessionError:
            self._check_current(current, generation)
            raise
        self._check_current(current, generation)
        if result.get('contextId') != native_context:
            raise ChromeSessionError(self.platform + '_connection_changed')
        return {**result, 'contextId': current}

    def _check_current(self, context, generation):
        row = self.store.connection(self.platform)
        if row is None or row['state'] != 'connected' or row['browser_context'] != context or row['generation'] != generation:
            raise ChromeSessionError(self.platform + '_connection_changed')

    def discard(self, context):
        # Foreground connections own no browser profile or cookies to delete.
        if context and context.startswith('owned:'):
            self.owned.discard(context)

    def close(self):
        self.owned.close()
