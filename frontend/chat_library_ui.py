"""Chat library controls. These actions never delete verification artifacts."""
from datetime import datetime
from nicegui import ui


def confirm_delete(title, action):
    with ui.dialog() as dialog, ui.card().classes('max-w-lg w-full'):
        ui.label(title).classes('section-title')
        ui.label('삭제한 대화는 복구할 수 없습니다. 원본 자료·검증 결과·사용자 업무 결정은 유지됩니다.').classes('muted')
        with ui.row().classes('w-full justify-end'):
            ui.button('취소',on_click=dialog.close).props('flat')
            def commit():
                try:action();dialog.close()
                except ValueError as exc:ui.notify(str(exc),type='negative')
            ui.button('삭제',on_click=commit).props('color=negative unelevated')
    dialog.open()


def render_library(adapter, open_chat, changed):
    selected={'id':None};last={'revision':None}
    with ui.column().classes('w-full gap-5'):
        ui.label('채팅 보관함').classes('title')
        ui.label('이 PC에 자동 저장됩니다. 대화별로 맥락을 분리하며 새 채팅은 이전 대화를 참조하지 않습니다.').classes('muted')
        with ui.row().classes('w-full gap-2'):
            ui.button('새 채팅',icon='add',on_click=lambda:open_chat(adapter.manage_chat('new'))).props('unelevated')
            def name_dialog(title, callback, value=''):
                with ui.dialog() as dialog,ui.card().classes('w-96'):
                    ui.label(title).classes('section-title')
                    name=ui.input('이름',value=value).props('outlined').classes('w-full')
                    def commit():
                        try:callback(name.value);changed();refresh(force=True);dialog.close()
                        except ValueError as exc:ui.notify(str(exc),type='negative')
                    ui.button('저장',on_click=commit)
                dialog.open()
            ui.button('폴더 만들기',icon='create_new_folder',on_click=lambda:name_dialog('새 폴더',lambda name:adapter.manage_chat('new_folder',name=name))).props('outline')
            ui.button('채팅기록 전체 삭제',icon='delete_sweep',on_click=lambda:confirm_delete('모든 채팅 기록을 삭제할까요?',lambda:change('delete_all'))).props('flat color=negative')
        with ui.card().classes('w-full') as library_card:
            with ui.row().classes('w-full items-center'):
                folder=ui.select({'*':'전체','':'미분류'},value='*',label='채팅 폴더').props('outlined dense').classes('w-56')
                search=ui.input('채팅 제목·내용 검색').props('outlined dense clearable').classes('grow')
                ui.button('폴더 이름 변경',on_click=lambda:folder_edit()).props('flat')
                ui.button('폴더 삭제',on_click=lambda:folder_delete()).props('flat color=negative')
            table=ui.table(columns=[{'name':k,'label':label,'field':k,'align':'left'} for k,label in
                                   [('title','채팅 제목'),('folder','폴더'),('count','메시지 수'),('updated','최근 대화')]],
                           rows=[],row_key='id',pagination=10,selection='multiple').classes('w-full chat-library-table').props('flat tabindex=0')
            table.add_slot('body', '''<q-tr :props="props" tabindex="0" class="cursor-pointer"
                @click="event => { $parent.$emit('chatFocus', props.row.id); const row = event.currentTarget; const browser = row.ownerDocument.defaultView; browser.clearTimeout(row._openChatTimer); if (event.detail < 2) row._openChatTimer = browser.setTimeout(() => $parent.$emit('openChat', props.row.id), 300); }"
                @keydown.enter.prevent.stop="$parent.$emit('openChat', props.row.id)"
                @keydown.f2="event => { const row = event.currentTarget; row.ownerDocument.defaultView.clearTimeout(row._openChatTimer); $parent.$emit('chatFocus', props.row.id); }"
                @contextmenu="event => { const row = event.currentTarget; row.ownerDocument.defaultView.clearTimeout(row._openChatTimer); $parent.$emit('chatContext', props.row.id); }">
                <q-td auto-width><q-checkbox v-model="props.selected" @click.stop :aria-label="'채팅 선택: ' + props.row.title" /></q-td>
                <q-td v-for="col in props.cols" :key="col.name" :props="props">
                    <span v-if="col.name === 'title'" @dblclick.stop="event => { const row = event.currentTarget.closest('tr'); row.ownerDocument.defaultView.clearTimeout(row._openChatTimer); $parent.$emit('renameChat', props.row.id); }" title="클릭해서 대화 열기 · 더블클릭 또는 F2로 이름 변경">{{ col.value }}</span>
                    <span v-else>{{ col.value }}</span>
                </q-td></q-tr>''')
            selection=ui.label('').classes('muted')
            ui.label('클릭해서 대화 열기 · 이름 더블클릭 또는 F2로 변경 · 체크박스 선택 후 우클릭으로 이동·삭제').classes('muted')
            def selection_changed():
                selection.set_text(f'체크한 채팅 {len(table.selected)}개')
            table.on_select(lambda e:selection_changed())
            def choose(e):selected['id']=e.args
            table.on('chatFocus',choose)
            table.on('openChat',lambda e:open_chat(e.args))
            def context_target(e):
                selected['id']=e.args
                if not table.selected:
                    table.selected=[r for r in table.rows if r['id']==e.args];table.update();selection_changed()
            table.on('chatContext',context_target)
            def chosen():
                if len(table.selected)>1:
                    ui.notify('이름 변경·대화 열기는 채팅 하나만 선택해 주세요.');return None
                key=table.selected[0]['id'] if table.selected else selected['id']
                if not key or key not in adapter.library.data['chats']:
                    ui.notify('먼저 채팅을 선택해 주세요.');return None
                return key
            def rename(key=None):
                key=key or chosen()
                if key:name_dialog('채팅 제목 변경',lambda name:adapter.manage_chat('rename',chat_id=key,name=name),adapter.library.current(key)['title'])
            table.on('renameChat',lambda e:rename(e.args))
            table.on('keydown',lambda:rename(),js_handler='e => {if(e.key === "F2" && !["INPUT","TEXTAREA"].includes(e.target.tagName)){e.preventDefault();e.stopPropagation();emit();}}')
            def checked_ids():
                keys=[r['id'] for r in table.selected]
                if not keys:ui.notify('먼저 체크박스로 채팅을 선택해 주세요.')
                return keys
            def move():
                keys=checked_ids()
                if not keys:return
                with ui.dialog() as dialog,ui.card().classes('w-96'):
                    ui.label(f'채팅 {len(keys)}개 폴더 이동').classes('section-title')
                    target=ui.select({'':'미분류',**adapter.library.data['folders']},value=adapter.library.current(keys[0])['folder_id'],label='이동할 폴더').classes('w-full')
                    ui.button('이동',on_click=lambda:(change('move_many',chat_ids=keys,folder_id=target.value),dialog.close()))
                dialog.open()
            def delete_checked():
                keys=checked_ids()
                if keys:confirm_delete(f'선택한 채팅 {len(keys)}개를 삭제할까요?',lambda:change('delete_many',chat_ids=keys))
            with library_card,ui.context_menu():
                ui.menu_item('폴더 이동',on_click=move).props('role=menuitem')
                ui.menu_item('선택한 채팅 삭제',on_click=delete_checked).props('role=menuitem').classes('text-red-600')
            with ui.row().classes('gap-2'):
                def open_selected():
                    key=chosen()
                    if key:open_chat(key)
                ui.button('대화 열기',on_click=open_selected)
    def change(action,**args):
        adapter.manage_chat(action,**args)
        if action=='delete_all':
            selected['id']=None;folder.set_value('*');search.set_value('')
        if action in ('move_many','delete_many','delete_all'):
            table.selected=[];selected['id']=None;selection_changed()
        changed();refresh(force=True)
    def folder_edit():
        key=folder.value
        if key not in adapter.library.data['folders']:ui.notify('변경할 폴더를 선택해 주세요.');return
        name_dialog('폴더 이름 변경',lambda name:adapter.manage_chat('rename_folder',folder_id=key,name=name),adapter.library.data['folders'][key])
    def folder_delete():
        key=folder.value
        if key not in adapter.library.data['folders']:ui.notify('삭제할 폴더를 선택해 주세요.');return
        with ui.dialog() as dialog,ui.card():
            ui.label('폴더만 삭제하고 포함된 채팅은 미분류로 옮깁니다.')
            ui.button('폴더 삭제',on_click=lambda:(change('delete_folder',folder_id=key),dialog.close())).props('color=negative')
        dialog.open()
    def refresh(force=False):
        data=adapter.library.snapshot();signature=(data['revision'],folder.value,search.value)
        if not force and last['revision']==signature:return
        last['revision']=signature
        options={'*':'전체','':'미분류',**data['folders']}
        if folder.value not in options:folder.value='*'
        folder.set_options(options)
        query=(search.value or '').strip().casefold()
        rows=[]
        for c in sorted(data['chats'].values(),key=lambda c:c['updated_at'],reverse=True):
            if folder.value!='*' and folder.value!=c['folder_id']:continue
            if query and query not in (c['title']+' '+' '.join(m['text'] for m in c['messages'])).casefold():continue
            rows.append({'id':c['id'],'title':c['title'],'folder':data['folders'].get(c['folder_id'],'미분류'),
                         'count':len(c['messages']),'updated':datetime.fromtimestamp(c['updated_at']).strftime('%m/%d %H:%M')})
        selected_keys={r['id'] for r in table.selected}
        table.selected=[r for r in rows if r['id'] in selected_keys]
        if selected['id'] not in {r['id'] for r in rows}:selected['id']=None
        table.rows=rows;table.update();selection_changed()
    folder.on_value_change(lambda:refresh());search.on_value_change(lambda:refresh())
    refresh();ui.timer(1,refresh)
