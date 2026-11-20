"""NiceGUI local-only chat surface for the existing DEMO COMPANY workflow."""
from pathlib import Path
import os
import re
import sys
import time
from urllib.parse import quote, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_adapter import AgentAdapter, ROOT, BASE, REPORT, POLICY, DEFAULT_REQUEST, STATUS_LABELS, load, safe_file
from nicegui import app, ui
from fastapi import HTTPException
from fastapi.responses import FileResponse, Response

from public_adapter import PublicAdapter
adapter = PublicAdapter()


from public_access import PublicOrigin
app.add_middleware(PublicOrigin)

@app.get('/healthz')
def health():
    return {'status':'ok','ai_enabled':False,'data':'synthetic'}

@app.get('/download')
def download(path: str):
    try:
        file = safe_file(ROOT, path)
    except ValueError as exc:
        raise HTTPException(404, '파일을 찾을 수 없습니다.') from exc
    if file == (ROOT / REPORT):
        return Response(adapter.review_report(file.read_bytes().decode('utf-8')).encode('utf-8'),
                        media_type='text/markdown; charset=utf-8',
                        headers={'Content-Disposition':'attachment; filename="report.md"'})
    return FileResponse(file, filename=file.name, media_type='application/octet-stream')


@app.get('/review-status.csv')
def review_status_csv():
    return Response(adapter.review_csv(), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition':'attachment; filename="review-status.csv"'})


@app.get('/rule-results.csv')
def rule_results_csv():
    return Response(adapter.rule_results_csv(),media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition':'attachment; filename="rule-results.csv"'})


def download_url(path):
    return '/download?path=' + quote(str(path).replace('\\', '/'), safe='')


def web_report():
    text = adapter.review_report((ROOT / REPORT).read_text(encoding='utf-8'))
    # Resolve repository report links; never expose the whole workspace as static files.
    def replace(match):
        label, target = match.groups()
        target = unquote(target.strip('<>'))
        if urlsplit(target).scheme:
            return label
        path = ((ROOT / REPORT).parent / target).resolve()
        try:
            relative = path.relative_to(ROOT).as_posix()
            safe_file(ROOT, relative)
        except (ValueError, OSError):
            return label
        return f'[{label}]({download_url(relative)})'
    return re.sub(r'\[([^\]]+)\]\((<[^>]+>|[^\n]+?)\)', replace, text)


@ui.page('/')
def main_page():
    from dashboard_ui import render_page
    variant = {'1': 'priority', 'classic': 'classic'}.get(os.environ.get('DEMO_DASHBOARD_PREVIEW'))
    render_page(adapter, download_url, web_report, evidence_form, dashboard_variant=variant)


@ui.page('/dashboard-preview')
def dashboard_preview_page():
    from dashboard_ui import render_page
    render_page(adapter, download_url, web_report, evidence_form, dashboard_variant='priority')


def evidence_form(on_register_month=None):
    state = {'token': None, 'scope': None, 'upload_binding': None, 'upload_epoch': 0,
             'job_id': None, 'job_month': None, 'signature': None, 'months': None}
    ui.label('추가 증빙으로 재대사').classes('text-xl font-semibold')
    month_options = {item['month']: item.get('label', item['month'] + ' · 가상센터')
                     for item in adapter.evidence_months()}
    initial_month = '2029-07' if '2029-07' in month_options else next(iter(month_options), None)
    month = ui.select(month_options, value=initial_month, label='증빙을 적용할 청구월').classes('w-72').props('data-testid=evidence-month')
    with ui.row().classes('items-center gap-3'):
        ui.label('청구서가 등록된 월만 표시합니다. 다른 월의 증빙은 그 월의 청구서·청구기간을 먼저 등록한 뒤 연결할 수 있습니다.').classes('muted').props('data-testid=evidence-month-help')
        if on_register_month:
            ui.button('다른 월 등록',icon='calendar_month',on_click=on_register_month).props('outline no-caps data-testid=evidence-register-month')
        else:
            ui.label('월별 검증에서 새 청구월을 등록해 주세요.').classes('muted')
    scope_label = ui.label().classes('muted').props('data-testid=evidence-scope')
    ui.label('증빙 파일에서 비교할 숫자의 위치를 지정하세요. 원본은 덮어쓰지 않고 새 파일로 보관합니다.').classes('muted')
    ui.label('파일 선택은 임시 업로드입니다. 아래 ‘증빙 등록 및 재검증’을 누르면 선택한 월을 바로 재대사하고, 이 화면의 결과를 자동 갱신합니다. 새로고침이나 별도 채팅 요청은 필요 없습니다.').classes('muted')
    uploaded = ui.label('파일을 선택해 주세요.').classes('text-sm')
    def delete_upload():
        try:
            if not state['token']:
                return
            adapter.discard_upload(state['token'])
            state.update(token=None, upload_binding=None, upload_epoch=state['upload_epoch'] + 1)
            uploader.reset()
            delete_button.set_enabled(False)
            confirmed.set_value(False)
            reviewed.set_value(False)
            uploaded.set_text('업로드 파일을 삭제했습니다. 다시 파일을 선택해 주세요.')
        except Exception as exc:
            ui.notify(str(exc), type='negative')
    delete_button = ui.button('업로드 파일 삭제', icon='delete_outline', on_click=delete_upload).props('outline no-caps data-testid=evidence-delete-upload')
    delete_button.set_enabled(False)
    async def on_upload(event):
        try:
            if not state['scope']:
                raise ValueError('먼저 증빙을 적용할 청구월을 선택해 주세요.')
            epoch = state['upload_epoch']
            binding = (month.value, state['scope']['version_token'])
            state['token'] = None
            state['upload_binding'] = None
            delete_button.set_enabled(False)
            token = await adapter.upload_file(event.file)
            if epoch != state['upload_epoch'] or not state['scope'] or binding != (month.value, state['scope']['version_token']):
                raise ValueError('업로드 중 청구월이 변경되었습니다. 선택한 월의 파일을 다시 올려 주세요.')
            state['token'] = token
            state['upload_binding'] = binding
            suffix = Path(event.file.name).suffix.lower()
            fmt.set_value(suffix[1:] if suffix in ('.csv', '.xlsx', '.xlsb') else 'reviewed_transcription')
            delete_button.set_enabled(True)
            uploaded.set_text(event.file.name + ' · 업로드 완료, 아직 검증에 등록되지 않음')
        except Exception as exc:
            ui.notify(str(exc), type='negative')
    uploader = ui.upload(label='CSV · XLSX · XLSB · PDF · 이미지 (파일 크기 제한 없음)', on_upload=on_upload, auto_upload=True,
              max_files=1).props('accept=".csv,.xlsx,.xlsb,.pdf,.png,.jpg,.jpeg" data-testid=evidence-upload').classes('w-full')
    with ui.row().classes('w-full items-center gap-3').props('data-testid=evidence-upload-actions') as upload_actions:
        pass
    uploaded.move(upload_actions);uploaded.classes('grow break-all')
    delete_button.move(upload_actions)
    ui.label('잘못 선택한 파일은 위 버튼으로 삭제한 뒤 다시 올리세요. 검증에 등록된 자료·완료 결과는 삭제하지 않습니다.').classes('muted text-xs')
    target = ui.select({}, label='대사할 청구 항목', with_input=True).classes('w-full')
    with ui.row().classes('w-full'):
        axis = ui.select({'actual_cost': '실비 공급가액', 'work': '실제 작업 수량', 'contract': '당월 적용 단가'}, value='actual_cost', label='비교 종류').classes('w-56')
        unit = ui.input('증빙 수량·금액 단위', value='KRW').classes('w-48')
        fmt = ui.select({'csv': 'CSV 행·열', 'xlsx': 'XLSX 시트·셀', 'xlsb': 'XLSB 시트·셀', 'reviewed_transcription': 'PDF/이미지 확인한 숫자'}, value='csv', label='숫자 읽기 방식').classes('w-64')
    with ui.row().classes('w-full'):
        row = ui.number('CSV 행 (헤더 다음은 2)', value=2, min=2, precision=0)
        column = ui.input('CSV 열 제목')
        sheet = ui.input('엑셀 시트명')
        cell = ui.input('엑셀 셀 (예: D8)')
        row.bind_visibility_from(fmt, 'value', backward=lambda v: v == 'csv')
        column.bind_visibility_from(fmt, 'value', backward=lambda v: v == 'csv')
        sheet.bind_visibility_from(fmt, 'value', backward=lambda v: v in ('xlsx', 'xlsb'))
        cell.bind_visibility_from(fmt, 'value', backward=lambda v: v in ('xlsx', 'xlsb'))
    with ui.row().classes('w-full'):
        value = ui.input('PDF/이미지에서 확인한 숫자')
        reference = ui.input('페이지·원본 위치').classes('grow')
    reviewed = ui.checkbox('PDF/이미지 원문에서 이 숫자와 위치를 직접 확인했습니다.')
    for element in (value, reference, reviewed):
        element.bind_visibility_from(fmt, 'value', backward=lambda v: v == 'reviewed_transcription')
    def set_unit():
        item = next((i for i in (state['scope'] or {}).get('targets', [])
                     if i.get('target_key',i.get('invoice_row', i.get('row'))) == target.value), {})
        unit.set_value('KRW' if axis.value == 'actual_cost' else str(item.get('unit') or ''))
    axis.on_value_change(set_unit)
    target.on_value_change(set_unit)
    confirmed = ui.checkbox('위 청구 항목의 전체 범위이며, 표시된 기간·단위·KRW 공급가액 기준을 확인했습니다.')
    replace = ui.checkbox('같은 청구 항목·비교 종류의 기존 증빙이 있으면 이번 파일로 교체합니다.')
    ui.label('여러 영수증을 함께 합산해야 하면 현재 웹 폼 대신 기존 JSON 등록 도구를 사용하세요. 이 화면은 한 항목·한 숫자씩 등록합니다.').classes('muted')
    def register():
        try:
            if adapter.snapshot()['job']['status'] == 'running':
                raise ValueError('실행이 끝난 뒤 등록해 주세요.')
            if not state['scope'] or not state['token'] or target.value is None:
                raise ValueError('파일과 청구 항목을 선택해 주세요.')
            if state['upload_binding'] != (month.value, state['scope']['version_token']):
                raise ValueError('선택한 월 또는 자료 버전이 바뀌었습니다. 파일을 다시 선택해 주세요.')
            selector = ({'format': 'csv', 'row': int(row.value), 'column': column.value} if fmt.value == 'csv' else
                        {'format': fmt.value, 'sheet': sheet.value, 'cell': cell.value.strip().upper()} if fmt.value in ('xlsx', 'xlsb') else
                        {'format': 'reviewed_transcription', 'value': value.value, 'reference': reference.value, 'confirmed': reviewed.value})
            registration = adapter.build_registration(state['token'], target.value, axis.value, unit.value,
                selector, confirmed.value, replace.value, month=month.value, version_token=state['scope']['version_token'])
            adapter.start_evidence(registration, month=month.value)
            job = adapter.snapshot().get('verification_job', adapter.snapshot()['job'])
            state.update(job_id=job.get('id'), job_month=month.value, signature=None)
            state['token'] = None
            state['upload_binding'] = None
            delete_button.set_enabled(False)
            uploader.reset()
            uploaded.set_text('등록 요청 완료 · 아래에서 재대사 진행 상태와 결과가 자동 갱신됩니다.')
            ui.notify(f'{month.value} 증빙 등록·재대사를 시작했습니다.')
            refresh()
        except Exception as exc:
            ui.notify(str(exc), type='negative')
    register_button = ui.button('증빙 등록 및 재검증', icon='play_arrow', on_click=register).props('unelevated no-caps')
    status_label = ui.label('파일 등록 전 · 검증 결과는 변경되지 않았습니다.').classes('text-sm').props('data-testid=evidence-status')
    result_box = ui.column().classes('w-full').props('data-testid=evidence-result')

    def show_result():
        result_box.clear()
        if not month.value:
            return
        with result_box:
            if month.value == '2029-07':
                if (adapter.root / REPORT).is_file():
                    ui.link('선택 월 보고서 다운로드', download_url(REPORT.as_posix()))
                return
            try:
                monthly_state = adapter.monthly_state(month.value)
                pending = bool(monthly_state.get('waiting_for_input'))
                result = adapter.monthly_result(month.value, pending=pending) or adapter.monthly_result(month.value)
            except Exception as exc:
                ui.label(str(exc)).classes('text-red-600 text-sm')
                return
            if pending:
                ui.label('사용자 답변 대기 · 월별 검증의 미확정 항목에 답하면 이어서 실행할 수 있습니다. 마지막 완료 결과는 유지됩니다.').classes('text-amber-700 text-sm')
            if not result:
                ui.label('이 월은 아직 게시된 검증 결과가 없습니다. 증빙 등록 후 재검증 결과를 여기에 표시합니다.').classes('muted')
                return
            comparisons = result.get('evidence_comparisons', [])
            if comparisons:
                ui.label(f'{month.value} 추가 증빙 대사 결과' + (' · 답변 전 중간 결과' if pending else '')).classes('font-semibold')
                columns = [{'name': key, 'label': label, 'field': key,
                            'align': 'right' if key in ('청구값', '비교값', '차이') else 'left'}
                           for key, label in [('invoice_row', '청구 행'), ('검사', '비교 항목'), ('결과', '결과'),
                                              ('청구값', '청구값'), ('비교값', '증빙값'), ('차이', '차이'), ('단위', '단위')]]
                ui.table(columns=columns, rows=comparisons, pagination=10).classes('w-full')
                ui.label('표는 등록한 증빙과 해당 청구 항목의 비교 결과입니다. 전체 청구금액의 승인을 의미하지 않습니다.').classes('muted')
            else:
                ui.label('현재 결과에는 추가 증빙 비교가 없습니다.').classes('muted')
            if result.get('report'):
                ui.link('선택 월 보고서 다운로드', download_url(result['report']))
            if result.get('csv'):
                ui.link('선택 월 확인 목록 CSV', download_url(result['csv']))

    def change_month():
        state.update(token=None, scope=None, upload_binding=None, upload_epoch=state['upload_epoch'] + 1,
                     signature=None)
        uploader.reset()
        delete_button.set_enabled(False)
        target.set_value(None)
        confirmed.set_value(False)
        replace.set_value(False)
        reviewed.set_value(False)
        uploaded.set_text('선택한 월의 파일을 올려 주세요. 월을 바꾸면 이전 파일 선택은 해제됩니다.')
        try:
            if not month.value:
                raise ValueError('먼저 월별 검증에서 청구서와 WMS를 등록해 주세요.')
            scope = adapter.evidence_scope(month.value)
            state['scope'] = scope
            period = scope['period']
            scope_label.set_text(f"가상센터 · {month.value} / {period['start']} ~ {period['end']}")
            target.set_options({item.get('target_key',item.get('invoice_row', item.get('row'))):
                (item.get('claim_name','')+' · ' if isinstance(item.get('target_key'),str) else '')+f"청구내역 {item.get('invoice_row', item.get('row'))}행 · {item.get('label') or (item.get('category_context', '') + ' ' + item.get('item_context', ''))} · {item.get('unit', '')}"
                for item in scope.get('targets', [])})
            if not target.options:
                raise ValueError('선택한 월에서 지원하는 청구 항목을 읽지 못했습니다. 월별 검증에서 청구 양식과 등록 상태를 확인해 주세요.')
            uploader.set_enabled(True)
            register_button.set_enabled(True)
            status_label.set_text('파일 등록 전 · 검증 결과는 변경되지 않았습니다.')
        except Exception as exc:
            state['scope'] = None
            target.set_options({})
            scope_label.set_text(str(exc))
            uploader.set_enabled(False)
            register_button.set_enabled(False)
        show_result()

    def refresh():
        snapshot = adapter.snapshot()
        job = snapshot.get('verification_job', snapshot['job'])
        busy = snapshot['job']['status'] == 'running' or job['status'] == 'running'
        delete_button.set_enabled(bool(state['token']) and not busy)
        register_button.set_enabled(bool(state['scope']) and not busy)
        current_options = {item['month']: item.get('label', item['month'] + ' · 가상센터')
                           for item in adapter.evidence_months()}
        if current_options != month.options:
            previous = month.value
            month.set_options(current_options)
            if previous in current_options:
                month.set_value(previous)
            elif current_options:
                month.set_value(next(iter(current_options)))
        signature = (job.get('id'), job.get('status'), month.value)
        if signature == state['signature']:
            return
        state['signature'] = signature
        if job.get('id') != state['job_id'] or month.value != state['job_month']:
            return
        status = job.get('status')
        if status == 'running':
            status_label.set_text(f'{month.value} 증빙 등록·재검증 실행 중 · 완료되면 결과를 자동 표시합니다.')
        elif status == 'failed':
            status_label.set_text('실행 실패 · ' + str(job.get('error', '실행 로그를 확인해 주세요.')) + ' · 마지막 완료 결과는 유지됩니다.')
            show_result()
        elif status in ('completed', 'awaiting_user_decision'):
            status_label.set_text(f'{month.value} 증빙 재대사 ' + ('완료 · 결과를 자동 갱신했습니다.' if status == 'completed' else '사용자 답변 대기'))
            # Publication changes the registry version, so the next upload must
            # bind to its new scope rather than silently reuse the old version.
            try:
                state['scope'] = adapter.evidence_scope(month.value)
            except Exception:
                state['scope'] = None
            show_result()

    month.on_value_change(change_month)
    change_month()
    ui.timer(1, refresh)


if __name__ in {'__main__', '__mp_main__'}:
    ui.run(host='0.0.0.0', port=int(os.environ.get('DEMO_WEB_PORT', '8090')),
           title='DEMO COMPANY · 물류비 청구 검증', reload=False, show=False,
           reconnect_timeout=30.0, proxy_headers=True, forwarded_allow_ips='*', ws_max_size=1048576)
