"""Complete public screen walkthrough; simulation state belongs to one page only."""
import csv
import io
import json
from pathlib import Path
from decimal import Decimal
from nicegui import ui


class DemoScreens:
    def __init__(self, adapter):
        self.adapter = adapter
        self.root = Path(__file__).resolve().parents[1]
        self.data = json.loads((self.root/'data/demo_company/source/DEMO_DATA.json').read_text('utf-8'))
        self.costs = self.data['costs']
        self.navigate = lambda tab: None
        self.chats = [
            {'id': 1, 'title': '7월 물류비 검증 요약', 'messages': [('user','이번 달 청구 내역을 요약해줘'), ('assistant',self.answer('요약'))]},
            {'id': 2, 'title': '소모품 수량 차이 확인', 'messages': [('user','소모품 때문에 다른 건을 알려줘'), ('assistant',self.answer('소모품'))]},
        ]
        self.active = 1
        self.next_id = 3
        self.evidence = []
        self.rules = [{'id':1,'항목':'B2B 상품 수량','기준':'소모품 제외 비교','범위':'이번 2029-07 시연','상태':'사용 중'}]
        self.decisions = []
        self.render_messages = lambda: None
        self.render_library = lambda: None

    def note(self, text='시연 화면 · AI 미연결 · 입력한 내용은 현재 접속 화면에서만 유지됩니다.'):
        with ui.row().classes('w-full items-center gap-2 rounded-lg p-3').style('background:#F5EFE7'):
            ui.icon('science',color='primary')
            ui.label(text).classes('text-sm')

    def answer(self, question):
        d=self.data
        if any(x in question for x in ('소모품','차이','원인')):
            samples=[s for s in d['scenarios'] if s['type']=='소모품 제외 시 일치']
            lines=[f"{s['order']}: 청구 {s['claim']:,}EA / WMS {s['wms']:,}EA. WMS의 쇼핑백 24EA를 제외하면 청구 수량과 같습니다." for s in samples]
            return '시연 답변 · 가상 원본에 넣은 차이 사례입니다.\n\n'+'\n\n'.join(lines)+'\n\n상품교체 청구에는 산술 차이 36,000원, 상품해체에는 작업량 차이 12PCS도 있습니다. 수량 차이를 금액 차이로 환산하지 않습니다. 실제 Python 판정은 확인 목록에서 조회하세요.'
        if any(x in question for x in ('자료','등록','8월','다음','월별')):
            return '시연 답변 · 월별 검증에서 회사·센터·기간을 지정하고 청구서, WMS 입고·출고, 계약·단가표, 상품 Master를 확인합니다. 현재 실제 계산 대상은 포함된 2029-07 가상 자료입니다. 8월 등록·파일 선택은 화면 체험이며 새 검증 결과로 저장하지 않습니다.'
        if any(x in question for x in ('보관','증빙','잠정')):
            return '시연 답변 · 가상 보관 구역 6개를 모두 채웠습니다. F구역은 작업량 증빙을 일부러 비워 둔 확인 사례입니다. 시설 지원 2건과 포장기 임대도 증빙 미확인 사례입니다. 추가 증빙 화면에서 자료 선택·항목 연결·비교 흐름을 체험할 수 있습니다.'
        if any(x in question for x in ('기준','단가','수식','처리')):
            return '시연 답변 · 아래 예외·비교 기준 관리에서 단가, 소모품 포함 여부, 기간·연결키, BOX 환산, 조건·수식을 선택하고 미리보기·적용·중지·복원을 체험하세요. 시연 저장은 이 접속 화면에만 적용되며 실제 계산 판정은 유지됩니다.'
        return (f"시연 답변 · 아르보코스 / 솔빛센터 / 2029-07\n\n청구 {d['invoice_lines']}항목, 공급가액 {d['supply_amount']:,}원, 부가세 {d['vat']:,}원, 합계 {d['total_with_vat']:,}원입니다.\n\n"
                f"입고 {d['inbound_lines']}행 · B2B {d['b2b_orders']}주문 · B2C {d['b2c_parcels']:,}송장을 가상 원본으로 구성했습니다. 입출고·보관·부자재·택배/용차·기타·차감을 모두 조회할 수 있습니다.\n\n"
                '이 응답은 준비된 가상 자료 설명입니다. AI 추론을 실행하지 않았습니다. 대시보드와 확인 목록은 실제 Python 계산 결과입니다.')

    def active_chat(self):
        return next(c for c in self.chats if c['id']==self.active)

    def new_chat(self):
        self.active=self.next_id;self.next_id+=1
        self.chats.insert(0,{'id':self.active,'title':'새 대화','messages':[]})
        self.render_messages();self.render_library();self.navigate('대화')

    def seed_prompt(self, text):
        self.navigate('대화');self.prompt.set_value(text);self.prompt.run_method('focus')

    def chat(self):
        with ui.column().classes('w-full gap-4').props('data-testid=demo-chat-screen'):
            with ui.row().classes('w-full items-center justify-between'):
                title=ui.label('검증 에이전트와 대화').classes('title')
                with ui.row():
                    ui.button('새 대화',icon='add',on_click=self.new_chat).props('outline no-caps')
                    ui.button('대화 보관함 열기',icon='folder_open',on_click=lambda:self.navigate('채팅 보관함')).props('flat no-caps')
            self.note()
            with ui.row().classes('gap-2'):
                for label in ('청구 요약','소모품 차이 원인','증빙이 필요한 항목','다음 월 자료 등록'):
                    ui.button(label,on_click=lambda v=label:self.seed_prompt(v)).props('outline no-caps dense')
            @ui.refreshable
            def render():
                chat=self.active_chat();title.set_text(chat['title'])
                with ui.column().classes('w-full gap-3').props('data-testid=demo-chat-log role=log aria-live=polite'):
                    if not chat['messages']:
                        ui.label('질문을 입력하거나 위 예시를 선택해 대화를 시작하세요.').classes('muted py-6')
                    for i,(role,body) in enumerate(chat['messages']):
                        with ui.row().classes('w-full justify-end' if role=='user' else 'w-full'):
                            with ui.card().classes('gap-2 max-w-full').style('max-width:880px;background:'+('#F5EFE7' if role=='user' else '#fff')):
                                with ui.row().classes('w-full justify-between items-center'):
                                    ui.label('나' if role=='user' else '물류비 검증 · 시연').classes('font-semibold')
                                    ui.button(icon='delete_outline',on_click=lambda n=i:self.delete_message(n)).props('flat round dense aria-label="메시지 삭제"')
                                ui.label(body).style('white-space:pre-wrap;overflow-wrap:anywhere')
            render();self.render_messages=render.refresh
            self.prompt=ui.textarea('메시지',placeholder='예: 소모품 수량 차이를 설명해줘').props('outlined autogrow maxlength=1000 data-testid=demo-chat-input').classes('w-full')
            with ui.row().classes('w-full justify-between items-center'):
                ui.label('AI 호출 없이 준비된 사례로 대화 흐름을 체험합니다.').classes('muted')
                ui.button('메시지 보내기',icon='send',on_click=self.send).props('unelevated no-caps data-testid=demo-chat-send')
            self.prompt.on('keydown.enter',self.send,js_handler='(e)=>{if(!e.shiftKey){e.preventDefault();emit();}}')
            with ui.expansion('예외·비교 기준 관리',icon='bookmark_border').classes('w-full').props('data-testid=demo-rules-expansion'):
                self.management_buttons()
                self.rules_panel=ui.column().classes('w-full');self.refresh_rules()

    def send(self):
        question=(self.prompt.value or '').strip()[:1000]
        if not question:return
        chat=self.active_chat()
        if len(chat['messages'])>=100:ui.notify('새 대화에서 이어서 체험해 주세요.');return
        if not chat['messages']:chat['title']=question[:35]
        chat['messages'].extend([('user',question),('assistant',self.answer(question))])
        self.prompt.set_value('');self.render_messages();self.render_library()

    def delete_message(self,index):
        self.active_chat()['messages'].pop(index);self.render_messages();self.render_library()

    def library(self):
        ui.label('채팅 보관함').classes('title')
        self.note('가상 대화와 이번 접속의 시연 대화입니다. 새로고침하면 초기화됩니다.')
        search=ui.input('대화 검색').props('outlined clearable data-testid=demo-library-search').classes('w-full')
        @ui.refreshable
        def render():
            with ui.column().classes('w-full gap-3').props('data-testid=demo-library-list'):
                found=[c for c in self.chats if (search.value or '').casefold() in c['title'].casefold()]
                if not found:ui.label('검색 조건에 해당하는 대화가 없습니다.')
                for c in found:
                    with ui.card().classes('w-full gap-3'):
                        ui.label(c['title']).classes('font-semibold text-lg')
                        ui.label(f"메시지 {len(c['messages'])}개 · 가상 시연 기록").classes('muted')
                        with ui.row():
                            ui.button('대화 열기',on_click=lambda key=c['id']:self.open_chat(key)).props('outline no-caps')
                            ui.button('이름 변경',on_click=lambda key=c['id']:self.rename_chat(key)).props('flat no-caps')
                            ui.button('삭제',on_click=lambda key=c['id']:self.remove_chat(key)).props('flat no-caps')
        render();self.render_library=render.refresh;search.on_value_change(lambda:render.refresh())
        ui.button('새 대화 만들기',icon='add',on_click=self.new_chat).props('unelevated no-caps')

    def open_chat(self,key):
        self.active=key;self.render_messages();self.navigate('대화')

    def rename_chat(self,key):
        chat=next(c for c in self.chats if c['id']==key)
        with ui.dialog().props('data-testid=demo-rename-dialog') as dialog,ui.card().classes('w-96 max-w-full'):
            title=ui.input('대화 이름',value=chat['title']).props('outlined maxlength=80').classes('w-full')
            def save():
                if not (title.value or '').strip():return
                chat['title']=title.value.strip();dialog.close();self.render_library();self.render_messages()
            with ui.row():ui.button('저장',on_click=save);ui.button('취소',on_click=dialog.close).props('flat')
        dialog.open()

    def remove_chat(self,key):
        with ui.dialog() as dialog,ui.card():
            ui.label('이 시연 대화를 삭제할까요?')
            def remove():
                self.chats=[c for c in self.chats if c['id']!=key]
                if not self.chats:self.new_chat()
                elif self.active==key:self.active=self.chats[0]['id']
                dialog.close();self.render_library();self.render_messages()
            ui.button('삭제 확인',on_click=remove);ui.button('취소',on_click=dialog.close).props('flat')
        dialog.open()

    def file_preview(self, name):
        source=self.root/'data/demo_company/source'
        with ui.dialog().props('data-testid=demo-file-dialog') as dialog,ui.card().classes('w-[1100px] max-w-full gap-4'):
            with ui.row().classes('w-full justify-between'):
                ui.label(name+' · 가상 원본').classes('text-lg font-semibold')
                ui.button('자료 닫기',on_click=dialog.close).props('flat no-caps')
            if name.endswith('.xlsx'):
                from portable_workbook import open_workbook,read_rows
                with open_workbook(source/name) as wb:sheet_names=wb.sheets
                content=ui.column().classes('w-full overflow-auto')
                def draw(sheet):
                    content.clear();rows=[];columns=set()
                    with open_workbook(source/name) as wb:
                        for i,cells in read_rows(wb,sheet):
                            if not any(c.get('value') is not None for c in cells.values()):continue
                            columns.update(cells)
                            rows.append({'row':i,**{col:str(c.get('value','')) for col,c in cells.items()}})
                            if len(rows)>=35:break
                    with content:
                        cols=sorted(columns,key=lambda v:(len(v),v))
                        self.table(rows,['row',*cols],labels={'row':'원본 행'})
                        ui.label('처음 35개 비어 있지 않은 행 · 열이 많으면 가로로 스크롤하세요.').classes('muted')
                select=ui.select(sheet_names,value=sheet_names[0],label='시트').props('outlined').classes('w-full')
                select.on_value_change(lambda e:draw(e.value));draw(select.value)
            elif name.endswith('.csv'):
                with (source/name).open(encoding='utf-8-sig',newline='') as f:
                    reader=csv.DictReader(f);rows=[]
                    for i,row in enumerate(reader):
                        rows.append(row)
                        if i>=34:break
                self.table(rows,list(rows[0]) if rows else [])
                ui.label('처음 35행 미리보기').classes('muted')
            else:
                ui.label('가상 계약 기준표').classes('font-semibold')
                self.table(self.costs,['label','unit','rate'],labels={'label':'항목','unit':'단위','rate':'가상 계약 단가'})
        dialog.open()

    @staticmethod
    def excel_col(index):
        result='';index+=1
        while index:index,r=divmod(index-1,26);result=chr(65+r)+result
        return result

    @staticmethod
    def table(rows, fields, labels=None):
        labels=labels or {}
        return ui.table(columns=[{'name':k,'field':k,'label':labels.get(k,k),'align':'left','sortable':True} for k in fields],rows=rows,pagination=8).props('flat wrap-cells=false').classes('w-full')

    def monthly(self):
        ui.label('월별 검증').classes('title')
        self.note('2029-07은 가상 원본을 실제 계산합니다. 신규월 등록은 시연이며 파일을 업로드하지 않습니다.')
        with ui.card().classes('w-full gap-4').props('data-testid=demo-monthly-screen'):
            with ui.row().classes('w-full gap-4'):
                ui.input('회사',value='아르보코스').props('outlined readonly')
                ui.input('센터',value='솔빛센터').props('outlined readonly')
                month=ui.select(['2029-07','2029-08'],value='2029-07',label='청구 월').props('outlined data-testid=demo-month')
            with ui.row().classes('w-full gap-4'):
                ui.input('비교 시작일',value='2029-07-01').props('outlined type=date')
                ui.input('비교 종료일',value='2029-07-25').props('outlined type=date')
            ui.label('1. 청구·대사 자료 확인').classes('text-lg font-semibold')
            files=[('청구서','claim.xlsx','78개 비용 · 입고 120행 · B2B 240건 · B2C 2,400건'),('WMS 입고','inbound_july.xlsx','121개 입고 행'),('WMS 출고','outbound_july.csv',f"{self.data['wms_rows']:,}개 출고 행"),('상품 Master','master.xlsx','가상 상품 12개 · 소모품 1개'),('계약·단가','contract.pdf','독립 가상 계약 기준'),('작업·정산 증빙','operational_evidence.csv','비용별 수량·단가·금액 78행')]
            for label,name,description in files:
                with ui.row().classes('w-full items-center justify-between p-3 rounded-lg').style('background:#FAF8F5'):
                    with ui.column().classes('gap-1'):
                        ui.label(label+' · '+name).classes('font-semibold');ui.label(description).classes('muted')
                    ui.button('자료 미리보기',icon='description',on_click=lambda n=name:self.file_preview(n)).props('flat no-caps')
            ui.label('2. 자료 추가·재사용 선택').classes('text-lg font-semibold')
            choices=ui.select({name:label+' · '+name for label,name,_ in files},multiple=True,value=['claim.xlsx','inbound_july.xlsx','outbound_july.csv'],label='체험할 가상 파일').props('outlined use-chips data-testid=demo-month-files').classes('w-full')
            ui.checkbox('기존 가상 계약·상품 Master 재사용',value=True)
            status=ui.label('자료를 선택한 뒤 등록 내역을 확인하세요.').props('data-testid=demo-month-registration')
            def register():
                if not choices.value:ui.notify('가상 파일을 선택해 주세요.');return
                status.set_text(f'{month.value} · {len(choices.value)}개 가상 파일 등록 시연 완료. 원본 등록 및 계산 결과는 변경되지 않습니다.')
            ui.button('자료 등록 시연',icon='inventory',on_click=register).props('outline no-caps data-testid=demo-month-register')
            ui.label('3. 검증 실행·결과 조회').classes('text-lg font-semibold')
            def calculate():
                if month.value!='2029-07':
                    status.set_text('2029-08 실행 시연: 자료 확인 → 수량·단가 계산 → 차이 원인 분석 → 보고서. 실제 신규월 계산은 실행하지 않았습니다.');return
                try:self.adapter.start('public_calculate');status.set_text('2029-07 가상 원본 실제 계산을 시작했습니다. 상단에서 진행 상태를 확인하세요.')
                except ValueError as exc:ui.notify(str(exc),type='warning')
            with ui.row():
                ui.button('선택 월 검증 실행',icon='play_arrow',on_click=calculate).props('unelevated no-caps')
                ui.button('7월 계산 결과 보기',icon='fact_check',on_click=lambda:self.navigate('확인 목록')).props('outline no-caps')
        return lambda *args:self.navigate('월별 검증')

    def evidence_screen(self):
        ui.label('추가 증빙').classes('title')
        self.note('포함된 가상 증빙으로 항목 연결·비교 화면을 체험합니다. 파일 업로드·실제 판정 변경은 하지 않습니다.')
        with ui.card().classes('w-full gap-4').props('data-testid=demo-evidence-screen'):
            ui.label('1. 청구 항목 연결').classes('text-lg font-semibold')
            item=ui.select({r['row']:f"{r['row']}행 · {r['label']} · {r['unit']} · {r['amount']:,}원" for r in self.costs},value=93,label='증빙을 연결할 비용').props('outlined use-input data-testid=demo-evidence-item').classes('w-full')
            axis=ui.select(['작업량','계약 단가','실제 지출 금액'],value='작업량',label='비교할 근거').props('outlined').classes('w-full')
            file=ui.select(['operational_evidence.csv','contract.pdf'],value='operational_evidence.csv',label='가상 증빙 선택').props('outlined').classes('w-full')
            ui.button('증빙 미리보기',icon='description',on_click=lambda:self.file_preview(file.value)).props('outline no-caps')
            ui.label('2. 범위·단위 확인').classes('text-lg font-semibold')
            ui.input('비교기간',value='2029-07-01 ~ 2029-07-25').props('outlined readonly').classes('w-full')
            confirmed=ui.checkbox('같은 청구 항목·기간·단위의 증빙임을 시연에서 확인')
            preview=ui.column().classes('w-full').props('data-testid=demo-evidence-preview')
            def compare():
                preview.clear();r=next(r for r in self.costs if r['row']==item.value)
                claim=r['quantity'] if axis.value=='작업량' else r['rate'] if axis.value=='계약 단가' else r['amount']
                val=claim-(12 if r['row']==56 and axis.value=='작업량' else 36000 if r['row']==55 and axis.value=='실제 지출 금액' else 0)
                with preview:
                    ui.label(f"시연 비교 · {r['label']}").classes('font-semibold')
                    self.table([{'청구값':claim,'비교값':val,'차이':claim-val,'결과':'일치 예시' if claim==val else '불일치 예시'}],['청구값','비교값','차이','결과'])
                    ui.label('이 표는 화면 체험을 위한 비교 예시입니다. 실제 검증 결과에 반영되지 않습니다.').classes('muted')
            ui.button('비교 미리보기',icon='compare_arrows',on_click=compare).props('outline no-caps data-testid=demo-evidence-compare')
            def register():
                if not confirmed.value:ui.notify('청구 범위·단위 확인을 체크해 주세요.',type='warning');return
                r=next(r for r in self.costs if r['row']==item.value)
                self.evidence.append({'항목':r['label'],'근거':axis.value,'자료':file.value,'상태':'등록 시연 완료'})
                draw.refresh();ui.notify('이번 접속의 시연 목록에 추가했습니다.')
            ui.button('증빙 연결 시연',icon='link',on_click=register).props('unelevated no-caps data-testid=demo-evidence-register')
            ui.label('3. 등록 이력').classes('text-lg font-semibold')
            @ui.refreshable
            def draw():
                self.table(self.evidence or [{'항목':'상품 보관 · A구역','근거':'작업량','자료':'operational_evidence.csv','상태':'가상 예시'}],['항목','근거','자료','상태'])
            draw()

    def management_buttons(self):
        with ui.row().classes('gap-2'):
            ui.button('청구 단가 비교',icon='price_change',on_click=lambda:self.rule_dialog('지정 단가')).props('outline no-caps data-testid=demo-rate-open')
            ui.button('조건·비교 기준 설정',icon='tune',on_click=self.rule_dialog).props('outline no-caps data-testid=demo-rule-open')
            ui.button('담당자 처리·이력',icon='assignment_turned_in',on_click=self.decision_dialog).props('outline no-caps data-testid=demo-decisions-open')

    def refresh_rules(self):
        self.rules_panel.clear()
        with self.rules_panel:
            for r in self.rules:
                with ui.card().classes('w-full gap-2'):
                    ui.label(f"{r['항목']} · {r['기준']} · {r['상태']}").classes('font-semibold')
                    ui.label(r['범위']+' · 시연 저장').classes('muted')
                    def toggle(row=r):
                        row['상태']='중지' if row['상태']=='사용 중' else '사용 중';self.refresh_rules()
                    ui.button('중지' if r['상태']=='사용 중' else '복원',on_click=toggle).props('flat no-caps')

    def rule_dialog(self,initial='소모품 포함/제외'):
        if not isinstance(initial,str):initial='소모품 포함/제외'
        with ui.dialog().props('data-testid=demo-rule-dialog') as dialog,ui.card().classes('w-[900px] max-w-full gap-4'):
            ui.label('비교 기준 설정 · 미리보기').classes('title')
            self.note('시연 기준을 저장하고 중지·복원할 수 있습니다. 실제 청구·원장·계산은 변경되지 않습니다.')
            item=ui.select({r['row']:f"{r['label']} · {r['unit']} ({r['row']}행)" for r in self.costs},value=20,label='대상 청구 항목').props('outlined use-input').classes('w-full')
            mode=ui.select(['소모품 포함/제외','지정 단가','기간·연결키','BOX·PCS 환산','B2C 주문 연결','조건·계산식'],value=initial,label='기준 종류').props('outlined data-testid=demo-rule-mode').classes('w-full')
            scope=ui.select(['이번 청구만','2029-08 이후 같은 회사·센터·항목'],value='이번 청구만',label='적용 범위').props('outlined').classes('w-full')
            fields=ui.column().classes('w-full');values={}
            def draw_fields():
                fields.clear();values.clear()
                with fields:
                    if mode.value=='소모품 포함/제외':values['policy']=ui.select(['소모품 제외','소모품 포함'],value='소모품 제외',label='수량 기준').props('outlined')
                    elif mode.value=='지정 단가':values['rate']=ui.number('비교 단가 (원)',value=300,min=0,precision=2).props('outlined')
                    elif mode.value=='BOX·PCS 환산':
                        values['pack']=ui.number('박스당 EA',value=24,min=1,precision=0).props('outlined')
                        values['sum']=ui.select(['품번 합산 후 환산','WMS 행별 환산 후 합산'],value='품번 합산 후 환산',label='환산 방법').props('outlined')
                    elif mode.value in ('기간·연결키','B2C 주문 연결'):
                        values['start']=ui.input('시작일',value='2029-07-01').props('outlined type=date')
                        values['end']=ui.input('종료일',value='2029-07-25').props('outlined type=date')
                        values['key']=ui.select(['주문번호','송장번호','주문번호 + 송장번호'],value='주문번호',label='연결키').props('outlined')
                    else:
                        values['condition']=ui.input('조건',value='수량 > 0').props('outlined maxlength=200').classes('w-full')
                        values['formula']=ui.input('계산식',value='수량 * 300').props('outlined maxlength=200').classes('w-full')
                        ui.label('계산식 입력·확인 시연입니다. 입력 문장을 실행하지 않습니다.').classes('muted')
            mode.on_value_change(draw_fields);draw_fields()
            preview=ui.column().classes('w-full').props('data-testid=demo-rule-preview');prepared={}
            def compare():
                prepared.clear();r=next(r for r in self.costs if r['row']==item.value)
                payload={k:v.value for k,v in values.items()};prepared.update(row=r.copy(),mode=mode.value,scope=scope.value,values=payload)
                preview.clear()
                with preview:
                    ui.label(f"시연 미리보기 · {r['label']} · {mode.value}").classes('font-semibold')
                    if mode.value=='지정 단가':
                        amount=Decimal(str(r['quantity']))*Decimal(str(payload['rate'] or 0))
                        ui.label(f"청구 {r['amount']:,}원 → 수량 {r['quantity']:,} × 단가 {payload['rate']:,} = {amount:,.2f}원 · 차이 {Decimal(r['amount'])-amount:,.2f}원")
                    else:
                        ui.label(' / '.join(f'{k}: {v}' for k,v in payload.items()))
                        ui.label('입력한 기준의 적용 순서를 체험합니다. 실제 수량 재대사·수식 실행은 수행하지 않았습니다.').classes('muted')
            ui.button('기준 미리보기',on_click=compare).props('outline no-caps data-testid=demo-rule-preview-button')
            def apply():
                current={k:v.value for k,v in values.items()}
                if not prepared or prepared['row']['row']!=item.value or prepared['mode']!=mode.value or prepared['scope']!=scope.value or prepared['values']!=current:
                    ui.notify('현재 입력한 기준으로 미리보기를 먼저 확인해 주세요.',type='warning');return
                self.rules.append({'id':len(self.rules)+1,'항목':prepared['row']['label'],'기준':mode.value,'범위':scope.value,'상태':'사용 중'})
                self.refresh_rules();dialog.close();ui.notify('시연 기준을 저장했습니다. 대화의 기준 관리에서 중지·복원할 수 있습니다.')
            with ui.row():
                ui.button('시연 기준 적용',on_click=apply).props('unelevated no-caps data-testid=demo-rule-apply')
                ui.button('기준 닫기',on_click=dialog.close).props('flat no-caps')
        dialog.open()

    def decision_dialog(self):
        with ui.dialog().props('data-testid=demo-decision-dialog') as dialog,ui.card().classes('w-[900px] max-w-full gap-4'):
            ui.label('담당자 처리·이력').classes('title');self.note('처리 완료·예외 인정·취소를 체험합니다. 실제 확인 필요 금액과 판정은 유지됩니다.')
            item=ui.select({r['row']:f"{r['label']} ({r['row']}행)" for r in self.costs},value=55,label='처리 대상').props('outlined').classes('w-full')
            action=ui.select(['처리 완료','예외 인정','완료 취소'],value='처리 완료',label='담당자 처리').props('outlined')
            reason=ui.textarea('처리 사유',value='가상 증빙과 청구 범위를 확인했습니다.').props('outlined maxlength=500').classes('w-full')
            @ui.refreshable
            def history():
                self.table(self.decisions or [{'항목':'시설 지원','처리':'잠정 확정 예시','사유':'가상 증빙 미확인 사례'}],['항목','처리','사유'])
            def apply():
                if not (reason.value or '').strip():ui.notify('시연 처리 사유를 입력해 주세요.');return
                r=next(r for r in self.costs if r['row']==item.value)
                self.decisions.insert(0,{'항목':r['label'],'처리':action.value+' · 시연','사유':reason.value.strip()[:500]});history.refresh()
            ui.button('담당자 처리 시연 적용',on_click=apply).props('unelevated no-caps data-testid=demo-decision-apply')
            history();ui.button('처리 닫기',on_click=dialog.close).props('flat no-caps')
        dialog.open()
