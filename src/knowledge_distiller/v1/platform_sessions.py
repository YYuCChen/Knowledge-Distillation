"""Six isolated persistent browser sessions; reuse existing platform raw readers."""
from .douyin_session import DouyinOwnedSession
from .chrome import ChromeSessionError, _read_endpoint
from .opencli_session import read_opencli

PLATFORMS = {
    'douyin': ('抖音', 'https://www.douyin.com/user/self', ['https://www.douyin.com'],
               "Boolean(document.querySelector('[data-e2e=\"user-info\"]')?.getClientRects().length)"),
    'youtube': ('YouTube', 'https://www.youtube.com/', ['https://www.youtube.com', 'https://accounts.google.com'],
                'Boolean(window.ytcfg?.get("LOGGED_IN"))'),
    'xiaohongshu': ('小红书', 'https://www.xiaohongshu.com/explore', ['https://www.xiaohongshu.com'],
                    "(()=>{const x=window.__INITIAL_STATE__?.user?.loggedIn;return (x===true||x?.value===true)&&!Array.from(document.querySelectorAll('.login-container')).some(e=>e.getClientRects().length)})()"),
    'x': ('X', 'https://x.com/home', ['https://x.com'],
          "Boolean(document.querySelector('[data-testid=\"AppTabBar_Profile_Link\"]'))"),
    'zhihu': ('知乎', 'https://www.zhihu.com/', ['https://www.zhihu.com','https://zhuanlan.zhihu.com'],
              "(async()=>{try{const r=await fetch('/api/v4/me?include=url_token',{credentials:'include'});return r.ok&&Boolean((await r.json()).url_token)}catch{return false}})()"),
    'weibo': ('微博', 'https://weibo.com/', ['https://weibo.com'],
              "(async()=>{try{let uid=document.querySelector('#app')?.__vue_app__?.config?.globalProperties?.$store?.state?.config?.config?.uid;if(!uid){const r=await fetch('/ajax/config/get_config',{credentials:'include'});uid=(await r.json()).data?.uid}if(!uid)return false;const r=await fetch('/ajax/profile/info?uid='+encodeURIComponent(uid),{credentials:'include'});const d=await r.json();return Boolean(r.ok&&d.data?.user?.id)}catch{return false}})()"),
}


class PlatformOwnedSession(DouyinOwnedSession):
    def __init__(self, store, root, platform, **kwargs):
        self.platform = platform
        self.label, self.home, self.cookie_urls, self.login_probe = PLATFORMS[platform]
        super().__init__(store, root, **kwargs)

    def _wait_login(self, page, timeout=180):
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = page.call('Runtime.evaluate', {'expression': self.login_probe,
                'returnByValue': True, 'awaitPromise': True}, page=True)
            if result.get('result', {}).get('value') is True:
                return True
            time.sleep(1)
        return False

    def _pending_key(self):
        return self.platform + '_pending_login'

    def _launch_manual(self, identifier):
        import os
        import subprocess
        from pathlib import Path
        self.close()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        profile = self.root / identifier
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(profile, 0o700)
        from .desktop_paths import chrome_executable
        executable = chrome_executable()
        if not executable.is_file():
            raise ChromeSessionError(self.platform + '_browser_missing')
        # Ordinary visible Chrome: no debugging, headless, or automation arguments.
        self._process = subprocess.Popen([str(executable), '--user-data-dir=' + str(profile), self.home],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._profile, self._headed = profile, True

    def verify(self):
        if self.platform not in {'youtube', 'x'}:
            return super().verify()
        from uuid import uuid4
        import json
        from .chrome import DouyinConnection
        from .keychain import KeychainError
        identifier = self.store.setting(self._pending_key())
        if not identifier:
            identifier = uuid4().hex
            self._launch_manual(identifier)
            self.store.set_settings({self._pending_key(): identifier})
            raise ChromeSessionError(self.platform + '_login_pending')
        if len(identifier) != 32 or any(c not in '0123456789abcdef' for c in identifier):
            raise ChromeSessionError(self.platform + '_browser_unavailable')
        # Called only by the user's explicit "完成登录" action.
        with self._lock:
            try:
                self.close()
                port = self._launch(identifier)
                with self._page(self.home, port) as page:
                    if not self._wait_login(page, timeout=25):
                        self._launch_manual(identifier)
                        raise ChromeSessionError(self.platform + '_login_pending')
                    rows = page.cookies(self.cookie_urls)
                    cookies = rows if self.platform == 'youtube' else {r['name']:r['value'] for r in rows}
                    if not cookies:
                        raise ChromeSessionError(self.platform + '_login_required')
                    secret = self._secret(identifier)
                    secret.save(json.dumps(cookies, ensure_ascii=False))
                    from .local_secrets import LocalSecrets
                    if isinstance(self.secret_factory, LocalSecrets):
                        self.secret_factory.mark_validated(self.platform + '-session-' + identifier)
                    try:
                        secret.set_label('知识蒸馏器｜' + self.label + '登录态｜' + identifier[-8:])
                    except KeychainError:
                        pass
                self.close()
                return DouyinConnection(None, 'owned:' + identifier)
            except ChromeSessionError:
                raise
            except Exception as error:
                self.close()
                raise ChromeSessionError(self.platform + '_browser_unavailable') from error

    def login_committed(self):
        self.store.set_settings({self._pending_key(): ''})

    def cancel_login(self):
        identifier = self.store.setting(self._pending_key())
        if identifier:
            active = self.store.connection(self.platform)
            if active is None or active['browser_context'] != 'owned:' + identifier:
                self.discard('owned:' + identifier)
            self.store.set_settings({self._pending_key(): ''})

    def read(self, url, context=None):
        current = 'owned:' + self._context()
        if context is not None and context != current:
            raise ChromeSessionError(self.platform + '_connection_changed')
        self.cookies()
        with self._lock:
            endpoint = _read_endpoint(self._launch(self._context()))
            result = read_opencli('xpost' if self.platform == 'x' else self.platform,
                                 self.platform, url, current, endpoint=endpoint)
        row = self.store.connection(self.platform)
        if row is None or row['state'] != 'connected' or row['browser_context'] != current:
            raise ChromeSessionError(self.platform + '_connection_changed')
        return result
