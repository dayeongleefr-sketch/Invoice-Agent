"""Read-only monthly billed amounts from the last completed publications.

No payment/approval inference, no WMS re-execution, no aggregation of run history.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import lru_cache
from hashlib import sha256
from html import escape
from pathlib import Path
import re

from agent_adapter import intake_module


# Sunset-inspired gold -> deep orange -> brown, ordered from light to dark.
# Pale beige is reserved for previous-year bars; tooltips share the segment colors.
COST_COLORS = {
    '기타비용': '#DAAF58',
    '보관료': '#CF8D35',
    '입/출고 수수료': '#C16B2B',
    '부자재 비용': '#97502A',
    '택배 및 용차 비용': '#6D3825',
    '차감, 누락 비용': '#432A20',
}


def cost_color(label):
    """Keep each category's shade stable across months and year selections."""
    palette = tuple(COST_COLORS.values())
    return COST_COLORS.get(label) or palette[int(sha256(label.encode()).hexdigest()[:8], 16) % len(palette)]


def hover_composition(point):
    """Color the unchanged net bar by positive cost shares, only on emphasis.

    Credits retain their signed amounts in the tooltip, not positive slices.
    A net-zero/negative bar or missing breakdown stays solid.
    """
    if point is None or amount(point['amount']) <= 0:
        return None
    categories = checked_categories(point['amount'], point.get('categories'))
    if categories is None:
        return None
    positive = [(label, value) for label, value in categories.items() if value > 0]
    positive.sort(key=lambda item: (list(COST_COLORS).index(item[0])
                                   if item[0] in COST_COLORS else len(COST_COLORS), item[0]))
    total = sum((value for _, value in positive), Decimal(0))
    offset = Decimal(0)
    stops = []
    for index, (label, value) in enumerate(positive):
        end = Decimal(1) if index == len(positive) - 1 else offset + value / total
        stops.extend([{'offset': float(offset), 'color': cost_color(label)},
                      {'offset': float(end), 'color': cost_color(label)}])
        offset = end
    return {'type': 'linear', 'x': 0, 'y': 1, 'x2': 0, 'y2': 0,
            'colorStops': stops, 'global': False}


def amount(value):
    if isinstance(value, bool) or value is None:
        raise ValueError('청구금액을 읽을 수 없습니다.')
    try:
        result = Decimal(str(value).replace(',', '').strip())
    except InvalidOperation as exc:
        raise ValueError('청구금액이 숫자가 아닙니다.') from exc
    if not result.is_finite():
        raise ValueError('청구금액이 유효한 숫자가 아닙니다.')
    return result


def exact_won(value):
    text = format(value, ',f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def compact_won(value):
    value = amount(value)
    if abs(value) >= 100_000_000:
        return f'{(value / 100_000_000).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):,.2f}억 원'
    if abs(value) >= 10_000:
        return f'{(value / 10_000).quantize(Decimal("1"), rounding=ROUND_HALF_UP):,f}만 원'
    return exact_won(value) + '원'


def money_html(value, *, signed=False):
    value = amount(value)
    sign = '+' if signed and value > 0 else ''
    exact = sign + exact_won(value) + '원'
    return f'<span title="{exact}" aria-label="{exact}">{sign}{compact_won(value)}</span>'


def publication_signature(root):
    """Cheap polling: only publication pointers, never scan original WMS files."""
    folder = Path(root) / 'data/demo_company/monthly'
    return tuple((p.parent.name, p.stat().st_mtime_ns, p.stat().st_size)
                 for p in sorted(folder.glob('*/latest.json'))
                 if re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])', p.parent.name))


def invoice_net(items, summary):
    """Sum each stored cost once, with the explicitly named credit section signed.

    Check an available invoice supply total; never treat a partial amount as the
    monthly total. Unit prices, VAT and detail-sheet sums are not added again.
    """
    total = Decimal(0)
    seen = set()
    columns = items[0].get('source_columns', {}) if items else {}
    value_col = columns.get('N', 'N')
    label_cols = [columns.get(c, c) for c in ('C', 'G', 'M')]
    for item in items:
        key = item['row']
        if key in seen:
            raise ValueError('같은 청구 비용이 중복되어 월 합계를 표시하지 않았습니다.')
        seen.add(key)
        value = amount(item['amount'])
        stored = summary.get(key, {}).get(value_col, {})
        if stored.get('error') or amount(stored.get('value')) != value:
            raise ValueError('저장된 검증 결과와 원본 청구금액이 다릅니다.')
        category = str(item.get('category', ''))
        total += -abs(value) if '차감' in category else value
    # Supported SF and header-mapped summaries use these explicit total labels.
    totals = {}
    for row_number, cells in summary.items():
        if row_number in seen:
            continue
        labels = {re.sub(r'\s+', '', str(cells.get(c, {}).get('value', ''))) for c in label_cols}
        for label in labels & {'합계', '공급가액', '차감후공급가액', '청구금액', '차감비용'}:
            cell = cells.get(value_col, {})
            if cell.get('error'):
                raise ValueError('청구 합계 셀에 오류가 있습니다.')
            value = amount(cell.get('value'))
            if label in totals and totals[label] != value:
                raise ValueError('청구 합계가 여러 개여서 월 총액을 확정할 수 없습니다.')
            totals[label] = value
    nets = [totals[k] for k in ('합계', '차감후공급가액', '공급가액') if k in totals]
    if '청구금액' in totals and '차감비용' in totals:
        nets.append(totals['청구금액'] - abs(totals['차감비용']))
    if any(net != total for net in nets):
        raise ValueError('청구 비용 합계와 공급가액이 달라 월 총액을 표시하지 않았습니다.')
    return total


def invoice_categories(items, summary):
    """Use source section headings, otherwise the registered fee category."""
    columns = items[0].get('source_columns', {}) if items else {}
    category_col = columns.get('C', 'C')
    value_cols = [columns.get(c, c) for c in ('J', 'L', 'M', 'N')]
    headings, section = {}, ''
    for n, cells in sorted(summary.items()):
        label = str(cells.get(category_col, {}).get('value') or '').strip()
        if label and not any(c in cells for c in value_cols):
            section = re.sub(r'^\d+(?:-\d+)*\.\s*', '', label)
        headings[n] = section
    categories = {}
    for item in items:
        label = str(item.get('section') or headings.get(item['row']) or item.get('category') or '기타 비용')
        label = re.sub(r'^\d+(?:-\d+)*\.\s*', '', label)
        value = amount(item['amount'])
        if '차감' in str(item.get('category', '')):
            value = -abs(value)
        categories[label] = categories.get(label, Decimal(0)) + value
    return categories


def checked_categories(total, categories):
    if categories is None:
        return None
    values = {str(k): amount(v) for k, v in categories.items()}
    if not values or sum(values.values(), Decimal(0)) != amount(total):
        raise ValueError('비용별 금액 합계가 월 청구액과 다릅니다.')
    return values


@lru_cache(maxsize=60)
def completed_month(root, month, publication_token):
    intake_module()
    from monthly_cases import current_result, digest, safe, SOURCE
    from monthly_verification import rows
    root = Path(root)
    result = current_result(root, month)
    if not result or result.get('waiting_for_input') or not str(result.get('processing_status', '')).startswith('completed'):
        raise ValueError('완료된 검증 결과가 없습니다.')
    case = result['case']
    if (case.get('month') != month or case.get('company_id') != 'demo_company'
            or case.get('warehouse') != '가상센터' or case.get('currency') != 'KRW'):
        raise ValueError('같은 회사·센터·원화 기준의 결과가 아닙니다.')
    claims = [m for m in case['materials'] if m['role'] == 'claim']
    if not claims:
        raise ValueError('완료 결과에 청구서가 없습니다.')
    fees = result.get('fee_items', [])
    total = Decimal(0)
    categories = {}
    hashes = set()
    for claim in claims:
        if claim['sha256'] in hashes:
            raise ValueError('중복 청구 파일이 포함되어 있습니다.')
        hashes.add(claim['sha256'])
        items = [i for i in fees if i.get('claim_path', claims[0]['path'] if len(claims) == 1 else '') == claim['path']]
        if not items:
            raise ValueError('등록 청구서 중 금액을 읽지 못한 파일이 있습니다.')
        path = safe(root, claim['path'], SOURCE)
        if digest(path) != claim['sha256']:
            raise ValueError('검증에 사용한 원본 청구서가 변경되었습니다.')
        sheets = {i.get('source_sheet', '청구내역') for i in items}
        if len(sheets) != 1:
            raise ValueError('청구 합계 시트를 하나로 식별할 수 없습니다.')
        summary = dict(rows(path, sheets.pop()))
        subtotal = invoice_net(items, summary)
        breakdown = checked_categories(subtotal, invoice_categories(items, summary))
        total += subtotal
        for label, value in breakdown.items():
            categories[label] = categories.get(label, Decimal(0)) + value
    if len(fees) != sum(sum(i.get('claim_path', claims[0]['path'] if len(claims) == 1 else '') == c['path'] for i in fees) for c in claims):
        raise ValueError('청구 비용의 원본 파일을 식별할 수 없습니다.')
    return {'month': month, 'amount': total, 'categories': checked_categories(total, categories),
            'revision': case['revision'], 'period': case['period']}


def load_trend(root, july_month=None, july_amount=None, july_categories=None):
    points, notices = [], []
    if july_month and july_amount is not None:
        point = {'month': july_month, 'amount': amount(july_amount)}
        try:
            point['categories'] = checked_categories(july_amount, july_categories)
        except ValueError as exc:
            notices.append(f'{july_month}: {exc}')
        points.append(point)
    for token in publication_signature(root):
        month = token[0]
        if month == july_month:
            continue
        try:
            points.append(completed_month(str(Path(root).resolve()), month, token))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            notices.append(f'{month}: {exc}')
    return {'points': sorted(points, key=lambda p: p['month']), 'notices': notices}


def sample_points(year):
    """UI-only fictional values. Never merged into real results or written to disk."""
    examples = {
        year - 1: (820, 790, 860, 910, 900, 965, 940, 990, 1020, 1085, 1040, 1130),
        year: (870, 830, 905, 935, 980, 1010, 1035, 1040, 1120, 1160, 1105, 1220),
    }
    points = []
    for y, values in examples.items():
        for m, v in enumerate(values, 1):
            total = Decimal(v) * 1_000_000
            weights = {'보관료': 4400 + m % 4 * 75 + (y - year) * 50,
                       '입/출고 수수료': 3400 + m % 3 * 60,
                       '택배 및 용차 비용': 600 + m % 5 * 35,
                       '부자재 비용': 150 + m % 2 * 20, '차감, 누락 비용': -50}
            categories = {k: total * weight / 10000 for k, weight in weights.items()}
            categories['기타비용'] = total - sum(categories.values(), Decimal(0))
            points.append({'month': f'{y}-{m:02}', 'amount': total,
                           'categories': categories, 'sample': True})
    return points


def displayed_points(real_points, sample_year):
    """Chart-only preview: real completed months replace fictional placeholders."""
    points = {p['month']: p for p in sample_points(sample_year)}
    for point in real_points:
        points[point['month']] = {**point, 'sample': False}
    return [points[month] for month in sorted(points)]


def yoy_comparison(points, month):
    values = {p['month']: amount(p['amount']) for p in points}
    samples = {p['month'] for p in points if p.get('sample')}
    previous_month = f'{int(month[:4]) - 1}{month[4:]}'
    current, previous = values.get(month), values.get(previous_month)
    delta = current - previous if current is not None and previous is not None else None
    percent = delta / previous * 100 if delta is not None and previous > 0 else None
    return {'month': month, 'current': current, 'previous': previous, 'delta': delta, 'percent': percent,
            'sample': month in samples or previous_month in samples}


def yoy_text(comparison, *, show_sample=True, compact=False):
    if comparison['current'] is None:
        return '해당 월 자료 없음'
    if comparison['previous'] is None:
        return '전년 동월 자료 없음'
    delta = comparison['delta']
    delta_text = ('+' if delta > 0 else '') + (compact_won(delta) if compact else exact_won(delta) + '원')
    if comparison['percent'] is None:
        return delta_text + ' · 증감률 계산 불가(전년 0원 이하)'
    suffix = ' · 임의 데이터 포함' if show_sample and comparison.get('sample') else ''
    return f"{delta_text} · {comparison['percent']:+.2f}%{suffix}"


def chart_years(points, year=None):
    available = sorted({int(p['month'][:4]) for p in points})
    if year == 'all':
        return available
    return [int(year)] if year is not None else available[-1:]


def cost_comparison(points, month):
    lookup = {p['month']: p for p in points}
    current = lookup.get(month)
    y, m = map(int, month.split('-'))
    previous_month = f'{y - 1}-12' if m == 1 else f'{y}-{m - 1:02}'
    previous = lookup.get(previous_month)
    current_values = checked_categories(current['amount'], current.get('categories')) if current else None
    previous_values = checked_categories(previous['amount'], previous.get('categories')) if previous else None
    names = list(dict.fromkeys([*(current_values or {}), *(previous_values or {})]))
    preferred = tuple(COST_COLORS)
    names.sort(key=lambda n: preferred.index(n) if n in preferred else len(preferred))
    entries = []
    if current_values is not None:
        for name in names:
            value = current_values.get(name, Decimal(0))
            delta = value - previous_values.get(name, Decimal(0)) if previous_values is not None else None
            entries.append({'label': name, 'amount': value, 'delta': delta})
    return {'entries': entries, 'previous_month': previous_month,
            'previous_available': previous_values is not None,
            'sample_comparison': bool((current or {}).get('sample') or (previous or {}).get('sample'))}


def tooltip_html(points, month):
    point = next((p for p in points if p['month'] == month), None)
    if point is None:
        return '해당 월 자료 없음'
    comparison = cost_comparison(points, month)
    text = [f'<div data-testid="monthly-cost-tooltip" style="font-size:16px;line-height:1.6;min-width:320px;color:#191F28">',
            f'<b>{escape(month)} 청구 물류비</b> <span style="color:#6B7684">({"임의 데이터" if point.get("sample") else "실제"})</span>',
            f'<div style="font-size:22px;font-weight:600;margin:4px 0 12px">{money_html(point["amount"])}</div>']
    if comparison['entries']:
        text.append('<table style="width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums">'
                    '<thead style="color:#6B7684"><tr><th style="text-align:left;font-weight:400">비용</th>'
                    '<th style="text-align:right;font-weight:400;padding-left:14px">청구액</th>'
                    '<th style="text-align:right;font-weight:400;padding-left:14px">전월 대비</th></tr></thead><tbody>')
        for entry in comparison['entries']:
            delta = entry['delta']
            change = '—' if delta is None else money_html(delta, signed=True)
            color = '#6B7684' if not delta else '#B54747' if delta > 0 else '#287B66'
            swatch = (f'<span style="display:inline-block;width:18px;height:18px;border-radius:3px;'
                      f'vertical-align:-4px;border:1px solid rgba(92,69,51,.15);'
                      f'margin-right:8px;background:{cost_color(entry["label"])}"></span>')
            text.append(f'<tr><td style="padding:5px 0">{swatch}{escape(entry["label"])}</td>'
                        f'<td style="text-align:right;padding-left:14px">{money_html(entry["amount"])}</td>'
                        f'<td style="text-align:right;padding-left:14px;color:{color}">{change}</td></tr>')
        text.append('</tbody></table>')
        if hover_composition(point) and any(e['amount'] < 0 for e in comparison['entries']):
            text.append('<div style="font-size:14px;color:#6B7684;margin-top:8px">막대 색상은 차감 전 비용 비중 · 차감액은 음수로 별도 표시</div>')
        if not comparison['previous_available']:
            text.append('<div style="font-size:14px;color:#6B7684;margin-top:6px">전월 비용 구성 자료 없음</div>')
        elif comparison['sample_comparison']:
            text.append('<div style="font-size:14px;color:#6B7684;margin-top:6px">전월 대비는 임의 데이터 포함</div>')
    else:
        text.append('<div style="color:#6B7684">비용 구성 자료 없음</div>')
    text.append('<div style="font-size:14px;border-top:1px solid #EAE1D5;margin-top:10px;padding-top:10px">전년 동월 대비 '
                + escape(yoy_text(yoy_comparison(points, month), compact=True)) + '</div></div>')
    return ''.join(text)


def chart_options(points, year=None):
    """One selected year, or all years side by side by calendar month."""
    years = chart_years(points, year)
    lookup = {p['month']: amount(p['amount']) for p in points}
    compositions = {p['month']: hover_composition(p) for p in points}
    samples = {p['month'] for p in points if p.get('sample')}
    series = []
    palette = ('#75604A', '#D9C9B5', '#A58D70', '#B9A58F', '#5C5146')
    for y in years:
        values = []
        for m in range(1, 13):
            month = f'{y}-{m:02}'
            value = lookup.get(month)
            values.append({'value': float(value) if value is not None else None,
                           'exact': exact_won(value) if value is not None else None, 'month': month,
                           'sample': month in samples,
                           'tooltip': tooltip_html(points, month),
                           'comparison': yoy_text(yoy_comparison(points, month))})
            if compositions.get(month):
                values[-1]['emphasis'] = {'itemStyle': {'color': compositions[month]}}
        series.append({'name': str(y) + '년', 'type': 'bar', 'data': values,
                       'barMaxWidth': 26, 'barGap': '20%', 'legendHoverLink': False,
                       'itemStyle': {'color': palette[(len(years) - 1 - years.index(y)) % len(palette)],
                                     'borderRadius': [3, 3, 0, 0]}})
    return {
        'animationDuration': 250,
        'textStyle': {'fontFamily': 'Pretendard, Malgun Gothic, sans-serif', 'fontSize': 15},
        'legend': {'type': 'scroll', 'top': 0, 'right': 0, 'itemWidth': 12, 'itemHeight': 12,
                   'textStyle': {'fontSize': 15, 'color': '#6B7684'}},
        'grid': {'left': 0, 'right': 8, 'top': 65, 'bottom': 10, 'containLabel': True},
        'tooltip': {'trigger': 'item', 'renderMode': 'html', 'confine': False, 'appendTo': 'body', 'enterable': True,
                    'className': 'monthly-cost-tooltip-surface',
                    'transitionDuration': 0, 'backgroundColor': '#FFFFFF', 'borderColor': '#D9C9B5',
                    'borderRadius': 10, 'padding': 16,
                    'textStyle': {'fontFamily': 'Pretendard, Malgun Gothic, sans-serif', 'fontSize': 16, 'color': '#191F28'},
                    # Avoid compositing/animating text with translate3d, which softens it on scaled displays.
                    'extraCssText': ('max-width:calc(100vw - 24px);max-height:calc(100vh - 24px);overflow:auto;'
                                     'box-sizing:border-box;z-index:10000;'
                                     'box-shadow:0 4px 16px rgba(25,31,40,.10);'),
                    ':position': '''(point, params, dom, rect, size) => {
                        const chart = document.querySelector('[data-testid="monthly-cost-chart"]');
                        const origin = chart.getBoundingClientRect();
                        const viewportWidth = document.documentElement.clientWidth;
                        const viewportHeight = document.documentElement.clientHeight;
                        const width = size.contentSize[0], height = size.contentSize[1];
                        const barLeft = rect ? rect.x : point[0];
                        const barRight = rect ? rect.x + rect.width : point[0];
                        const right = barRight + 12;
                        let x = origin.left + right + width <= viewportWidth - 12
                            ? right : barLeft - width - 12;
                        x = Math.max(12 - origin.left, Math.min(x, viewportWidth - origin.left - width - 12));
                        const y = Math.max(12 - origin.top, Math.min(point[1] - height / 2,
                            viewportHeight - origin.top - height - 12));
                        const scale = window.devicePixelRatio || 1;
                        const snap = value => Math.round(value * scale) / scale;
                        dom.style.setProperty('--monthly-tip-left', `${snap(origin.left + x + window.scrollX)}px`);
                        dom.style.setProperty('--monthly-tip-top', `${snap(origin.top + y + window.scrollY)}px`);
                        return [x, y];
                    }''',
                    ':formatter': '(p) => p.data?.tooltip || "해당 월 자료 없음"'},
        'xAxis': {'type': 'category', 'data': [f'{m}월' for m in range(1, 13)],
                  'axisTick': {'show': False}, 'axisLine': {'lineStyle': {'color': '#EAE1D5'}},
                  'axisLabel': {'color': '#6B7684', 'fontSize': 14, 'interval': 0, 'hideOverlap': True}},
        'yAxis': {'type': 'value', 'name': '억원', 'splitNumber': 4,
                  'nameTextStyle': {'color': '#6B7684', 'fontSize': 14},
                  'axisLabel': {'fontSize': 14, ':formatter': '(v) => (v / 100000000).toLocaleString("ko-KR", {maximumFractionDigits: 2})'},
                  'splitLine': {'lineStyle': {'color': '#F0ECE6'}}},
        'series': series,
    }


def render_trend(root, july_month=None, july_amount=None, july_categories=None):
    from nicegui import ui, run, background_tasks
    from datetime import datetime, timedelta, timezone
    sample_year = int(july_month[:4]) if july_month else datetime.now(timezone(timedelta(hours=9))).year
    state = {'signature': None, 'busy': False, 'updating': False,
             'data': {'points': [], 'notices': []}, 'pinned': None, 'chart': None}

    def unpin():
        chart, pinned = state['chart'], state['pinned']
        state['pinned'] = None
        if not pinned or chart is None or chart.is_deleted:
            return
        chart.run_chart_method('setOption', {'tooltip': {'triggerOn': 'mousemove|click', 'alwaysShowContent': False}})
        chart.run_chart_method('dispatchAction', {'type': 'hideTip'})
        if pinned:
            chart.run_chart_method('dispatchAction', {'type': 'downplay', **pinned})

    def pin(event):
        chart = event.sender
        if chart is not state['chart'] or not event.data or event.data.get('value') is None:
            return
        key = {'seriesIndex': event.series_index, 'dataIndex': event.data_index}
        if state['pinned'] == key:
            unpin()
            return
        if state['pinned']:
            chart.run_chart_method('dispatchAction', {'type': 'downplay', **state['pinned']})
        state['pinned'] = key
        chart.run_chart_method('setOption', {'tooltip': {'triggerOn': 'none', 'alwaysShowContent': True}})
        chart.run_chart_method('dispatchAction', {'type': 'highlight', **key})
        chart.run_chart_method('dispatchAction', {'type': 'showTip', **key})

    with ui.card().classes('w-full gap-3 monthly-trend-card').props('data-testid=monthly-cost-trend'):
        with ui.row().classes('w-full items-center justify-between gap-2'):
            ui.label('월별 청구 물류비').classes('section-title')
            with ui.row().classes('items-center gap-3'):
                demo_badge = ui.label('임의 데이터 포함').classes('trend-sample-badge').props('data-testid=trend-sample-badge')
                year = ui.select([], label='연도').props('outlined dense data-testid=trend-year').classes('w-32')
        content = ui.column().classes('w-full gap-2 monthly-trend-content')

    ui.on('monthly_cost_unpin', unpin)
    ui.run_javascript('''(() => {
        if (window.monthlyCostClickAway) {
            document.removeEventListener('click', window.monthlyCostClickAway.capture, true);
        }
        const binding = {pointClick: null, armed: false};
        binding.capture = event => {
            // Wait for ECharts to identify a bar click before treating it as click-away.
            setTimeout(() => {
                if (!binding.armed || event === binding.pointClick) return;
                if (event.target.closest?.('[data-testid="monthly-cost-tooltip"]')) return;
                binding.armed = false;
                emitEvent('monthly_cost_unpin');
            }, 0);
        };
        window.monthlyCostClickAway = binding;
        document.addEventListener('click', binding.capture, true);
    })()''')

    def draw():
        if state['updating']:
            return
        state['chart'] = None
        state['pinned'] = None
        content.clear()
        points = displayed_points(state['data']['points'], sample_year)
        visible_years = {str(y) for y in chart_years(points, year.value)}
        selected = [p for p in points if p['month'][:4] in visible_years]
        has_samples = any(p.get('sample') for p in points if p['month'][:4] in visible_years)
        demo_badge.set_visibility(has_samples)
        real = [p for p in selected if not p.get('sample')]
        with content:
            if selected:
                latest = (real or selected)[-1]
                comparison = yoy_comparison(points, latest['month'])
                with ui.column().classes('trend-yoy w-full gap-1').props('data-testid=trend-yoy'):
                    ui.label(f"{latest['month'][:4]}년 {int(latest['month'][5:])}월 전년 동월 대비").classes('muted')
                    ui.label(yoy_text(comparison, show_sample=False, compact=True)).classes('trend-yoy-value')
                chart = ui.echart(chart_options(points, year.value), on_point_click=pin, renderer='svg').classes('w-full monthly-trend-chart')
                state['chart'] = chart
                chart.props('data-testid=monthly-cost-chart')
                chart.on('chart:click', js_handler='''event => {
                    if (event.componentType !== 'series' || !event.data || event.data.value == null) return;
                    const binding = window.monthlyCostClickAway;
                    if (binding) {
                        binding.pointClick = event.event?.event;
                        binding.armed = true;
                    }
                }''')
                description = ' · '.join(
                    f"{p['month']}: {exact_won(p['amount'])}원 ({'임의 데이터' if p.get('sample') else '실제'})"
                    for p in points if p['month'][:4] in visible_years)
                chart._props.update({'role': 'img', 'aria-label': description})
            else:
                ui.label('완료된 월별 검증 결과가 없습니다.').classes('muted')
            for notice in state['data']['notices']:
                ui.label(notice).classes('muted')

    def configure():
        state['updating'] = True
        try:
            points = displayed_points(state['data']['points'], sample_year)
            years = sorted({p['month'][:4] for p in points})
            options = {'all': '전체', **{y: y + '년' for y in years}}
            selected_year = year.value if year.value in options else 'all'
            year.set_options(options, value=selected_year)
        finally:
            state['updating'] = False
        draw()

    async def refresh():
        signature = publication_signature(root)
        if state['busy'] or state['signature'] == signature:
            return
        state['busy'] = True
        try:
            state['data'] = await run.io_bound(load_trend, root, july_month, july_amount, july_categories)
            if content.is_deleted:
                return
            configure()
            state['signature'] = signature
        finally:
            state['busy'] = False

    year.on_value_change(draw)
    ui.timer(2, refresh)
    background_tasks.create(refresh())
