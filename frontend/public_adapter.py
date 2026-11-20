"""Anonymous sample-data access: fixed calculations and read-only original UI."""
import threading
import time
import uuid
from agent_adapter import AgentAdapter


class PublicAdapter:
    public_web=True
    READ={'root','issues','review_items','review_token','rule_history',
          'rule_options','saved_memory','review_csv','rule_results_csv','review_report','claim_items'}

    def __init__(self):
        self._adapter=AgentAdapter()
        self._last_started=None
        self._previews=threading.BoundedSemaphore(2)

    def __getattr__(self,name):
        if name in self.READ:return getattr(self._adapter,name)
        raise AttributeError(name)

    def snapshot(self):
        value=self._adapter.snapshot()
        value.update(messages=[],questions=[],memory_preview={},disposition_preview={},rule_preview={},
                     command_intake=None,chat_selection={},chat_result={},model_calls=0)
        return value

    def calculation_status(self):
        job=self._adapter.job
        if job['status']=='running':return '가상 청구·WMS를 계산하고 있습니다.'
        if job['status']=='failed':return '계산하지 못했습니다. 마지막 완료 결과는 유지됩니다.'
        if job.get('action')=='public_calculate':return '계산 완료 · 미확정 항목은 결과에 남아 있습니다.'
        return '가상 자료의 계산 결과를 조회하고 있습니다.'

    def preview(self,*args,**kwargs):
        if not self._previews.acquire(blocking=False):
            raise ValueError('원본을 조회 중입니다. 잠시 후 다시 확인해 주세요.')
        try:return self._adapter.preview(*args,**kwargs)
        finally:self._previews.release()

    def start(self,action,**kwargs):
        if action!='public_calculate' or kwargs:
            raise ValueError('공개 체험에서는 포함된 가상 자료 계산만 실행합니다.')
        adapter=self._adapter
        with adapter._lock:
            if adapter._busy():raise ValueError('이미 계산 중입니다. 완료 후 확인해 주세요.')
            if self._last_started is not None and time.monotonic()-self._last_started<30:
                raise ValueError('최근 계산 결과를 확인해 주세요. 30초 후 다시 실행할 수 있습니다.')
            self._last_started=time.monotonic()
            conversation_id=adapter.library.data['active_id']
            adapter.job={'id':uuid.uuid4().hex,'action':action,'status':'running','started_at':time.time(),
                         'conversation_id':conversation_id,'invoice_month':'2029-07'}
            adapter._save_job()
            threading.Thread(target=adapter._run,args=({'action':action,'conversation_id':conversation_id},),daemon=True).start()
