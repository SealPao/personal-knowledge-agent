import json
import os
import queue
import re
import threading
import time
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
import ai_service as svc
import budget
import local_ai
import web_search

FONT='Microsoft JhengHei UI'; WHITE='#ffffff'; BG='#f1f5f9'; INK='#1b2c40'; MUTED='#64748b'; BLUE='#2563eb'
DEFAULT={'provider':'openai','openai_model':'gpt-5.4-mini','local_model':'','local_base':'http://127.0.0.1:11434/v1','max_tokens':800,'network_enabled':False,'auto_start_local':True}

def read_config(path):
    try:
        with open(path,'r',encoding='utf-8') as f: data=json.load(f)
        if not isinstance(data,dict): return dict(DEFAULT)
        result={**DEFAULT,**{k:v for k,v in data.items() if k in DEFAULT}}
        result['network_enabled']=data.get('network_enabled') is True
        result['auto_start_local']=data.get('auto_start_local',True) is True
        return result
    except (OSError,ValueError,TypeError): return dict(DEFAULT)

def write_config(path,config):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    with open(temp,'w',encoding='utf-8') as f: json.dump({k:config.get(k,DEFAULT[k]) for k in DEFAULT},f,ensure_ascii=False,indent=2)
    os.replace(temp,path)

def lab(p,text='',**kw):
    kw.setdefault('font',(FONT,10)); kw.setdefault('bg',WHITE); kw.setdefault('fg',INK)
    return tk.Label(p,text=text,**kw)

def btn(p,text,command,primary=False):
    return tk.Button(p,text=text,command=command,bd=0,padx=14,pady=7,cursor='hand2',font=(FONT,10,'bold'),bg=BLUE if primary else '#e9f0fb',fg=WHITE if primary else '#1d4ed8')

def selected_sources(sources,flags):
    if len(sources)!=len(flags) or any(type(flag) is not bool for flag in flags): raise ValueError('來源選擇狀態無效')
    chosen=[source for source,flag in zip(sources,flags) if flag]
    if not chosen: raise ValueError('請至少勾選一個來源，或取消並重新搜尋。')
    return chosen

class AIWindow(tk.Toplevel):
    def __init__(self,master,db_path):
        super().__init__(master); self.title('AI 問答 · 私人知識助理'); self.geometry('1080x760'); self.minsize(850,620); self.configure(bg=BG)
        self.db=Path(db_path); self.config_path=self.db.parent/'ai-settings.json'; self.config=read_config(self.config_path)
        self.events=queue.Queue(); self.busy=False; self.sources=[]; self.sources_by_row=[]; self.download_cancel=threading.Event(); self.downloading=False; self.start_cancel=threading.Event(); self.starting_local=False; self.last_local_running=local_ai.running(); self.answer_started=None; self.download_phase='準備下載'; self.history=[]; self.pending_question=''; self.last_details=''; self.return_to_main=False; self.clarify_question=''
        self.question=tk.StringVar(); self.hint=tk.StringVar(); self._set_hint()
        head=tk.Frame(self,bg=BG); head.pack(fill='x',padx=25,pady=(20,12))
        lab(head,'與你的文件對話',bg=BG,font=(FONT,19,'bold')).pack(side='left')
        btn(head,'返回文件搜尋',self.close).pack(side='left',padx=12)
        btn(head,'AI 設定',self.settings).pack(side='right')
        self.local_button=btn(head,'關閉內建本地 AI' if local_ai.running() else '啟動內建本地 AI',self.toggle_local)
        self.local_button.pack(side='right',padx=8)
        btn(head,'預算與用量',self.open_budget).pack(side='right',padx=8)
        btn(head,'用閱讀器找答案',self.open_device).pack(side='right')
        lab(self,textvariable=self.hint,bg=BG,fg=MUTED,anchor='w').pack(fill='x',padx=25)
        self.download_button=btn(self,'下載可選本地模型（約 15.6 GB）',self.download_model)
        self.download_button.pack(anchor='w',padx=25,pady=(5,0))
        self.model_info=tk.StringVar(); lab(self,textvariable=self.model_info,bg=BG,fg=MUTED,font=(FONT,9)).pack(anchor='w',padx=25)
        self.download_progress=ttk.Progressbar(self,orient='horizontal',mode='determinate',maximum=100); self.download_progress.pack(fill='x',padx=25,pady=(4,0)); self.download_progress.pack_forget()
        self.refresh_model_info()
        card=tk.Frame(self,bg=WHITE,highlightbackground='#dbe4ef',highlightthickness=1); card.pack(fill='x',padx=25,pady=12)
        lab(card,'你想知道什麼？',font=(FONT,11,'bold')).pack(anchor='w',padx=15,pady=(12,4))
        row=tk.Frame(card,bg=WHITE); row.pack(fill='x',padx=15,pady=(0,15))
        self.entry=tk.Entry(row,textvariable=self.question,font=(FONT,12),bd=0,highlightthickness=1,highlightbackground='#cbd5e1',highlightcolor=BLUE)
        self.entry.pack(side='left',fill='x',expand=True,ipady=8); self.entry.bind('<Return>',lambda e:self.prepare())
        self.ask_button=btn(row,'尋找資料並回答',self.prepare,True); self.ask_button.pack(side='left',padx=(10,0))
        opts=tk.Frame(card,bg=WHITE); opts.pack(fill='x',padx=15,pady=(0,8))
        self.review_sources=tk.BooleanVar(value=False); tk.Checkbutton(opts,text='回答前手動選擇來源',variable=self.review_sources,bg=WHITE,fg=MUTED,font=(FONT,9),anchor='w').pack(side='left')
        self.web_enabled=tk.BooleanVar(value=False); tk.Checkbutton(opts,text='網路搜尋（問題會送到 DuckDuckGo）',variable=self.web_enabled,bg=WHITE,fg=MUTED,font=(FONT,9),anchor='w').pack(side='left',padx=12)
        bottom=tk.Frame(self,bg=BG); bottom.pack(side='bottom',fill='x',padx=25,pady=(0,15))
        self.status=tk.StringVar(value='回答會依本地搜尋結果產生，並標明來源。')
        lab(bottom,textvariable=self.status,bg=BG,fg=MUTED,anchor='w').pack(fill='x')
        self.stop_answer_button=btn(bottom,'停止本地模型並中止回答',self.stop_local_answer)
        body=tk.PanedWindow(self,orient='horizontal',bg=BG,bd=0,sashwidth=8); body.pack(fill='both',expand=True,padx=25,pady=(0,12))
        a=tk.Frame(body,bg=WHITE,highlightbackground='#dbe4ef',highlightthickness=1); s=tk.Frame(body,bg=WHITE,highlightbackground='#dbe4ef',highlightthickness=1)
        body.add(a,minsize=400,stretch='always'); self.source_panel=s; self.chat_body=body; self.source_visible=False
        aheader=tk.Frame(a,bg=WHITE); aheader.pack(fill='x',padx=16,pady=(13,8))
        lab(aheader,'對話',font=(FONT,12,'bold')).pack(side='left')
        btn(aheader,'顯示／隱藏來源',self.toggle_sources).pack(side='right')
        btn(aheader,'開始新對話',self.new_chat).pack(side='right',padx=6)
        f=tk.Frame(a,bg=WHITE); f.pack(fill='both',expand=True,padx=16,pady=(0,16))
        self.answer_box=tk.Text(f,wrap='word',font=(FONT,11),fg=INK,bg=WHITE,bd=0,spacing3=7)
        sb=ttk.Scrollbar(f,command=self.answer_box.yview); self.answer_box.configure(yscrollcommand=sb.set); self.answer_box.pack(side='left',fill='both',expand=True); sb.pack(side='right',fill='y')
        self._answer_text('你好！你可以詢問文件中的產品或操作問題，也可以接著上一個問題追問。')
        lab(s,'回答所引用的來源',font=(FONT,12,'bold')).pack(anchor='w',padx=15,pady=(13,6))
        self.cites=ttk.Treeview(s,columns=('number','file','where'),show='headings',height=8,selectmode='browse')
        for key,title,width in [('number','編號',48),('file','文件',180),('where','位置',130)]: self.cites.heading(key,text=title); self.cites.column(key,width=width,minwidth=40)
        self.cites.pack(fill='both',expand=True,padx=12,pady=(0,5)); self.cites.bind('<<TreeviewSelect>>',self.source_select)
        self.source_info=tk.Text(s,height=8,wrap='word',font=(FONT,9),fg=INK,bg='#f8fafc',bd=0,padx=10,pady=8)
        self.source_info.pack(fill='x',padx=12,pady=(5,8)); self.source_info.configure(state='disabled')
        btn(s,'開啟所選原始文件',self.open_source).pack(anchor='w',padx=12,pady=(0,14))
        self.protocol('WM_DELETE_WINDOW',self.close); self.poll_id=self.after(150,self.poll); self.after(250,self.restore_last_ai)

    def restore_last_ai(self):
        self.config=read_config(self.config_path)
        if self.busy or local_ai.running() or self.config['provider']!='local' or self.config['local_model']!=local_ai.MODEL or not self.config['auto_start_local']: return
        try: info=local_ai.model_status()
        except OSError: self.status.set('無法讀取本地模型，請檢查模型檔案。'); return
        if not info['ready'] or not info['runtime']:
            self.status.set('上次的本地模型未就緒；請檢查模型檔和執行程式。'); return
        self.toggle_local()
    def _set_hint(self):
        if self.config['provider']=='local':
            if self.config['local_model']==local_ai.MODEL:
                self.hint.set('目前 AI：內建本地模型 · '+('已啟動' if local_ai.headers_for(self.config['local_base'].rstrip('/')+'/models') else '未啟動'))
            else: self.hint.set('目前 AI：自訂本機服務（連線狀態以測試結果為準）')
        elif not self.config['network_enabled']: self.hint.set('目前 AI：OpenAI · 外網關閉，無法連線')
        else: self.hint.set('目前 AI：OpenAI · 提問、近期對話及搜尋片段會傳送至 OpenAI，可能產生費用。')
    def toggle_local(self):
        if self.starting_local:
            self.start_cancel.set(); self.local_button.configure(state='disabled'); self.status.set('正在取消本地 AI 啟動…'); return
        if self.busy: return
        if local_ai.running():
            local_ai.stop(); self.config=read_config(self.config_path); self.config['auto_start_local']=False
            try: write_config(self.config_path,self.config)
            except OSError as e: self.status.set('已關閉模型，但無法儲存設定：'+str(e)); return
            self.local_button.configure(text='啟動內建本地 AI'); self._set_hint(); self.refresh_model_info(); self.status.set('內建本地 AI 已關閉；下次不會自動啟動。'); return
        self.start_cancel.clear(); self.starting_local=True; self._set_busy(True); self.local_button.configure(text='取消啟動本地 AI'); self.hint.set('目前 AI：內建本地模型 · 啟動中'); self.status.set('正在啟動本地模型，首次載入可能需要數分鐘…')
        def start():
            try:
                try: result=local_ai.start(cancelled=self.start_cancel.is_set)
                except local_ai.StartCancelled: raise
                except RuntimeError:
                    self.events.put(('local_fallback',None)); result=local_ai.start(gpu=False,cancelled=self.start_cancel.is_set)
                self.events.put(('local_started',(True,result)))
            except Exception as e: self.events.put(('local_started',(False,str(e))))
        threading.Thread(target=start,daemon=True).start()
    def refresh_model_info(self):
        try:
            info=local_ai.model_status()
            if info['ready']:
                text='' if local_ai.running() else '本地模型已下載（依檔案大小判斷）。'
                if not self.downloading: self.download_button.configure(text='模型管理／驗證')
            elif info['present']: text=f"模型下載進度：{info['present']/info['total']*100:.1f}%；可繼續下載。"
            else: text='尚未下載內建模型。'
            if not info['runtime']: text+=' 缺少本地執行程式。'
            self.model_info.set(text)
        except OSError: self.model_info.set('無法讀取本地模型狀態。')
    def download_model(self):
        if self.downloading:
            self.download_cancel.set(); self.status.set('正在取消下載…'); return
        if self.busy or local_ai.running(): return
        if not messagebox.askyesno('下載可選本地 AI','將從 Hugging Face 下載 Google Gemma 4 模型及圖片組件，約 15.6 GB，並驗證完整性。下載需要網路與足夠磁碟空間；取消或中斷後可繼續下載。確定下載嗎？',parent=self): return
        self.downloading=True; self.download_cancel.clear(); self._set_busy(True); self.local_button.configure(state='disabled'); self.download_button.configure(text='取消模型下載'); self.download_phase='準備下載或驗證'; self.download_progress['value']=0; self.download_progress.pack(fill='x',padx=25,pady=(4,0),after=self.download_button); self.model_info.set('準備下載或驗證…'); self.status.set('正在準備模型…')
        def worker():
            last=[0.0]
            def progress(done,total):
                now=time.monotonic()
                if now-last[0]>=1 or done==total:
                    last[0]=now; self.events.put(('download_progress',(done,total)))
            def phase(name): self.events.put(('download_phase',name))
            try: local_ai.download(progress,self.download_cancel.is_set,phase=phase); self.events.put(('download_done',(True,'下載完成並已驗證，可啟動內建本地 AI。')))
            except Exception as e: self.events.put(('download_done',(False,str(e))))
        threading.Thread(target=worker,daemon=True).start()
    def open_budget(self):
        import budget_ui
        budget_ui.BudgetWindow(self,self.db,self.config['openai_model'])
    def open_device(self):
        import adb_ui
        adb_ui.DeviceWindow(self.master,self.db,self.question.get().strip())
    def toggle_sources(self):
        if self.source_visible: self.chat_body.forget(self.source_panel); self.source_visible=False
        else: self.chat_body.add(self.source_panel,minsize=280,stretch='always'); self.source_visible=True
    def new_chat(self):
        if self.busy: return
        self.history=[]; self.pending_question=''; self.clarify_question=''; self.last_details=''; self.sources_by_row=[]; self.cites.delete(*self.cites.get_children()); self._source_text('')
        self._answer_text('已開始新對話。')
    def _append_chat(self,role,value):
        self.answer_box.configure(state='normal'); self.answer_box.insert('end','\n\n'+role+'：\n'+value); self.answer_box.see('end'); self.answer_box.configure(state='disabled')
    @staticmethod
    def _compact_reply(value):
        lines=[]
        for line in value.splitlines():
            if line.lstrip().startswith(('原文 [','已依相同文件','已依回答中的明確機型','每項後方顯示對照原文')): continue
            lines.append(re.sub(r'\s*\[\d+\]','',line))
        return '\n'.join(lines).strip()
    def _answer_text(self,value):
        self.answer_box.configure(state='normal'); self.answer_box.delete('1.0','end'); self.answer_box.insert('1.0',value); self.answer_box.configure(state='disabled')
    def _source_text(self,value):
        self.source_info.configure(state='normal'); self.source_info.delete('1.0','end'); self.source_info.insert('1.0',value); self.source_info.configure(state='disabled')
    def _set_busy(self,value):
        self.busy=value; self.ask_button.configure(state='disabled' if value else 'normal')

    def settings(self):
        w=tk.Toplevel(self); w.title('AI 設定'); w.geometry('690x690'); w.configure(bg=WHITE); w.transient(self)
        f=tk.Frame(w,bg=WHITE); f.pack(fill='both',expand=True,padx=25,pady=20)
        lab(f,'AI 服務設定',font=(FONT,16,'bold')).pack(anchor='w')
        lab(f,'OpenAI 或本地模型可切換；不會自動下載模型。',fg=MUTED).pack(anchor='w',pady=(4,15))
        provider=tk.StringVar(value=self.config['provider']); om=tk.StringVar(value=self.config['openai_model']); lm=tk.StringVar(value=self.config['local_model']); base=tk.StringVar(value=self.config['local_base']); maximum=tk.StringVar(value=str(self.config['max_tokens'])); key=tk.StringVar(); network=tk.BooleanVar(value=bool(self.config['network_enabled'])); auto_local=tk.BooleanVar(value=self.config['auto_start_local'])
        tk.Checkbutton(f,text='允許 AI 連接外網（關閉時仍可使用本機模型）',variable=network,bg=WHITE,fg=INK,font=(FONT,10,'bold'),anchor='w').pack(fill='x',pady=(0,4))
        tk.Checkbutton(f,text='使用內建模型時自動啟動上次的模型',variable=auto_local,bg=WHITE,fg=INK,font=(FONT,9),anchor='w').pack(fill='x')
        mode=tk.Frame(f,bg=WHITE); mode.pack(fill='x')
        tk.Radiobutton(mode,text='OpenAI',variable=provider,value='openai',bg=WHITE,font=(FONT,10)).pack(side='left')
        tk.Radiobutton(mode,text='本地 AI（OpenAI 相容服務）',variable=provider,value='local',bg=WHITE,font=(FONT,10)).pack(side='left')
        def field(label,variable,masked=False):
            lab(f,label,font=(FONT,10,'bold')).pack(anchor='w',pady=(11,3))
            tk.Entry(f,textvariable=variable,font=(FONT,10),show='•' if masked else '',relief='solid',bd=1).pack(fill='x',ipady=4)
        field('OpenAI 模型',om); field('OpenAI API Key（留空代表不更換）',key,True)
        key_status=tk.StringVar()
        try: key_status.set('已儲存憑證' if svc.load_key() else '尚未儲存憑證')
        except Exception: key_status.set('無法讀取 Windows 憑證管理員')
        krow=tk.Frame(f,bg=WHITE); krow.pack(fill='x',pady=4); lab(krow,textvariable=key_status,fg=MUTED).pack(side='left')
        def remove():
            if messagebox.askyesno('刪除 Key','要從 Windows 憑證管理員刪除已儲存的 OpenAI API Key 嗎？',parent=w):
                try: svc.delete_key(); key_status.set('尚未儲存憑證'); key.set('')
                except Exception as e: messagebox.showerror('刪除失敗',str(e),parent=w)
        btn(krow,'刪除已儲存 Key',remove).pack(side='right')
        field('本地模型名稱',lm); field('本地服務位址（僅限此電腦）',base); field('最大輸出 Token（64–4096）',maximum)
        lab(f,'Key 存於 Windows 憑證管理員；設定檔與知識庫不包含 Key。',fg=MUTED,font=(FONT,9)).pack(anchor='w',pady=(12,4))
        def save():
            try:
                limit=int(maximum.get())
                if not 64<=limit<=4096: raise ValueError('最大輸出 Token 必須介於 64 和 4096')
                if not om.get().strip(): raise ValueError('請填寫 OpenAI 模型名稱')
                svc.local_endpoint(base.get())
                if provider.get()=='local' and not lm.get().strip(): raise ValueError('使用本地 AI 時請填寫本地模型名稱')
                if key.get().strip(): svc.save_key(key.get())
                conf={'provider':provider.get(),'openai_model':om.get().strip(),'local_model':lm.get().strip(),'local_base':base.get().strip(),'max_tokens':limit,'network_enabled':network.get(),'auto_start_local':auto_local.get()}
                write_config(self.config_path,conf); self.config=conf; self._set_hint(); key.set(''); w.destroy()
                if hasattr(self.master,'refresh_network'): self.master.refresh_network()
            except Exception as e: messagebox.showerror('無法儲存設定',str(e),parent=w)
        actions=tk.Frame(f,bg=WHITE); actions.pack(fill='x',pady=(10,0))
        btn(actions,'儲存設定',save,True).pack(side='right')
        def test():
            # Read widget values on the UI thread.
            mode=provider.get(); addr=base.get().strip(); typed=key.get().strip(); allow=network.get()
            def safe_work():
                try:
                    candidate=typed or (svc.load_key() if mode=='openai' else None)
                    count=svc.test_connection(mode,candidate,addr,network_enabled=allow)
                    self.events.put(('connection_test',(True,f'服務已連線，回傳 {count} 個可見模型；實際模型權限仍需以問答確認。')))
                except Exception as e: self.events.put(('connection_test',(False,str(e))))
            threading.Thread(target=safe_work,daemon=True).start()
        btn(actions,'測試連線',test).pack(side='right',padx=10)

    def prepare(self):
        if self.busy: return
        self.config=read_config(self.config_path); self._set_hint()
        q=self.question.get().strip()
        if not q or len(q)>svc.MAX_QUESTION: messagebox.showinfo('請輸入問題','問題不可為空，最多 2000 字。',parent=self); return
        if re.search(r'^(你|您)(可以|能|會|会)(回答|做|幫|帮|協助|协助).{0,10}[?？]?$|^(你|您)(有什麼|有什么)功能[?？]?$',q):
            self._append_chat('你',q); reply='我可以協助搜尋已匯入的文件、整理操作步驟並附上來源，也可以接著上一個問題追問。你可以直接問我產品、機型或操作問題。'; self._append_chat('助理',reply); self.history.append({'question':q,'answer':reply}); self.question.set(''); self.status.set('可以繼續提問。'); return
        if q.strip(' ！!？?。') in ('你好','您好','嗨','哈囉','哈啰'):
            reply='你好！想查詢哪個產品或操作問題？'; self._append_chat('你',q); self._append_chat('助理',reply); self.history.append({'question':q,'answer':reply}); self.question.set(''); self.status.set('可以繼續提問。'); return
        if self.config['provider']=='local' and not self.config['local_model']:
            messagebox.showinfo('請設定本地模型','請先在 AI 設定填入本地模型名稱。',parent=self); return
        if self.config['provider']=='openai':
            if not self.config['network_enabled']:
                messagebox.showinfo('外網存取已關閉','若要使用 OpenAI，請先在 AI 設定開啟外網存取。',parent=self); return
            try: exists=bool(svc.load_key())
            except Exception as e: messagebox.showerror('無法讀取 Key',str(e),parent=self); return
            if not exists: messagebox.showinfo('請設定 API Key','請先在 AI 設定儲存 OpenAI API Key。',parent=self); return
        visible_q=q
        if self.clarify_question and len(q)<=60 and not re.search(r'[?？]|(怎麼|如何|怎麽|哪裡|为何|為什麼)',q):
            q=(self.clarify_question+'；補充品牌或機型：'+q)[:svc.MAX_QUESTION]
        self.clarify_question=''
        self.pending_question=q; self.last_details=''; self._append_chat('你',visible_q); self.question.set(''); self.cites.delete(*self.cites.get_children()); self.sources_by_row=[]; self._source_text('')
        self._set_busy(True); self.status.set('正在本地搜尋相關文件…'); approved=dict(self.config); review=self.review_sources.get(); use_web=self.web_enabled.get()
        if use_web: self.status.set('正在本地搜尋，並將問題傳送至 DuckDuckGo…')
        def work():
            try:
                sources=svc.retrieve(str(self.db),svc.search_question(q,self.history))
                if use_web:
                    try:
                        found=web_search.search(q)
                        models=svc.model_identifiers(q)
                        if models: found=[item for item in found if all(svc.matches_model(item['title']+' '+item['snippet'],term) for term in models)]
                        if not found: self.events.put(('web_error','網路摘要沒有明確匹配指定型號'))
                        sources=sources[:3]+web_search.to_sources(found,start=min(len(sources),3)+1)
                    except Exception as e: self.events.put(('web_error',str(e)))
                self.events.put(('retrieved',(q,sources,approved,review)))
            except Exception as e: self.events.put(('error',str(e)))
        threading.Thread(target=work,daemon=True).start()

    def preview(self,q,sources):
        if not sources:
            self._set_busy(False); self.status.set('沒有找到可用來源，請換一個問題或確認文件已匯入。'); return
        w=tk.Toplevel(self); w.title('確認使用的資料'); w.geometry('850x620'); w.configure(bg=WHITE); w.transient(self); w.grab_set()
        destination='OpenAI（可能產生費用）' if self.config['provider']=='openai' else '此電腦上的本地模型服務'
        lab(w,'確認使用的資料',font=(FONT,15,'bold')).pack(anchor='w',padx=20,pady=(18,5))
        lab(w,'以下問題與片段將傳送至：'+destination,fg=MUTED).pack(anchor='w',padx=20)
        lab(w,'勾選的檔名、片段及近期對話會傳送給 AI；取消勾選的來源不會傳送。完整路徑不額外傳送，請先檢查內容。',fg=MUTED,font=(FONT,9),wraplength=800,justify='left').pack(anchor='w',padx=20,pady=(2,8))
        choices=tk.Frame(w,bg=WHITE); choices.pack(fill='x',padx=20,pady=(0,4))
        selected=[]
        for source in sources:
            enabled=tk.BooleanVar(value=True); selected.append(enabled)
            tk.Checkbutton(choices,text=f'[{source["id"]}] {Path(source["path"]).name} · {source.get("kind","未確認")}',variable=enabled,bg=WHITE,fg=INK,font=(FONT,9),anchor='w').pack(fill='x')
        buttons=tk.Frame(w,bg=WHITE); buttons.pack(side='bottom',fill='x',padx=20,pady=15)
        def cancel(): w.destroy(); self._set_busy(False); self.status.set('已取消傳送。')
        approved=dict(self.config)
        def submit():
            try: chosen=selected_sources(sources,[enabled.get() for enabled in selected])
            except ValueError as e: messagebox.showinfo('請選擇來源',str(e),parent=w); return
            self.send(w,q,chosen,approved)
        btn(buttons,'取消',cancel).pack(side='right'); btn(buttons,'傳送並產生回答',submit,True).pack(side='right',padx=8)
        preview_area=tk.Frame(w,bg=WHITE); preview_area.pack(fill='both',expand=True,padx=20,pady=8)
        text=tk.Text(preview_area,wrap='word',font=(FONT,10),fg=INK,bg='#f8fafc',bd=0,padx=12,pady=12)
        scroll=ttk.Scrollbar(preview_area,command=text.yview); text.configure(yscrollcommand=scroll.set)
        text.pack(side='left',fill='both',expand=True); scroll.pack(side='right',fill='y')
        if self.history:
            text.insert('end','近期對話（將與問題一同傳送）：\n')
            for turn in svc.bounded_history(self.history): text.insert('end','你：'+turn['question']+'\n助理：'+turn['answer']+'\n\n')
        text.insert('end','問題：'+q+'\n\n')
        for s in sources: text.insert('end',f'[{s["id"]}] {Path(s["path"]).name} · {s["locator"]}\n來源類型：{s.get("kind","未確認")}\n{s["excerpt"]}\n\n')
        text.configure(state='disabled'); w.protocol('WM_DELETE_WINDOW',cancel)

    def stop_local_answer(self):
        if self.answer_started is None or not local_ai.running(): return
        self.stop_answer_button.configure(state='disabled'); self.status.set('正在停止本地模型與回答…')
        threading.Thread(target=local_ai.stop,daemon=True).start()

    def send(self,w,q,sources,approved=None):
        if w is not None: w.destroy()
        self.status.set('正在等待模型回答…'); config=read_config(self.config_path)
        if approved is not None and any(config.get(k)!=approved.get(k) for k in ('provider','local_base','openai_model','local_model','network_enabled','max_tokens')):
            self._set_busy(False); self.status.set('AI 設定已變更，未傳送資料；請重新提問並檢查預覽。'); return
        if config['provider']=='openai' and not config['network_enabled']:
            self._set_busy(False); self.status.set('外網存取已關閉，未傳送問題或片段。'); return
        self.answer_started=time.monotonic()
        self.answer_backend='內建本地 AI（可能重試來源核對，需數分鐘）' if config['provider']=='local' and config['local_model']==local_ai.MODEL else 'OpenAI' if config['provider']=='openai' else '本機模型服務'
        if config['provider']=='local' and config['local_model']==local_ai.MODEL and local_ai.running():
            self.stop_answer_button.configure(state='normal'); self.stop_answer_button.pack(anchor='w',pady=(4,0))
        def work():
            ticket=None
            try:
                if any(svc.source_state(source) for source in sources): raise ValueError('來源在預覽後已變更或無法存取，未傳送；請重新掃描並提問。')
                key=svc.load_key() if config['provider']=='openai' else None
                context=svc.bounded_history(getattr(self,'history',[]))
                if config['provider']=='openai': ticket=budget.reserve(self.db,q,sources,config['openai_model'],config['max_tokens'],extra_input_tokens=len(json.dumps(context,ensure_ascii=False).encode('utf8'))+64)
                result=svc.answer(q,sources,config['provider'],config['openai_model'] if config['provider']=='openai' else config['local_model'],key,config['local_base'],config['max_tokens'],network_enabled=config['network_enabled'],history=context)
                if ticket: budget.finish(self.db,ticket,result[2])
                self.events.put(('answer',result))
            except svc.EvidenceError:
                self.events.put(('unverified',sources))
            except Exception as e:
                if ticket:
                    try: budget.uncertain(self.db,ticket)
                    except Exception: pass
                self.events.put(('error',str(e)))
        threading.Thread(target=work,daemon=True).start()

    def poll(self):
        try:
            while True:
                kind,value=self.events.get_nowait()
                if kind=='web_error': self._append_chat('助理','網路搜尋未完成：'+value+'。本次若有本地資料，仍會嘗試使用。')
                elif kind=='retrieved':
                    q,sources,approved,review=value
                    if not sources:
                        self._set_busy(False)
                        model=svc.model_identifiers(q)
                        if model:
                            self.clarify_question=q; self._append_chat('助理',f'我目前找不到能確認「{model[0]}」的資料。請問它是哪個品牌和完整型號？如果方便，也可以告訴我韌體版本。')
                        else: self._append_chat('助理','找不到明確符合的資料。你能提供品牌、完整型號，或補充想操作的功能嗎？')
                        self.status.set('等待你補充資訊。')
                    elif review: self.preview(q,sources)
                    else: self.send(None,q,sources,approved)
                elif kind=='error': self.answer_started=None; self.stop_answer_button.pack_forget(); self._set_busy(False); self._append_chat('助理','無法完成：'+value); self.status.set('無法完成：'+value); messagebox.showerror('無法完成問答',value,parent=self)
                elif kind=='unverified':
                    self.answer_started=None; self.stop_answer_button.pack_forget(); self._set_busy(False); self.sources_by_row=value
                    self._append_chat('助理','目前無法根據找到的資料可靠地整理答案。可按「顯示／隱藏來源」查看候選文件，或換個問法。')
                    self.cites.delete(*self.cites.get_children()); self._source_text('')
                    for i,source in enumerate(value): self.cites.insert('','end',iid=str(i),values=(f'[{source["id"]}]',Path(source['path']).name,source['locator']))
                    if value: self.cites.selection_set('0'); self.source_select()
                    self.status.set('來源核對未通過；右側顯示搜尋到的候選來源。')
                elif kind=='answer':
                    text,sources,usage=value; self.answer_started=None; self.stop_answer_button.pack_forget(); self._set_busy(False); self.last_details=text; reply=self._compact_reply(text); self._append_chat('助理',reply); self.history.append({'question':self.pending_question,'answer':reply}); self.history=self.history[-10:]; self.sources_by_row=sources
                    self.cites.delete(*self.cites.get_children()); self._source_text('')
                    for i,s in enumerate(sources): self.cites.insert('','end',iid=str(i),values=(f'[{s["id"]}]',s.get('title') or Path(s['path']).name,s['locator']))
                    if sources: self.cites.selection_set('0'); self.source_select()
                    stale=any(svc.source_state(source) for source in sources)
                    self.status.set('來源在回答期間變更或無法存取；請重新掃描並提問。' if stale else '回答已產生。請核對來源類型與片段；AI 回答可能有錯誤。')
                elif kind=='connection_test':
                    ok,description=value
                    if ok: messagebox.showinfo('連線測試',description,parent=self)
                    else: messagebox.showerror('連線測試失敗',description,parent=self)
                elif kind=='local_fallback':
                    if not self.start_cancel.is_set(): self.status.set('顯示卡加速未能啟動，正在嘗試 CPU；可能較慢…')
                elif kind=='local_started':
                    ok,result=value; self.starting_local=False; self._set_busy(False); self.local_button.configure(state='normal',text='啟動內建本地 AI')
                    if ok and self.start_cancel.is_set():
                        local_ai.stop(); self.status.set('已取消啟動本地 AI。'); continue
                    if ok:
                        base,model=result; self.config=read_config(self.config_path); self.config.update(provider='local',local_base=base,local_model=model,auto_start_local=True)
                        try: write_config(self.config_path,self.config)
                        except Exception as e:
                            local_ai.stop(); self.status.set('無法保存設定：'+str(e)); continue
                        self.local_button.configure(text='關閉內建本地 AI'); self._set_hint(); self.refresh_model_info(); self.status.set('內建本地 AI 已啟動，可使用本地文件問答與支援圖片的閱讀器探索。')
                    else:
                        self._set_hint(); self.refresh_model_info()
                        self.status.set(result if self.start_cancel.is_set() else '本地模型啟動失敗：'+result)
                        if not self.start_cancel.is_set(): messagebox.showerror('本地模型啟動失敗',result,parent=self)
                elif kind=='download_phase':
                    self.download_phase=value; self.model_info.set(value+'…')
                elif kind=='download_progress':
                    done,total=value; percent=done/total*100 if total else 0; self.download_progress['value']=percent
                    detail=f'{self.download_phase}：{percent:.1f}%（{done/1024**3:.2f} / {total/1024**3:.2f} GiB）'
                    self.model_info.set(detail); self.status.set(detail)
                elif kind=='download_done':
                    ok,description=value; self.downloading=False; self._set_busy(False); self.local_button.configure(state='normal'); self.download_button.configure(text='下載可選本地模型（約 15.6 GB）'); self.download_progress.pack_forget(); self.status.set(description); self.refresh_model_info()
                    if ok: self.model_info.set('模型完整性驗證完成；按「啟動內建本地 AI」載入。')
                    if not ok and not self.download_cancel.is_set(): messagebox.showerror('模型下載未完成',description,parent=self)
        except queue.Empty: pass
        if self.answer_started is not None and str(self.stop_answer_button.cget('state'))!='disabled':
            elapsed=int(time.monotonic()-self.answer_started); self.status.set(f'正在等待 {self.answer_backend}：已等待 {elapsed//60} 分 {elapsed%60:02d} 秒。')
        running=local_ai.running()
        if running!=self.last_local_running:
            self.last_local_running=running
            if not self.starting_local:
                self.local_button.configure(text='關閉內建本地 AI' if running else '啟動內建本地 AI')
                self.config=read_config(self.config_path); self._set_hint()
                if not running and not self.busy: self.status.set('內建本地 AI 已停止；需要時請重新啟動。')
        if self.winfo_exists(): self.poll_id=self.after(150,self.poll)
    def source_select(self,event=None):
        selected=self.cites.selection()
        if not selected: return
        s=self.sources_by_row[int(selected[0])]
        state=svc.source_state(s)
        self._source_text((state+'。\n' if state else '')+'來源類型：'+s.get('kind','未確認')+'\n'+s['path']+'\n'+s['locator']+'\n\n'+s['excerpt']+'\n\n逐項引用：\n'+getattr(self,'last_details',''))
    def open_source(self):
        selected=self.cites.selection()
        if not selected: return
        source=self.sources_by_row[int(selected[0])]; state=svc.source_state(source)
        if state: messagebox.showwarning('來源目前無法開啟',state,parent=self); return
        if source.get('kind')==web_search.KIND:
            if not web_search.safe_public_url(source.get('url')): messagebox.showerror('網址無效','無法開啟此網路來源。',parent=self); return
            webbrowser.open(source['url']); return
        try: os.startfile(source['path'])
        except OSError as e: messagebox.showerror('無法開啟來源',str(e),parent=self)
    def close(self):
        if self.busy:
            message='請先按「取消模型下載」並等待下載停止。' if self.downloading else '請先按「取消啟動本地 AI」並等待載入停止。' if self.starting_local else '請等待搜尋或模型回應結束。'
            messagebox.showinfo('處理中',message,parent=self); return
        if self.return_to_main:
            self.withdraw(); self.master.deiconify(); self.master.lift(); return
        self.destroy()
    def destroy(self):
        if getattr(self,'poll_id',None):
            try: self.after_cancel(self.poll_id)
            except tk.TclError: pass
            self.poll_id=None
        super().destroy()
        try:
            if self.return_to_main and self.master.winfo_exists(): self.master.deiconify(); self.master.lift()
        except tk.TclError: pass
