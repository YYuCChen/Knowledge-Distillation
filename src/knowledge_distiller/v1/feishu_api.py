"""Feishu bot API, scoped to the explicitly configured application."""
from __future__ import annotations

import json
import threading
import time

import httpx




class FeishuAPIError(RuntimeError):
    def __init__(self, code):
        self.code = code
        # API bodies may contain private input. Keep them out of exceptions/logs.
        super().__init__(f'feishu_api_{code}')


class FeishuAPI:
    def __init__(self, app_id, *, secret_loader=None, client=None):
        self.app_id = app_id
        def missing_secret():
            raise FeishuAPIError('credential_missing')
        self.secret_loader = secret_loader or missing_secret
        self.client = client or httpx.Client(timeout=20)
        self._token, self._expires = None, 0
        self._lock = threading.Lock()

    def _data(self, response):
        if response.status_code >= 400:
            raise FeishuAPIError(f'http_{response.status_code}')
        try:
            data = response.json()
        except ValueError as error:
            raise FeishuAPIError('invalid_response') from error
        if not isinstance(data, dict) or data.get('code') != 0:
            code = data.get('code', 'invalid_response') if isinstance(data, dict) else 'invalid_response'
            raise FeishuAPIError(code)
        return data

    def _authorization(self):
        with self._lock:
            if self._token is None or time.monotonic() >= self._expires:
                data = self._data(self.client.post(
                    'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
                    json={'app_id':self.app_id, 'app_secret':self.secret_loader()}))
                token, expiry = data.get('tenant_access_token'), data.get('expire')
                if not isinstance(token, str) or not token or not isinstance(expiry, (int,float)) or expiry <= 60:
                    raise FeishuAPIError('invalid_token_response')
                self._token, self._expires = token, time.monotonic() + expiry - 60
            return {'Authorization':'Bearer ' + self._token}

    def request(self, method, path, **kwargs):
        if not path.startswith('/open-apis/') or '?' in path or '#' in path:
            raise ValueError('invalid Feishu API path')
        data = self._data(self.client.request(method, 'https://open.feishu.cn' + path,
                                             headers=self._authorization(), **kwargs))
        return data

    def bot_info(self):
        return self.request('GET', '/open-apis/bot/v3/info')['bot']

    def history(self, params):
        return self.request('GET', '/open-apis/im/v1/messages', params=params)['data']

    def send_card(self, chat_id, card, *, delivery_id):
        result = self.request('POST', '/open-apis/im/v1/messages',
            params={'receive_id_type':'chat_id'},
            json={'receive_id':chat_id,'msg_type':'interactive',
                  'content':json.dumps(card,ensure_ascii=False),'uuid':delivery_id})
        return result['data']['message_id']

    def update_card(self, message_id, card):
        if not message_id.startswith('om_') or not message_id.replace('_','').isalnum():
            raise ValueError('invalid Feishu message ID')
        self.request('PATCH', '/open-apis/im/v1/messages/' + message_id,
                     json={'content':json.dumps(card,ensure_ascii=False)})

    def reply_card(self, message_id, card, *, delivery_id):
        if not message_id.startswith('om_') or not message_id.replace('_','').isalnum():
            raise ValueError('invalid Feishu message ID')
        result=self.request('POST','/open-apis/im/v1/messages/'+message_id+'/reply',
            json={'msg_type':'interactive','content':json.dumps(card,ensure_ascii=False),
                  'reply_in_thread':False,'uuid':delivery_id})
        return result['data']['message_id']

    def download_message_image(self, message_id, image_key):
        import re
        if not re.fullmatch(r'om_[A-Za-z0-9_-]+',message_id) or not re.fullmatch(r'[A-Za-z0-9_-]+',image_key):
            raise ValueError('invalid Feishu resource identity')
        url='https://open.feishu.cn/open-apis/im/v1/messages/'+message_id+'/resources/'+image_key
        with self.client.stream('GET',url,headers=self._authorization(),params={'type':'image'}) as response:
            if response.status_code>=400:raise FeishuAPIError(f'http_{response.status_code}')
            content=bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content)>20*1024*1024:raise FeishuAPIError('image_too_large')
            return bytes(content)

    def upload_image(self, content):
        return self.request('POST','/open-apis/im/v1/images',
            data={'image_type':'message'},files={'image':('confirmation.png',content,'image/png')})['data']['image_key']

    def upload_audio(self, content, *, duration_ms):
        return self.request('POST','/open-apis/im/v1/files',
            data={'file_type':'opus','file_name':'confirmation.opus','duration':str(duration_ms)},
            files={'file':('confirmation.opus',content,'audio/ogg')})['data']['file_key']

    def close(self):
        self.client.close()
