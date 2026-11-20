"""Editable dates with local calendar selection; never infer a billing period."""
from datetime import date, datetime, timedelta, timezone
import re
from nicegui import ui


def billing_period(start_text, end_text, default_year):
    """Normalize explicit period inputs; billing month follows the end date."""
    token=r'(?:20\d{2}[-/.])?\d{1,2}[-/.]\d{1,2}'
    start_text=(start_text or '').strip();end_text=(end_text or '').strip()
    match=re.fullmatch(rf'({token})\s*(?:~|～|–|—|-)\s*({token})',start_text)
    if match:
        start_text,end_text=match.groups()
    def parse(value):
        parts=[int(p) for p in re.split(r'[-/.]',value)] if re.fullmatch(token,value) else []
        if len(parts)==2:parts.insert(0,default_year)
        if len(parts)!=3:raise ValueError('시작일과 종료일을 입력해 주세요. 예: 2029-06-25 / 2029-07-26')
        try:return date(*parts)
        except ValueError:raise ValueError('실제 존재하는 날짜를 입력해 주세요.') from None
    start,end=parse(start_text),parse(end_text)
    if start>end:raise ValueError('종료일은 시작일 이후여야 합니다. 연도를 넘는 기간은 연도까지 입력해 주세요.')
    return start.isoformat(),end.isoformat(),end.strftime('%Y-%m')


def query_date(text, default_year):
    """An optional checklist boundary; unlike a billing period, either end may be empty."""
    text = (text or '').strip()
    if not text:
        return None
    if not re.fullmatch(r'(?:20\d{2}[-/.])?\d{1,2}[-/.]\d{1,2}', text):
        raise ValueError('날짜를 YYYY-MM-DD로 입력해 주세요.')
    return billing_period(text, text, default_year)[0]


def filter_dates(rows, start=None, end=None):
    """Inclusive bounds over the displayed source date; undated rows stay undated."""
    if not start and not end:
        return rows
    result = []
    for row in rows:
        try:
            day = date.fromisoformat(str(row.get('일자') or '')).isoformat()
        except ValueError:
            continue
        if (not start or day >= start) and (not end or day <= end):
            result.append(row)
    return result


def calendar_input(label, *, month_only=False, placeholder=None, default_date=None, compact=False):
    field=ui.input(label,placeholder=placeholder).props('outlined')

    if not month_only:
        with field.add_slot('append'):
            with ui.button(icon='calendar_month').props(f'flat round dense aria-label="{label.split(" (")[0]} 달력 열기"').tooltip('달력에서 선택'):
                with ui.menu().props('anchor="top right" self="bottom right" :offset="[0, 8]"').classes('date-input-calendar') as menu:
                    if compact:
                        menu.classes('compact-date-calendar')
                    def choose(event):
                        if event.value:field.set_value(event.value);menu.close()
                    picker=ui.date(on_change=choose).props('minimal').classes('shadow-none')
                    if not compact:
                        ui.button('닫기',on_click=menu.close).props('flat dense').classes('float-right m-1')
        def sync_calendar():
            today=datetime.now(timezone(timedelta(hours=9))).date()
            raw=(field.value or '').strip()
            try:
                normalized,_,_=billing_period(raw,raw,today.year)
            except ValueError:normalized=None
            picker._props['model-value']=normalized
            picker._props['default-year-month']=((normalized or default_date)[:7].replace('-','/') if normalized or default_date else today.strftime('%Y/%m'))
            picker.update()
        menu.on('before-show',sync_calendar)
        return field

    def open_calendar():
        today=datetime.now(timezone(timedelta(hours=9))).date()
        raw=(field.value or '').strip()
        valid=bool(re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])',raw)) if month_only else False
        if not month_only:
            try:date.fromisoformat(raw);valid=bool(re.fullmatch(r'20\d{2}-\d{2}-\d{2}',raw))
            except ValueError:pass
        with ui.dialog() as dialog,ui.card().classes('gap-4 max-w-full'):
            ui.label('청구월 선택' if month_only else label.split(' (')[0]+' 선택').classes('section-title')
            if month_only:
                with ui.row().classes('items-center flex-nowrap'):
                    def step(delta):
                        try:value=int(year.value)
                        except (TypeError,ValueError):value=today.year
                        year.set_value(max(2000,min(2099,value+delta)))
                    ui.button(icon='chevron_left',on_click=lambda:step(-1)).props('flat round aria-label=이전연도')
                    year=ui.number('연도',value=int(raw[:4]) if valid else today.year,min=2000,max=2099,step=1,precision=0).props('outlined dense').classes('w-32')
                    ui.button(icon='chevron_right',on_click=lambda:step(1)).props('flat round aria-label=다음연도')
                def choose_month(number):
                    value=year.value
                    if value is None or not float(value).is_integer() or not 2000<=value<=2099:
                        ui.notify('연도를 2000~2099 사이로 입력해 주세요.',type='warning');return
                    field.set_value(f'{int(value):04d}-{number:02d}');dialog.close()
                with ui.element('div').classes('grid grid-cols-3 gap-2 w-full'):
                    for number in range(1,13):
                        ui.button(f'{number}월',on_click=lambda m=number:choose_month(m)).props('unelevated' if valid and int(raw[5:7])==number else 'flat')
            else:
                def choose_date(event):
                    if event.value:field.set_value(event.value);dialog.close()
                ui.date(raw if valid else None,on_change=choose_date).props(f'default-year-month={raw[:7].replace("-","/") if valid else today.strftime("%Y/%m")}').classes('shadow-none')
            ui.label('입력창에 직접 입력해도 됩니다.').classes('muted')
            ui.button('닫기',on_click=dialog.close).props('flat').classes('self-end')
        dialog.open()

    with field.add_slot('append'):
        ui.button(icon='calendar_month',on_click=open_calendar).props(f'flat round dense aria-label="{label.split(" (")[0]} 달력 열기"').tooltip('달력에서 선택')
    return field
