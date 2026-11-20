"""Monthly intake, explicit review, execution and result selection in the local UI."""
from datetime import date, datetime, timezone, timedelta
import calendar
import csv
import io
import re
from pathlib import Path
from nicegui import ui
from date_inputs import calendar_input, billing_period
from agent_adapter import ROOT, safe_file, load
from dashboard_view import display_rows, filter_issues, format_number, excel_lookup, render_locations


def render_contract_documents(result, download_url):
    documents=result.get('contract_documents',[])
    if not documents:return
    kinds={'renewal':'자동연장','actual_cost':'실비','agreement':'별도협의',
           'amendment':'계약 변경','surcharge':'추가요금','period':'계약기간'}
    with ui.expansion('계약서 분석 · 단가와 적용 조건',icon='description').classes('w-full').props('data-testid=contract-analysis'):
        for document in documents:
            ui.label(document['name']).classes('font-semibold')
            ui.link('계약서 원본',download_url(document['path']),new_tab=True)
            ui.label(document['period_reason']).classes('text-sm muted')
            if document.get('error'):ui.label(document['error']).classes('text-sm text-amber-700')
            data=[]
            for i,row in enumerate(document['rates']):
                data.append({'id':i,'label':row['label'],'unit':row['unit'],
                    'rate':format_number(row['rate']),'source':f"{row['page']}쪽" if row.get('page') else row.get('location',''),
                    'state':'원문 숫자 확인' if row.get('text_verified') else '문자 인식 후보 · 원문 확인 필요'})
            if data:
                columns=[{'name':key,'field':key,'label':label,'align':'right' if key=='rate' else 'left'} for key,label in
                         [('label','계약 항목'),('unit','단위'),('rate','단가(원)'),('source','원본'),('state','읽기 결과')]]
                ui.table(columns=columns,rows=data,row_key='id',pagination=10).classes('w-full').props('data-testid=contract-rates')
            if document['clauses']:
                with ui.expansion('기간·실비·추가요금 조항').classes('w-full'):
                    for clause in document['clauses']:
                        ui.label(f"{kinds.get(clause['kind'],clause['kind'])} · {clause['page']}쪽").classes('font-semibold text-sm')
                        ui.label(clause['excerpt']).classes('text-sm')
                        if not clause.get('text_verified'):ui.label('문자 인식 결과 · 원문 확인 필요').classes('text-xs muted')


def render_monthly(adapter, download_url):
    files=[];inputs={};uploaders={};last={'signature':None};selected={'month':None,'rows':[]}
    ui.label('월별 청구 검증').classes('title')
    ui.label('2029년 7월은 기존 대시보드에서 조회합니다. 새 월은 별도 저장하며 7월의 잠정 결정은 승계하지 않습니다.').classes('notice text-sm')
    with ui.expansion('1. 새 월 등록 · 수정 자료 등록',icon='upload_file',value=True).classes('report-section mt-5'):
        with ui.row().classes('w-full gap-4'):
            start=calendar_input('청구기간 시작일 (YYYY-MM-DD)',placeholder='예: 2029-06-25 또는 6/25~7/26').classes('w-80 max-w-full')
            end=calendar_input('청구기간 종료일 (YYYY-MM-DD)').classes('w-80 max-w-full')
        month_label=ui.label('청구월: 기간을 입력하면 종료일 기준으로 자동 설정됩니다.').classes('text-sm font-semibold text-primary')
        default_year=datetime.now(timezone(timedelta(hours=9))).year
        def update_month():
            try:
                _,_,value=billing_period(start.value,end.value,default_year)
                month_label.set_text(f'청구월: {value} · 종료일 기준 자동 설정')
            except ValueError as exc:month_label.set_text('청구월: '+str(exc))
        def normalize_period():
            try:
                first,last,_=billing_period(start.value,end.value,default_year)
                start.set_value(first);end.set_value(last)
            except ValueError:pass
            update_month()
        start.on_value_change(update_month);end.on_value_change(update_month)
        start.on('blur',normalize_period);end.on('blur',normalize_period)
        labels={'claim':'실제 청구서 (XLSB/XLSX/XLSM) · 여러 개 가능','inbound':'입고 WMS (XLSX/CSV) · 여러 개 가능',
                'outbound':'출고 WMS (XLSX/CSV) · 여러 개 가능','contract':'계약서 (선택)','master':'제품 Master (선택)',
                'rates':'당월 적용 단가표 (선택 · CSV/XLSX)'}
        uploaded=ui.column().classes('w-full').props('data-testid=monthly-selected-files')
        def render_files():
            uploaded.clear()
            with uploaded:
                if files:ui.label('등록할 파일').classes('muted text-sm')
                for entry in list(files):
                    with ui.row().classes('w-full items-center'):
                        ui.label(labels[entry['role']].split(' (')[0]+' · '+entry['name']).classes('text-sm grow')
                        ui.button('파일 삭제',icon='delete_outline',on_click=lambda e=entry: remove(e)).props('outline dense no-caps data-testid=monthly-delete-upload').tooltip('등록 전 업로드 파일 삭제')
        def remove(entry):
            try:
                adapter.discard_upload(entry['token'])
                files.remove(entry);render_files()
                uploaders[entry['role']].reset()
            except Exception as exc:ui.notify(str(exc),type='negative')
        async def upload(event,role):
            try:
                token=await adapter.upload_file(event.file,monthly=True)
                files.append({'role':role,'token':token,'name':event.file.name});render_files()
            except Exception as exc:ui.notify(str(exc),type='negative')
        with ui.element('div').classes('grid grid-cols-1 md:grid-cols-2 gap-4 w-full'):
            for role,label in labels.items():
                async def handler(event,r=role):await upload(event,r)
                accept='.xlsb,.xlsx,.xlsm' if role=='claim' else '.xlsx,.csv' if role in ('inbound','outbound','master','rates') else '.pdf,.xlsx,.csv'
                uploaders[role]=ui.upload(label=label,on_upload=handler,auto_upload=True,multiple=True).props('accept="'+accept+'" data-testid=monthly-upload-'+role).classes('w-full')
        with ui.expansion('기존 계약·Master를 재사용하려면 선택',icon='folder_open').classes('w-full'):
            source=adapter.root/'data/demo_company/source'
            for role,keyword in [('contract','계약'),('master','Master')]:
                options={}
                for p in source.rglob('*'):
                    if not p.is_file() or '월별등록' in p.parts or p.name.startswith('~$'):continue
                    if keyword.casefold() in p.as_posix().casefold() and p.suffix.lower() in (('.pdf','.xlsx','.csv') if role=='contract' else ('.xlsx','.csv')):
                        options[p.relative_to(adapter.root).as_posix()]=p.name
                inputs[role]=ui.select(options,multiple=True,value=[],label='재사용할 '+('계약서' if role=='contract' else 'Master')).props('outlined use-chips clearable').classes('w-full')
            ui.label('선택한 파일은 해당 월 자료로 복사 보관합니다. 재사용 확인은 계약 단가·비용의 적정성 승인이 아닙니다.').classes('muted')
        ui.label('단가표 형식: 첫 행의 열 제목을 청구항목 / 단가 / 단위로 지정합니다. 청구항목은 청구내역의 분류명(C열)과 항목명(G열)을 한 칸 띄어 합친 값입니다.').classes('muted')
        ui.separator().classes('my-4')
        ui.label('2. 해당 월 적용 기준').classes('section-title')
        with ui.row().classes('w-full gap-4'):
            company=ui.input('WMS 회사구분 표기',value='[90] DEMO COMPANY').props('outlined').classes('grow')
            warehouse=ui.input('WMS 창고 표기',value='[DM001]DEMO').props('outlined').classes('grow')
        with ui.row().classes('w-full gap-4'):
            consumables=ui.select({'include':'포함 — 상품과 소모품 수량을 함께 비교','exclude':'제외 — ‘(소모품)’ 표시 수량을 빼고 비교'},label='소모품 수량을 포함할까요?').props('outlined').classes('grow')
            blank=ui.select({'ask':'빈 수량 단위는 질문','EA':'이번 자료의 빈 수량 단위는 EA로 해석'},label='WMS 빈 단위 기준').props('outlined').classes('grow')
            yearless=ui.select({'ask':'연도 없는 일자는 질문','period':'지정 청구기간에 유일하게 포함되는 날짜로 해석'},label='연도 없는 날짜').props('outlined').classes('grow')
        exclude=ui.select(['비연동(WMS)','이동요청'],multiple=True,value=[],label='입고 제외 유형 (선택한 유형만 제외)').props('outlined').classes('w-full')
        with ui.expansion('포장비·박스 수량 비교 기준 (해당 월에만 적용)',icon='inventory_2').classes('w-full'):
            packing_group=ui.select({'unconfirmed':'미확정','shipment':'같은 출고일·출고번호',
                                     'order_carrier_tracking':'주문번호·택배사·송장번호'},value='unconfirmed',label='포장 1건을 세는 기준').props('outlined').classes('w-full')
            prepacked=ui.select({'unconfirmed':'미확정','include':'선포장 포함','exclude_explicit':'품명에 (선포장)이 명시된 수량만 제외'},value='unconfirmed',label='포장비 계산의 선포장 수량').props('outlined').classes('w-full')
            threshold=ui.number('기본포장에 포함되는 상품 개수',value=None,min=1,max=10000,precision=0).props('outlined').classes('w-full')
            carton=ui.select({'unconfirmed':'환산 기준 미확정','master_then_claim':'Master 우선 · 없으면 유일한 청구 내품수량'},value='unconfirmed',label='박스·낱개 참고 환산').props('outlined').classes('w-full')
            ui.label('실제 작업 횟수의 승인과 구분합니다. 다른 달의 잠정 기준은 자동 복사하지 않습니다.').classes('muted')
        unresolved=ui.select({'record':'미확정으로 남기고 가능한 검사 완료','ask':'미확정 항목의 답변을 기다림'},value='record',label='근거가 부족한 항목 처리').props('outlined').classes('w-full')
        ui.label('입고: 일자+입고예정번호+품번 / B2B: 일자+주문번호 / B2C: 청구 주문번호와 WMS 주문번호(연동)로 비교합니다. 반복 행을 임의 제거하지 않습니다.').classes('muted')
        confirm=ui.checkbox('DEMO COMPANY 가상센터 자료이며, 위 청구기간·파일 역할·연결키·단위 기준을 해당 월에 적용하도록 확인합니다.')
        ui.label('지원: 원본 입출고·상세 금액·선택 기준의 포장비·Master 환산·계약 기재단가 비교. 계약기간·실비·별도협의 조건은 원문과 함께 확인합니다.').classes('muted')
        def register():
            try:
                materials=[{k:v for k,v in f.items() if k!='name'} for f in files]
                for role,element in inputs.items():
                    for path in element.value or []:
                        materials.append({'role':role,'reuse':path})
                first,last,billing_month=billing_period(start.value,end.value,default_year)
                payload={'month':billing_month,'start':first,'end':last,'confirmed':confirm.value,
                         'materials':materials,'rules':{'company':company.value.strip(),'warehouse':warehouse.value.strip(),
                         'consumables':consumables.value,'blank_unit':blank.value,'yearless_date':yearless.value,'inbound_exclusions':exclude.value,
                         'packing_group':packing_group.value,'prepacked':prepacked.value,
                         'packing_threshold':int(threshold.value) if threshold.value is not None else None,
                         'carton_basis':carton.value,'unresolved_policy':unresolved.value}}
                adapter.start_monthly('monthly_register',payload['month'],registration=payload)
                ui.notify('자료 등록을 시작했습니다. 아래 실행 상태를 확인해 주세요.')
            except Exception as exc:ui.notify(str(exc),type='negative')
        ui.button('이 월의 자료·기준 등록',icon='save',on_click=register).props('unelevated no-caps')
    with ui.card().classes('w-full mt-5 gap-4'):
        ui.label('3. 월 선택 · 검증 실행').classes('section-title')
        months=ui.select({},label='등록된 청구월').props('outlined').classes('w-full')
        status=ui.label('').classes('muted')
        status.set_visibility(False)
        def launch(answers=None):
            try:
                if not months.value:raise ValueError('등록된 월을 선택해 주세요.')
                adapter.start_monthly('monthly_agent',months.value,
                    request=f"DEMO COMPANY 가상센터 {months.value} 전체 청구 검증",answers=answers)
            except Exception as exc:ui.notify(str(exc),type='negative')
        with ui.row().classes('gap-3'):
            run_button=ui.button('검증 실행',icon='play_arrow',on_click=lambda:launch()).props('unelevated no-caps data-testid=monthly-run-agent')
        question_box=ui.column().classes('w-full')
    result_box=ui.column().classes('w-full mt-5')
    answer_values={};answer_button={'value':None}

    def show_result():
        selected['month']=months.value;result_box.clear();question_box.clear();answer_button['value']=None
        if not months.value:
            status.set_text('');status.set_visibility(False)
            return
        state=adapter.monthly_state(months.value)
        case=load(adapter.monthly_folder(months.value)/'case.json')
        status.set_text(f"{months.value} · {case['period']['start']} ~ {case['period']['end']} · "+{
            'registered':'자료 등록 완료 · 아직 검증 전','running':'실행 중','failed':'실행 실패 · 마지막 완료 결과 보존',
            'awaiting_user_decision':'사용자 답변 대기','completed_with_limitations':'실행 완료 · 한계 포함'}.get(state.get('processing_status'),'대기'))
        status.set_visibility(True)
        if state.get('error'):
            with result_box:ui.label(state['error']).classes('text-red-600 text-sm')
        if state.get('waiting_for_input'):
            with question_box,ui.expansion('미확정 항목 답변',icon='help_outline',value=True).classes('w-full'):
                answers={}
                ui.label('기간·단위·파일 오류는 위 등록 기준이나 자료를 수정하세요. 아래 답변은 해당 판정만 제외하며 정상으로 바꾸지 않습니다.').classes('muted')
                for q in state['questions']:
                    ui.label(q['text']).classes('text-sm mt-3')
                    ui.label(q.get('evidence','')).classes('muted')
                    key=(months.value,state.get('identity'),q['id'])
                    answers[q['id']]=ui.select(['확정할 수 없음 — 해당 판정만 제외'],value=answer_values.get(key),label='이 항목에 대한 결정',
                        on_change=lambda e,k=key:answer_values.__setitem__(k,e.value)).props('outlined').classes('w-full')
                answer_button['value']=ui.button('답변 저장 후 재검증',on_click=lambda:launch({k:v.value for k,v in answers.items()})).props('unelevated')
                answer_button['value'].set_enabled(adapter.snapshot()['job']['status']!='running')
        pending=bool(state.get('waiting_for_input'))
        try:
            result=adapter.monthly_result(months.value,pending=pending) or adapter.monthly_result(months.value)
        except Exception as exc:
            with result_box:ui.label(str(exc)).classes('text-red-600')
            return
        if not result:
            with result_box:ui.label('아직 게시된 검증 결과가 없습니다.').classes('muted')
            return
        with result_box:
            with ui.card().classes('w-full gap-4'):
                ui.label(months.value+' 검증 결과').classes('section-title')
                ui.label('전체 청구금액 적정성 미확정'+(' · 답변 전 중간 결과' if result['waiting_for_input'] else '')).classes('notice')
                if result['case']['revision']!=case['revision']:ui.label('자료 등록이 변경되었습니다. 아래는 이전 자료의 완료 결과입니다. 새 자료로 다시 실행하세요.').classes('text-amber-700')
                from recurring_rules import snapshot as recurring_snapshot
                rules=recurring_snapshot(adapter.root)
                if result.get('recurring_snapshot_hash')!=rules['fingerprint'] and (rules['events'] or result.get('recurring_rules')):
                    ui.label('저장한 비교 기준이 변경되었습니다. 아래는 이전 완료 결과입니다. 이 월을 재검증하면 최신 기준으로 비교합니다.').classes('text-amber-700')
                cols=[{'name':k,'label':label,'field':k,'align':'left' if k=='group' else 'right'} for k,label in [('group','검증 항목'),('정상','정상'),('불일치','불일치'),('미검증','미검증')]]
                ui.table(columns=cols,rows=result['summary'],row_key='group').classes('w-full')
                with ui.expansion('등록 파일별 반영 상태',icon='folder_open').classes('w-full'):
                    for m in result.get('material_coverage',[]):
                        label='기능 미구현 포함' if m['status']=='not_implemented' else '검사 입력에 포함'
                        ui.label(f"{labels[m['role']].split(' (')[0]} · {Path(m['path']).name} · {label}").classes('text-sm')
                        for reason in m['limitations']:ui.label(reason).classes('muted text-sm')
                with ui.row():
                    ui.link('보고서 다운로드',download_url(result['report']))
                    ui.link('확인 목록 CSV',download_url(result['csv']))
                render_contract_documents(result,download_url)
                if result.get('layout_observations'):
                    with ui.expansion('파일에서 읽은 열 확인',icon='table_view').classes('w-full'):
                        for layout in result['layout_observations']:
                            ui.label(Path(layout['path']).name+' · '+layout['section']).classes('font-medium')
                            if layout['status']=='mapped':
                                ui.label(f"{layout['sheet']} · 제목 {layout['header']}행").classes('muted')
                                ui.label(' / '.join(f'{name}: {col}열' for name,col in layout['columns'].items())).classes('text-sm')
                            else:ui.label(layout['reason']).classes('text-amber-700')
            csv_path=safe_file(adapter.root,result['csv'])
            with csv_path.open(encoding='utf-8-sig',newline='') as stream:rr=[dict(r,_id=str(i)) for i,r in enumerate(csv.DictReader(stream))]
            fee_labels={(item.get('claim_path',''),item.get('source_sheet','청구내역'),item['row']):item['label'] for item in result.get('fee_items',[])}
            for r in rr:
                ref=re.match(r'([^!]+)![A-Z]+(\d+)',r.get('청구위치',''))
                if ref:r['_ui_cost_label']=fee_labels.get((r.get('청구원본',''),ref[1],int(ref[2])),fee_labels.get(('',ref[1],int(ref[2])),''))
            with ui.card().classes('w-full gap-4'):
                ui.label('확인 목록 · 행을 선택해 원본 위치 조회').classes('section-title')
                with ui.row().classes('w-full'):
                    group=ui.select(['전체']+sorted({r['검사'] for r in rr}),value='전체',label='검증 유형').props('outlined dense').classes('w-64')
                    query=ui.input('주문번호·품번·원인 검색').props('outlined dense').classes('grow')
                cols=[{'name':k,'label':{'_display_target':'확인 대상','_display_locator':'원본 위치'}.get(k,k),'field':('_display_'+k if k in ('청구값','비교값','차이') else k),'align':'right' if k in ('청구값','비교값','차이') else 'left'} for k in ('검사','결과','_display_target','_display_locator','청구값','비교값','차이','단위')]
                table=ui.table(columns=cols,rows=display_rows(rr),row_key='_id',pagination=10).classes('w-full')
                def apply():table.rows=display_rows(filter_issues(rr,group.value,query=query.value or ''));table.update()
                group.on_value_change(apply);query.on_value_change(apply)
                def detail(e):
                    row=e.args[1]
                    with ui.dialog() as dialog,ui.card().classes('w-[800px] max-w-[95vw]'):
                        ui.label(row['검사']+' · '+row['결과']).classes('section-title')
                        ui.label(row['_display_target']).classes('font-semibold')
                        if row.get('청구원본'):ui.label('청구서: '+Path(row['청구원본']).name).classes('text-sm')
                        render_locations('청구서에서 찾기',row.get('청구위치'))
                        render_locations('WMS에서 찾기',row.get('WMS위치'))
                        if row.get('계약위치'):ui.label('계약서에서 찾기: '+row['계약위치']).classes('text-sm break-all')
                        for key in ('사유구분','사유','일자','주문번호','입고예정번호','품번'):
                            if row.get(key):ui.label(key+': '+(row.get('_display_reason',row[key]) if key=='사유' else row[key])).classes('text-sm break-all')
                        from calculation_view import render_brief
                        render_brief(row)
                        for m in result['case']['materials']:
                            ui.link('원본 · '+m['role']+' · '+m['name'],download_url(m['path'])).classes('text-xs break-all')
                        ui.button('닫기',on_click=dialog.close).props('flat')
                    dialog.open()
                table.on('rowClick',detail)
            with ui.expansion('전체 보고서',icon='description').classes('report-section'):
                ui.markdown(safe_file(adapter.root,result['report']).read_text('utf-8'))
    months.on_value_change(show_result)
    def refresh():
        snapshot=adapter.snapshot();job=snapshot.get('verification_job',snapshot['job']);busy=job['status']=='running' or snapshot['job']['status']=='running'
        run_button.set_enabled(not busy)
        if answer_button['value'] is not None:answer_button['value'].set_enabled(not busy)
        cases=adapter.monthly_cases()
        signature=(tuple((c['month'],c['revision']) for c in cases),job.get('id'),job['status'])
        if signature==last['signature']:return
        last['signature']=signature
        months.set_options({c['month']:c['month']+' · 가상센터' for c in cases})
        if job.get('month') in months.options:months.set_value(job['month'])
        elif not months.value and cases:months.set_value(cases[-1]['month'])
        show_result()
        if job.get('month') and job['status']=='failed':
            status.set_text('실행 실패 · '+str(job.get('error','실행 로그를 확인해 주세요.')))
            status.set_visibility(True)
    refresh();ui.timer(1,refresh)
    def focus_month(month):
        refresh()
        if month in months.options:
            months.set_value(month)
            show_result()
    return focus_month
