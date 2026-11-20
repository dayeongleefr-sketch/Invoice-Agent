"""Alternative dashboard presentation; all figures come from the shared saved result."""
from nicegui import ui

from dashboard_view import STATUS_COLORS, format_krw_exact, summary_presentation
from monthly_trend import render_trend
from report_overview_ui import render_money


# This preview's cost composition uses a cream-to-deep-brown palette.
# Keep the original dashboard and its monthly chart colors unchanged.
COMPOSITION_COLORS = {
    '보관료': '#EFE3CE',
    '입/출고 수수료': '#D3BB99',
    '기타비용': '#AE8A63',
    '택배 및 용차 비용': '#82634E',
    '부자재 비용': '#523B2D',
    '차감, 누락 비용': '#34261F',
}


STYLE = '''
.priority-execution {padding:0;gap:6px}
.priority-execution > .q-column {flex-direction:row;align-items:center;gap:12px}
.priority-execution .muted {font-size:14px}
.priority-dashboard {width:100%;gap:16px}
.priority-dashboard .pd-kicker {font-size:16px;color:#75604A;font-weight:600;letter-spacing:.5px}
.priority-dashboard .pd-meta {font-size:16px;color:#6B7684;line-height:1.5}
.priority-dashboard .pd-title {font-size:28px;font-weight:700;letter-spacing:-1px;line-height:1.4}
.priority-dashboard .pd-section-title {font-size:18px;font-weight:650;line-height:1.5}
.priority-dashboard .pd-heading {width:100%;display:flex;justify-content:space-between;align-items:center;gap:16px}
.priority-dashboard .pd-hero {width:100%;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.priority-dashboard .pd-total,.priority-dashboard .pd-attention {padding:16px 20px;gap:8px;min-width:0}
.priority-dashboard .pd-attention {background:#FAF7F2;border-color:#D7C4AF}
.priority-dashboard .pd-card-heading {display:flex;align-items:center;justify-content:space-between;gap:12px;width:100%;min-height:32px}
.priority-dashboard .pd-card-icon {color:#8D7963;background:#F3EEE6;padding:7px;border-radius:9px;box-sizing:content-box}
.priority-dashboard .pd-amount-row {display:flex;align-items:baseline;justify-content:space-between;gap:16px;width:100%;min-height:48px}
.priority-dashboard .pd-main-value {font-size:36px;font-weight:650;letter-spacing:-1.2px;color:#75604A;line-height:1.25}
.priority-dashboard .pd-total-value {font-size:36px;font-weight:650;letter-spacing:-1.2px;line-height:1.25}
.priority-dashboard .money-unit {font-size:16px;font-weight:400;letter-spacing:0;color:#6B7684}
.priority-dashboard .pd-ratio {font-size:24px;font-weight:650;color:#75604A;font-variant-numeric:tabular-nums;line-height:1.3}
.priority-dashboard .pd-track {height:10px;background:#E9E3DA;border-radius:6px;overflow:hidden;width:100%;display:flex}
.priority-dashboard .pd-track-fill {height:100%;background:#75604A}
.priority-dashboard .pd-breakdown {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;width:100%}
.priority-dashboard .pd-part {display:flex;flex-direction:column;gap:4px;min-width:0}
.priority-dashboard .pd-part .money-display {font-size:21px;font-weight:600;letter-spacing:-.3px;gap:4px}
.priority-dashboard .pd-dot {width:8px;height:8px;border-radius:50%;flex:none;display:inline-block}
.priority-dashboard .pd-total-footer {border-top:1px solid #EEE8E0;padding-top:10px;width:100%;margin-top:auto;display:flex;justify-content:space-between;gap:12px;align-items:center}
.priority-dashboard .pd-total-footer .pd-meta {color:#41464E;font-weight:500}
.priority-dashboard .pd-total-footer .money-display {font-size:24px;font-weight:600;white-space:nowrap}
.priority-dashboard .pd-work {width:100%;gap:8px}
.priority-dashboard .pd-task-grid {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;width:100%}
.priority-dashboard .pd-task {padding:14px 18px;gap:7px;cursor:pointer;transition:border-color .15s,background-color .15s;min-width:0}
.priority-dashboard .pd-task .pd-card-heading {min-height:26px}
.priority-dashboard .pd-task:hover {background:#FAF8F5;border-color:#C9B69F}
.priority-dashboard .pd-task-name {font-size:18px;font-weight:600}
.priority-dashboard .pd-task-count {font-size:30px;font-weight:650;line-height:1.3;font-variant-numeric:tabular-nums;letter-spacing:-.7px}
.priority-dashboard .pd-task-ratio {font-size:20px;font-weight:600;color:#6B7684;line-height:1.4;font-variant-numeric:tabular-nums}
.priority-dashboard .pd-task-unit {font-size:16px;color:#6B7684;margin-left:5px}
.priority-dashboard .pd-task-detail {font-size:16px;color:#6B7684;line-height:1.5}
.priority-dashboard .pd-task .pd-track {height:6px;background:#F0EBE4}
.priority-dashboard .pd-wms-note {display:flex;justify-content:space-between;align-items:center;gap:12px;width:100%;padding:0 4px}
.priority-dashboard .pd-wms-note .q-btn {font-size:16px;font-weight:500}
.priority-dashboard .pd-wms-note .q-icon {font-size:20px}
.priority-dashboard .pd-analysis {display:grid;grid-template-columns:minmax(0,1.45fr) minmax(0,1fr);gap:14px;width:100%;align-items:stretch}
.priority-dashboard .pd-analysis > * {min-width:0}
.priority-dashboard .monthly-trend-card {padding:18px 20px;gap:8px}
.priority-dashboard .monthly-trend-chart {height:260px;min-height:260px;flex:none}
.priority-dashboard .trend-yoy {padding-top:0}
.priority-dashboard .trend-yoy .muted {font-size:16px}
.priority-dashboard .trend-yoy-value {font-size:18px}
.priority-dashboard .pd-costs {padding:18px 20px;gap:10px}
.priority-dashboard .pd-composition {width:100%;height:14px;display:flex;border-radius:5px;overflow:hidden;flex:none}
.priority-dashboard .pd-composition > div {height:100%;box-shadow:inset 0 0 0 1px rgba(82,59,45,.08)}
.priority-dashboard .pd-cost-list {width:100%;gap:0}
.priority-dashboard .pd-cost-row {width:100%;display:grid;grid-template-columns:10px minmax(0,1fr) auto 16px;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid #F1EDE7;cursor:pointer}
.priority-dashboard .pd-cost-row:last-child {border-bottom:none}
.priority-dashboard .pd-cost-row:hover {background:#FAF8F5}
.priority-dashboard .pd-cost-row .money-display {font-size:21px;font-weight:600;letter-spacing:-.4px;white-space:nowrap;gap:3px}
.priority-dashboard .pd-cost-row .money-unit {font-size:16px}
.priority-dashboard .pd-cost-label {font-size:18px;font-weight:500;line-height:1.5}
.priority-dashboard .pd-cost-swatch {height:10px;width:10px;border-radius:3px;box-shadow:inset 0 0 0 1px rgba(82,59,45,.12)}
.priority-dashboard .pd-details {width:100%;border:1px solid #EEE8E0;border-radius:16px;padding:0 8px}
.priority-dashboard .pd-details > .q-expansion-item__container > .q-item {padding:12px 16px;font-size:18px}
.priority-dashboard .pd-detail-content {padding:0 16px 20px;width:100%;gap:12px}
.priority-dashboard .pd-check-row {display:flex;justify-content:space-between;align-items:center;gap:16px;width:100%;border-bottom:1px solid #F2F0EC;padding:12px 0;cursor:pointer;font-size:18px}
.priority-dashboard .pd-check-values {display:flex;gap:14px;flex-wrap:wrap;font-size:16px}
.priority-dashboard [role=button]:focus-visible {outline:2px solid #75604A;outline-offset:4px;border-radius:5px}
@media(max-width:1150px) {
 .priority-dashboard .pd-analysis {grid-template-columns:1fr}
 .priority-dashboard .pd-cost-list {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));column-gap:24px}
 .priority-dashboard .pd-task {padding:14px 16px;gap:8px}
}
@media(max-width:760px) {
 .priority-dashboard .pd-hero,.priority-dashboard .pd-task-grid {grid-template-columns:1fr}
 .priority-dashboard .pd-heading {flex-wrap:wrap}
 .priority-dashboard .pd-total,.priority-dashboard .pd-attention,.priority-dashboard .pd-costs,.priority-dashboard .monthly-trend-card {padding:16px}
 .priority-dashboard .pd-main-value,.priority-dashboard .pd-total-value {font-size:34px}
 .priority-dashboard .pd-task-count {font-size:28px}
 .priority-dashboard .pd-cost-list {display:flex}
 .priority-dashboard .pd-check-row {align-items:flex-start;flex-direction:column;gap:8px}
 .priority-dashboard .pd-wms-note {flex-wrap:wrap;gap:4px}
 .priority-dashboard .pd-total-footer {flex-wrap:wrap;gap:6px}
 .priority-execution > .q-column {flex-wrap:wrap;gap:4px}
}
'''


def _activate(element, callback):
    element.props('role=button tabindex=0')
    element.on('click', callback)
    element.on('keydown.enter.prevent', callback)
    element.on('keydown.space.prevent', callback)


def _track(percent, color='#75604A', *, label):
    with ui.element('div').classes('pd-track').props(f'role=img aria-label="{label}"'):
        ui.element('div').classes('pd-track-fill').style(
            f'width:{min(100, max(0, float(percent or 0)))}%;background:{color}')


def render_dashboard(data, summary, root, stamp, coverage_rows, *, show_rows, show_filter,
                     show_section, show_report, request_verification):
    """Arrange existing summary values; never reclassify verification results."""
    with ui.column().classes('priority-dashboard').props('data-testid=priority-dashboard'):
        with ui.element('div').classes('pd-heading'):
            with ui.column().classes('gap-1'):
                ui.label('대시보드 미리보기').classes('pd-kicker')
                ui.label('물류비 검증 현황').classes('pd-title')
                ui.label(f"DEMO COMPANY · 가상센터 · {data['month']} 청구").classes('pd-meta')
                ui.label(f"{data['period'].get('start', '미등록')} ~ {data['period'].get('end', '미등록')}").classes('pd-meta')
            with ui.row().classes('items-center gap-3'):
                ui.button('기존 화면', on_click=lambda: ui.navigate.to('http://127.0.0.1:8080/#view=dashboard')).props('flat no-caps')
                ui.button('검증 요청', icon='add', on_click=request_verification).props('unelevated no-caps')

        with ui.element('div').classes('pd-hero'):
            with ui.card().classes('pd-total'):
                with ui.element('div').classes('pd-card-heading'):
                    ui.label('전체 청구 물류비').classes('pd-section-title')
                    ui.icon('receipt_long', size='18px').classes('pd-card-icon')
                with ui.element('div').classes('pd-amount-row'):
                    render_money(data['totals']['net'], 'pd-total-value', 'dashboard-billed-amount', compact=True, eok=True)
                ui.label('차감 후 공급가액 · 부가세 제외').classes('pd-meta')
                with ui.element('div').classes('pd-total-footer').tooltip(format_krw_exact(data['totals']['payable'])):
                    ui.label('부가세 포함 총 청구액').classes('pd-meta')
                    render_money(data['totals']['payable'], '', 'dashboard-payable-amount', compact=True, eok=True)
            with ui.card().classes('pd-attention'):
                with ui.element('div').classes('pd-card-heading'):
                    ui.label('확인 필요 물류비').classes('pd-section-title')
                    ui.button('보고서 보기', icon='arrow_forward', on_click=show_report).props('flat dense no-caps data-testid=open-dashboard-report')
                with ui.element('div').classes('pd-amount-row'):
                    render_money(data['review_amount'], 'pd-main-value', 'dashboard-review-amount', compact=True, eok=True)
                    with ui.column().classes('items-end gap-0'):
                        ui.label(f"{data['ratio']:.2f}%" if data['ratio'] is not None else '산정 불가').classes('pd-ratio').props('data-testid=dashboard-money-ratio')
                        ui.label('전체 청구액 대비').classes('pd-meta')
                _track(data['ratio'], label='전체 청구액 대비 확인 필요 비중')
                with ui.element('div').classes('pd-breakdown'):
                    for part in data.get('money_breakdown', []):
                        if not part['amount'] and not part['ids']:
                            continue
                        with ui.element('div').classes('pd-part'):
                            with ui.row().classes('items-center gap-2'):
                                ui.element('span').classes('pd-dot').style('background:' + ('#E5484D' if part['label'] == '불일치' else '#D99013'))
                                ui.label('불일치 관련 청구액' if part['label'] == '불일치' else part['label']).classes('pd-meta')
                            render_money(part['amount'], '', compact=True)

        targets = {line['group']: line for line in data['lines']}
        cost_ids = list(dict.fromkeys(i for line in data['lines']
                                     if line['group'] in ('비용 산식·상세 합계', 'B2C 포장비', '실제 발생 미검증') for i in line['ids']))
        tasks = []
        for group, title, unit, key, color in [
                ('B2B 상품 수량', 'B2B 미완료', '출고 건', 'b2b', '#75604A'),
                ('B2C 주문 연결', 'B2C 미완료', '청구 줄', 'b2c', '#75604A')]:
            target = targets.get(group, {})
            tasks.append((title, target.get('count', 0), data['scope_counts'][group], unit,
                          target.get('ids', []), '수량 차이·출고 근거 확인' if key == 'b2b' else 'WMS와 연결되지 않은 발송', key, color))
        tasks.append(('비용 항목 확인 필요', data['summary_count'], data['claim_item_count'], '비용 항목', cost_ids,
                      f"불일치 {data['summary_difference_count']:,} · 미검증 {data['summary_unverified_count']:,}", 'cost', '#75604A'))
        with ui.column().classes('pd-work'):
            with ui.element('div').classes('pd-task-grid'):
                for title, count, total, unit, ids, description, key, color in tasks:
                    ratio = count / total * 100 if total else 0
                    with ui.card().classes('pd-task').props(f'data-testid=priority-task-{key} data-history-view=checklist aria-label="{title}"') as task:
                        with ui.element('div').classes('pd-card-heading'):
                            ui.label(title).classes('pd-task-name')
                            ui.icon('arrow_forward', size='19px', color='primary')
                        with ui.row().classes('w-full items-baseline justify-between gap-2'):
                            with ui.row().classes('items-baseline gap-0'):
                                ui.label(f'{count:,}').classes('pd-task-count').style(f'color:{color}')
                                ui.label('/ ' + f'{total:,} ' + unit).classes('pd-task-unit')
                            ui.label(f'{ratio:.2f}%').classes('pd-task-ratio').tooltip('전체 대상 중 남은 확인 비중')
                        _track(ratio, color, label=f'{title}: 전체 {total:,}{unit} 중 {count:,}{unit}')
                        ui.label(description if count else '남은 확인 없음').classes('pd-task-detail')
                    _activate(task, lambda ids=ids: show_rows(ids))
            wms_only = targets.get('WMS에만 있는 실적', {})
            with ui.element('div').classes('pd-wms-note'):
                if wms_only.get('count'):
                    ui.button(f"WMS에만 있는 입출고 {wms_only['count']:,}건", icon='info_outline',
                              on_click=lambda: show_rows(wms_only['ids'])).props('flat dense no-caps data-history-view=checklist data-testid=priority-wms-only')
                ui.button('전체 확인 목록', icon='arrow_forward', on_click=show_filter).props('flat dense no-caps data-history-view=checklist')

        with ui.element('div').classes('pd-analysis'):
            render_trend(root, data['month'], data['totals']['net'],
                         {s['label']: s['amount'] for s in data['billed_sections']})
            sections = sorted(data['billed_sections'], key=lambda s: s['amount'], reverse=True)
            positive_total = sum(s['amount'] for s in sections if s['amount'] > 0)
            with ui.card().classes('pd-costs'):
                with ui.element('div').classes('pd-card-heading'):
                    ui.label('항목별 청구 물류비').classes('pd-section-title')
                    ui.label(data['month']).classes('pd-meta')
                if positive_total:
                    with ui.element('div').classes('pd-composition').props('role=img aria-label="차감 전 양수 비용의 구성 비중" data-testid=priority-cost-composition'):
                        for section in sections:
                            if section['amount'] > 0:
                                ui.element('div').style(f"width:{float(section['amount'] / positive_total * 100)}%;background:{COMPOSITION_COLORS.get(section['label'], '#75604A')}").tooltip(
                                    section['label'] + ' · ' + format_krw_exact(section['amount']))
                with ui.column().classes('pd-cost-list'):
                    for section in sections:
                        with ui.element('div').classes('pd-cost-row').props(f'aria-label="{section["label"]} 상세"') as row:
                            ui.element('span').classes('pd-cost-swatch').style(f"background:{COMPOSITION_COLORS.get(section['label'], '#75604A')}")
                            ui.label(section['label']).classes('pd-cost-label')
                            render_money(section['amount'], '', compact=True)
                            ui.icon('chevron_right', size='17px', color='primary')
                        _activate(row, lambda section=section: show_section(section))

        with ui.expansion('검증 현황·전체 범위', icon='fact_check').classes('pd-details').props('data-testid=priority-verification-details'):
            with ui.column().classes('pd-detail-content'):
                for row in summary:
                    label, unit = summary_presentation(row)
                    with ui.element('div').classes('pd-check-row').props('data-history-view=checklist') as check:
                        with ui.column().classes('gap-0'):
                            ui.label(label)
                            ui.label(unit).classes('pd-meta')
                        with ui.element('div').classes('pd-check-values'):
                            for status, color in STATUS_COLORS.items():
                                ui.label(f'{status} {row[status]:,}').style(f'color:{color}')
                    _activate(check, lambda group=row['group']: show_filter(group, '전체'))
                if any(row.get('manual_normal') for row in summary):
                    ui.label('정상에는 담당자가 처리완료한 내역도 포함됩니다.').classes('pd-meta')
                with ui.expansion('원본 행 수·집계 단위').classes('w-full'):
                    ui.table(columns=[{'name': k, 'field': k, 'label': title, 'align': 'right' if k in ('raw_rows', 'targets') else 'left'}
                                      for k, title in [('label', '구분'), ('raw_rows', '원본 행 수'), ('targets', '비교 대상 수'), ('unit', '집계 단위')]],
                             rows=coverage_rows, row_key='group').props('flat').classes('w-full')
        if stamp:
            ui.label('최신 저장 보고서 · ' + stamp + ' KST').classes('pd-meta')
