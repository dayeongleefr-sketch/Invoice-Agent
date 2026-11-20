"""Notifications derive from actual verification jobs, never from chat prose."""
import re

ACTIONS={'agent','evidence','monthly_verify','monthly_agent','monthly_evidence'}
TERMINAL={'completed','failed','awaiting_user_decision'}


def description(job):
    if not job or job.get('action') not in ACTIONS or not job.get('id'):return None
    status=job.get('status')
    if status not in TERMINAL|{'running'}:return None
    result=job.get('result') or {}
    month=job.get('month') or job.get('invoice_month')
    if not month:
        match=re.search(r'(20\d{2})\s*(?:년\s*|[-./])(0?[1-9]|1[0-2])(?:월|\b)',str(job.get('request') or result.get('request') or ''))
        if match:month=f'{match[1]}-{int(match[2]):02}'
    label=f'{month[:4]}년 {int(month[5:]):d}월' if isinstance(month,str) and re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])',month) else '요청한 청구'
    title={'running':'검증 실행 중','completed':'검증 완료','failed':'검증 실패','awaiting_user_decision':'사용자 답변 필요'}[status]
    message={'running':'완료되면 알림과 결과 보기 버튼을 표시합니다.',
        'completed':'검증 실행이 끝났습니다. 불일치·미검증 항목은 결과에서 확인해 주세요.',
        'failed':'이번 실행을 완료하지 못했습니다. 마지막 완료 결과는 보존되어 있습니다.',
        'awaiting_user_decision':'확인이 필요한 기준이 있어 답변을 기다리고 있습니다. 아직 검증 완료가 아닙니다.'}[status]
    return dict(id=job['id'],status=status,title=f'{label} · {title}',message=message,month=month,
        conversation_id=job.get('conversation_id'),finished_at=job.get('finished_at'),
        destination='monthly' if month and month!='2029-07' else 'dashboard')


class JobNotifications:
    def __init__(self):
        self.initialized=False
        self.observed=None
        self.delivered=set()

    def poll(self,job):
        item=description(job)
        if item is None:
            self.initialized=True
            return None
        marker=(item['id'],item['status'])
        if not self.initialized:
            self.initialized=True;self.observed=marker
            if item['status'] in TERMINAL:self.delivered.add(marker)
            return None
        prior=self.observed;self.observed=marker
        if item['status'] not in TERMINAL or marker in self.delivered or marker==prior:return None
        self.delivered.add(marker)
        return item
