"""Window for durable, explicitly started organization jobs."""
import queue, threading, time, tkinter as tk
from tkinter import ttk,messagebox
from pathlib import Path
import organize_batch as batch, ai_ui, budget

class OrganizeBatchWindow(tk.Toplevel):
    def __init__(self,master,db):
        super().__init__(master);self.title('整庫整理');self.geometry('1000x720');self.db=Path(db);self.events=queue.Queue();self.cancel=threading.Event();self.busy=False
        self.last_refresh=0;self.current=tk.StringVar(value='');self.job=batch.latest(db)
        if self.job: batch.recover(db,self.job)
        tk.Label(self,text='整庫整理',font=('Microsoft JhengHei',18,'bold')).pack(anchor='w',padx=20,pady=(15,4))
        tk.Label(self,text='逐段整理已索引文字；AI 草稿與 OCR 結果需校對。混合 PDF 圖片與表格尚未自動處理。',anchor='w').pack(fill='x',padx=20)
        cfg=budget.get_settings(self.db); cap=str(cfg.get('monthly','0')); tk.Label(self,text='APP 每月預算：'+('未限制' if cap=='0' else 'US$ '+cap)+'（與下方 Token 整理上限分開）',anchor='w').pack(fill='x',padx=20)
        bar=tk.Frame(self);bar.pack(fill='x',padx=20,pady=10)
        tk.Label(bar,text='整個工作 Token 預留上限').pack(side='left');self.limit=tk.StringVar(value=str(batch.stats(db,self.job)['limit']) if self.job else '100000');tk.Entry(bar,textvariable=self.limit,width=12).pack(side='left',padx=8)
        self.start=tk.Button(bar,text='建立並開始',command=self.new);self.start.pack(side='left',padx=4)
        self.resume=tk.Button(bar,text='繼續上次工作',command=self.begin);self.resume.pack(side='left',padx=4)
        tk.Button(bar,text='暫停',command=self.cancel.set).pack(side='left',padx=4)
        tk.Button(bar,text='重試失敗項目',command=self.retry).pack(side='left',padx=4)
        tk.Button(bar,text='略過未知項目',command=self.skip_unknown).pack(side='left',padx=4)
        self.progress=ttk.Progressbar(self,maximum=100);self.progress.pack(fill='x',padx=20,pady=5)
        self.status=tk.StringVar();tk.Label(self,textvariable=self.status,justify='left',anchor='w',wraplength=940).pack(fill='x',padx=20,pady=5)
        tk.Label(self,textvariable=self.current,justify='left',anchor='w',wraplength=940).pack(fill='x',padx=20)
        table=tk.Frame(self);table.pack(fill='both',expand=True,padx=20,pady=8)
        self.tree=ttk.Treeview(table,columns=('name','location','state'),show='headings',height=9)
        for key,title,width in [('name','文件',300),('location','位置',300),('state','狀態',130)]:self.tree.heading(key,text=title);self.tree.column(key,width=width)
        scroll=ttk.Scrollbar(table,command=self.tree.yview);self.tree.configure(yscrollcommand=scroll.set);self.tree.pack(side='left',fill='both',expand=True);scroll.pack(side='right',fill='y');self.tree.bind('<<TreeviewSelect>>',self.select)
        self.details=tk.Text(self,wrap='word',height=9,font=('Microsoft JhengHei',10));self.details.pack(fill='both',expand=True,padx=20,pady=(0,15))
        self.protocol('WM_DELETE_WINDOW',self.close);self.after(200,self.poll);self.refresh()
    def new(self):
        if self.busy:return
        config=ai_ui.read_config(self.db.parent/'ai-settings.json')
        if config['provider']=='openai' and not messagebox.askyesno('開始雲端整理','將逐段把文件內容傳送至 OpenAI，可能產生費用。Token 為保守預留，不等於官方用量。確定開始？',parent=self):return
        try:self.job=batch.create(self.db,config,self.limit.get());self.begin()
        except Exception as e:messagebox.showerror('無法開始',str(e),parent=self)
    def skip_unknown(self):
        if self.busy or not self.job:return
        if not messagebox.askyesno('略過未知項目','未知項目的實際費用可能尚未確認。略過後不會重送，也不會刪除費用預留；可繼續其他文件。確定略過？',parent=self):return
        try:
            count=batch.skip_unknown(self.db,self.job);self.refresh();messagebox.showinfo('處理完成',f'已略過 {count} 個未知項目；費用預留仍保留。',parent=self)
        except Exception as e:messagebox.showerror('無法略過',str(e),parent=self)
    def retry(self):
        if self.busy or not self.job:return
        try:batch.retry_failed(self.db,self.job);self.refresh()
        except Exception as e:messagebox.showerror('無法重試',str(e),parent=self)
    def begin(self):
        if self.busy or not self.job:return
        try:batch.set_limit(self.db,self.job,self.limit.get())
        except Exception as e:messagebox.showerror('Token 上限無效',str(e),parent=self);return
        self.cancel.clear();self.busy=True;self.last_refresh=0;self.start.configure(state='disabled');self.resume.configure(state='disabled');self.status.set('正在整理…')
        def work():
            try:batch.run(self.db,self.job,self.cancel.is_set,lambda x:self.events.put(('progress',x)));self.events.put(('done','工作已暫停或處理完畢。'))
            except Exception as e:self.events.put(('error',str(e)))
        threading.Thread(target=work,daemon=True).start()
    def refresh(self):
        self.last_refresh=time.monotonic()
        if not self.job:self.status.set('尚無整理工作。請先匯入文件並設定 AI。');return
        info=batch.stats(self.db,self.job);counts=info['counts'];self.progress['value']=100*info['processed']/max(info['documents'],1)
        self.status.set(f"文件已處理 {info['processed']} / {info['documents']}；待執行片段 {counts.get('pending',0)}，待校對 {counts.get('review',0)}，失敗 {counts.get('failed',0)}，略過 {counts.get('skipped',0)}，狀態未知 {counts.get('unknown',0)}。\nToken 保守預留 {info['reserved']} / {info['limit']}；這不是官方實際用量。完成比例包含待校對及失敗文件。")
        db=batch.connect(self.db)
        try:self.rows=db.execute("SELECT path,locator,state,result,detail FROM organize_items WHERE job_id=? ORDER BY CASE state WHEN 'running' THEN 0 WHEN 'unknown' THEN 1 WHEN 'failed' THEN 2 WHEN 'pending' THEN 3 WHEN 'review' THEN 4 ELSE 5 END,id LIMIT 300",(self.job,)).fetchall()
        finally:db.close()
        self.tree.delete(*self.tree.get_children())
        states={'pending':'待處理','running':'處理中','review':'待校對','failed':'失敗','skipped':'略過','unknown':'中斷待檢查'}
        for i,(path,loc,state,result,detail) in enumerate(self.rows):self.tree.insert('','end',iid=str(i),values=(Path(path).name,loc,states.get(state,state)))
    def select(self,event=None):
        sel=self.tree.selection()
        if not sel:return
        path,loc,state,result,detail=self.rows[int(sel[0])];self.details.delete('1.0','end');self.details.insert('1.0',f'{Path(path).name} · {loc}\n{detail}\n\n{result}')
    def poll(self):
        try:
            while True:
                kind,value=self.events.get_nowait()
                if kind=='progress':self.current.set(value)
                else:
                    self.busy=False;self.current.set('');self.start.configure(state='normal');self.resume.configure(state='normal');self.refresh()
                    if kind=='error':messagebox.showerror('整理暫停',value,parent=self)
        except queue.Empty:pass
        if self.busy and time.monotonic()-self.last_refresh>=5:self.refresh()
        if self.winfo_exists():self.after(200,self.poll)
    def close(self):
        if self.busy:messagebox.showinfo('正在整理','請先按暫停，等待目前片段處理完畢。',parent=self);return
        self.destroy()
