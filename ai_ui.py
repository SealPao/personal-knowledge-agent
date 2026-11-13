import json
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
import ai_service as svc
import budget

FONT='Microsoft JhengHei UI'; WHITE='#ffffff'; BG='#f1f5f9'; INK='#1b2c40'; MUTED='#64748b'; BLUE='#2563eb'
DEFAULT={'provider':'openai','openai_model':'gpt-5.4-mini','local_model':'','local_base':'http://127.0.0.1:11434/v1','max_tokens':800,'network_enabled':False}

def read_config(path):
    try:
        with open(path,'r',encoding='utf-8') as f: data=json.load(f)
        if not isinstance(data,dict): return dict(DEFAULT)
        result={**DEFAULT,**{k:v for k,v in data.items() if k in DEFAULT}}
        result['network_enabled']=data.get('network_enabled') is True
        return result
    except (OSError,ValueError,TypeError): return dict(DEFAULT)

def write_config(path,config):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    with open(temp,'w',encoding='utf-8') as f: json.dump({k:config[k] for k in DEFAULT},f,ensure_ascii=False,indent=2)
    os.replace(temp,path)

def lab(p,text='',**kw):
    kw.setdefault('font',(FONT,10)); kw.setdefault('bg',WHITE); kw.setdefault('fg',INK)
    return tk.Label(p,text=text,**kw)

def btn(p,text,command,primary=False):
    return tk.Button(p,text=text,command=command,bd=0,padx=14,pady=7,cursor='hand2',font=(FONT,10,'bold'),bg=BLUE if primary else '#e9f0fb',fg=WHITE if primary else '#1d4ed8')

class AIWindow(tk.Toplevel):
    def __init__(self,master,db_path):
        super().__init__(master); self.title('AI 問答 · 私人知識助理'); self.geometry('1080x760'); self.minsize(850,620); self.configure(bg=BG)
        self.db=Path(db_path); self.config_path=self.db.parent/'ai-settings.json'; self.config=read_config(self.config_path)
        self.events=queue.Queue(); self.busy=False; self.sources=[]; self.sources_by_row=[]
        self.question=tk.StringVar(); self.hint=tk.StringVar(); self._set_hint()
        head=tk.Frame(self,bg=BG); head.pack(fill='x',padx=25,pady=(20,12))
        lab(head,'與你的文件對話',bg=BG,font=(FONT,19,'bold')).pack(side='left')
        btn(head,'AI 設定',self.settings).pack(side='right')
        btn(head,'預算與用量',self.open_budget).pack(side='right',padx=8)
        btn(head,'用閱讀器找答案',self.open_device).pack(side='right')
        lab(self,textvariable=self.hint,bg=BG,fg=MUTED,anchor='w').pack(fill='x',padx=25)
        card=tk.Frame(self,bg=WHITE,highlightbackground='#dbe4ef',highlightthickness=1); card.pack(fill='x',padx=25,pady=12)
        lab(card,'你想知道什麼？',font=(FONT,11,'bold')).pack(anchor='w',padx=15,pady=(12,4))
        row=tk.Frame(card,bg=WHITE); row.pack(fill='x',padx=15,pady=(0,15))
        self.entry=tk.Entry(row,textvariable=self.question,font=(FONT,12),bd=0,highlightthickness=1,highlightbackground='#cbd5e1',highlightcolor=BLUE)
        self.entry.pack(side='left',fill='x',expand=True,ipady=8); self.entry.bind('<Return>',lambda e:self.prepare())
        self.ask_button=btn(row,'尋找資料並回答',self.prepare,True); self.ask_button.pack(side='left',padx=(10,0))
        bottom=tk.Frame(self,bg=BG); bottom.pack(side='bottom',fill='x',padx=25,pady=(0,15))
        self.status=tk.StringVar(value='回答會依本地搜尋結果產生，並標明來源。')
        lab(bottom,textvariable=self.status,bg=BG,fg=MUTED,anchor='w').pack(fill='x')
        body=tk.PanedWindow(self,orient='horizontal',bg=BG,bd=0,sashwidth=8); body.pack(fill='both',expand=True,padx=25,pady=(0,12))
        a=tk.Frame(body,bg=WHITE,highlightbackground='#dbe4ef',highlightthickness=1); s=tk.Frame(body,bg=WHITE,highlightbackground='#dbe4ef',highlightthickness=1)
        body.add(a,minsize=400,stretch='always'); body.add(s,minsize=280,stretch='always')
        lab(a,'AI 回答',font=(FONT,12,'bold')).pack(anchor='w',padx=16,pady=(13,8))
        f=tk.Frame(a,bg=WHITE); f.pack(fill='both',expand=True,padx=16,pady=(0,16))
        self.answer_box=tk.Text(f,wrap='word',font=(FONT,11),fg=INK,bg=WHITE,bd=0,spacing3=7)
        sb=ttk.Scrollbar(f,command=self.answer_box.yview); self.answer_box.configure(yscrollcommand=sb.set); self.answer_box.pack(side='left',fill='both',expand=True); sb.pack(side='right',fill='y')
        self._answer_text('輸入問題後，會先顯示預計使用的資料片段；確認後才傳送給選定的 AI。')
        lab(s,'回答所引用的來源',font=(FONT,12,'bold')).pack(anchor='w',padx=15,pady=(13,6))
        self.cites=ttk.Treeview(s,columns=('number','file','where'),show='headings',height=8,selectmode='browse')
        for key,title,width in [('number','編號',48),('file','文件',180),('where','位置',130)]: self.cites.heading(key,text=title); self.cites.column(key,width=width,minwidth=40)
        self.cites.pack(fill='both',expand=True,padx=12,pady=(0,5)); self.cites.bind('<<TreeviewSelect>>',self.source_select)
        self.source_info=tk.Text(s,height=8,wrap='word',font=(FONT,9),fg=INK,bg='#f8fafc',bd=0,padx=10,pady=8)
        self.source_info.pack(fill='x',padx=12,pady=(5,8)); self.source_info.configure(state='disabled')
        btn(s,'開啟所選原始文件',self.open_source).pack(anchor='w',padx=12,pady=(0,14))
        self.protocol('WM_DELETE_WINDOW',self.close); self.after(150,self.poll)

    def _set_hint(self):
        if self.config['provider']=='local': self.hint.set('本地 AI 模式：使用你自己啟動的本機模型服務，不使用 OpenAI。')
        elif not self.config['network_enabled']: self.hint.set('外網存取已關閉。OpenAI 無法連線；本地模型仍可使用。')
        else: self.hint.set('OpenAI 模式：確認後會將問題與選中的文件片段傳送至 OpenAI，可能產生 API 費用。')
    def open_budget(self):
        import budget_ui
        budget_ui.BudgetWindow(self,self.db,self.config['openai_model'])
    def open_device(self):
        import adb_ui
        adb_ui.DeviceWindow(self.master,self.db,self.question.get().strip())
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
        provider=tk.StringVar(value=self.config['provider']); om=tk.StringVar(value=self.config['openai_model']); lm=tk.StringVar(value=self.config['local_model']); base=tk.StringVar(value=self.config['local_base']); maximum=tk.StringVar(value=str(self.config['max_tokens'])); key=tk.StringVar(); network=tk.BooleanVar(value=bool(self.config['network_enabled']))
        tk.Checkbutton(f,text='允許 AI 連接外網（關閉時仍可使用本機模型）',variable=network,bg=WHITE,fg=INK,font=(FONT,10,'bold'),anchor='w').pack(fill='x',pady=(0,4))
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
                conf={'provider':provider.get(),'openai_model':om.get().strip(),'local_model':lm.get().strip(),'local_base':base.get().strip(),'max_tokens':limit,'network_enabled':network.get()}
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
        if self.config['provider']=='local' and not self.config['local_model']:
            messagebox.showinfo('請設定本地模型','請先在 AI 設定填入本地模型名稱。',parent=self); return
        if self.config['provider']=='openai':
            if not self.config['network_enabled']:
                messagebox.showinfo('外網存取已關閉','若要使用 OpenAI，請先在 AI 設定開啟外網存取。',parent=self); return
            try: exists=bool(svc.load_key())
            except Exception as e: messagebox.showerror('無法讀取 Key',str(e),parent=self); return
            if not exists: messagebox.showinfo('請設定 API Key','請先在 AI 設定儲存 OpenAI API Key。',parent=self); return
        self._answer_text('正在準備問題，完成後會顯示可供確認的來源片段。'); self.cites.delete(*self.cites.get_children()); self.sources_by_row=[]; self._source_text('')
        self._set_busy(True); self.status.set('正在本地搜尋相關文件…')
        def work():
            try:
                sources=svc.retrieve(str(self.db),q)
                self.events.put(('retrieved',(q,sources)))
            except Exception as e: self.events.put(('error',str(e)))
        threading.Thread(target=work,daemon=True).start()

    def preview(self,q,sources):
        if not sources:
            self._set_busy(False); self.status.set('沒有找到可用來源，請換一個問題或確認文件已匯入。'); return
        w=tk.Toplevel(self); w.title('確認使用的資料'); w.geometry('850x620'); w.configure(bg=WHITE); w.transient(self); w.grab_set()
        destination='OpenAI（可能產生費用）' if self.config['provider']=='openai' else '此電腦上的本地模型服務'
        lab(w,'確認使用的資料',font=(FONT,15,'bold')).pack(anchor='w',padx=20,pady=(18,5))
        lab(w,'以下問題與片段將傳送至：'+destination,fg=MUTED).pack(anchor='w',padx=20)
        lab(w,'不會額外傳送來源路徑；片段本身可能包含路徑或其他敏感資訊，請先檢查。',fg=MUTED,font=(FONT,9)).pack(anchor='w',padx=20,pady=(2,8))
        buttons=tk.Frame(w,bg=WHITE); buttons.pack(side='bottom',fill='x',padx=20,pady=15)
        def cancel(): w.destroy(); self._set_busy(False); self.status.set('已取消傳送。')
        approved=dict(self.config)
        btn(buttons,'取消',cancel).pack(side='right'); btn(buttons,'傳送並產生回答',lambda:self.send(w,q,sources,approved),True).pack(side='right',padx=8)
        text=tk.Text(w,wrap='word',font=(FONT,10),fg=INK,bg='#f8fafc',bd=0,padx=12,pady=12); text.pack(fill='both',expand=True,padx=20,pady=8)
        text.insert('end','問題：'+q+'\n\n')
        for s in sources: text.insert('end',f'[{s["id"]}] {Path(s["path"]).name} · {s["locator"]}\n{s["excerpt"]}\n\n')
        text.configure(state='disabled'); w.protocol('WM_DELETE_WINDOW',cancel)

    def send(self,w,q,sources,approved=None):
        w.destroy(); self.status.set('正在等待模型回答…'); config=read_config(self.config_path)
        if approved is not None and any(config.get(k)!=approved.get(k) for k in ('provider','local_base','openai_model','local_model','network_enabled')):
            self._set_busy(False); self.status.set('AI 設定已變更，未傳送資料；請重新提問並檢查預覽。'); return
        if config['provider']=='openai' and not config['network_enabled']:
            self._set_busy(False); self.status.set('外網存取已關閉，未傳送問題或片段。'); return
        def work():
            ticket=None
            try:
                key=svc.load_key() if config['provider']=='openai' else None
                if config['provider']=='openai': ticket=budget.reserve(self.db,q,sources,config['openai_model'],config['max_tokens'])
                result=svc.answer(q,sources,config['provider'],config['openai_model'] if config['provider']=='openai' else config['local_model'],key,config['local_base'],config['max_tokens'],network_enabled=config['network_enabled'])
                if ticket: budget.finish(self.db,ticket,result[2])
                self.events.put(('answer',result))
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
                if kind=='retrieved': self.preview(*value)
                elif kind=='error': self._set_busy(False); self.status.set('無法完成：'+value); messagebox.showerror('無法完成問答',value,parent=self)
                elif kind=='answer':
                    text,sources,usage=value; self._set_busy(False); self._answer_text(text); self.sources_by_row=sources
                    self.cites.delete(*self.cites.get_children()); self._source_text('')
                    for i,s in enumerate(sources): self.cites.insert('','end',iid=str(i),values=(f'[{s["id"]}]',Path(s['path']).name,s['locator']))
                    if sources: self.cites.selection_set('0'); self.source_select()
                    self.status.set('回答已產生。請核對來源；AI 回答可能有錯誤。')
                elif kind=='connection_test':
                    ok,description=value
                    if ok: messagebox.showinfo('連線測試',description,parent=self)
                    else: messagebox.showerror('連線測試失敗',description,parent=self)
        except queue.Empty: pass
        if self.winfo_exists(): self.after(150,self.poll)
    def source_select(self,event=None):
        selected=self.cites.selection()
        if not selected: return
        s=self.sources_by_row[int(selected[0])]
        try: stale=os.stat(s['path']).st_mtime_ns!=s['mtime_ns']
        except OSError: stale=True
        self._source_text(('來源已變更或無法存取。\n' if stale else '')+s['path']+'\n'+s['locator']+'\n\n'+s['excerpt'])
    def open_source(self):
        selected=self.cites.selection()
        if not selected: return
        try: os.startfile(self.sources_by_row[int(selected[0])]['path'])
        except OSError as e: messagebox.showerror('無法開啟來源',str(e),parent=self)
    def close(self):
        if self.busy: messagebox.showinfo('處理中','請等待搜尋或模型回應結束。',parent=self); return
        self.destroy()
