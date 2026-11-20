"""A single, expanded report inside the dashboard's report dialog."""
import csv
import io
from decimal import Decimal, ROUND_HALF_UP
from nicegui import ui
from dashboard_view import format_number, format_krw_exact, format_krw_parts
from monthly_trend import COST_COLORS, cost_color


def render_coverage_chart(data):
    """Read-only billed amounts by comparison result, not connection method."""
    coverage = data['coverage']
    total = data['totals']['net']
    amounts = coverage['comparison_amounts']
    parts = [("일치", amounts['match'], '#1A9D72'),
             ("불일치", amounts['difference'], '#E5484D'),
             ("잠정 확정", amounts['provisional'], '#D8C8B5')]
    if amounts['unverified']:
        parts.append(("미검증", amounts['unverified'], '#AEB5BE'))
    if total <= 0 or any(amount < 0 for _, amount, _ in parts):
        return
    with ui.column().classes('w-full gap-2').props('data-testid=report-coverage-chart'):
        with ui.row().classes('w-full items-center justify-between gap-3'):
            ui.label('대사 결과별 청구액 비중').classes('text-sm font-medium')
            with ui.row().classes('items-center gap-4'):
                for label, amount, color in parts:
                    with ui.row().classes('items-center gap-2'):
                        ui.element('span').style(f'width:9px;height:9px;border-radius:50%;background:{color}')
                        ui.label(f'{label} {amount / total * 100:.2f}%').classes('text-sm')
        alt = ' · '.join(f'{label} {amount / total * 100:.2f}%' for label, amount, _ in parts)
        with ui.element('div').classes('w-full flex overflow-hidden').style('height:16px;border-radius:6px;cursor:default').props(f'role=img aria-label="{alt}"'):
            for label, amount, color in parts:
                if amount:
                    ui.element('div').style(f'width:{float(amount / total * 100)}%;height:100%;background:{color}')


def format_completion_amount(value):
    """Compact card text only; retain original won amounts for calculations."""
    amount = Decimal(str(value))
    if abs(amount) >= Decimal(100000000):
        return f'{amount / Decimal(100000000):,.2f}억'
    man = amount / Decimal(10000)
    if 0 < abs(man) < 1:
        return f'{format(man, ",f").rstrip("0").rstrip(".")}만원'
    return f'{man.quantize(Decimal(1), rounding=ROUND_HALF_UP):,.0f}만원'


def render_money(value, css, testid=None, *, compact=False, eok=False):
    with ui.element('div').classes('money-display ' + css) as money:
        if testid:
            money.props(f'data-testid={testid}')
        if compact:
            amount, unit = (f'{Decimal(str(value)) / Decimal(100000000):,.2f}', '억원') if eok else format_krw_parts(value)
            ui.label(amount).classes('money-number')
            ui.label(unit).classes('money-unit')
        else:
            ui.label(format_krw_exact(value)).classes('money-number')
    return money


def section_csv(rows):
    """Export this cost section in displayed sort order, across all pages."""
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    fields = [('label','청구 항목'),('connection','비교 근거'),('comparison_result','비교 결과'),
              ('unit','규격·단위'),('quantity','청구 수량'),('unit_price','기재 단가(원)'),('amount','금액(원)')]
    writer.writerow([label for _, label in fields])
    for row in rows:
        values = []
        for key, _ in fields:
            value = str(row.get(key, ''))
            if key in ('quantity','unit_price','amount'):
                value = value.replace(',', '')
            elif value.startswith(('=', '+', '-', '@')):
                value = "'" + value
            values.append(value)
        writer.writerow(values)
    return output.getvalue().encode('utf-8-sig')


def open_section_details(section, source_name, show_checks, download_source, dialog, priority='source'):
    """Show the original billed components, including matching/provisional rows."""
    items = {item['row']: item for item in section['items']}
    dialog.clear()
    with dialog:
        with ui.card().classes('cost-detail-dialog'):
            with ui.row().classes('w-full justify-between items-center'):
                with ui.column().classes('gap-1'):
                    ui.label(section['label']).classes('section-title').props('data-testid=cost-section-title')
                    ui.label(f"전체 {len(items):,}개 청구 항목 · 합계 {format_number(section['amount'])}원").classes('font-semibold').props('data-testid=cost-section-total')
                ui.button(icon='close', on_click=dialog.close).props('flat round dense aria-label="비용 상세 닫기"')
            with ui.row().classes('w-full justify-between items-center gap-2'):
                ui.label(source_name + ' · 청구내역').classes('muted text-xs break-all')
                ui.button('이 비용 내역 다운로드', icon='download',
                          on_click=lambda: ui.download(section_csv(ordered_rows()), f"{section['label']}_상세내역.csv")).props('flat dense no-caps data-testid=cost-section-download')

            def ordered_rows():
                ordered = sorted(rows, key=lambda row: row['row'])
                if table.pagination.get('sortBy') == 'comparison_result':
                    ordered.sort(key=lambda row: (row['comparison_rank'], row['row']),
                                 reverse=bool(table.pagination.get('descending')))
                return ordered

            def view_checks(row_numbers=None):
                from cost_check_context import make_context
                if row_numbers is None:
                    ordered = sorted(rows, key=lambda row: row['row'])
                    if table.pagination.get('sortBy') == 'comparison_result':
                        ordered.sort(key=lambda row: (row['comparison_rank'], row['row']),
                                     reverse=bool(table.pagination.get('descending')))
                    row_numbers = [row['row'] for row in ordered]
                context = make_context(section, row_numbers)
                show_checks(list(context['targets']), context=context)
                dialog.close()

            columns = [{'name': key, 'field': key, 'label': label, 'align': align} for key, label, align in (
                ('label', '청구 항목', 'left'), ('unit', '규격·단위', 'left'),
                ('quantity', '청구 수량', 'right'), ('unit_price', '기재 단가(원)', 'right'),
                ('amount', '금액(원)', 'right'))]
            if any('connection' in item for item in items.values()):
                columns[1:1] = [{'name': k, 'field': k, 'label': label, 'align': 'left'} for k, label in
                                (('connection', '비교 근거'), ('comparison_result', '비교 결과'))]
            widths = {'connection': 120, 'comparison_result': 104, 'unit': 110,
                      'quantity': 84, 'unit_price': 100, 'amount': 120}
            for column in columns:
                column['classes'] = 'cost-col-' + column['name']
                column['headerClasses'] = 'cost-col-' + column['name']
                if column['name'] in widths:
                    column['headerStyle'] = f"width:{widths[column['name']]}px"
                if column['name'] == 'comparison_result':
                    column['sortable'] = True
                    column[':sort'] = '(a, b, rowA, rowB) => rowA.comparison_rank - rowB.comparison_rank || rowA.row - rowB.row'
            rows = [dict(row=item['row'],
                         label=(item['category'] + ' · ' if item['category'] and item['category'] != item['label'] else '') + item['label'],
                         unit=item['unit'] or '—', quantity=format_number(item['quantity']),
                         unit_price=format_number(item['unit_price']), amount=format_number(item['amount']),
                         method=item.get('method', ''), verdict=item.get('verdict'),
                         connection=item.get('connection', ''), comparison_result=item.get('comparison_result', '')) for item in items.values()]
            def status_key(row):
                if row.get('verdict'):
                    return 'unlinked' if row['verdict']=='unverified' else row['verdict']
                if row['comparison_result'] == '비교값 차이':
                    return 'difference'
                return row['method'] if row['method'] in ('provisional', 'unlinked') else 'match'

            ranks = {'difference': 0, 'provisional': 1, 'unlinked': 2, 'match': 3}
            for row in rows:
                row['comparison_rank'] = ranks[status_key(row)]
                row['comparison_result'] = {'match':'일치','difference':'불일치','provisional':'잠정 확정','unlinked':'미검증'}[status_key(row)]
            with ui.row().classes('w-full justify-between items-center gap-2'):
                if any('connection' in item for item in items.values()):
                    counts = {key: sum(status_key(row) == key for row in rows)
                              for key in ('match', 'difference', 'provisional', 'unlinked')}
                    status_summary = [f"{label} {counts[key]:,}개" for key, label in
                                      (('match', '일치'), ('difference', '불일치'), ('provisional', '잠정 확정'))]
                    if counts['unlinked']:
                        status_summary.append(f"자료 대조 미완료 {counts['unlinked']:,}개")
                    ui.label(' · '.join(status_summary)).classes('muted text-sm').props('data-testid=cost-detail-counts')
            pagination = {'rowsPerPage': 10, 'page': 1, 'descending': False,
                          'sortBy': 'comparison_result' if priority in ranks and priority != 'match' else None}
            table = ui.table(columns=columns, rows=sorted(rows, key=lambda row: row['row']),
                             row_key='row', pagination=pagination).classes('w-full cost-items-table').props('flat sort-order=ad data-testid=cost-section-items')
            table.add_slot('body-cell-connection', '''<q-td :props="props">
                <span v-if="props.row.method === 'provisional'" class="muted">별도 증빙 없음</span>
                <span v-else-if="props.row.method === 'unlinked'" class="muted">—</span>
                <span v-else>{{ props.value }}</span>
            </q-td>''')
            table.add_slot('body-cell-comparison_result', '''<q-td :props="props">
                <span v-if="props.value === '불일치'" class="cost-status cost-status-difference">불일치</span>
                <span v-else-if="props.value === '일치'" class="cost-status cost-status-match">일치</span>
                <span v-else-if="props.value === '잠정 확정'" class="cost-status cost-status-provisional">잠정 확정</span>
                <span v-else-if="props.value === '미검증'" class="cost-status cost-status-evidence">미검증</span>
                <span v-else>{{ props.value }}</span>
            </q-td>''')
            with ui.row().classes('w-full justify-end'):
                ids = list(dict.fromkeys(i for item in items.values() for i in item['check_ids']))
                ui.button('이 비용의 검증 결과 보기', icon='arrow_forward', on_click=lambda: view_checks()).props('outline no-caps data-testid=cost-section-checks').set_enabled(bool(ids))
            detail = ui.column().classes('cost-source-preview w-full gap-3').props('data-testid=cost-source-preview')
            detail.set_visibility(False)

            def show_source(row_number):
                item = items[row_number]
                detail.clear()
                detail.set_visibility(True)
                with detail:
                    ui.label(f"청구내역 · {row_number}행 원본 저장값").classes('font-semibold text-sm')
                    labels = {'C': '구분', 'G': '항목', 'J': '규격', 'L': '단가', 'M': '수량', 'N': '금액'}
                    cells = [{'cell': f'{col}{row_number}', 'field': labels.get(col, col + '열'),
                              'value': format_number(value)} for col, value in item['source_cells'].items() if value is not None]
                    ui.table(columns=[{'name': k, 'field': k, 'label': label, 'align': 'left'} for k, label in
                                      (('cell', '셀'), ('field', '항목'), ('value', '저장값'))],
                             rows=cells, row_key='cell').classes('w-full').props('flat hide-bottom')
                    if item['check_ids']:
                        ui.button('이 항목의 검증 결과 보기', icon='arrow_forward', on_click=lambda: view_checks([row_number])).props('flat no-caps data-testid=cost-item-checks')
                    else:
                        ui.label('이 청구 행에 직접 연결된 검증 결과가 없습니다.').classes('muted')
                detail.run_method('scrollIntoView', {'behavior': 'smooth', 'block': 'nearest'})

            table.on('rowClick', lambda e: show_source(e.args[1]['row']))
    dialog.open()


def render_cost_composition(data, show_section):
    """Show one proportional cost stack and its breakdown; retain signed credits."""
    palette_order = {label: index for index, label in enumerate(COST_COLORS)}
    sections = sorted(data['billed_sections'],
                      key=lambda s: (palette_order.get(s['label'], len(palette_order)), s['label']))
    positive_total = sum(s['amount'] for s in sections if s['amount'] > 0)
    with ui.card().classes('cost-composition-card w-full').props('data-testid=cost-composition-card'):
        with ui.row().classes('w-full items-center justify-between gap-2'):
            ui.label('항목별 청구 물류비').classes('section-title')
            ui.label(data['month']).classes('muted')
        ui.label('차감 전 비용 비중').classes('cost-composition-caption')
        if positive_total:
            with ui.element('div').classes('cost-composition-track').props(
                    'role=img aria-label="차감 전 비용의 항목별 비중" data-testid=cost-composition-track'):
                for section in sections:
                    if section['amount'] > 0:
                        share = float(section['amount'] / positive_total * 100)
                        ui.element('div').classes('cost-composition-fill').style(
                            f'width:{share}%;background:{cost_color(section["label"])}').tooltip(
                                f"{section['label']} · {format_krw_exact(section['amount'])} · {share:.2f}%")
        with ui.column().classes('cost-composition-list').props('data-testid=billed-breakdown'):
            for section in sections:
                color = cost_color(section['label'])
                with ui.element('div').classes('cost-composition-row').props(
                        f'role=button tabindex=0 aria-label="{section["label"]} 상세"') as tile:
                    ui.element('span').classes('cost-composition-swatch').style(f'background:{color}')
                    ui.label(section['label']).classes('cost-composition-label')
                    with ui.column().classes('items-end gap-0'):
                        render_money(section['amount'], 'cost-composition-value', compact=True).tooltip(format_krw_exact(section['amount']))
                        if section['amount'] > 0 and positive_total:
                            share = float(section['amount'] / positive_total * 100)
                            ui.label(f'{share:.2f}%').classes('cost-composition-share')
                    ui.icon('chevron_right', size='16px', color='primary')
                    if section['amount'] < 0:
                        tile.classes('cost-composition-credit')
                tile.on('click', lambda s=section: show_section(s))
                tile.on('keydown.enter', lambda s=section: show_section(s))
                tile.on('keydown.space.prevent', lambda s=section: show_section(s))


def render_financial_dashboard(data, show_rows, show_section, show_report, *, classic=False):
    totals = data['totals']
    with ui.element('div').classes('financial-grid').props('data-testid=financial-dashboard'):
        with ui.card().classes('financial-main-card'):
            with ui.row().classes('financial-card-heading w-full items-center'):
                ui.label('청구 물류비').classes('section-title')
            render_money(totals['net'], 'financial-value', 'dashboard-billed-amount', compact=True, eok=True)
            with ui.column().classes('gap-0'):
                ui.label('차감 후 공급가액 · 부가세 제외').classes('muted text-xs')
            with ui.row().classes('financial-tax-row w-full items-center justify-between text-sm gap-2'):
                ui.label('부가세 포함 최종 청구').classes('muted')
                ui.label(format_krw_exact(totals['payable'])).classes('financial-tax-value font-semibold')
        with ui.card().classes('financial-main-card financial-review financial-report-link').props(
                'role=button tabindex=0 aria-haspopup=dialog aria-label="확인 필요 물류비 · 보고서 보기" '
                'data-testid=open-dashboard-report') as report_card:
            with ui.row().classes('financial-card-heading w-full items-center justify-between gap-2'):
                ui.label('확인 필요 물류비').classes('section-title')
                with ui.row().classes('items-center gap-1 text-primary'):
                    ui.label('보고서 보기').classes('text-sm font-medium')
                    ui.icon('arrow_forward', size='16px')
            with ui.element('div').classes('financial-headline w-full'):
                render_money(data['review_amount'], 'financial-value', 'dashboard-review-amount', compact=True, eok=True)
                with ui.column().classes('financial-share items-end gap-1'):
                    ui.label('전체 청구액 대비').classes('muted text-xs')
                    ui.label(f"{data['ratio']:.2f}%" if data['ratio'] is not None else '산정 불가').classes('financial-ratio').props('data-testid=dashboard-money-ratio')
            with ui.element('div').classes('financial-track').props('role=img aria-label="전체 청구액 대비 확인 필요 금액 비중"'):
                width = min(100, max(0, float(data['ratio'] or 0)))
                ui.element('div').style(f'width:{width}%;background:var(--accent);height:100%')
            with ui.column().classes('w-full gap-1').props('data-testid=review-money-breakdown'):
                for part in data.get('money_breakdown', []):
                    if not part['amount'] and not part['ids']:
                        continue
                    with ui.row().classes('w-full items-baseline justify-between gap-2 text-sm'):
                        ui.label(part['label']).classes('muted')
                        with ui.row().classes('items-baseline gap-2'):
                            render_money(part['amount'], 'review-breakdown-money', compact=True)
                            ui.label(f"{part['ratio']:.2f}%" if part['ratio'] is not None else '—').classes('muted')
        report_card.on('click', show_report)
        report_card.on('keydown.enter.prevent', show_report)
        report_card.on('keydown.space.prevent', show_report)
    if classic:
        with ui.card().classes('billed-detail-card w-full gap-2').props('data-testid=classic-cost-breakdown'):
            ui.label('항목별 청구 물류비').classes('section-title')
            with ui.element('div').classes('billed-breakdown').props('data-testid=billed-breakdown'):
                for section in data['billed_sections']:
                    with ui.element('div').classes('billed-breakdown-row').props(
                            f'role=button tabindex=0 aria-label="{section["label"]} 상세"') as tile:
                        ui.label(section['label']).classes('billed-breakdown-label')
                        render_money(section['amount'], 'billed-breakdown-value', compact=True).tooltip(format_krw_exact(section['amount']))
                        ui.icon('chevron_right', size='16px').classes('billed-breakdown-chevron')
                    tile.on('click', lambda s=section: show_section(s))
                    tile.on('keydown.enter', lambda s=section: show_section(s))
                    tile.on('keydown.space.prevent', lambda s=section: show_section(s))
    targets = {r['group']: r for r in data['lines']}
    configs = [('B2B 상품 수량', 'B2B 미완료', '출고 건'), ('B2C 주문 연결', 'B2C 미완료', '청구 줄'),
               ('비용', '청구금액 확인 필요', '비용 항목')]
    with ui.element('div').classes('completion-grid'):
        for group, label, unit in configs:
            if not classic and group == 'B2C 주문 연결':
                unit = '청구 행'
            if group == '비용':
                count, total = data['summary_count'], data['claim_item_count']
                ids = [i for r in data['lines'] if r['group'] in ('비용 산식·상세 합계', 'B2C 포장비', '실제 발생 미검증') for i in r['ids']]
                cost_amounts = data.get('completion_amounts', {}).get('cost', {})
                if not classic:
                    count = cost_amounts.get('count', count)
                    ids = cost_amounts.get('ids', ids)
            else:
                target = targets.get(group, {})
                count, total = target.get('count', 0), data['scope_counts'][group]
                ids = target.get('ids', [])
            percent = count / total * 100 if total else None
            ratio = f'{percent:.2f}%' if percent is not None else '—'
            key = {'B2B 상품 수량': 'b2b', 'B2C 주문 연결': 'b2c', '비용': 'cost'}[group]
            with ui.card().classes('completion-card gap-2').props(f'role=button tabindex=0 data-history-view=checklist aria-label="{label}" data-testid=completion-card-{key}') as card:
                with ui.row().classes('w-full items-center justify-between'):
                    ui.label(label).classes('font-semibold text-sm')
                    ui.icon('arrow_forward', color='primary', size='18px')
                with ui.row().classes('w-full items-end justify-between gap-2'):
                    with ui.column().classes('gap-1'):
                        with ui.element('div').classes('completion-count'):
                            ui.label(f'{count:,}').classes('completion-number text-3xl font-bold').style('color:#B27412' if classic else 'color:var(--accent)')
                            if not classic:
                                unit_help = {'b2b': '같은 출고일·주문번호를 한 출고 건으로 집계',
                                             'b2c': '택배 청구 상세의 한 줄을 한 행으로 집계',
                                             'cost': '요약 또는 상세에 확인할 내용이 남은 비용 항목 · 같은 비용은 한 번 집계'}
                                ui.label(unit).classes('completion-count-unit').props(
                                    f'data-testid=completion-{key}-count-unit').tooltip(unit_help[key])
                        ui.label(f'전체 {total:,} {unit} 중').classes('muted')
                    with ui.column().classes('completion-share gap-0 items-end'):
                        ui.label(ratio).classes('completion-ratio').props('data-testid=completion-ratio')
                        if classic:
                            ui.label('미완료 비중').classes('muted')
                if percent is not None and not classic:
                    with ui.element('div').classes('completion-track').props(
                            f'role=img aria-label="{label}: 전체 {total:,}{unit} 중 {count:,}{unit}, {ratio}" data-testid=completion-track-{key}'):
                        ui.element('div').style(f'width:{min(100, max(0, percent))}%;height:100%;background:var(--accent)')
                if not classic:
                    amounts = data.get('completion_amounts', {}).get(key, {})
                    with ui.element('div').classes('completion-money-sentence'):
                        total_amount, pending_amount = amounts.get('total'), amounts.get('pending')
                        total_label = '전체(연결분)' if amounts.get('total_unlinked') else '전체'
                        total_text = (f'{total_label} {format_completion_amount(total_amount)}'
                                      if total_amount is not None else '전체 금액 미연결')
                        pending_label = '확인필요(연결분)' if amounts.get('pending_unlinked') else '확인필요'
                        pending_text = (f'{format_completion_amount(pending_amount)} {pending_label}'
                                        if pending_amount is not None else '확인필요 금액 미연결')
                        for field, text, css in (('total', total_text, 'completion-money-total'),
                                                 ('pending', pending_text, 'completion-money-pending')):
                            if field == 'pending':
                                ui.label('/').classes('completion-money-separator')
                            amount_label = ui.label(text).classes(css).props(f'data-testid=completion-{key}-{field}-amount')
                            value, missing = amounts.get(field), amounts.get(field + '_unlinked', 0)
                            if value is not None:
                                note = amounts.get('basis', '')
                                if missing:
                                    note += f' · 금액 미연결 {missing:,}개는 합계에서 제외'
                                amount_label.tooltip(f'{format_number(value)}원 · {note}')
                if group == '비용':
                    difference_count = data['summary_difference_count'] if classic else cost_amounts.get('difference_count', data['summary_difference_count'])
                    unverified_count = data.get('other_unverified_count', data['summary_unverified_count']) if classic else cost_amounts.get('unverified_count', data['summary_unverified_count'])
                    provisional_count = data.get('summary_provisional_count', 0) if classic else cost_amounts.get('provisional_count', 0)
                    parts = [f"불일치 {difference_count}", f"잠정 확정 {provisional_count}", f"미검증 {unverified_count}"]
                    ui.label(' · '.join(parts)).classes('muted text-xs').props('data-testid=cost-pending-reasons')
            card.on('click', lambda ids=ids: show_rows(ids))
            card.on('keydown.enter', lambda ids=ids: show_rows(ids))
            card.on('keydown.space.prevent', lambda ids=ids: show_rows(ids))
    wms_only = targets.get('WMS에만 있는 실적', {})
    if wms_only.get('count') and not classic:
        ui.button(f"WMS에만 있는 입출고 {wms_only['count']:,}건", icon='info_outline',
                  on_click=lambda: show_rows(wms_only['ids'])).props(
                      'flat dense no-caps data-history-view=checklist data-testid=dashboard-wms-only')
    if data['unpriced_count']:
        ui.label(f"금액 연결이 안 된 수량·실적 {data['unpriced_count']:,}건은 금액 비중에서 제외했습니다.").classes('muted text-xs')


def render_overview(data, show_rows, download_report, download_csv, show_section=None):
    coverage = data['coverage']
    totals, period = data['totals'], data['period']
    with ui.row().classes('w-full items-center justify-between gap-2'):
        ui.label('DEMO COMPANY · 가상센터').classes('text-sm font-semibold')
        ui.label(f"{period.get('start', '')} ~ {period.get('end', '')}").classes('muted')
    with ui.element('div').classes('report-money-grid'):
        for title, amount, subtitle, testid in (
            ('전체 청구 물류비', totals['net'], f"비용 {data['claim_item_count']:,}개 항목 · 부가세 제외", 'report-billed-amount'),
            ('일치 물류비', coverage['comparison_amounts']['match'],
             f"자료 대사 일치 {coverage['comparison_counts']['match']:,}항목", 'report-matched-amount'),
            ('잠정 확정 물류비', coverage['comparison_amounts']['provisional'],
             f"증빙 확인 전 청구액 임시 인정 · {coverage['comparison_counts']['provisional']:,}항목", 'report-provisional-amount'),
        ):
            with ui.column().classes('report-money-card'):
                ui.label(title).classes('report-money-title')
                render_money(amount, 'report-money-value', testid, compact=True, eok=True)
                ui.label(subtitle).classes('report-money-caption')
    render_coverage_chart(data)
    with ui.row().classes('w-full items-center justify-between gap-2'):
        ui.label('비용별 대사 현황').classes('section-title')
        with ui.row().classes('items-center gap-2').props('data-testid=report-review-total'):
            ui.label('확인 필요 합계').classes('text-sm font-medium')
            ui.label(format_krw_exact(data['review_amount'])).classes('font-semibold text-primary')
    columns = [{'name': k, 'field': k, 'label': label, 'align': align} for k, label, align in (
        ('label', '비용 항목', 'left'), ('billed', '청구액', 'right'),
        ('matched', '일치', 'right'), ('difference', '불일치', 'right'),
        ('provisional', '잠정 확정', 'right'),
        ('review', '확인 필요 금액', 'right'), ('status', '비교한 자료·처리 상태', 'left'))]
    rows = []
    review_sections = {s['label']: s for s in data['review_sections']}
    for section in coverage['sections']:
        review = review_sections[section['label']]
        rows.append({'label': section['label'], 'billed': format_krw_exact(section['amount']),
                     'matched': format_krw_exact(section['verdict_amounts']['match']) if section['verdict_counts']['match'] else '—',
                     'difference': format_krw_exact(section['verdict_amounts']['difference']) if section['verdict_counts']['difference'] else '—',
                     'provisional': format_krw_exact(section['verdict_amounts']['provisional']) if section['verdict_counts']['provisional'] else '—',
                     'review': format_krw_exact(review['amount']) if review['atoms'] else '—',
                     'status': ' · '.join(f'{label} {section["counts"][key]:,}항목'
                                          for key, label in (('wms', 'WMS'), ('detail', '청구 상세'), ('evidence', '추가 증빙'))
                                          if section['counts'][key]),
                     'difference_count': section['verdict_counts']['difference'],
                     'provisional_count': section['verdict_counts']['provisional'],
                     'unverified_count': section['verdict_counts']['unverified'],
                     'ids': section['ids']})
    table = ui.table(columns=columns, rows=rows, row_key='label').classes('w-full report-summary-table').props('flat hide-bottom data-testid=report-cost-coverage')
    table.add_slot('body-cell-review', '''<q-td :props="props" class="report-review-cell">
        <div class="report-review-amount">{{ props.value }}</div>
    </q-td>''')
    table.add_slot('body-cell-status', '''<q-td :props="props" class="report-status-cell">
        <div v-if="props.value">{{ props.value }}</div>
        <div class="cost-status-list">
            <span v-if="props.row.difference_count" class="cost-status cost-status-difference">불일치 {{ props.row.difference_count }}항목</span>
            <span v-if="props.row.provisional_count" class="cost-status cost-status-provisional">잠정 확정 {{ props.row.provisional_count }}항목</span>
            <span v-if="props.row.unverified_count" class="cost-status cost-status-evidence">미검증 {{ props.row.unverified_count }}항목</span>
        </div>
    </q-td>''')
    table.add_slot('body-cell-label', '''<q-td :props="props"><span class="text-primary">{{ props.value }}</span><q-icon name="chevron_right" color="primary" class="q-ml-xs" /></q-td>''')
    for column in ('billed', 'matched', 'difference', 'provisional'):
        table.add_slot('body-cell-' + column, '''<q-td :props="props" class="report-amount-cell">{{ props.value }}</q-td>''')
    by_label = {s['label']: s for s in coverage['sections']}
    table.on('rowClick', lambda e: show_section(by_label[e.args[1]['label']]) if show_section else show_rows(e.args[1]['ids']))
    ui.label('확인 필요 금액 = 불일치 + 미검증. 잠정 확정·처리완료는 제외합니다.').classes('muted text-xs')


def coverage_csv(data):
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(['비용 구분', '청구 항목', '청구 위치', '청구액(원)', '비교 근거', '연결한 비교의 결과', '원본 근거'])
    for item in data['coverage']['items']:
        evidence = list(dict.fromkeys(e for c in item['comparisons'] for e in c['evidence']))
        writer.writerow([item['section'], item['label'], f"청구내역!N{item['row']}",
                         str(item['amount']), item['connection'], item['comparison_result'], '; '.join(evidence)])
    return ('\ufeff' + out.getvalue()).encode('utf-8')


def amount_csv(data):
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(['청구 시트', '원본 행', '연결된 청구내역 행', '확인 필요 청구액(원)', '구분', '집계 기준', '금액 산출 근거'])
    for target, amount in sorted(data['amount_atoms'].items()):
        writer.writerow([target[0], target[1], target[2] if len(target) > 2 else '',
                         str(amount), data.get('amount_categories', {}).get(target, ''),
                         '공급가액 · 요약/상세 중복 제외 · 과다청구액 아님', data.get('amount_basis', {}).get(target, '')])
    return ('\ufeff' + out.getvalue()).encode('utf-8')
