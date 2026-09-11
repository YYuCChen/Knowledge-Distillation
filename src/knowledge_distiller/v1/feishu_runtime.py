"""Own the SDK lifetime; keep network intake separate from the distill worker."""
import json
import logging
import os
import subprocess
import sys
import threading
import time

from .feishu_inbox import event_message


logger=logging.getLogger(__name__)


class FeishuRuntime:
    def __init__(self,inbox,api,intake,actions,synchronize,*,allow_pairing=False):
        self.inbox,self.api,self.intake,self.actions=inbox,api,intake,actions
        self.synchronize=synchronize
        self._stop=threading.Event()
        self._rescan=threading.Event()
        self._threads=[]
        self._process=None
        self._lifecycle=threading.Lock()
        self.state='stopped'
        self.error=None
        self.allow_pairing=allow_pairing
        from .feishu_action_queue import ActionQueue
        self.action_queue=ActionQueue(inbox,actions)

    def start(self):
        if any(t.is_alive() for t in self._threads):
            return
        if not self.allow_pairing:self.inbox.binding()
        self._stop.clear();self._rescan.set()
        self._threads=[threading.Thread(target=self._socket_loop,name='feishu-socket',daemon=True),
                       threading.Thread(target=self._intake_loop,name='feishu-intake',daemon=True),
                       threading.Thread(target=self._action_loop,name='feishu-actions',daemon=True)]
        for thread in self._threads:thread.start()

    def stop(self):
        with self._lifecycle:
            self._stop.set();self._rescan.set()
            process=self._process
        if process and process.poll() is None:
            process.terminate()
            try:process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill();process.wait(timeout=3)
        # Do not release the app/data ownership while intake can still write.
        # Active adapters have their own network/subprocess timeouts. Cancellation
        # below stops subsequent pages/parts; join owns the current call to exit.
        for thread in self._threads:thread.join()
        self.state='stopped'

    def _socket_loop(self):
        while not self._stop.is_set():
            self.state='connecting'
            command=([sys.executable,'--feishu-worker'] if getattr(sys,'frozen',False)
                     else [sys.executable,'-m','knowledge_distiller.v1.feishu_socket'])
            process=None
            try:
                with self._lifecycle:
                    if self._stop.is_set():break
                    process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,text=True,encoding='utf-8',bufsize=1,
                        env={**os.environ,'PYTHONUTF8':'1'})
                    self._process=process
                process.stdin.write(json.dumps({'app_id':self.inbox.app_id, 'credentials_root':str(self.inbox.store.path.parent / 'credentials')})+'\n');process.stdin.flush()
                for line in process.stdout:
                    if self._stop.is_set():break
                    if not line.startswith('KD_FEISHU\t'):continue
                    packet=json.loads(line.split('\t',1)[1])
                    try:
                        if packet['kind']=='message':
                            message=event_message(packet['payload'])
                            from .feishu_pairing import is_bound,accept
                            if self.allow_pairing and not is_bound(self.inbox):
                                if accept(self.inbox,message):self._rescan.set()
                            else:self.inbox.receive(message)
                            response={}
                        elif packet['kind']=='action':
                            response=self.action_queue.receive(packet['payload'])
                        elif packet['kind']=='reconnected':
                            self._rescan.set();response={}
                        else:raise ValueError('unknown socket packet')
                        self.state='connected'
                    except Exception as error:
                        # A failed durable receipt must not be ACKed as success.
                        self.error='feishu_local_handler_failed'
                        logger.error('Feishu local handler failed (%s)',type(error).__name__)
                        response={'failed':True}
                    process.stdin.write(json.dumps(response,ensure_ascii=False)+'\n');process.stdin.flush()
            except Exception as error:
                if not self._stop.is_set():
                    self.error='feishu_connection_failed'
                    logger.error('Feishu connection failed (%s)',type(error).__name__)
            finally:
                if process:
                    if process.poll() is None:
                        process.terminate()
                        try:process.wait(timeout=3)
                        except subprocess.TimeoutExpired:process.kill();process.wait()
                    for pipe in (process.stdin,process.stdout):
                        if pipe:pipe.close()
                self._process=None
            if not self._stop.is_set():
                self.state='reconnecting';self.error='feishu_connection_failed'
                self._rescan.set();self._stop.wait(5)

    def _action_loop(self):
        while not self._stop.is_set():
            try:
                if self.action_queue.process_one():
                    continue
            except Exception as error:
                self.error='feishu_action_failed'
                logger.error('Feishu action queue failed; durable action retained (%s)',type(error).__name__)
                self._stop.wait(2)
            self._stop.wait(.2)

    def _intake_loop(self):
        next_scan=0
        while not self._stop.is_set():
            if self.allow_pairing:
                from .feishu_pairing import is_bound
                if not is_bound(self.inbox):
                    self._stop.wait(2)
                    continue
            if self._rescan.is_set() or time.monotonic()>=next_scan:
                self._rescan.clear()
                try:
                    self.inbox.backfill(self.api.history,until_ms=int(time.time()*1000),cancelled=self._stop.is_set)
                    next_scan=time.monotonic()+60
                except InterruptedError:
                    if self._stop.is_set():return
                    raise
                except Exception as error:
                    self.error='feishu_history_failed'
                    logger.error('Feishu history failed (%s)',type(error).__name__)
                    next_scan=time.monotonic()+10
            # A durable input gets its receipt before potentially slow discovery.
            if self._stop.is_set():return
            try:self.synchronize()
            except Exception as error:
                self.error='feishu_sync_failed'
                logger.error('Feishu synchronization failed (%s)',type(error).__name__)
            # A history outage must not strand messages already committed by
            # the live connection. Each receipt retains its own queue identity.
            try:
                for receipt in self.inbox.pending():
                    if self._stop.is_set():return
                    try:
                        self.intake.process(receipt['message_id'],cancelled=self._stop.is_set)
                    except InterruptedError:
                        if self._stop.is_set():return
                        raise
                    except Exception as error:
                        self.intake._state((self.inbox.app_id,receipt['message_id']),
                                           'needs_desktop','飞书投递处理未完成，请在电脑查看。')
                        logger.error('Feishu intake failed (%s)',type(error).__name__)
                if self._stop.is_set():return
                self.synchronize()
            except Exception as error:
                self.error='feishu_sync_failed'
                logger.error('Feishu synchronization failed (%s)',type(error).__name__)
            self._stop.wait(2)
