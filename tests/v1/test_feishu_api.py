import json

import httpx
import pytest

from knowledge_distiller.v1.feishu_api import FeishuAPI, FeishuAPIError


def test_bot_token_is_cached_and_history_uses_exact_bound_query():
    requests=[]
    def handle(request):
        requests.append(request)
        if request.url.path.endswith('/tenant_access_token/internal'):
            assert json.loads(request.content)=={'app_id':'new-app','app_secret':'test-secret'}
            return httpx.Response(200,json={'code':0,'tenant_access_token':'test-token','expire':7200})
        assert request.headers['Authorization']=='Bearer test-token'
        return httpx.Response(200,json={'code':0,'data':{'items':[],'has_more':False}})
    api=FeishuAPI('new-app',secret_loader=lambda:'test-secret',client=httpx.Client(transport=httpx.MockTransport(handle)))
    api.history({'container_id':'oc_bound','page_size':50})
    api.history({'container_id':'oc_bound','page_token':'next'})
    assert len(requests)==3
    assert requests[1].url.params['container_id']=='oc_bound'
    assert requests[2].url.params['page_token']=='next'


def test_error_does_not_include_private_response_content():
    api=FeishuAPI('new-app',secret_loader=lambda:'test-secret',client=httpx.Client(transport=httpx.MockTransport(
        lambda _:httpx.Response(200,json={'code':99991672,'msg':'secret text not for logs'}))))
    with pytest.raises(FeishuAPIError) as caught:api.history({})
    assert caught.value.code==99991672
    assert 'secret text' not in str(caught.value)


def test_reply_carries_stable_uuid_and_original_parent():
    captured=[]
    def handle(request):
        if request.url.path.endswith('/tenant_access_token/internal'):
            return httpx.Response(200,json={'code':0,'tenant_access_token':'token','expire':7200})
        captured.append(request)
        return httpx.Response(200,json={'code':0,'data':{'message_id':'om_reply'}})
    api=FeishuAPI('new-app',secret_loader=lambda:'test-secret',client=httpx.Client(transport=httpx.MockTransport(handle)))
    assert api.reply_card('om_source',{'schema':'2.0'},delivery_id='stable-id')=='om_reply'
    assert captured[0].url.path.endswith('/om_source/reply')
    assert json.loads(captured[0].content)['uuid']=='stable-id'
    assert json.loads(captured[0].content)['reply_in_thread'] is False
