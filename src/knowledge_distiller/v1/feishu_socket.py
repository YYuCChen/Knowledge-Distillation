"""Owned SDK child process. No model, source collection, or API writes here."""
import json
import sys


def worker_main():
    import lark_oapi as lark
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
    from .local_secrets import LocalSecrets

    config=json.loads(sys.stdin.readline())
    app_id=config['app_id']

    def dispatch(kind,data=None):
        payload=json.loads(lark.JSON.marshal(data)) if data is not None else {}
        print('KD_FEISHU\t'+json.dumps({'kind':kind,'payload':payload},ensure_ascii=False),flush=True)
        line=sys.stdin.readline()
        if not line:
            raise SystemExit(0)
        response=json.loads(line)
        if response.get('failed'):
            raise RuntimeError('feishu_local_handler_failed')
        return response

    handler=(lark.EventDispatcherHandler.builder('','')
             .register_p2_im_message_receive_v1(lambda event:dispatch('message',event))
             .register_p2_card_action_trigger(lambda event:P2CardActionTriggerResponse(dispatch('action',event)))
             .register_p2_im_message_message_read_v1(lambda event:None)
             .build())
    class ConnectedClient(lark.ws.Client):
        # SDK 1.7.3 only exposes a reconnect callback. Report initial success
        # at the same completed handshake boundary, never just process startup.
        async def _connect(self):
            await super()._connect()
            dispatch('reconnected')

    client=ConnectedClient(app_id,LocalSecrets(config['credentials_root'])('feishu-app-'+app_id).load(),
                          event_handler=handler,log_level=lark.LogLevel.ERROR)
    client.start()


if __name__=='__main__':
    worker_main()
