"""Read-only presentation helpers; never calculate or change business verdicts."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

STATUS_COLORS = {'정상': '#1A9D72', '불일치': '#E5484D', '미검증': '#D99013'}
GROUP_ALIASES = {'청구 요약과 상세 합계 비교': '요약·상세 집계'}
SUMMARY_PRESENTATION = {
    'B2B 상품 수량': ('B2B 출고 · 수량 확인', '일자·주문번호별 출고'),
    'WMS에만 있는 실적': ('WMS 입출고 · 청구 포함 확인', '청구서에 없는 입출고'),
    'B2C 주문 연결': ('B2C 출고 · 주문 연결 확인', '택배 청구서 행별'),
    '청구·상세 금액 계산': ('청구 금액 · 계산 확인', '수량 × 단가 등 계산식별'),
    '물류비 금액 검증': ('청구 비용 · 금액 대조', '청구서의 비용 항목별'),
    '요약·상세 집계': ('청구 금액 · 상세 합계 확인', '청구 요약과 상세 합계 비교'),
}


def summary_presentation(row):
    """Consistent dashboard names; filter keys and business verdicts stay intact."""
    return SUMMARY_PRESENTATION.get(row['group'], (row['label'], row['unit']))

CARD_DEFINITIONS = (
    ('B2B 상품 수량', '불일치', 'B2B 수량 불일치', '출고건에서 수량 차이', 'inventory_2'),
    ('B2C 주문 연결', '미검증', 'B2C 발송 미연결', '택배 청구행 미연결', 'local_shipping'),
    ('청구·상세 금액 계산', '미검증', '금액 미검증', '금액 계산 미검증', 'calculate'),
    ('청구 요약과 상세 합계 비교', '불일치', '요약·상세 불일치', '비용 항목의 합계 차이', 'difference'),
)


def report_sections(text):
    parts = re.split(r'^###\s+(.+)$', text, flags=re.MULTILINE)
    return [('보고서 안내', parts[0])] + [(parts[i].strip(), parts[i+1]) for i in range(1, len(parts), 2)]


def summary_rows(text):
    section = next((body for title, body in report_sections(text) if title == '검사별 요약'), '')
    rows = []
    for line in section.splitlines():
        if not line.startswith('|'):
            continue
        cells = [c.strip() for c in line.strip('|').split('|')]
        if len(cells) != 5 or not all(re.fullmatch(r'[\d,]+', c) for c in cells[2:]):
            continue
        rows.append({'label': cells[0], 'group': GROUP_ALIASES.get(cells[0], cells[0]),
                     'unit': cells[1], **dict(zip(('정상', '불일치', '미검증'), [int(c.replace(',', '')) for c in cells[2:]]))})
    return rows


def dashboard_cards(summary):
    lookup = {r['label']: r for r in summary}
    return [{'title': title, 'group': GROUP_ALIASES.get(label, label), 'status': status,
             'count': lookup[label][status] if label in lookup else None, 'unit': unit,
             'total':sum(lookup[label][s] for s in STATUS_COLORS) if label in lookup else None,
             'total_unit':{'B2B 상품 수량':'출고건','B2C 주문 연결':'택배 청구행','청구·상세 금액 계산':'금액 비교 항목','청구 요약과 상세 합계 비교':'청구 비용 항목'}[label],
             'source_unit': lookup[label]['unit'] if label in lookup else '요약 자료 없음', 'icon': icon}
            for label, status, title, unit, icon in CARD_DEFINITIONS]


def shares(row):
    total = sum(row[s] for s in STATUS_COLORS)
    return {s: row[s] * 100 / total if total else 0 for s in STATUS_COLORS}


def format_number(value):
    if value is None or str(value).strip() == '':
        return '—'
    try:
        number = Decimal(str(value).replace(',', ''))
        if not number.is_finite():
            return str(value)
        return format(number, ',f').rstrip('0').rstrip('.') if number % 1 else format(number, ',.0f')
    except InvalidOperation:
        return str(value)


def format_krw_parts(value):
    """A short display number and unit; never used for amount calculations."""
    if isinstance(value, bool):
        return '—', ''
    try:
        amount = Decimal(str(value).replace(',', ''))
        if not amount.is_finite():
            return '—', ''
        magnitude = abs(amount)
        if magnitude < 10000:
            return format_number(amount), '원'
        man = (amount / Decimal(10000)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        if abs(man) >= 10000:
            eok = (amount / Decimal(100000000)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            return format(eok, ',.2f'), '억원'
        return format(man, ',.0f'), '만원'
    except (InvalidOperation, ValueError):
        return '—', ''


def format_krw_compact(value):
    return ' '.join(part for part in format_krw_parts(value) if part)


def format_krw_exact(value):
    """Spell out the exact amount in Korean currency groups without rounding."""
    if isinstance(value, bool):
        return '—'
    try:
        amount = Decimal(str(value).replace(',', ''))
        if not amount.is_finite():
            return '—'
        remainder = abs(amount)
        parts = []
        for divisor, unit in ((Decimal(10**12), '조'), (Decimal(10**8), '억'), (Decimal(10**4), '만')):
            group = int(remainder // divisor)
            if group:
                parts.append(f'{group:,}{unit}')
                remainder -= group * divisor
        if remainder or not parts:
            parts.append(format_number(remainder))
        return ('-' if amount < 0 else '') + ' '.join(parts) + '원'
    except (InvalidOperation, ValueError):
        return '—'


def format_krw_grouped(value):
    """Original dashboard display: 억·만 원, rounded to the nearest 만원."""
    if isinstance(value, bool):return '—'
    try:
        amount = Decimal(str(value).replace(',', ''))
        if not amount.is_finite():return '—'
        if abs(amount) < 10000:return format_krw_exact(amount)
        rounded = (amount / 10000).quantize(Decimal(1), rounding=ROUND_HALF_UP) * 10000
        return format_krw_exact(rounded)[:-1] + ' 원'
    except (InvalidOperation, ValueError):
        return '—'


def filter_issues(rows, group='전체', status='전체', query=''):
    term = query.strip().casefold()
    return [r for r in rows if (group == '전체' or r['검사'] == group)
            and (status == '전체' or r['결과'] == status)
            and (not term or term in ' '.join(str(v) for v in r.values()).casefold())]


CHECK_CATEGORIES = ('입고', 'B2B 출고', 'B2C 출고', '금액·단가', '작업·증빙', '박스·낱개 참고 환산', '그 외 실적')


def check_category(name):
    """Presentation grouping only; original check names and verdicts stay intact."""
    if 'BOX·PCS' in name:return '박스·낱개 참고 환산'
    if name.startswith('입고'):return '입고'
    if name.startswith('B2B'):return 'B2B 출고'
    if name.startswith('B2C'):return 'B2C 출고'
    if name in ('계약 적용·실비 적정성','실제 과금 작업량','잠정 확정 · 증빙 확인 필요','임가공 작업상품 수량'):return '작업·증빙'
    if name in ('물류비 금액 검증','청구·상세 금액 계산','요약·상세 집계','계약서·청구 기재단가','택배 B2B·B2C 합산'):return '금액·단가'
    return '그 외 실적'


def check_filter_options(rows,category='전체'):
    from review_catalog import display_group, filter_values
    names=sorted({display_group(r) for r in rows})
    available={check_category(name) for name in names}
    categories=filter_values(category)
    return (['전체']+[c for c in CHECK_CATEGORIES if c in available],
            ['전체']+[name for name in names if check_category(name) in categories] if '전체' not in categories else ['전체'])


def preview_table_columns(result):
    """Wide descriptive text; non-wrapping IDs/numbers, with horizontal scrolling."""
    numeric=preview_number_columns(result['headers'])
    columns=[];total=0
    for field,label in [('원본 행','원본 행')]+[(c,result['headers'].get(c,'항목명 없음')) for c in result['columns']]:
        descriptive=bool(re.search(r'품명|상품명|제품명|규격|거래처|채널명|비고|설명|주소',label))
        width=320 if re.search(r'품명|상품명|제품명|비고|설명|주소',label) else 200 if descriptive else 82 if field=='원본 행' else 110 if field in numeric else 180 if re.search(r'번호|바코드',label) else 135
        if not descriptive:
            import unicodedata
            text_values=[label]+[str(row.get(field,'')) for row in result.get('rows',[])]
            length=max(sum(2 if unicodedata.east_asian_width(ch) in ('W','F') else 1 for ch in text) for text in text_values)
            width=max(width,min(360,length*8+24))
        columns.append({'name':field,'label':label,'field':field,'align':'right' if field in numeric or field=='원본 행' else 'left',
                        'headerStyle':f'width:{width}px;min-width:{width}px',
                        'style':f'width:{width}px;max-width:{width}px',
                        'classes':'preview-description' if descriptive else 'preview-value'})
        total+=width
    return columns,total


def display_explanation(value):
    """Translate internal decision references in prose; retain stored evidence."""
    text=str(value or '')
    replacements={
        'U44 잠정 확정; 증빙 등록 시 같은 청구 행·단위로 비교':'청구값 잠정 인정 · 증빙 등록 후 재대사',
        'U44 사용자 요청: 현재 청구값 잠정 확정; 추가 증빙 등록 시 재대사':'사용자 결정에 따라 청구값을 잠정 인정했습니다. 증빙 등록 후 재대사합니다.',
        'U35에 따라':'사용자 결정에 따라',
        'U35 사용자 지정:':'사용자 결정:',
        'U31 비교 제외':'빈 작업량 비교 제외',
        'U45 사용자 채택 단가':'사용자가 채택한 청구 단가',
    }
    for old,new in replacements.items():text=text.replace(old,new)
    # Decision codes in explanatory prose are not source order/SKU identifiers.
    return re.sub(r'(?<![\w/.-])U\d{2,3}(?![\w/.-])','사용자 결정',text)


def preview_number_columns(headers):
    """Only quantities/amounts get separators; identifiers and dates stay exact."""
    return {col for col,label in headers.items()
            if re.search(r'금액|단가|수량|운임|요금|비용|합계|박스 수|파렛트 수',label)
            and not re.search(r'번호|코드|품번|바코드|일자|날짜|출고일|입고일',label)}


def preview_display_rows(result):
    numeric=preview_number_columns(result['headers'])
    return [dict(row,**{c:format_number(row[c]) for c in numeric if c in row})
            for row in result['rows']]


def review_page(rows, pagination):
    """Sort all matching records locally; send only the requested page to the UI."""
    allowed={'_display_group','결과','_display_review','_display_target','_display_locator','일자',
             '청구값','비교값','차이','단위'}
    sort_by=pagination.get('sortBy')
    descending=bool(pagination.get('descending',False))
    ordered=rows
    if sort_by in allowed:
        def key(row):
            value=row.get('_effective_status') if sort_by=='결과' else row.get(sort_by)
            if sort_by in ('청구값','비교값','차이'):
                try:
                    number=Decimal(str(value).replace(',',''))
                    if number.is_finite():return (1,number)
                except InvalidOperation:pass
                return (0,Decimal(0))
            return str(value or '').casefold()
        ordered=sorted(rows,key=key,reverse=descending)
    size=int(pagination.get('rowsPerPage',10) or 100)
    size=max(1,min(size,100))
    pages=max(1,(len(rows)+size-1)//size)
    page=max(1,min(int(pagination.get('page',1) or 1),pages))
    return ordered[(page-1)*size:page*size],dict(page=page,rowsPerPage=size,rowsNumber=len(rows),
        sortBy=sort_by if sort_by in allowed else None,descending=descending)


def review_table_rows(rows):
    fields=('_id','_display_group','결과','_effective_status','_display_status','_review_status','_display_review',
            '_display_target','_display_target_kind','_display_locator','_display_source','일자',
            '청구값','비교값','차이','단위','_display_청구값','_display_비교값','_display_차이','_rule_status')
    return [{k:row.get(k,'') for k in fields} for row in rows]


def display_rows(rows):
    result = []
    for row in rows:
        r = dict(row, **{f'_display_{k}': format_number(row.get(k)) for k in ('청구값', '비교값', '차이')})
        r.update(_display_group=row.get('검사', ''), _display_basis=display_explanation(row.get('적용기준', '')),
                 _display_reason=display_explanation(row.get('사유', '')),
                 _normal_basis=display_explanation(row.get('_normal_basis', '')))
        shown = row.get('_effective_status') or row.get('결과', '')
        r['_display_status'] = '일치' if shown == '정상' and row.get('_cost_policy') and row.get('_review_status') not in ('reviewed','exception_accepted') else shown
        if row.get('_evidence_review'):
            r.update(_display_group='잠정 확정 · 증빙 확인 필요', _display_reason='실제 발생 여부는 미검증입니다. 영수증·작업기록 등 증빙 확인이 필요합니다.',
                     _display_basis='현재 청구값을 잠정 확정하고 증빙 등록 후 실제 발생 여부를 비교합니다.')
        old_basis = '청구 기재 수량×기재 단가; 실제 작업량 또는 계약 적용 확정과 구분'
        if old_basis in r['_display_basis']:
            r['_display_basis'] = r['_display_basis'].replace(old_basis, '청구서 계산 확인 · 단가 × 수량 = 청구금액인지 비교')
        row = r
        position = row.get('청구위치') or row.get('WMS위치') or ''
        summary = '청구내역!' in position and not (row.get('주문번호') or row.get('입고예정번호'))
        if row.get('주문번호'):
            target, kind = row['주문번호'], '주문번호'
        elif row.get('입고예정번호'):
            target, kind = row['입고예정번호'], '입고예정번호'
        elif summary:
            target = row.get('_ui_cost_label') or row.get('_display_basis', '').split(' · ')[0].strip() or '청구 비용 항목'
            kind = '비용 요약행 · 주문번호 대상 아님'
            if row.get('검사','').startswith('B2C WMS 포장'):
                name=str(row.get('사유','')).split(' · ')[0]
                target=(name if name in ('기본포장','추가포장') else '포장비')+' · '+('수량' if '수량' in row['검사'] else '금액')
                kind='청구 합계와 WMS 잠정 계산 비교'
        elif row.get('품번'):
            target, kind = row['품번'], '품번'
        elif row.get('송장번호'):
            target, kind = row['송장번호'], '송장번호'
        else:
            target, kind = '원본 위치로 조회', '식별번호 기재 없음'
        if row.get('_cost_context_label'):
            original = target
            target = row['_cost_context_label']
            kind = f'{kind} · {original}' if row.get('주문번호') or row.get('품번') else '비용 상세에서 선택한 청구 항목'
        r.update(_display_target=target, _display_target_kind=kind,
                 _display_locator=compact_location(position),
                 _display_source=(row.get('청구원본') or '').replace('\\','/').rsplit('/',1)[-1])
        result.append(r)
    return result


def source_groups(position):
    """Group recorded references without losing file identity or unknown text."""
    groups = {}
    for part in (position or '').split(';'):
        part = part.strip()
        if not part:
            continue
        if re.search(r'\.json(?:#|$)',part,re.I):
            continue  # Calculation artifacts are not spreadsheet source locations.
        file, sep, ref = part.rpartition(' / ')
        if not sep:
            file, ref = '', part
        # A recorded location may list several cells, with the sheet written once
        # (Sheet!A2,A4) or repeated (Sheet!A2,Sheet!B4).
        parsed, sheet = [], None
        for token in ref.split(','):
            token = token.strip()
            if '!' in token:
                sheet, token = token.rsplit('!', 1)
            if not sheet or not re.fullmatch(r'\$?[A-Z]*\$?\d+(?::\$?[A-Z]*\$?(?:\d+|마지막행))?', token):
                parsed = [('기타 위치', ref)]
                break
            parsed.append((sheet, token.replace('$', '')))
        for sheet, cell in parsed:
            group = groups.setdefault((file, sheet), {'file':file, 'sheet':sheet, 'cells':[]})
            group['cells'].append(cell)
    return sorted(groups.values(), key=lambda g: g['sheet'] != '청구내역')


def compact_location(position):
    groups = source_groups(position)
    labels = []
    for group in groups[:2]:
        cells = group['cells']
        label = cells[0] if len(cells) == 1 else f'{len(cells)}곳'
        labels.append(f"{group['sheet']} · {label}")
    return ' / '.join(labels) + (f' 외 {len(groups)-2}개 시트' if len(groups)>2 else '') if groups else '원본 위치 기록 없음'


def render_locations(title, position, preview=None):
    """Compact sheet navigation shared by the July and monthly views."""
    from nicegui import ui
    groups = source_groups(position or '')
    if preview:
        button_label = '청구서에서 보기' if title.startswith('청구서') else 'WMS에서 보기' if title.startswith('WMS') else title
        ui.button(button_label, icon='table_view', on_click=lambda:preview(0)).props('outline dense no-caps').classes('w-full').set_enabled(bool(groups))
    if not groups:return
    if not preview:
        ui.label(title).classes('text-sm font-semibold')
    for group_index, group in enumerate(groups):
        cells, sheet = group['cells'], group['sheet']
        label = f'{sheet} · {cells[0]}' if len(cells) == 1 else f'{sheet} · {len(cells)}곳'
        if preview:
            with ui.column().classes('w-full gap-1'):
                ui.label('시트: ' + label).classes('muted text-xs')
        with ui.expansion('위치 복사' if preview else label).classes('w-full'):
            if group['file']:
                ui.label(group['file'].replace('\\', '/').rsplit('/',1)[-1]).classes('text-xs break-words')
            ui.label('엑셀 Ctrl+G에 복사한 위치를 붙여넣으세요.').classes('muted')
            for cell in cells:
                with ui.row().classes('w-full items-center justify-between'):
                    ui.label(cell + ('행' if cell.isdigit() else '')).classes('text-sm')
                    if sheet not in ('CSV', '기타 위치'):
                        lookup = excel_lookup({'청구위치':sheet+'!'+cell})
                        ui.button('위치 복사', icon='content_copy', on_click=lambda value=lookup: ui.clipboard.write(value)).props('flat dense no-caps').tooltip(lookup)


def excel_lookup(row):
    """An existing sheet/cell reference, never a guessed order or date."""
    position = row.get('청구위치') or row.get('WMS위치') or ''
    match = re.search(r'([^/;!\n]+)!([A-Z]+\d+(?::[A-Z]+\d+)?|\d+)', position)
    if not match or match[1].strip() == 'CSV':
        return ''
    sheet, cells = match[1].strip(), match[2]
    if cells.isdigit():cells += ':' + cells
    if ' ' in sheet:sheet = "'" + sheet.replace("'", "''") + "'"
    return sheet + '!' + cells


def cause_label(row):
    # Never promote an unstructured explanation to an independently confirmed cause.
    if row.get('원인확정') == '확정':
        return '확정 원인 · 원본 기록 기준'
    if re.search('추정|후보|가능성', row.get('사유', '')):
        return '원인 후보 · 확정되지 않음'
    return '기록된 사유 · 원인 확정 여부는 별도'


def chat_route(message):
    """Non-executing conversation must never fall through to invoice recalculation."""
    text = message.strip()
    if re.fullmatch(r'(안녕(?:하세요|하십니까)?|반가워(?:요)?|hi|hello)[!~.\s]*', text, re.I):
        return 'greeting'
    if re.search(r'사용법|도움말|무엇을 할|뭘 할|어떻게 사용|업로드 방법|파일.*어떻게.*올', text):
        return 'help'
    if re.search(r'원인|근거', text) and re.search(r'보여|조회|알려', text):
        return 'investigate'
    if re.search(r'결과|보고서', text) and re.search(r'보여|조회|열어', text):
        return 'read'
    if re.search(r'(검증|대사)(?:을|를)?\s*(?:해\s*줘|해줘|해주세요|해\s*주세요|실행|시작|진행|요청)', text):
        return 'agent'
    if 'DEMO COMPANY' in text.upper() and '가상센터' in text and re.search(r'(검증|대사)\s*$', text):
        return 'agent'
    return 'clarify'


def conversation_reply(route):
    if route == 'greeting':
        return '안녕하세요. 물류비 청구 검증을 도와드립니다. 검증 실행, 저장된 결과·원인 조회, 월별 자료 등록을 할 수 있습니다. 어떤 작업이 필요하신가요?'
    if route == 'help':
        return '기존 결과는 대시보드·확인 목록에서 조회하고, 새 월 자료는 월별 검증 탭에 등록하세요. “DEMO COMPANY 가상센터 2029년 7월 청구 검증해줘”, “결과 보여줘”, “원인과 근거 보여줘”로 요청할 수 있습니다. 현재 자유로운 결과 질의응답은 아직 연결되지 않았습니다.'
    return '이 메시지로는 검증을 실행하지 않았습니다. 검증 실행·결과 조회·원인 조회 중 원하는 작업을 알려주세요. 특정 금액이나 미검증 사유는 확인 목록의 해당 행에서 볼 수 있습니다. 현재 자유로운 결과 질의응답은 아직 연결되지 않았습니다.'
