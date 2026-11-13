import os
import json
import queue
import sqlite3
import threading
import tempfile
import uuid
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from contextlib import closing
from pathlib import Path
import core
import workspace

BASE = workspace.application_base()
DB = workspace.active_database(BASE)
NAVY = '#17263a'
INK = '#1b2c40'
MUTED = '#64748b'
BG = '#f1f5f9'
WHITE = '#ffffff'
BLUE = '#2563eb'
BORDER = '#dbe4ef'
FONT = 'Microsoft JhengHei UI'

def label(parent, text='', **kw):
    kw.setdefault('font',(FONT,10))
    return tk.Label(parent, text=text, **kw)

def button(parent, text, command, bg=BLUE, fg=WHITE, **kw):
    return tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                     activebackground='#1d4ed8' if bg == BLUE else '#e2e8f0',
                     activeforeground=fg, bd=0, cursor='hand2',
                     font=(FONT, 10, 'bold'), padx=15, pady=8, **kw)

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('私人知識助理'); self.geometry('1250x800'); self.minsize(960, 650)
        self.configure(bg=BG)
        self.events=queue.Queue(); self.stop=threading.Event(); self.running=False
        self.rows=[]; self.selected_row=None; self.search_id=0
        self.rootpath=tk.StringVar(); self.status=tk.StringVar(value='正在讀取本地知識庫')
        self.network_state=tk.StringVar(value='外網：關閉')
        self.auto_enabled=tk.BooleanVar(value=False)
        try:
            self.auto_enabled.set(json.loads((DB.parent/'preferences.json').read_text(encoding='utf-8')).get('auto_import') is True)
        except (OSError,ValueError,AttributeError): pass
        self.query=tk.StringVar(); self.format_filter=tk.StringVar(value='全部格式'); self.brand_filter=tk.StringVar(value='全部品牌')
        self.metrics=tk.StringVar(value='尚未建立索引'); self.result_title=tk.StringVar(value='搜尋結果')
        self.source_name=tk.StringVar(value='選取一筆結果'); self.source_locator=tk.StringVar(value='')
        self.source_warning=tk.StringVar(value='搜尋後選取結果，就能在這裡閱讀內容與查看出處。')
        if DB.exists():
            try:
                with closing(core.connect(DB)) as db:
                    row=db.execute("SELECT value FROM settings WHERE key='root'").fetchone()
                    if row: self.rootpath.set(row[0])
            except sqlite3.Error: pass
        self._build()
        self.refresh_network()
        self.refresh_metrics()
        self.after(120,self.poll); self.after(1800000,self.auto_tick); self.protocol('WM_DELETE_WINDOW',self.close)

    def _build(self):
        sidebar=tk.Frame(self,bg=NAVY,width=220); sidebar.pack(side='left',fill='y'); sidebar.pack_propagate(False)
        label(sidebar,'◈',bg=NAVY,fg='#93c5fd',font=(FONT,30,'bold')).pack(anchor='w',padx=24,pady=(26,0))
        label(sidebar,'私人知識助理',bg=NAVY,fg=WHITE,font=(FONT,16,'bold')).pack(anchor='w',padx=24,pady=(0,5))
        label(sidebar,'你的文件，留在你的電腦',bg=NAVY,fg='#b5c4d5',font=(FONT,9)).pack(anchor='w',padx=24)
        tk.Frame(sidebar,bg='#33465b',height=1).pack(fill='x',padx=22,pady=24)
        label(sidebar,'●  文件搜尋',bg=NAVY,fg=WHITE,font=(FONT,11,'bold')).pack(anchor='w',padx=25)
        button(sidebar,'✦  AI 問答',self.open_ai,bg='#30445b',fg=WHITE).pack(fill='x',padx=20,pady=(15,0))
        button(sidebar,'▤  文件管理與編輯',self.open_documents,bg='#30445b',fg=WHITE).pack(fill='x',padx=20,pady=(6,0))
        button(sidebar,'選擇或匯入知識庫',self.library_options,bg='#30445b',fg=WHITE).pack(fill='x',padx=20,pady=(6,0))
        label(sidebar,'知識庫',bg=NAVY,fg='#9cb0c5',font=(FONT,9,'bold')).pack(anchor='w',padx=24,pady=(30,6))
        label(sidebar,textvariable=self.metrics,bg=NAVY,fg='#dbeafe',justify='left',wraplength=170).pack(anchor='w',padx=24)
        button(sidebar,'查看匯入狀態',self.show_status,bg='#30445b',fg=WHITE).pack(fill='x',padx=20,pady=(18,4))
        button(sidebar,'重新掃描來源',self.start,bg='#30445b',fg=WHITE).pack(fill='x',padx=20,pady=4)
        button(sidebar,'取消目前作業',self.cancel_work,bg='#30445b',fg=WHITE).pack(fill='x',padx=20,pady=4)
        tk.Checkbutton(sidebar,text='自動增量匯入',variable=self.auto_enabled,command=self.set_auto,bg=NAVY,fg='#dbeafe',selectcolor='#30445b',activebackground=NAVY,activeforeground=WHITE,font=(FONT,9),anchor='w').pack(fill='x',padx=20,pady=(10,0))
        label(sidebar,'開啟後每 30 分鐘檢查一次',bg=NAVY,fg='#a8bbce',font=(FONT,8)).pack(anchor='w',padx=25)
        label(sidebar,textvariable=self.network_state,bg=NAVY,fg='#a8bbce',font=(FONT,9)).pack(side='bottom',anchor='w',padx=24,pady=22)

        main=tk.Frame(self,bg=BG); main.pack(side='left',fill='both',expand=True)
        header=tk.Frame(main,bg=BG); header.pack(fill='x',padx=28,pady=(25,12))
        label(header,'搜尋你的知識庫',bg=BG,fg=INK,font=(FONT,21,'bold')).pack(anchor='w')
        label(header,'輸入產品、機型或操作關鍵字，找到原始文件中的相關內容。',bg=BG,fg=MUTED).pack(anchor='w',pady=(5,0))
        searchcard=tk.Frame(main,bg=WHITE,highlightbackground=BORDER,highlightthickness=1); searchcard.pack(fill='x',padx=28,pady=(0,14))
        searchrow=tk.Frame(searchcard,bg=WHITE); searchrow.pack(fill='x',padx=17,pady=(15,7))
        self.entry=tk.Entry(searchrow,textvariable=self.query,font=(FONT,13),fg=INK,bd=0,highlightthickness=1,highlightbackground=BORDER,highlightcolor=BLUE,insertbackground=INK)
        self.entry.pack(side='left',fill='x',expand=True,ipady=9); self.entry.bind('<Return>',lambda e:self.do_search())
        button(searchrow,'搜尋文件',self.do_search).pack(side='left',padx=(10,0))
        tips=tk.Frame(searchcard,bg=WHITE); tips.pack(fill='x',padx=17,pady=(0,13))
        label(tips,'試試看',bg=WHITE,fg=MUTED,font=(FONT,9)).pack(side='left',padx=(0,5))
        for phrase in ('BOOX','充電','Wi-Fi'):
            tk.Button(tips,text=phrase,command=lambda x=phrase:self.example(x),bg='#eef4ff',fg='#315b9a',bd=0,cursor='hand2',font=(FONT,9),padx=8,pady=2).pack(side='left',padx=3)
        label(tips,'此處使用關鍵字搜尋；AI 問答請選左側按鈕。',bg=WHITE,fg=MUTED,font=(FONT,9)).pack(side='right')

        footer=tk.Frame(main,bg=BG); footer.pack(side='bottom',fill='x',padx=28,pady=13)
        label(footer,textvariable=self.status,bg=BG,fg=MUTED,font=(FONT,9),anchor='w').pack(side='left',fill='x',expand=True)
        body=tk.PanedWindow(main,orient='horizontal',bg=BG,bd=0,sashwidth=8,showhandle=False); body.pack(fill='both',expand=True,padx=28)
        left=tk.Frame(body,bg=WHITE,highlightbackground=BORDER,highlightthickness=1); right=tk.Frame(body,bg=WHITE,highlightbackground=BORDER,highlightthickness=1)
        body.add(left,minsize=370,stretch='always'); body.add(right,minsize=340,stretch='always')
        lefthead=tk.Frame(left,bg=WHITE); lefthead.pack(fill='x',padx=16,pady=(14,8))
        label(lefthead,textvariable=self.result_title,bg=WHITE,fg=INK,font=(FONT,12,'bold')).pack(side='left')
        choices=ttk.Combobox(lefthead,textvariable=self.format_filter,values=('全部格式','PDF','HTML','Markdown','CSV','TXT','Word','Excel'),state='readonly',width=12)
        choices.pack(side='right'); choices.bind('<<ComboboxSelected>>',lambda e:self.do_search())
        brands=ttk.Combobox(lefthead,textvariable=self.brand_filter,values=('全部品牌','BOOX','Kobo','HyRead','Pubook','mooInk','Bigme','Supernote','Targus'),state='readonly',width=11)
        brands.pack(side='right',padx=5); brands.bind('<<ComboboxSelected>>',lambda e:self.do_search())
        style=ttk.Style(self); style.theme_use('clam')
        style.configure('Results.Treeview',font=(FONT,10),rowheight=44,background=WHITE,fieldbackground=WHITE,foreground=INK,borderwidth=0)
        style.configure('Results.Treeview.Heading',font=(FONT,9,'bold'),background='#f8fafc',foreground=MUTED,relief='flat')
        style.map('Results.Treeview',background=[('selected','#e6efff')],foreground=[('selected',INK)])
        table=tk.Frame(left,bg=WHITE); table.pack(fill='both',expand=True,padx=8,pady=(0,8))
        self.list=ttk.Treeview(table,columns=('file','locator'),show='headings',style='Results.Treeview',selectmode='browse')
        self.list.heading('file',text='文件'); self.list.heading('locator',text='位置')
        self.list.column('file',width=235,minwidth=130); self.list.column('locator',width=170,minwidth=100)
        scroll=ttk.Scrollbar(table,orient='vertical',command=self.list.yview); self.list.configure(yscrollcommand=scroll.set)
        self.list.pack(side='left',fill='both',expand=True); scroll.pack(side='right',fill='y'); self.list.bind('<<TreeviewSelect>>',self.selected)

        rhead=tk.Frame(right,bg=WHITE); rhead.pack(fill='x',padx=20,pady=(16,5))
        label(rhead,'內容與來源',bg=WHITE,fg=INK,font=(FONT,12,'bold')).pack(anchor='w')
        label(rhead,textvariable=self.source_name,bg=WHITE,fg=INK,font=(FONT,11,'bold'),wraplength=480,justify='left').pack(anchor='w',pady=(16,3))
        label(rhead,textvariable=self.source_locator,bg=WHITE,fg=BLUE,font=(FONT,9),wraplength=480,justify='left').pack(anchor='w')
        label(rhead,textvariable=self.source_warning,bg=WHITE,fg=MUTED,font=(FONT,9),wraplength=480,justify='left').pack(anchor='w',pady=(8,8))
        tk.Frame(right,bg=BORDER,height=1).pack(fill='x',padx=20)
        self.openbtn=button(right,'開啟原始文件',self.open_source,bg='#edf4ff',fg='#1d4ed8'); self.openbtn.pack(side='bottom',anchor='w',padx=20,pady=(0,17)); self.openbtn.configure(state='disabled')
        reading=tk.Frame(right,bg=WHITE); reading.pack(fill='both',expand=True,padx=20,pady=10)
        self.details=tk.Text(reading,wrap='word',font=(FONT,11),fg=INK,bg=WHITE,bd=0,relief='flat',spacing1=4,spacing3=6,padx=2,pady=2)
        vbar=ttk.Scrollbar(reading,orient='vertical',command=self.details.yview); self.details.configure(yscrollcommand=vbar.set)
        self.details.pack(side='left',fill='both',expand=True); vbar.pack(side='right',fill='y'); self.details.configure(state='disabled')

    def refresh_metrics(self):
        if not DB.exists(): self.metrics.set('尚未建立索引'); self.status.set('選擇來源資料夾後建立索引。'); return
        try:
            with closing(core.connect(DB)) as db:
                total=db.execute('SELECT count(*) FROM documents').fetchone()[0]
                searchable=db.execute("""SELECT count(*) FROM documents d WHERE d.status='indexed' AND
                  (EXISTS(SELECT 1 FROM chunks c WHERE c.document_id=d.id) OR EXISTS(SELECT 1 FROM pdf_derived p WHERE p.document_id=d.id AND p.source_sha256=d.sha256))""").fetchone()[0]
                attention=db.execute("""SELECT count(*) FROM documents d WHERE status<>'indexed' OR detail LIKE '%擷取失敗%' OR
                  (NOT EXISTS(SELECT 1 FROM chunks c WHERE c.document_id=d.id) AND NOT EXISTS(SELECT 1 FROM pdf_derived p WHERE p.document_id=d.id AND p.source_sha256=d.sha256))""").fetchone()[0]
            self.metrics.set(f'{total} 個來源檔案\n{searchable} 個可搜尋\n{attention} 個待處理')
            self.status.set('本地索引已就緒。來源資料夾保持唯讀。' if self.rootpath.get() and os.path.isdir(self.rootpath.get()) else '來源資料夾目前無法存取；仍可搜尋先前的索引。')
        except Exception as e: self.status.set('無法讀取索引：'+str(e))

    def example(self, phrase): self.query.set(phrase); self.do_search()
    def cancel_work(self):
        if self.running: self.stop.set(); self.status.set('正在取消，請稍候…')
        else: self.status.set('目前沒有進行中的掃描或匯入。')
    def open_ai(self):
        if not DB.exists(): messagebox.showinfo('尚未建立索引','請先掃描來源資料夾。'); return
        import ai_ui
        ai_ui.AIWindow(self,DB)
    def open_documents(self):
        if not DB.exists(): messagebox.showinfo('尚未建立索引','請先掃描來源資料夾。'); return
        import document_ui
        document_ui.DocumentWindow(self,DB)
    def library_options(self):
        if self.running: return
        w=tk.Toplevel(self); w.title('知識庫管理'); w.geometry('880x560'); w.minsize(720,480); w.configure(bg=WHITE); w.transient(self)
        label(w,'知識庫管理',bg=WHITE,fg=INK,font=(FONT,16,'bold')).pack(anchor='w',padx=24,pady=(18,5))
        label(w,'匯入 .pka 會建立可隨 APP 搬移的副本；指定資料夾會唯讀參照原位置。',bg=WHITE,fg=MUTED,wraplength=800,justify='left').pack(anchor='w',padx=24)
        actions=tk.Frame(w,bg=WHITE); actions.pack(fill='x',padx=24,pady=12)
        button(actions,'匯入 .pka',lambda:(w.destroy(),self.import_package())).pack(side='left')
        button(actions,'指定文件資料夾',lambda:(w.destroy(),self.choose_source()),bg='#edf4ff',fg='#1d4ed8').pack(side='left',padx=8)
        button(actions,'將目前知識庫變成可攜副本',lambda:(w.destroy(),self.make_portable_copy()),bg='#edf4ff',fg='#1d4ed8').pack(side='left')
        label(w,'已建立的知識庫',bg=WHITE,fg=INK,font=(FONT,11,'bold')).pack(anchor='w',padx=24)
        entries=workspace.known_libraries(BASE)
        table=ttk.Treeview(w,columns=('type','location','count','state'),show='headings',height=7,selectmode='browse')
        for key,title,width in [('type','類型',110),('location','文件位置',440),('count','文件數',70),('state','狀態',100)]:
            table.heading(key,text=title); table.column(key,width=width,minwidth=65,stretch=(key=='location'))
        table.pack(fill='both',expand=True,padx=24,pady=(8,4))
        for i,item in enumerate(entries):
            active=os.path.normcase(str(item['database']))==os.path.normcase(str(Path(DB).resolve()))
            state=('目前使用' if active else '可切換') if item['available'] else '來源無法存取'
            table.insert('','end',iid=str(i),values=(item['kind'],item['root'],item['count'],state))
        label(w,'切換後重新啟動 APP。來源未連接時，請先確認磁碟或 OneDrive 狀態。',bg=WHITE,fg=MUTED).pack(anchor='w',padx=24,pady=5)
        def switch():
            selected=table.selection()
            if not selected: messagebox.showinfo('選擇知識庫','請先選擇清單中的一項。',parent=w); return
            item=entries[int(selected[0])]
            if not item['available']: messagebox.showerror('來源無法存取','請先連接來源資料夾，再切換。',parent=w); return
            if self._activate_library(item['database']): w.destroy()
        button(w,'使用所選知識庫',switch).pack(anchor='w',padx=24,pady=(3,16))
    def _activate_library(self,target):
        try:
            workspace.activate(BASE,target)
            self.status.set('已設定切換；重新啟動後生效。')
            messagebox.showinfo('已設定切換','請關閉並重新開啟 APP，切換後可使用選擇的知識庫。',parent=self)
            return True
        except Exception as e:
            messagebox.showerror('無法切換知識庫',str(e),parent=self); return False
    def make_portable_copy(self):
        if self.running: return
        if not DB.is_file(): messagebox.showinfo('尚無知識庫','請先指定文件資料夾或匯入 .pka。',parent=self); return
        parent=workspace.library_parent(BASE); scratch=Path(BASE)/'data'
        try:
            roots=workspace.protected_roots(BASE,DB)
            if any(core.contained(parent,r) or core.contained(scratch,r) for r in roots): raise ValueError('APP 資料不能寫入原始知識庫')
            parent.mkdir(parents=True,exist_ok=True)
        except Exception as e:
            messagebox.showerror('無法建立可攜副本',str(e),parent=self); return
        self.running=True; self.stop.clear(); self.status.set('正在驗證並複製文件；需要足夠磁碟空間…')
        def worker():
            try:
                import package_service
                progress=lambda text:self.events.put(('package_progress',text))
                with tempfile.TemporaryDirectory(prefix='pka-portable-',dir=scratch) as temp:
                    archive=Path(temp)/'copy.pka'
                    package_service.create_package(DB,archive,progress,self.stop.is_set)
                    result=package_service.restore_package(archive,parent,None,progress,self.stop.is_set)
                self.events.put(('package_done',result))
            except Exception as e: self.events.put(('package_error',str(e)))
        threading.Thread(target=worker,daemon=True).start()
    def choose_source(self):
        if self.running: return
        source=filedialog.askdirectory(parent=self,title='選擇唯讀使用的文件資料夾')
        if not source: return
        source=str(Path(source).resolve())
        if self.rootpath.get() and os.path.normcase(os.path.realpath(self.rootpath.get()))==os.path.normcase(source):
            self.start(); return
        for item in workspace.known_libraries(BASE):
            if item['kind']!='可攜副本' and os.path.normcase(os.path.realpath(item['root']))==os.path.normcase(source):
                self._activate_library(item['database']); return
        if core.contained(BASE,source):
            messagebox.showerror('無法選擇','APP 資料夾不能放在來源資料夾內。',parent=self); return
        target=Path(BASE)/'data'/'external-libraries'/uuid.uuid4().hex/'knowledge.sqlite3'
        self.running=True; self.stop.clear(); self.status.set('正在唯讀掃描所選資料夾…')
        def worker():
            try:
                target.parent.mkdir(parents=True,exist_ok=True)
                counts,complete=core.scan(source,str(target),lambda text:self.events.put(('progress',text)),self.stop.is_set)
                if not complete: raise ValueError('掃描未完成，未切換知識庫。')
                self.events.put(('source_done',(target,counts)))
            except Exception as e: self.events.put(('source_error',str(e)))
        threading.Thread(target=worker,daemon=True).start()
    def import_package(self):
        if self.running: return
        archive=filedialog.askopenfilename(parent=self,title='選擇要匯入的 PKA 知識庫',filetypes=[('PKA 知識庫','*.pka')])
        if not archive: return
        parent=workspace.library_parent(BASE)
        try:
            roots=workspace.protected_roots(BASE,DB if DB.exists() else None)
            if any(core.contained(parent,r) for r in roots): raise ValueError('還原不能寫入原始知識庫')
            parent.mkdir(parents=True,exist_ok=True)
        except Exception as e:
            messagebox.showerror('無法匯入',str(e),parent=self); return
        self.running=True; self.stop.clear(); self.status.set('正在驗證與匯入知識庫…')
        def worker():
            try:
                import package_service
                result=package_service.restore_package(archive,parent,None,lambda text:self.events.put(('package_progress',text)),self.stop.is_set)
                self.events.put(('package_done',result))
            except Exception as e: self.events.put(('package_error',str(e)))
        threading.Thread(target=worker,daemon=True).start()
    def refresh_network(self):
        import ai_ui
        enabled=ai_ui.read_config(DB.parent/'ai-settings.json')['network_enabled']
        self.network_state.set('外網：已開啟' if enabled else '外網：關閉 · 本地 AI 可用')
    def do_search(self):
        q=self.query.get().strip()
        if not q: self.status.set('請輸入要搜尋的產品、機型或關鍵字。'); self.entry.focus_set(); return
        if not DB.exists(): messagebox.showinfo('尚未建立索引','請先選擇來源資料夾並掃描。'); return
        self.search_id+=1; search_id=self.search_id; selected_filter=self.format_filter.get(); brand=self.brand_filter.get(); self.status.set('正在搜尋本地文件…')
        def worker():
            try:
                data=core.search(str(DB),q,limit=500,extension_filter=selected_filter,brand_filter=brand)
                self.events.put(('search',(search_id,q,data)))
            except Exception as e: self.events.put(('search_error',(search_id,str(e))))
        threading.Thread(target=worker,daemon=True).start()

    def _show_results(self,q,rows):
        self.rows=rows; self.selected_row=None; self.list.delete(*self.list.get_children())
        self.source_name.set('選取一筆結果'); self.source_locator.set(''); self.source_warning.set('搜尋後選取結果，就能在這裡閱讀內容與查看出處。')
        self._set_details(''); self.openbtn.configure(state='disabled')
        for i,(path,status,sha,locator,text,mtime) in enumerate(rows):
            name=Path(path).name
            self.list.insert('', 'end',iid=str(i),values=(name,locator))
        self.result_title.set(f'搜尋結果  ·  {len(rows)} 筆')
        if rows:
            self.status.set('顯示最多 500 筆；請選擇左側結果查看內容與出處。')
            self.list.selection_set('0'); self.list.focus('0'); self.selected()
        else:
            self.status.set('沒有找到符合的連續文字。試試較短的關鍵字，或檢查文件是否已成功匯入。')
            self.source_warning.set('沒有找到結果。可以試試「BOOX」、「充電」或較短的產品名稱。')

    def _set_details(self, text):
        self.details.configure(state='normal'); self.details.delete('1.0','end'); self.details.insert('1.0',text); self.details.configure(state='disabled')
    def selected(self,event=None):
        selection=self.list.selection()
        if not selection: return
        self.selected_row=self.rows[int(selection[0])]
        path,status,sha,locator,text,mtime=self.selected_row
        self.source_name.set(Path(path).name); self.source_locator.set(locator)
        try:
            now=os.stat(path).st_mtime_ns
            warning='來源已變更，顯示的內容可能是舊版。' if now != mtime else '來源：'+path
        except OSError: warning='原始文件目前無法存取；以下為先前索引的內容。'
        if status != 'indexed': warning='來源狀態：'+status+'。'+warning
        self.source_warning.set(warning); self._set_details(text); self.openbtn.configure(state='normal')
    def open_source(self):
        if not self.selected_row: return
        try: os.startfile(self.selected_row[0])
        except OSError as e: messagebox.showerror('無法開啟原始文件',str(e))

    def set_auto(self):
        try:
            if self.rootpath.get() and core.contained(DB,self.rootpath.get()):
                raise OSError('APP 資料夾不可位於來源資料夾內')
            DB.parent.mkdir(parents=True,exist_ok=True)
            path=DB.parent/'preferences.json'; temp=path.with_suffix('.tmp')
            temp.write_text(json.dumps({'auto_import':self.auto_enabled.get()}),encoding='utf-8'); os.replace(temp,path)
            self.status.set('已開啟自動增量匯入（每 30 分鐘）。' if self.auto_enabled.get() else '已關閉自動增量匯入。')
        except OSError as e:
            self.auto_enabled.set(False); messagebox.showerror('無法儲存設定',str(e))
    def auto_tick(self):
        if self.auto_enabled.get() and not self.running and self.rootpath.get() and os.path.isdir(self.rootpath.get()): self.start(auto=True)
        self.after(1800000,self.auto_tick)
    def start(self,auto=False):
        if self.running: return
        source=self.rootpath.get()
        if source and not os.path.isdir(source):
            if not auto: messagebox.showwarning('來源無法存取','請先重新連接來源資料夾；若要使用其他位置，請選「選擇或匯入知識庫」。',parent=self)
            return
        if not source or not os.path.isdir(source):
            source=filedialog.askdirectory(title='選擇知識庫來源資料夾')
            if not source: return
            self.rootpath.set(source)
        if core.contained(DB,source): messagebox.showerror('無法開始','索引不能放在來源資料夾內。'); return
        DB.parent.mkdir(parents=True,exist_ok=True); self.stop.clear(); self.running=True
        def worker():
            try: self.events.put(('done',core.scan(source,str(DB),lambda x:self.events.put(('progress',x)),self.stop.is_set,fast=auto)))
            except Exception as e: self.events.put(('error',str(e)))
        threading.Thread(target=worker,daemon=True).start()
        self.status.set('正在重新掃描，原始文件維持唯讀…')

    def poll(self):
        try:
            for _ in range(150):
                kind,value=self.events.get_nowait()
                if kind=='progress': self.status.set('正在唯讀掃描：'+value)
                elif kind=='package_progress': self.status.set(value)
                elif kind=='package_error':
                    self.running=False; self.status.set('匯入未完成。'); messagebox.showerror('匯入失敗',value,parent=self)
                elif kind=='source_error':
                    self.running=False; self.status.set('未切換知識庫。'); messagebox.showerror('掃描未完成',value,parent=self)
                elif kind=='source_done':
                    self.running=False; target,counts=value
                    try:
                        workspace.activate(BASE,target)
                        self.status.set('已建立獨立索引；重新啟動後使用所選來源。')
                        messagebox.showinfo('知識庫已建立',f'掃描完成。請關閉並重新開啟 APP，使用所選的文件資料夾。\n\n來源文件留在原位置，未複製至 APP。',parent=self)
                    except Exception as e: messagebox.showerror('無法切換知識庫',str(e),parent=self)
                elif kind=='package_done':
                    self.running=False; path,count,size=value
                    try:
                        workspace.activate(BASE,path/'data'/'knowledge.sqlite3')
                        self.status.set(f'已匯入 {count} 個文件；重新啟動後使用新知識庫。')
                        messagebox.showinfo('匯入完成',f'已驗證並匯入 {count} 個文件至 APP 資料夾。請關閉並重新開啟 APP 使用新知識庫。',parent=self)
                    except Exception as e: messagebox.showerror('已還原但無法切換',str(e),parent=self)
                elif kind=='search':
                    sid,q,rows=value
                    if sid==self.search_id: self._show_results(q,rows)
                elif kind=='search_error':
                    sid,error=value
                    if sid==self.search_id: self.status.set('搜尋失敗：'+error)
                elif kind in ('done','error'):
                    self.running=False
                    if kind=='error': self.status.set('掃描失敗：'+value); messagebox.showerror('掃描失敗',value)
                    else:
                        counts,complete=value; self.refresh_metrics()
                        self.status.set(('掃描完成' if complete else '掃描未完全完成')+f'：更新 {counts["indexed"]}，未變更 {counts["unchanged"]}，略過 {counts["skipped"]}，錯誤 {counts["error"]}。')
        except queue.Empty: pass
        self.after(120,self.poll)

    def show_status(self):
        if not DB.exists(): messagebox.showinfo('知識庫','目前尚未建立索引。'); return
        try:
            with closing(core.connect(DB)) as db:
                rows=db.execute('''SELECT d.status,d.path,d.detail,(EXISTS(SELECT 1 FROM chunks c WHERE c.document_id=d.id) OR EXISTS(SELECT 1 FROM pdf_derived p WHERE p.document_id=d.id AND p.source_sha256=d.sha256))
                  FROM documents d WHERE d.status<>'indexed' OR d.detail LIKE '%擷取失敗%' OR (NOT EXISTS(SELECT 1 FROM chunks c WHERE c.document_id=d.id) AND NOT EXISTS(SELECT 1 FROM pdf_derived p WHERE p.document_id=d.id AND p.source_sha256=d.sha256))
                  ORDER BY d.status,d.path''').fetchall()
            win=tk.Toplevel(self); win.title('需要處理的文件'); win.geometry('920x520'); win.configure(bg=BG)
            label(win,f'需要處理的文件  ·  {len(rows)} 個',bg=BG,fg=INK,font=(FONT,14,'bold')).pack(anchor='w',padx=20,pady=(18,5))
            label(win,'未支援格式、沒有擷取到文字的文件和處理錯誤，都會列在這裡。',bg=BG,fg=MUTED).pack(anchor='w',padx=20,pady=(0,10))
            box=tk.Text(win,wrap='word',bg=WHITE,fg=INK,font=(FONT,10),bd=0,padx=15,pady=12); box.pack(fill='both',expand=True,padx=20,pady=(0,20))
            names={'unsupported':'格式尚未支援','error':'處理失敗','skipped':'已略過','missing':'來源不見了','indexed':'未擷取到文字'}
            for status,path,detail,has_text in rows:
                state='部分頁面擷取失敗' if status=='indexed' and has_text else names.get(status,status)
                box.insert('end',f'{state}  ·  {Path(path).name}\n{path}\n{detail}\n\n')
            box.configure(state='disabled')
        except Exception as e: messagebox.showerror('狀態讀取失敗',str(e))
    def close(self):
        if self.running:
            self.stop.set(); messagebox.showinfo('掃描中','已要求取消，請等待掃描結束後關閉。'); return
        def active(widget):
            return bool(getattr(widget,'importing',False) or getattr(widget,'busy',False)) or any(active(x) for x in widget.winfo_children())
        if any(active(child) for child in self.winfo_children()):
            messagebox.showinfo('處理中','文件匯入、備份或 AI 問答尚未完成。請等待，或在文件管理中取消作業。'); return
        import local_ai
        local_ai.stop()
        self.destroy()

if __name__=='__main__': App().mainloop()
