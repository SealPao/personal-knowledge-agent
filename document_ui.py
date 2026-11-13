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
import ai_service
import import_history
import package_service
import workspace

FONT='Microsoft JhengHei UI'; BG='#f1f5f9'; WHITE='#ffffff'; INK='#1b2c40'; MUTED='#64748b'; BLUE='#2563eb'
def label(parent,text='',**kw):
    kw.setdefault('font',(FONT,10)); kw.setdefault('bg',WHITE); kw.setdefault('fg',INK)
    return tk.Label(parent,text=text,**kw)
def button(parent,text,command,primary=False):
    return tk.Button(parent,text=text,command=command,bd=0,padx=14,pady=7,cursor='hand2',font=(FONT,10,'bold'),bg=BLUE if primary else '#e9f0fb',fg=WHITE if primary else '#1d4ed8')

def indexed_card_copy(db_path,path,limit=100000):
    """Read indexed text only; never open or modify the source document."""
    with closing(core.connect(str(db_path))) as db:
        doc=db.execute('SELECT id,status,detail,mtime_ns,size,sha256 FROM documents WHERE path=?',(str(path),)).fetchone()
        if not doc or doc[1]!='indexed': raise ValueError('文件目前沒有可用索引，請先重新掃描。')
        if doc[2] in core.AI_REVIEW_STATES: raise ValueError('AI 紀錄請在原知識卡中審核或編輯，不能轉成一般知識卡。')
        source={'path':str(path),'mtime_ns':doc[3],'size':doc[4]}
        state=ai_service.source_state(source)
        if state: raise ValueError(state+'；請先確認來源並重新掃描。')
        chunks=db.execute('SELECT locator,text FROM chunks WHERE document_id=? ORDER BY id',(doc[0],))
        derived=db.execute('SELECT locator,text,kind FROM pdf_derived WHERE document_id=? AND source_sha256=? ORDER BY page,id',(doc[0],doc[5]))
        pieces=[]; length=0; clipped=False
        for kind,rows in (('來源索引文字',chunks),('已校對的 PDF 衍生文字',derived)):
            for row in rows:
                locator,text=row[:2]; suffix='（'+row[2]+'）' if len(row)>2 else ''
                item=f'\n\n## {kind}{suffix} · {locator}\n{text}'
                remaining=limit-length
                if len(item)>remaining:
                    if remaining>0: pieces.append(item[:remaining])
                    clipped=True; break
                pieces.append(item); length+=len(item)
            if clipped: break
        if ai_service.source_state(source): raise ValueError('來源在讀取索引期間變更，未建立知識卡。')
        if not pieces: raise ValueError('此文件尚無可複製的索引文字或已校對的 PDF 衍生文字。')
        notice='\n（內容超過上限，僅複製前 100,000 字元）' if clipped else ''
        return ('來源檔案：'+str(path)+'\n來源索引 SHA-256：'+str(doc[5] or '未提供')
                +'\n這是由索引文字建立的獨立工作副本，不會自動隨來源更新；可能缺少圖片或格式，衍生內容可能有誤，請核對。'+notice+''.join(pieces))

def _import_one(db_path,path,retry_target='',stage=None,on_prepare=None):
    target=retry_target; staged=None
    def prepare(target_path,temp_path,sha,size):
        nonlocal staged
        on_prepare(target_path,temp_path,sha,size)
        staged={'planned':True,'target':str(target_path),'temp':str(temp_path),'expected_sha256':sha,'expected_size':size}
    try:
        if stage:
            target,created=core.recover_import_stage(str(db_path),stage['target'],stage['temp'],stage['expected_sha256'],stage['expected_size'],path)
        elif retry_target: target,created=core.retry_import_copy(str(db_path),retry_target,path)
        else: target,created=core.import_copy(str(db_path),path,on_prepare=prepare if on_prepare else None)
        with closing(core.connect(str(db_path))) as db:
            row=db.execute('''SELECT d.status,d.detail,EXISTS(SELECT 1 FROM chunks c WHERE c.document_id=d.id)
                FROM documents d WHERE d.path=?''',(str(target),)).fetchone()
        if not row: raise ValueError('副本已寫入，但索引紀錄無法確認')
        status,detail,has_text=row
        if status!='indexed': state='副本已保存，解析失敗'
        elif not has_text: state='副本已保存，沒有可搜尋文字'
        elif not created: state='已有副本'
        else: state='匯入成功'
        return {'source':str(path),'state':state,'detail':detail,'target':str(target)}
    except Exception as e:
        if stage or staged:
            info=dict(stage or staged); info.update(source=str(path),state='副本待復原',detail=str(e),target=str(target or info.get('target','')),planned=True)
            return info
        return {'source':str(path),'state':'未完成','detail':str(e),'target':str(target)}

def import_batch(db_path,paths,cancelled=lambda:False,progress=lambda _:None,on_item=lambda index,item:None,on_prepare=None):
    """Return a per-file outcome, including copied files whose parsing failed."""
    results=[]; paths=list(paths); total=len(paths)
    for index,path in enumerate(paths):
        if cancelled():
            results.extend({'source':str(p),'state':'未處理','detail':'使用者已取消','target':''} for p in paths[index:]); break
        if index==0 or index%10==0: progress(f'正在匯入 {index+1} / {total}：{Path(path).name}')
        prepare=(lambda target,temp,sha,size:on_prepare(index,target,temp,sha,size)) if on_prepare else None
        item=_import_one(db_path,path,on_prepare=prepare); on_item(index,item); results.append(item)
    return results

def retry_import_batch(db_path,previous,cancelled=lambda:False,progress=lambda _:None,on_item=lambda index,item:None,positions=None,on_prepare=None):
    """Retry failed or cancelled items while keeping successful outcomes."""
    results=[dict(item) for item in previous]
    if positions is not None:
        positions=list(positions)
        if not positions or any(type(i) is not int or i<0 or i>=len(results) for i in positions): raise ValueError('重試選擇無效')
        positions=set(positions)
    targets=[i for i,item in enumerate(results) if item['state'] in import_history.RETRY_STATES and (positions is None or i in positions)]
    for number,index in enumerate(targets,1):
        if cancelled(): break
        item=results[index]; progress(f'正在重試 {number} / {len(targets)}：{Path(item["source"]).name}')
        if item.get('target_missing'): continue
        # A saved copy is re-parsed in place; no new copy is created.
        retry_target=item['target'] if item['target'] else ''
        prepare=(lambda target,temp,sha,size:on_prepare(index,target,temp,sha,size)) if on_prepare else None
        results[index]=_import_one(db_path,item['source'],retry_target,stage=item if item.get('planned') else None,on_prepare=prepare); on_item(index,results[index])
    return results

class DocumentWindow(tk.Toplevel):
    def __init__(self,master,db_path):
        super().__init__(master); self.title('文件管理 · 私人知識助理'); self.geometry('1120x760'); self.minsize(900,620); self.configure(bg=BG)
        self.db_path=Path(db_path); self.cards=self.db_path.parent/'cards'; self.rows=[]; self.active=None; self.events=queue.Queue(); self.importing=False; self.cancel=threading.Event(); self.refresh_after=None; self.import_results=[]; self.import_batch_id=None
        self.filter=tk.StringVar(); self.kind=tk.StringVar(value='全部文件'); self.info=tk.StringVar(value='選擇文件以查看狀態。'); self.summary=tk.StringVar()
        head=tk.Frame(self,bg=BG); head.pack(fill='x',padx=24,pady=(20,8))
        label(head,'文件管理',bg=BG,font=(FONT,19,'bold')).pack(side='left')
        button(head,'新增知識卡',lambda:self.editor(),True).pack(side='right')
        button(head,'匯入文件副本',self.import_files).pack(side='right',padx=8)
        button(head,'備份與還原',self.backup_dialog).pack(side='right')
        button(head,'取消處理',self.cancel.set).pack(side='right',padx=8)
        button(head,'匯入結果',self.show_import_results).pack(side='right')
        label(self,'原始文件維持唯讀；自行編輯的知識卡保存在 APP 資料夾並可搜尋。',bg=BG,fg=MUTED).pack(anchor='w',padx=25)
        footer=tk.Frame(self,bg=BG); footer.pack(side='bottom',fill='x',padx=25,pady=(0,15))
        label(footer,textvariable=self.summary,bg=BG,fg=MUTED,anchor='w').pack(fill='x')
        bar=tk.Frame(self,bg=WHITE,highlightbackground='#dbe4ef',highlightthickness=1); bar.pack(fill='x',padx=24,pady=12)
        tk.Entry(bar,textvariable=self.filter,font=(FONT,11),bd=0,highlightthickness=1,highlightbackground='#dbe4ef').pack(side='left',fill='x',expand=True,padx=(12,8),pady=12,ipady=6)
        box=ttk.Combobox(bar,textvariable=self.kind,values=('全部文件','原始文件','匯入副本','我的知識卡','待審核 AI','需要處理','完全重複'),state='readonly',width=14); box.pack(side='left',padx=4); box.bind('<<ComboboxSelected>>',lambda e:self.refresh())
        button(bar,'搜尋文件',self.refresh,True).pack(side='left',padx=10)
        self.filter.trace_add('write',lambda *args:self.schedule_refresh())
        body=tk.PanedWindow(self,orient='horizontal',bg=BG,bd=0,sashwidth=8); body.pack(fill='both',expand=True,padx=24,pady=(0,12))
        left=tk.Frame(body,bg=WHITE,highlightbackground='#dbe4ef',highlightthickness=1); right=tk.Frame(body,bg=WHITE,highlightbackground='#dbe4ef',highlightthickness=1)
        body.add(left,minsize=420,stretch='always'); body.add(right,minsize=300,stretch='always')
        right_canvas=tk.Canvas(right,bg=WHITE,highlightthickness=0,bd=0)
        self.details_canvas=right_canvas
        right_scroll=ttk.Scrollbar(right,orient='vertical',command=right_canvas.yview)
        right_canvas.configure(yscrollcommand=right_scroll.set); right_scroll.pack(side='right',fill='y'); right_canvas.pack(side='left',fill='both',expand=True)
        right=tk.Frame(right_canvas,bg=WHITE); right_window=right_canvas.create_window((0,0),window=right,anchor='nw')
        right.bind('<Configure>',lambda event:right_canvas.configure(scrollregion=right_canvas.bbox('all')))
        right_canvas.bind('<Configure>',lambda event:right_canvas.itemconfigure(right_window,width=event.width))
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
        reviewbar=tk.Frame(right,bg=WHITE); reviewbar.pack(anchor='w',padx=18,pady=(8,0))
        button(reviewbar,'審核或撤回 AI 紀錄',self.review_card).pack(side='left')
        button(reviewbar,'查看審核歷史',self.review_history).pack(side='left',padx=6)
        label(right,'要新增原始資料？',font=(FONT,11,'bold')).pack(anchor='w',padx=18,pady=(14,4))
        label(right,'可按上方「匯入文件副本」，或把檔案放入既有來源資料夾後，在主畫面按「重新掃描來源」。APP 不會搬動或修改原檔。',fg=MUTED,justify='left',wraplength=370).pack(anchor='w',padx=18)
        label(right,'知識卡和原始文件分開保存；目前不會自動刪除、移動或重新命名原始文件。',fg=MUTED,justify='left',wraplength=370).pack(anchor='w',padx=18,pady=(12,0))
        self.refresh()
        self.poll_id=self.after(150,self.poll); self.protocol('WM_DELETE_WINDOW',self.close)

    def import_files(self):
        if self.importing: return
        paths=filedialog.askopenfilenames(parent=self,title='選擇要複製匯入的文件')
        if not paths: return
        self.importing=True; self.cancel.clear(); self.summary.set(f'正在匯入 {len(paths)} 個文件副本…')
        def work():
            try:
                with import_history.ImportHistory(self.db_path) as history:
                    batch=history.create(paths); self.events.put(('import_started',batch))
                    with import_history.operation_lock(self.db_path),import_history.batch_lock(self.db_path,batch):
                        import_batch(self.db_path,paths,self.cancel.is_set,lambda text:self.events.put(('progress',text)),lambda i,item:history.update(batch,i,item),lambda i,t,temp,sha,size:history.prepare(batch,i,t,temp,sha,size))
                        if self.cancel.is_set(): history.cancel_pending(batch)
                    self.events.put(('import',(batch,history.load(batch))))
            except Exception as e: self.events.put(('error',str(e)))
        threading.Thread(target=work,daemon=True).start()
    def poll(self):
        try:
            while True:
                kind,value=self.events.get_nowait()
                if kind=='progress': self.summary.set(value); continue
                if kind=='import_started': self.import_batch_id=value; continue
                self.importing=False
                if kind=='import':
                    self.import_batch_id,self.import_results=value; value=self.import_results; self.refresh(); self.master.refresh_metrics()
                    count=lambda state:sum(item['state']==state for item in value)
                    attention=sum(item['state'] not in ('匯入成功','已有副本') for item in value)
                    self.summary.set(f'匯入結果：成功 {count("匯入成功")}、已有副本 {count("已有副本")}、需處理或未處理 {attention}。')
                    if attention: messagebox.showwarning('部分文件需要處理','部分副本沒有可搜尋文字、解析失敗或尚未處理；請查看最近匯入結果。',parent=self)
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
        if self.winfo_exists(): self.poll_id=self.after(150,self.poll)

    def show_import_results(self):
        if self.importing: messagebox.showinfo('處理中','請等待目前作業完成後查看匯入紀錄。',parent=self); return
        try:
            with import_history.ImportHistory(self.db_path) as history: batches=history.batches()
        except (OSError,sqlite3.Error) as e: messagebox.showerror('無法讀取匯入紀錄',str(e),parent=self); return
        if not batches: messagebox.showinfo('尚無匯入紀錄','這個知識庫尚未記錄匯入批次。',parent=self); return
        w=tk.Toplevel(self); w.title('匯入紀錄與重試'); w.geometry('820x620'); w.configure(bg=WHITE)
        label(w,'匯入紀錄與重試',font=(FONT,15,'bold')).pack(anchor='w',padx=18,pady=(16,4))
        label(w,'重試會重新解析已保存的副本；尚未建立副本的項目會重新讀取原來源目前內容，不會修改它。搬移後請確認舊路徑；無文字文件可能需要 OCR。',fg=MUTED,wraplength=760,justify='left').pack(anchor='w',padx=18)
        label(w,'匯入紀錄與來源路徑保存在知識庫中，並隨 .pka 備份。',fg=MUTED,font=(FONT,9)).pack(anchor='w',padx=18,pady=(3,0))
        choices=[f'{created} · {count} 個檔案 · {ident[:8]}' for ident,created,updated,count in batches]
        selector=ttk.Combobox(w,values=choices,state='readonly'); selector.pack(fill='x',padx=18,pady=(8,0))
        initial=next((i for i,row in enumerate(batches) if row[0]==self.import_batch_id),0); selector.current(initial)
        current={'id':None,'items':[]}
        actions=tk.Frame(w,bg=WHITE); actions.pack(side='bottom',fill='x',padx=18,pady=(0,14))
        def retry():
            if self.importing: messagebox.showinfo('處理中','請等待目前作業完成。',parent=w); return
            snapshot=[dict(item) for item in current['items']]
            eligible=[(i,item) for i,item in enumerate(snapshot) if item['state'] in import_history.RETRY_STATES and not item.get('target_missing')]
            if not eligible:
                messagebox.showinfo('沒有可重試項目','沒有可重試項目；若副本無法存取，請檢查知識庫或重新選擇來源。無文字文件可使用 OCR。',parent=w); return
            batch=current['id']; choose=tk.Toplevel(w); choose.title('選擇重試項目'); choose.geometry('980x580'); choose.configure(bg=WHITE); choose.transient(w); choose.grab_set()
            label(choose,'選擇要重試的項目',font=(FONT,15,'bold')).pack(anchor='w',padx=18,pady=(14,4))
            label(choose,'按住 Ctrl 或 Shift 可複選；也可按「全選」。原來源會讀取當前內容，請核對路徑。',fg=MUTED,wraplength=920,justify='left').pack(anchor='w',padx=18)
            selection_info=tk.StringVar(value='尚未選擇項目。'); label(choose,textvariable=selection_info,fg=MUTED).pack(anchor='w',padx=18,pady=(5,0))
            bottom=tk.Frame(choose,bg=WHITE); bottom.pack(side='bottom',fill='x',padx=18,pady=(0,14))
            area=tk.Frame(choose,bg=WHITE); area.pack(fill='both',expand=True,padx=18,pady=10)
            tree=ttk.Treeview(area,columns=('state','method','source','target'),show='headings',selectmode='extended')
            for key,title,width in [('state','目前狀態',155),('method','重試方式',120),('source','原來源路徑',310),('target','APP 副本',310)]:
                tree.heading(key,text=title); tree.column(key,width=width,minwidth=80,stretch=True)
            scroll=ttk.Scrollbar(area,orient='vertical',command=tree.yview); horizontal=ttk.Scrollbar(area,orient='horizontal',command=tree.xview)
            tree.configure(yscrollcommand=scroll.set,xscrollcommand=horizontal.set)
            horizontal.pack(side='bottom',fill='x'); tree.pack(side='left',fill='both',expand=True); scroll.pack(side='right',fill='y')
            for index,item in eligible:
                method='復原 APP 暫存' if item.get('planned') else '解析 APP 副本' if item['target'] else '讀取原來源'
                tree.insert('','end',iid=str(index),values=(item['state'],method,item['source'],item['target']))
            def update_selection(event=None):
                chosen=[int(i) for i in tree.selection()]
                original=sum(not snapshot[i]['target'] for i in chosen)
                selection_info.set(f'已選 {len(chosen)} 項，其中 {original} 項會讀取原來源。')
            tree.bind('<<TreeviewSelect>>',update_selection)
            button(bottom,'全選',lambda:tree.selection_set(tree.get_children())).pack(side='left')
            def begin():
                positions=[int(i) for i in tree.selection()]
                if not positions: messagebox.showinfo('請選擇項目','請選擇至少一個要重試的項目。',parent=choose); return
                approved={i:(snapshot[i]['source'],snapshot[i]['target'],snapshot[i].get('planned'),snapshot[i].get('temp'),snapshot[i].get('expected_sha256'),snapshot[i].get('expected_size')) for i in positions}
                self.importing=True; self.cancel.clear(); self.summary.set(f'正在重試選定的 {len(positions)} 個項目…'); w.destroy()
                def work():
                    try:
                        with import_history.operation_lock(self.db_path),import_history.batch_lock(self.db_path,batch),import_history.ImportHistory(self.db_path) as history:
                            previous=history.load(batch)
                            if any(i>=len(previous) or previous[i].get('target_missing') or (previous[i]['source'],previous[i]['target'],previous[i].get('planned'),previous[i].get('temp'),previous[i].get('expected_sha256'),previous[i].get('expected_size'))!=approved[i] for i in positions):
                                raise ValueError('所選項目的來源或副本狀態已變更；請重新查看並選擇。')
                            retry_import_batch(self.db_path,previous,self.cancel.is_set,lambda text:self.events.put(('progress',text)),lambda i,item:history.update(batch,i,item),positions=positions,on_prepare=lambda i,t,temp,sha,size:history.prepare(batch,i,t,temp,sha,size))
                            if self.cancel.is_set(): history.cancel_pending(batch,positions=positions)
                            self.events.put(('import',(batch,history.load(batch))))
                    except Exception as e: self.events.put(('error',str(e)))
                threading.Thread(target=work,daemon=True).start()
            button(bottom,'重試所選',begin,True).pack(side='right')
        def show_attempts():
            try:
                with import_history.ImportHistory(self.db_path) as history: events=history.events(current['id'])
            except (OSError,sqlite3.Error) as e: messagebox.showerror('無法讀取嘗試歷程',str(e),parent=w); return
            view=tk.Toplevel(w); view.title('匯入嘗試歷程'); view.geometry('780x500'); view.configure(bg=WHITE)
            label(view,'匯入嘗試歷程',font=(FONT,14,'bold')).pack(anchor='w',padx=18,pady=(16,4))
            area=tk.Frame(view,bg=WHITE); area.pack(fill='both',expand=True,padx=18,pady=10)
            box=tk.Text(area,wrap='word',font=(FONT,10),bg='#f8fafc',bd=0,padx=10,pady=10)
            scroll=ttk.Scrollbar(area,command=box.yview); box.configure(yscrollcommand=scroll.set); box.pack(side='left',fill='both',expand=True); scroll.pack(side='right',fill='y')
            for when,source,state,detail in events: box.insert('end',f'{when} · {state}\n{source}\n{detail}\n\n')
            if not events: box.insert('end','尚無已完成的處理嘗試。')
            box.configure(state='disabled')
        def clean_staging():
            if self.importing: messagebox.showinfo('處理中','請等待目前作業完成。',parent=w); return
            snapshot=[dict(item) for item in current['items']]; staged=[(i,item) for i,item in enumerate(snapshot) if item.get('planned')]
            if not staged: messagebox.showinfo('沒有中斷暫存','此批次沒有已記錄的中斷暫存。',parent=w); return
            batch=current['id']; choose=tk.Toplevel(w); choose.title('清理中斷暫存'); choose.geometry('930x540'); choose.configure(bg=WHITE); choose.transient(w); choose.grab_set()
            label(choose,'選擇要清理的紀錄',font=(FONT,15,'bold')).pack(anchor='w',padx=18,pady=(14,4))
            label(choose,'只處理 APP 記錄的暫存；完整且可復原的檔案會保留。清理不完整暫存後，如需重試可能需要重新讀取原來源。',fg=MUTED,wraplength=870,justify='left').pack(anchor='w',padx=18)
            bottom=tk.Frame(choose,bg=WHITE); bottom.pack(side='bottom',fill='x',padx=18,pady=(0,14))
            area=tk.Frame(choose,bg=WHITE); area.pack(fill='both',expand=True,padx=18,pady=10)
            tree=ttk.Treeview(area,columns=('source','temp','state'),show='headings',selectmode='extended')
            for key,title,width in [('source','原來源',300),('temp','APP 暫存路徑',360),('state','紀錄狀態',180)]: tree.heading(key,text=title); tree.column(key,width=width,minwidth=80)
            scroll=ttk.Scrollbar(area,orient='vertical',command=tree.yview); horizontal=ttk.Scrollbar(area,orient='horizontal',command=tree.xview)
            tree.configure(yscrollcommand=scroll.set,xscrollcommand=horizontal.set); horizontal.pack(side='bottom',fill='x'); tree.pack(side='left',fill='both',expand=True); scroll.pack(side='right',fill='y')
            for index,item in staged: tree.insert('','end',iid=str(index),values=(item['source'],item.get('temp',''),item['detail']))
            button(bottom,'全選',lambda:tree.selection_set(tree.get_children())).pack(side='left')
            def begin_cleanup():
                positions=[int(i) for i in tree.selection()]
                if not positions: messagebox.showinfo('請選擇項目','請選擇至少一筆暫存紀錄。',parent=choose); return
                if not messagebox.askyesno('確認清理',f'要檢查並清理選定的 {len(positions)} 筆 APP 暫存嗎？完整可復原內容會保留；不會刪除原始文件。',parent=choose): return
                approved={i:(snapshot[i]['source'],snapshot[i]['target'],snapshot[i].get('temp'),snapshot[i].get('expected_sha256'),snapshot[i].get('expected_size')) for i in positions}
                self.importing=True; self.cancel.clear(); self.summary.set('正在檢查選定的 APP 暫存…'); w.destroy()
                def work():
                    try:
                        with import_history.operation_lock(self.db_path),import_history.batch_lock(self.db_path,batch),import_history.ImportHistory(self.db_path) as history:
                            items=history.load(batch)
                            if any(i>=len(items) or not items[i].get('planned') or (items[i]['source'],items[i]['target'],items[i].get('temp'),items[i].get('expected_sha256'),items[i].get('expected_size'))!=approved[i] for i in positions):
                                raise ValueError('暫存紀錄已變更，請重新查看並選擇。')
                            for number,index in enumerate(positions,1):
                                if self.cancel.is_set(): break
                                item=items[index]; self.events.put(('progress',f'正在檢查暫存 {number} / {len(positions)}'))
                                try:
                                    result=core.cleanup_import_stage(self.db_path,item['target'],item['temp'],item['expected_sha256'],item['expected_size'])
                                    if result=='已清理': history.clear_stage(batch,index,'APP 暫存已清理；如需重試請重新確認原來源。')
                                    else: history.note_stage(batch,index,'已清理多餘暫存並保留完整副本；請選擇復原。')
                                except (OSError,ValueError) as e: history.note_stage(batch,index,str(e))
                            self.events.put(('import',(batch,history.load(batch))))
                    except Exception as e: self.events.put(('error',str(e)))
                threading.Thread(target=work,daemon=True).start()
            button(bottom,'檢查並清理所選',begin_cleanup,True).pack(side='right')
        button(actions,'查看嘗試歷程',show_attempts).pack(side='left')
        button(actions,'處理中斷暫存',clean_staging).pack(side='left',padx=8)
        button(actions,'選擇重試項目',retry,True).pack(side='right')
        area=tk.Frame(w,bg=WHITE); area.pack(fill='both',expand=True,padx=18,pady=12)
        text=tk.Text(area,wrap='word',font=(FONT,10),fg=INK,bg='#f8fafc',bd=0,padx=10,pady=10)
        scroll=ttk.Scrollbar(area,command=text.yview); text.configure(yscrollcommand=scroll.set); text.pack(side='left',fill='both',expand=True); scroll.pack(side='right',fill='y')
        def display(event=None):
            index=selector.current()
            if index<0: return
            batch=batches[index][0]
            try:
                with import_history.ImportHistory(self.db_path) as history: items=history.load(batch)
            except (OSError,sqlite3.Error) as e: messagebox.showerror('無法讀取匯入紀錄',str(e),parent=w); return
            current['id']=batch; current['items']=items; self.import_batch_id=batch; self.import_results=items
            text.configure(state='normal'); text.delete('1.0','end')
            for item in items:
                text.insert('end',f"{item['state']} · {Path(item['source']).name}\n來源：{item['source']}\n")
                if item['target']: text.insert('end','副本或既有項目：'+item['target']+'\n')
                if item['detail']: text.insert('end',item['detail']+'\n')
                text.insert('end','\n')
            text.configure(state='disabled')
        selector.bind('<<ComboboxSelected>>',display); display()

    def close(self):
        if self.importing: messagebox.showinfo('處理中','請等候目前的匯入或備份完成，或先按取消。',parent=self); return
        if any(getattr(child,'busy',False) for child in self.winfo_children()):
            messagebox.showinfo('處理中','請等待 PDF 頁面處理完成。',parent=self); return
        self.destroy()
    def destroy(self):
        for ident in (getattr(self,'poll_id',None),getattr(self,'refresh_after',None)):
            if ident:
                try: self.after_cancel(ident)
                except tk.TclError: pass
        super().destroy()
    def schedule_refresh(self):
        if self.refresh_after:
            try: self.after_cancel(self.refresh_after)
            except tk.TclError: pass
        self.refresh_after=self.after(250,self.refresh)
    def backup_dialog(self):
        if self.importing: return
        w=tk.Toplevel(self); w.title('備份與還原'); w.geometry('620x330'); w.configure(bg=WHITE); w.transient(self)
        label(w,'備份與還原',font=(FONT,16,'bold')).pack(anchor='w',padx=22,pady=(20,8))
        label(w,'備份為單一 .pka，包含來源文件、知識卡、匯入副本與搜尋資料。若來源未同步或和索引不一致，會停止並提示。',fg=MUTED,justify='left',wraplength=560).pack(anchor='w',padx=22)
        label(w,'匯入會建立獨立知識庫，不會覆蓋原始文件。備份包含匯入紀錄與原始來源路徑；API Key 不會匯出，文件與路徑仍可能含私人資訊。',fg=MUTED,justify='left',wraplength=560).pack(anchor='w',padx=22,pady=12)
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
        if self.refresh_after:
            try: self.after_cancel(self.refresh_after)
            except tk.TclError: pass
            self.refresh_after=None
        selected_path=self.active[0] if self.active else None; selected_id=None
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
                card=self.is_card(path); imported=core.contained(path,self.db_path.parent/'imported'); pending=detail in core.AI_REVIEW_STATES and detail==core.AI_REVIEW_STATES[detail][0]; needs=status!='indexed' or not has_text or '擷取失敗' in detail or pending
                if kind=='原始文件' and (card or imported) or kind=='匯入副本' and not imported or kind=='我的知識卡' and not card or kind=='待審核 AI' and not pending or kind=='需要處理' and not needs or kind=='完全重複' and counts.get(sha,0)<2: continue
                title=Path(path).name
                if card:
                    try:
                        with open(path,'r',encoding='utf-8') as f: title=f.readline().removeprefix('# ').strip() or title
                    except OSError: pass
                if query and query not in (title+' '+path).casefold(): continue
                state={'indexed':'已索引' if has_text else '沒有文字','error':'處理失敗','unsupported':'格式未支援','skipped':'已略過','missing':'來源遺失'}.get(status,status)
                if status=='indexed' and '擷取失敗' in detail: state='部分擷取'
                if status=='indexed' and detail in core.AI_REVIEW_STATES: state='AI 待審核' if pending else 'AI 已人工核對'
                idx=len(self.rows); self.rows.append((path,status,detail,has_text,card,title,imported)); self.tree.insert('','end',iid=str(idx),values=(title,'知識卡' if card else '副本' if imported else Path(path).suffix.lower().lstrip('.').upper(),state))
                if path==selected_path: selected_id=str(idx)
            if selected_id is not None: self.tree.selection_set(selected_id); self.tree.focus(selected_id); self.select()
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
    def review_card(self):
        if not self.active or not self.active[4] or self.active[2] not in core.AI_REVIEW_STATES:
            messagebox.showinfo('請選擇 AI 紀錄','請選擇 AI 裝置觀察或 AI 網路研究知識卡。',parent=self); return
        question=('請先閱讀內容、來源和截圖。選「是」標記為已人工核對；選「否」改回待審核。\n\n'
                  '人工核對不表示網路研究的指令已在裝置上實測，也不會執行指令。')
        choice=messagebox.askyesnocancel('審核 AI 紀錄',question,parent=self)
        if choice is None: return
        try:
            detail=core.review_ai_card(self.db_path,self.active[0],choice)
            self.refresh(); self.master.refresh_metrics(); self.summary.set('已更新審核狀態：'+detail)
        except Exception as e: messagebox.showerror('無法更新審核狀態',str(e),parent=self)
    def review_history(self):
        if not self.active or not self.active[4] or self.active[2] not in core.AI_REVIEW_STATES:
            messagebox.showinfo('請選擇 AI 紀錄','請選擇 AI 裝置觀察或 AI 網路研究知識卡。',parent=self); return
        with closing(core.connect(str(self.db_path))) as db:
            records=db.execute('''SELECT r.reviewed_at,r.previous_detail,r.new_detail,r.content_sha256
                FROM ai_card_reviews r JOIN documents d ON d.id=r.document_id WHERE d.path=? ORDER BY r.id DESC''',(self.active[0],)).fetchall()
        w=tk.Toplevel(self); w.title('AI 紀錄審核歷史'); w.geometry('760x460'); w.configure(bg=WHITE); w.transient(self)
        label(w,'AI 紀錄審核歷史',font=(FONT,15,'bold')).pack(anchor='w',padx=18,pady=(15,5))
        label(w,'雜湊代表當時的知識卡內容；人工核對不代表已在裝置實測。',fg=MUTED).pack(anchor='w',padx=18)
        view=tk.Text(w,wrap='word',font=(FONT,10),fg=INK,bg='#f8fafc',bd=0,padx=12,pady=10); view.pack(fill='both',expand=True,padx=18,pady=12)
        for when,before,after,sha in records: view.insert('end',f'{when}\n{before} → {after}\nSHA-256：{sha}\n\n')
        if not records: view.insert('end','目前沒有人工審核狀態變更紀錄。')
        view.configure(state='disabled')
    def copy_selected(self):
        if not self.active: messagebox.showinfo('請先選擇文件','請先選擇已索引的文件。',parent=self); return
        try: body=indexed_card_copy(self.db_path,self.active[0])
        except (OSError,ValueError,sqlite3.Error) as e: messagebox.showinfo('無法建立知識卡',str(e),parent=self); return
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
