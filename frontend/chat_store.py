"""Local chat library. Deliberately separate from source, decisions and results."""
import copy
import json
from pathlib import Path
import threading
import time
import uuid


class ChatStore:
    def __init__(self, runtime):
        self.runtime=Path(runtime).resolve()
        self.runtime.mkdir(parents=True,exist_ok=True)
        self.path=self.runtime/'conversations.json'
        self.lock=threading.RLock()
        if self.path.exists():
            self.data=json.loads(self.path.read_text('utf-8'))
        else:
            self.data={'version':1,'revision':0,'active_id':None,'folders':{},'chats':{}}
            old=self.runtime/'chat.json'
            messages=json.loads(old.read_text('utf-8-sig')) if old.exists() else []
            self.new('이전 대화' if messages else '새 채팅')
            chat=self.current()
            self.data['legacy_chat_id']=chat['id']
            chat['messages']=[dict(m,id=uuid.uuid4().hex) for m in messages]
            for filename,field in [('chat_context.json','context'),('chat_last_result.json','last_result')]:
                path=self.runtime/filename
                if path.exists(): chat[field]=json.loads(path.read_text('utf-8-sig'))
            self.persist()
        # Remove legacy copies only after the durable library exists (also recovers interrupted migration).
        for name in ('chat.json','chat_context.json','chat_last_result.json'):
            path=self.runtime/name
            if path.exists():
                if path.resolve().parent!=self.runtime or path.is_symlink():raise ValueError('대화 파일 외부 연결은 허용하지 않습니다.')
                path.unlink()

    def persist(self):
        self.data['revision']+=1
        temp=self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.data,ensure_ascii=False,indent=2),'utf-8')
        temp.replace(self.path)

    def current(self, chat_id=None):
        key=chat_id or self.data['active_id']
        if key not in self.data['chats']:raise ValueError('삭제되었거나 없는 채팅입니다.')
        return self.data['chats'][key]

    def new(self, title='새 채팅'):
        with self.lock:
            key=uuid.uuid4().hex;now=time.time()
            self.data['chats'][key]={'id':key,'title':title,'folder_id':'','created_at':now,'updated_at':now,
                                     'messages':[],'context':{},'last_result':{}}
            self.data['active_id']=key;self.persist();return key

    def select(self, key):
        with self.lock:self.current(key);self.data['active_id']=key;self.persist()

    def append(self, role, text, chat_id=None):
        with self.lock:
            chat=self.current(chat_id)
            if role=='user' and not chat['messages'] and chat['title']=='새 채팅':chat['title']=text.strip().replace('\n',' ')[:45]
            chat['messages'].append({'id':uuid.uuid4().hex,'role':role,'text':text,'at':time.time()})
            chat['updated_at']=time.time();self.persist()

    def set_result(self, chat_id, result):
        with self.lock:
            chat=self.current(chat_id);chat['last_result']=result
            month=result.get('conversation_month')
            if result.get('clear_intake'):
                for key in ('command_intake','command_selection','memory_preview','disposition_preview','rule_preview'):
                    chat['context'].pop(key,None)
            # A question about another month does not retarget a pending change.
            keep_draft = result.get('preserve_pending') and chat['context'].get('command_intake')
            if month and not keep_draft:
                old_month=chat['context'].get('month')
                if old_month and month!=old_month:
                    chat['context'].pop('command_selection',None)
                    chat['context'].pop('command_intake',None)
                    chat['context'].pop('memory_preview',None)
                    chat['context'].pop('disposition_preview',None)
                chat['context']['month']=month
            if isinstance(result.get('intake'),dict) and not result.get('preserve_pending'):
                chat['context'].pop('memory_preview',None)
                chat['context'].pop('disposition_preview',None)
                chat['context']['command_intake']=copy.deepcopy(result['intake'])
                if result['intake'].get('state')=='cancelled' or result['intake'].get('clear_selection'):
                    chat['context'].pop('command_selection',None)
                if result['intake'].get('state')=='cancelled':
                    chat['context'].pop('command_intake',None)
            self.persist()

    def mutate(self, action, **args):
        with self.lock:
            if action=='new':return self.new()
            if action=='select':self.select(args['chat_id']);return
            if action=='delete_all':
                self.data['chats']={};self.new();return
            if action in ('move_many','delete_many'):
                keys=set(args.get('chat_ids',[]))
                if not keys or not keys.issubset(self.data['chats']):raise ValueError('대상 채팅을 다시 선택하세요.')
                if action=='move_many':
                    folder=args.get('folder_id','')
                    if folder and folder not in self.data['folders']:raise ValueError('없는 폴더입니다.')
                    for key in keys:self.data['chats'][key]['folder_id']=folder
                else:
                    for key in keys:del self.data['chats'][key]
                    if not self.data['chats']:self.new();return
                    if self.data['active_id'] in keys:self.data['active_id']=next(iter(self.data['chats']))
                self.persist();return
            if action in ('new_folder','rename_folder'):
                name=args.get('name','').strip()[:60]
                if not name:raise ValueError('폴더 이름을 입력해 주세요.')
                key=args.get('folder_id') or uuid.uuid4().hex
                if action=='rename_folder' and key not in self.data['folders']:raise ValueError('없는 폴더입니다.')
                self.data['folders'][key]=name
            elif action=='delete_folder':
                key=args['folder_id'];self.data['folders'].pop(key,None)
                for chat in self.data['chats'].values():
                    if chat['folder_id']==key:chat['folder_id']=''
            else:
                chat=self.current(args.get('chat_id'))
                if action=='rename':
                    name=args.get('name','').strip()[:80]
                    if not name:raise ValueError('채팅 제목을 입력해 주세요.')
                    chat['title']=name
                elif action=='move':
                    key=args.get('folder_id','')
                    if key and key not in self.data['folders']:raise ValueError('없는 폴더입니다.')
                    chat['folder_id']=key
                elif action=='delete_messages':
                    ids=set(args['message_ids'])
                    if not ids or not ids.issubset({m['id'] for m in chat['messages']}):raise ValueError('삭제할 메시지를 다시 선택하세요.')
                    chat['messages']=[m for m in chat['messages'] if m['id'] not in ids]
                    # Never reuse a deleted turn's month or cached reply as hidden model context.
                    chat['context']={};chat['last_result']={}
                elif action=='delete_chat':
                    del self.data['chats'][chat['id']]
                    if not self.data['chats']:self.new();return
                    if self.data['active_id']==chat['id']:self.data['active_id']=next(iter(self.data['chats']))
                else:raise ValueError('지원하지 않는 채팅 관리 동작입니다.')
            self.persist()

    def snapshot(self):
        with self.lock:return copy.deepcopy(self.data)
