"""Browse source documents and edit only app-owned knowledge cards."""
import os
import queue
import sqlite3
import threading
import difflib
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from contextlib import closing
from pathlib import Path
import core
import package_service
import workspace

FONT='Microsoft JhengHei UI'; BG='#f1f5f9'; WHITE='#ffffff'; INK='#1b2c40'; MUTED='#64748b'; BLUE='#2563eb'
def label(parent,text='',**kw):
    kw.setdefault('font',(FONT,10)); kw.setdefault('bg',WHITE); kw.setdefault('fg',INK)
    return tk.Label(parent,text=text,**kw)
def button(parent,text,command,primary=False):
    return tk.Button(parent,text=text,command=command,bd=0,padx=14,pady=7,cursor='hand2',font=(FONT,10,'bold'),bg=BLUE if primary else '#e9f0fb',fg=WHITE if primary else '#1d4ed8')

class DocumentWindow(tk.Toplevel):
    def __init__(self,master,db_path):
        super().__init__(master); self.title('文件管理 · 私人知識助理'); self.geometry('1120x760'); self.minsize(900,620); self.configure(bg=BG)
        self.db_path=Path(db_path); self.cards=self.db_path.parent/'cards'; self.rows=[]; self.active=None; self.events=queue.Queue(); self.importing=False; self.cancel=threading.Event()
        self.filter=tk.StringVar(); self.kind=tk.StringVar(value='全部文件'); self.info=tk.StringVar(value='選擇文件以查看狀態。'); self.summary=tk.StringVar()
        head=tk.Frame(self,bg=BG); head.pack(fill='x',padx=24,pady=(20,8))
        label(head,'文件管理',bg=BG,font=(FONT,19,'bold')).pack(side='left')
        button(head,'新增知識卡',lambda:self.editor(),True).pack(side='right')
        button(head,'匯入文件副本',self.import_files).pack(side='right',padx=8)
        button(head,'備份與還原',self.backup_dialog).pack(side='right')
        button(head,'取消處理',self.cancel.set).pack(side='right',padx=8)
        label(self,'原始文件維持唯讀；自行編輯的知識卡保存在 APP 資料夾並可搜尋。',bg=BG,fg=MUTED).pack(anchor='w',padx=25)
        footer=tk.Frame(self,bg=BG); footer.pack(side='bottom',fill='x',padx=25,pady=(0,15))
        label(footer,textvariable=self.summary,bg=BG,fg=MUTED,anchor='w').pack(fill='x')
        bar=tk.Frame(self,bg=WHITE,highlightbackground='#dbe4ef',highlightthickness=1); bar.pack(fill='x',padx=24,pady=12)
        tk.Entry(bar,textvariable=self.filter,font=(FONT,11),bd=0,highlightthickness=1,highlightbackground='#dbe4ef').pack(side='left',fill='x',expand=True,padx=(12,8),pady=12,ipady=6)
        box=ttk.Combobox(bar,textvariable=self.kind,values=('全部文件','原始文件','匯入副本','我的知識卡','需要處理','完全重複'),state='readonly',width=14); box.pack(side='left',padx=4); box.bind('<<ComboboxSelected>>',lambda e:self.refresh())
        button(bar,'搜尋文件',self.refresh,True).pack(side='left',padx=10)
        self.filter.trace_add('write',lambda *args:self.refresh())
        body=tk.PanedWindow(self,orient='horizontal',bg=BG,bd=0,sashwidth=8); body.pack(fill='both',expand=True,padx=24,pady=(0,12))
        left=tk.Frame(body,bg=WHITE,highlightbackground='#dbe4ef',highlightthickness=1); right=tk.Frame(body,bg=WHITE,highlightbackground='#dbe4ef',highlightthickness=1)
        body.add(left,minsize=420,stretch='always'); body.add(right,minsize=300,stretch='always')
        label(left,'文件清單',font=(FONT,12,'bold')).pack(anchor='w',padx=15,pady=12)
        table=tk.Frame(left,bg=WHITE); table.pack(fill='both',expand=True,padx=10,pady=(0,10))
        self.tree=ttk.Treeview(table,columns=('name','type','state'),show='headings',selectmode='browse')
        for col,title,width,minimum in [('name','名稱',230,120),('type','類型',60,45),('state','狀態',90,70)]: self.tree.heading(col,text=title); self.tree.column(col,width=width,minwidth=minimum)
        scroll=ttk.Scrollbar(table,command=self.tree.yview); self.tree.configure(yscrollcommand=scroll.set); self.tree.pack(side='left',fill='both',expand=True); scroll.pack(side='right',fill='y'); self.tree.bind('<<TreeviewSelect>>',self.select)
        label(right,'文件資訊',font=(FONT,12,'bold')).pack(anchor='w',padx=18,pady=14)
        self.description=tk.Text(right,height=11,wrap='word',font=(FONT,10),fg=INK,bg='#f8fafc',bd=0,padx=12,pady=12); self.description.pack(fill='x',padx=18); self.description.configure(state='disabled')
        buttons=tk.Frame(right,bg=WHITE); buttons.pack(fill='x',padx=18,pady=14)
        button(buttons,'開啟原文件',self.open_file).pack(side='left',padx=(0,6))
        button(buttons,'編輯知識卡',self.edit_selected).pack(side='left')
        button(right,'從已索引內容建立知識卡',self.copy_selected).pack(anchor='w',padx=18)
        tools=tk.Frame(right,bg=WHITE); tools.pack(fill='x',padx=18,pady=(8,0))
        button(tools,'查看文字版本',self.version_history).pack(side='left',padx=(0,6))
        button(tools,'PDF 圖片、表格與 OCR',self.pdf_tools).pack(side='left')
        button(right,'查看紀錄中的閱讀器截圖',self.device_capture).pack(anchor='w',padx=18,pady=(8,0))
        label(right,'要新增原始資料？',font=(FONT,11,'bold')).pack(anchor='w',padx=18,pady=(14,4))
        label(right,'可按上方「匯入文件副本」，或把檔案放入既有來源資料夾後，在主畫面按「重新掃描來源」。APP 不會搬動或修改原檔。',fg=MUTED,justify='left',wraplength=370).pack(anchor='w',padx=18)
        label(right,'知識卡和原始文件分開保存；目前不會自動刪除、移動或重新命名原始文件。',fg=MUTED,justify='left',wraplength=370).pack(anchor='w',padx=18,pady=(12,0))
        self.refresh()
        self.after(150,self.poll); self.protocol('WM_DELETE_WINDOW',self.close)

    def import_files(self):
        if self.importing: return
        paths=filedialog.askopenfilenames(parent=self,title='選擇要複製匯入的文件')
        if not paths: return
        self.importing=True; self.cancel.clear(); self.summary.set(f'正在匯入 {len(paths)} 個文件副本…')
        def work():
            created=0; duplicates=0; errors=[]
            for path in paths:
                if self.cancel.is_set(): errors.append('使用者已取消；其餘文件未處理'); break
                try:
                    result,new=core.import_copy(str(self.db_path),path)
                    if new: created+=1
                    else: duplicates+=1
                except Exception as e: errors.append(Path(path).name+'：'+str(e))
            self.events.put(('import',(created,duplicates,errors)))
        threading.Thread(target=work,daemon=True).start()
    def poll(self):
        try:
            while True:
                kind,value=self.events.get_nowait()
                if kind=='progress': self.summary.set(value); continue
                self.importing=False
                if kind=='import':
                    created,duplicates,errors=value; self.refresh(); self.master.refresh_metrics()
                    self.summary.set(f'匯入完成：新增 {created}、已有相同內容 {duplicates}、失敗 {len(errors)}。')
                    if errors: messagebox.showwarning('部分文件未匯入','\n'.join(errors[:20]),parent=self)
                elif kind=='backup':
                    path,count,size=value; self.summary.set(f'備份完成：{count} 個文件。'); messagebox.showinfo('備份完成',f'已建立 {count} 個文件的封裝：\n{path}',parent=self)
                elif kind=='restore':
                    path,count,size=value; self.summary.set(f'已還原 {count} 個文件到獨立資料夾。')
                    if messagebox.askyesno('還原完成',f'已還原 {count} 個文件到：\n{path}\n\n下次啟動 APP 時切換至這份還原的知識庫嗎？',parent=self):
                        try:
                            workspace.activate(workspace.application_base(),path/'data'/'knowledge.sqlite3')
                            messagebox.showinfo('已設定切換','請關閉並重新開啟 APP，才會使用還原的知識庫。',parent=self)
                        except Exception as e: messagebox.showerror('無法切換知識庫',str(e),parent=self)
                elif kind=='error': self.summary.set('操作未完成。'); messagebox.showerror('備份或還原未完成',value,parent=self)
        except queue.Empty: pass
        if self.winfo_exists(): self.after(150,self.poll)

    def close(self):
        if self.importing: messagebox.showinfo('處理中','請等候目前的匯入或備份完成，或先按取消。',parent=self); return
        if any(getattr(child,'busy',False) for child in self.winfo_children()):
            messagebox.showinfo('處理中','請等待 PDF 頁面處理完成。',parent=self); return
        self.destroy()
    def backup_dialog(self):
        if self.importing: return
        w=tk.Toplevel(self); w.title('備份與還原'); w.geometry('620x330'); w.configure(bg=WHITE); w.transient(self)
        label(w,'備份與還原',font=(FONT,16,'bold')).pack(anchor='w',padx=22,pady=(20,8))
        label(w,'備份為單一 .pka，包含來源文件、知識卡、匯入副本與搜尋資料。若來源未同步或和索引不一致，會停止並提示。',fg=MUTED,justify='left',wraplength=560).pack(anchor='w',padx=22)
        label(w,'匯入會在 APP 資料夾建立獨立知識庫，隨 APP 一起搬移；不會覆蓋原始文件。API Key 不會匯出；文件本身若含機密仍會被封裝。',fg=MUTED,justify='left',wraplength=560).pack(anchor='w',padx=22,pady=12)
        bar=tk.Frame(w,bg=WHITE); bar.pack(side='bottom',fill='x',padx=22,pady=20)
        def export():
            name=filedialog.asksaveasfilename(parent=w,title='儲存知識庫備份',defaultextension='.pka',filetypes=[('PKA 知識庫','*.pka')])
            if not name: return
            self._start_backup('backup',name); w.destroy()
        def restore():
            file=filedialog.askopenfilename(parent=w,title='選擇 PKA 備份',filetypes=[('PKA 知識庫','*.pka')])
            if not file: return
            self._start_backup('restore',file,str(workspace.library_parent(workspace.application_base()))); w.destroy()
        button(bar,'匯出 .pka',export,True).pack(side='left'); button(bar,'匯入 .pka',restore).pack(side='left',padx=10)
        default=workspace.default_database(workspace.application_base())
        if self.db_path.resolve()!=default.resolve() and default.exists():
            def back():
                try:
                    workspace.activate(workspace.application_base(),default)
                    messagebox.showinfo('已設定切換','重新啟動 APP 後會切回原本的知識庫。',parent=w); w.destroy()
                except Exception as e: messagebox.showerror('無法切換',str(e),parent=w)
            button(bar,'切回原本知識庫',back).pack(side='left')
    def _start_backup(self,kind,path,parent=None):
        if self.importing: return
        self.importing=True; self.cancel.clear(); self.summary.set('正在處理，請保持 APP 開啟…')
        def progress(text): self.events.put(('progress',text))
        def work():
            try:
                if kind=='backup':
                    roots=workspace.protected_roots(workspace.application_base(),self.db_path)
                    if any(core.contained(path,r) for r in roots): raise ValueError('備份不能寫入任何原始知識庫')
                    count,size=package_service.create_package(self.db_path,path,progress,self.cancel.is_set)
                    self.events.put(('backup',(path,count,size)))
                else:
                    with closing(core.connect(str(self.db_path))) as db: root=db.execute("SELECT value FROM settings WHERE key='root'").fetchone()[0]
                    roots=workspace.protected_roots(workspace.application_base(),self.db_path)
                    if any(core.contained(parent,r) for r in roots): raise ValueError('還原不能寫入任何原始知識庫')
                    Path(parent).mkdir(parents=True,exist_ok=True)
                    result=package_service.restore_package(path,parent,root,progress,self.cancel.is_set)
                    self.events.put(('restore',result))
            except Exception as e: self.events.put(('error',str(e)))
        threading.Thread(target=work,daemon=True).start()

    def is_card(self,path): return core.contained(path,self.cards) and Path(path).parent.resolve()==self.cards.resolve()
    def refresh(self):
        query=self.filter.get().strip().casefold(); kind=self.kind.get()
        try:
            with closing(core.connect(str(self.db_path))) as db:
                records=db.execute('''SELECT d.path,d.status,d.detail,(EXISTS(SELECT 1 FROM chunks c WHERE c.document_id=d.id) OR EXISTS(SELECT 1 FROM pdf_derived p WHERE p.document_id=d.id AND p.source_sha256=d.sha256)),d.sha256
                  FROM documents d ORDER BY d.path''').fetchall()
            counts={}
            for row in records:
                if row[4]: counts[row[4]]=counts.get(row[4],0)+1
            self.tree.delete(*self.tree.get_children()); self.rows=[]; self.active=None
            self.description.configure(state='normal'); self.description.delete('1.0','end'); self.description.configure(state='disabled')
            for path,status,detail,has_text,sha in records:
                card=self.is_card(path); imported=core.contained(path,self.db_path.parent/'imported'); needs=status!='indexed' or not has_text or '擷取失敗' in detail
                if kind=='原始文件' and (card or imported) or kind=='匯入副本' and not imported or kind=='我的知識卡' and not card or kind=='需要處理' and not needs or kind=='完全重複' and counts.get(sha,0)<2: continue
                title=Path(path).name
                if card:
                    try:
                        with open(path,'r',encoding='utf-8') as f: title=f.readline().removeprefix('# ').strip() or title
                    except OSError: pass
                if query and query not in (title+' '+path).casefold(): continue
                state={'indexed':'已索引' if has_text else '沒有文字','error':'處理失敗','unsupported':'格式未支援','skipped':'已略過','missing':'來源遺失'}.get(status,status)
                if status=='indexed' and '擷取失敗' in detail: state='部分擷取'
                idx=len(self.rows); self.rows.append((path,status,detail,has_text,card,title,imported)); self.tree.insert('','end',iid=str(idx),values=(title,'知識卡' if card else '副本' if imported else Path(path).suffix.lower().lstrip('.').upper(),state))
            self.summary.set(f'顯示 {len(self.rows)} 個文件；重複判斷只顯示已計算 SHA-256 且位元組相同者。')
        except sqlite3.Error as e: self.summary.set('讀取失敗：'+str(e))
    def select(self,event=None):
        selection=self.tree.selection()
        if not selection: return
        self.active=self.rows[int(selection[0])]; path,status,detail,has_text,card,title,imported=self.active
        text=f'{title}\n\n類型：{"使用者知識卡" if card else "匯入副本" if imported else "原始文件（唯讀）"}\n狀態：{status}\n路徑：{path}\n\n{detail}'
        self.description.configure(state='normal'); self.description.delete('1.0','end'); self.description.insert('1.0',text); self.description.configure(state='disabled')
    def open_file(self):
        if not self.active: return
        try: os.startfile(self.active[0])
        except OSError as e: messagebox.showerror('無法開啟',str(e),parent=self)
    def edit_selected(self):
        if not self.active: messagebox.showinfo('請先選擇文件','請選擇一張知識卡。',parent=self); return
        if not self.active[4]:
            messagebox.showinfo('原始文件是唯讀的','請使用「新增知識卡」建立獨立的編輯內容。原始文件不會被修改。',parent=self); return
        self.editor(self.active[0])
    def pdf_tools(self):
        if not self.active or Path(self.active[0]).suffix.lower()!='.pdf':
            messagebox.showinfo('請選擇 PDF','請先在清單中選擇已索引的 PDF 文件。',parent=self); return
        import pdf_ui
        pdf_ui.PDFWindow(self,self.db_path,self.active[0])
    def device_capture(self):
        if not self.active: messagebox.showinfo('請先選擇文件','請選擇閱讀器操作紀錄。',parent=self); return
        try:
            import adb_service,adb_ui
            data=adb_service.read_captures(self.db_path,self.active[0])
            if not data: messagebox.showinfo('沒有截圖','這份文件沒有保存閱讀器截圖。',parent=self); return
            adb_ui.show_captures(self,data)
        except Exception as e: messagebox.showerror('無法查看截圖',str(e),parent=self)
    def copy_selected(self):
        if not self.active: messagebox.showinfo('請先選擇文件','請先選擇已索引的文件。',parent=self); return
        source=self.active[0]
        with closing(core.connect(str(self.db_path))) as db:
            parts=db.execute('SELECT c.locator,c.text FROM chunks c JOIN documents d ON d.id=c.document_id WHERE d.path=? ORDER BY c.id',(source,)).fetchall()
        if not parts: messagebox.showinfo('沒有可複製的文字','此文件尚無可搜尋的文字。',parent=self); return
        pieces=[]; length=0; clipped=False
        for locator,text in parts:
            item=f'\n\n## {locator}\n{text}'
            if length+len(item)>100000:
                pieces.append(item[:max(0,100000-length)]); clipped=True; break
            pieces.append(item); length+=len(item)
        notice='\n（內容超過上限，僅複製前 100,000 字元）' if clipped else ''
        body=f'來源檔案：{source}\n這是由索引文字建立的工作副本，可能缺少圖片或格式。{notice}'+''.join(pieces)
        self.editor(initial_title=self.active[5]+' — 工作副本',initial_content=body)
    def version_history(self):
        if not self.active: messagebox.showinfo('請先選擇文件','請選擇要查看版本的文件。',parent=self); return
        path=self.active[0]
        with closing(core.connect(str(self.db_path))) as db:
            doc=db.execute('SELECT id,sha256 FROM documents WHERE path=?',(path,)).fetchone()
            versions=db.execute('SELECT id,sha256,recorded_at FROM document_revisions WHERE document_id=? ORDER BY id DESC',(doc[0],)).fetchall() if doc else []
        w=tk.Toplevel(self); w.title('文件文字版本'); w.geometry('850x620'); w.configure(bg=WHITE); w.transient(self)
        label(w,'文件文字版本',font=(FONT,15,'bold')).pack(anchor='w',padx=20,pady=(16,4))
        label(w,'保留的是從舊版擷取的文字，並非舊版原檔；只記錄啟用此功能後偵測到的變更。',fg=MUTED,wraplength=780,justify='left').pack(anchor='w',padx=20)
        rows=ttk.Treeview(w,columns=('time','hash'),show='headings',height=5,selectmode='browse')
        rows.heading('time',text='保留時間'); rows.heading('hash',text='SHA-256'); rows.column('time',width=180); rows.column('hash',width=500)
        rows.pack(fill='x',padx=20,pady=10)
        for i,(ident,sha,when) in enumerate(versions): rows.insert('','end',iid=str(i),values=(when,sha))
        text=tk.Text(w,wrap='none',font=(FONT,10),fg=INK,bg='#f8fafc',bd=0,padx=10,pady=10); text.pack(fill='both',expand=True,padx=20,pady=(0,20))
        if not versions: text.insert('1.0','目前沒有可比較的舊版文字。')
        def select(event=None):
            selected=rows.selection()
            if not selected: return
            ident=versions[int(selected[0])][0]
            with closing(core.connect(str(self.db_path))) as db:
                old=db.execute('SELECT locator,text FROM revision_chunks WHERE revision_id=? ORDER BY position',(ident,)).fetchall()
                current=db.execute('SELECT locator,text FROM chunks WHERE document_id=? ORDER BY id',(doc[0],)).fetchall()
            oldlines=('\n'.join(f'[{loc}]\n{body}' for loc,body in old))[:500000].splitlines()
            newlines=('\n'.join(f'[{loc}]\n{body}' for loc,body in current))[:500000].splitlines()
            diff='\n'.join(difflib.unified_diff(oldlines,newlines,fromfile='舊版擷取文字',tofile='目前擷取文字',n=2))
            text.delete('1.0','end'); text.insert('1.0',(diff[:300000] or '擷取文字沒有差異。')+'\n\n（差異顯示最多 300,000 字元）')
        rows.bind('<<TreeviewSelect>>',select)
    def editor(self,path=None,initial_title='',initial_content=''):
        title=initial_title[:160]; content=initial_content
        if path:
            try:
                data=Path(path).read_text(encoding='utf-8'); first,_,rest=data.partition('\n'); title=first.removeprefix('# ').strip(); content=rest.lstrip('\n')
            except OSError as e: messagebox.showerror('無法讀取知識卡',str(e),parent=self); return
        w=tk.Toplevel(self); w.title('編輯知識卡' if path else '新增知識卡'); w.geometry('760x650'); w.configure(bg=WHITE); w.transient(self)
        label(w,'編輯知識卡' if path else '新增知識卡',font=(FONT,16,'bold')).pack(anchor='w',padx=20,pady=(18,5))
        label(w,'此內容是你的獨立工作文件，不會覆蓋來源。儲存後會自動加入搜尋。',fg=MUTED).pack(anchor='w',padx=20)
        titlevar=tk.StringVar(value=title); tk.Entry(w,textvariable=titlevar,font=(FONT,12),relief='solid',bd=1).pack(fill='x',padx=20,pady=(15,8),ipady=5)
        bar=tk.Frame(w,bg=WHITE); bar.pack(side='bottom',fill='x',padx=20,pady=16)
        text=tk.Text(w,wrap='word',font=(FONT,11),fg=INK,bg='#f8fafc',relief='solid',bd=1,padx=12,pady=10); text.pack(fill='both',expand=True,padx=20); text.insert('1.0',content)
        def save():
            try:
                core.save_card(str(self.db_path),titlevar.get(),text.get('1.0','end').strip(),existing=path)
                self.refresh(); self.master.refresh_metrics(); w.destroy()
            except Exception as e: messagebox.showerror('無法儲存知識卡',str(e),parent=w)
        button(bar,'儲存並加入搜尋',save,True).pack(side='right')
