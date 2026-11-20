"""Browser Back/Forward for the mounted workspace tabs; no verification side effects."""
import json

from nicegui import ui


VIEWS = {'dashboard': '대시보드', 'chat': '대화', 'checklist': '확인 목록',
         'evidence': '추가 증빙', 'monthly': '월별 검증', 'library': '채팅 보관함'}


def bind_navigation_history(tabs, close_overlays):
    def restore(event):
        key = event.args.get('view') if isinstance(event.args, dict) else None
        if key not in VIEWS:
            return
        close_overlays()
        tabs.set_value(VIEWS[key])
        ui.run_javascript('if (window.skinNavigation) window.skinNavigation.restoring = false')

    ui.on('workspace_navigation', restore)
    ui.run_javascript('''(() => {
        const valid = new Set(''' + json.dumps(list(VIEWS)) + ''');
        const route = () => {
            const candidate = location.hash.startsWith('#view=') ? location.hash.slice(6) : '';
            return valid.has(candidate) ? candidate : 'dashboard';
        };
        if (window.skinNavigation) {
            window.removeEventListener('popstate', window.skinNavigation.pop);
            document.removeEventListener('click', window.skinNavigation.capture, true);
            document.removeEventListener('keydown', window.skinNavigation.capture, true);
        }
        const nav = {current: route()};
        const url = key => location.pathname + location.search + '#view=' + key;
        nav.go = key => {
            if (nav.restoring || !valid.has(key) || key === nav.current) return;
            nav.current = key;
            history.pushState({...history.state, skinView: key}, '', url(key));
        };
        nav.pop = () => {
            nav.current = route();
            nav.restoring = true;
            emitEvent('workspace_navigation', {view: nav.current});
        };
        const labels = ''' + json.dumps({label:key for key,label in VIEWS.items()}) + ''';
        nav.capture = event => {
            if (event.type === 'keydown' && event.key !== 'Enter') return;
            const element = event.target.closest?.('[data-history-view],.q-tab');
            if (!element) return;
            const key = element.dataset.historyView || labels[element.querySelector('.q-tab__label')?.textContent.trim()];
            if (key) nav.go(key);
        };
        window.skinNavigation = nav;
        history.replaceState({...history.state, skinView: nav.current}, '', url(nav.current));
        window.addEventListener('popstate', nav.pop);
        document.addEventListener('click', nav.capture, true);
        document.addEventListener('keydown', nav.capture, true);
        if (nav.current !== 'dashboard') emitEvent('workspace_navigation', {view: nav.current});
    })()''')

    def changed(event):
        value = event.value
        name = getattr(value, 'name', value)
        key = next((k for k, label in VIEWS.items() if label == name), None)
        if key:
            ui.run_javascript(f'window.skinNavigation?.go({json.dumps(key)})')

    tabs.on_value_change(changed)
