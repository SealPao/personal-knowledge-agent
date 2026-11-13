import queue
import threading
import tkinter as tk
from tkinter import ttk,messagebox
from pathlib import Path
import ocr_batch

FONT='Microsoft JhengHei UI'; WHITE='#ffffff'; BG='#f1f5f9'; INK='#1b2c40'; MUTED='#64748b'; BLUE='#2563eb'
def label(p,text='',**kw):
    kw.setdefault('font',(FONT,10)); kw.setdefault('bg',WHITE); kw.setdefault('fg',INK); return tk.Label(p,text=text,**kw)
def button(p,text,command,primary=False):
    return tk.Button(p,text=text,command=command,bd=0,padx=12,pady=7,font=(FONT,10,'bold'),bg=BLUE if primary else '#e9f0fb',fg=WHITE if primary else '#1d4ed8',cursor='hand2')

class BatchWindow(tk.Toplevel):
    def __init__(self,master,db_path,path,language,tessdata):
        super().__init__(master); self.title('批次 OCR 與校對'); self.geometry('1000x720'); self.minsize(850,600); self.configure(bg=BG)
        self.db=Path(db_path); self.path=path; self.language=language; self.tessdata=tessdata; self.busy=False; self.cancel=threading.Event(); self.events=queue.Queue(); self.rows=[]
        self.start=tk.StringVar(value='1'); self.end=tk.StringVar(value='1'); self.status=tk.StringVar(value='每次最多 25 頁；未校對結果不會提供給 AI。')
        top=tk.Frame(self,bg=BG); top.pack(fill='x',padx=20,pady=(18,6))
        label(top,'批次 OCR 與校對',bg=BG,font=(FONT,16,'bold')).pack(side='left')
        label(self,'語言：'+language+'　·　'+Path(path).name,bg=BG,fg=MUTED).pack(anchor='w',padx=20)
        bar=tk.Frame(self,bg=WHITE); bar.pack(fill='x',padx=20,pady=10)
        label(bar,'起始頁').pack(side='left',padx=(12,5),pady=10); tk.Entry(bar,textvariable=self.start,width=5).pack(side='left',ipady=5)
        label(bar,'結束頁').pack(side='left',padx=8); tk.Entry(bar,textvariable=self.end,width=5).pack(side='left',ipady=5)
        button(bar,'開始辨識',self.begin,True).pack(side='left',padx=12); button(bar,'取消',self.cancel.set).pack(side='left')
        footer=tk.Frame(self,bg=BG); footer.pack(side='bottom',fill='x',padx=20,pady=(0,12)); label(footer,textvariable=self.status,bg=BG,fg=MUTED).pack(anchor='w')
        body=tk.PanedWindow(self,orient='horizontal',bg=BG,bd=0,sashwidth=8); body.pack(fill='both',expand=True,padx=20,pady=(0,12))
        left=tk.Frame(body,bg=WHITE); right=tk.Frame(body,bg=WHITE); body.add(left,minsize=260,stretch='always'); body.add(right,minsize=380,stretch='always')
        label(left,'頁面結果',font=(FONT,12,'bold')).pack(anchor='w',padx=12,pady=10)
        self.tree=ttk.Treeview(left,columns=('page','lang','state'),show='headings',selectmode='browse')
        for key,title,width in [('page','頁碼',65),('lang','語言',150),('state','狀態',100)]: self.tree.heading(key,text=title); self.tree.column(key,width=width,minwidth=45)
        self.tree.pack(fill='both',expand=True,padx=10,pady=(0,10)); self.tree.bind('<<TreeviewSelect>>',self.select)
        label(right,'逐頁校對',font=(FONT,12,'bold')).pack(anchor='w',padx=14,pady=(10,5))
        label(right,'檢查並修正後儲存，文字才會加入搜尋並可供 AI 引用。',fg=MUTED,font=(FONT,9)).pack(anchor='w',padx=14)
        bottom=tk.Frame(right,bg=WHITE); bottom.pack(side='bottom',fill='x',padx=14,pady=12)
        button(bottom,'校對並加入搜尋',self.approve,True).pack(side='right')
        self.text=tk.Text(right,wrap='word',font=(FONT,10),fg=INK,bg='#f8fafc',bd=0,padx=10,pady=10); self.text.pack(fill='both',expand=True,padx=14,pady=10)
        self.refresh(); self.after(150,self.poll); self.protocol('WM_DELETE_WINDOW',self.close)
    def refresh(self):
        self.rows=ocr_batch.list_pages(self.db,self.path); self.tree.delete(*self.tree.get_children())
        names={'pending':'待校對','reviewed':'已加入搜尋','failed':'辨識失敗'}
        for i,row in enumerate(self.rows): self.tree.insert('','end',iid=str(i),values=(row[1],row[2],names.get(row[3],row[3])))
    def begin(self):
        if self.busy: return
        try:
            first=int(self.start.get()); last=int(self.end.get())
            if first<1 or last<first or last-first+1>ocr_batch.MAX_PAGES: raise ValueError()
        except ValueError: messagebox.showerror('頁碼錯誤','每次請選擇 1 至 25 頁的有效範圍。',parent=self); return
        self.busy=True; self.cancel.clear(); self.status.set('正在本機逐頁辨識…')
        def work():
            try:
                stats=ocr_batch.run(self.db,self.path,first,last,self.language,self.tessdata,lambda x:self.events.put(('progress',x)),self.cancel.is_set)
                self.events.put(('done',stats))
            except Exception as e: self.events.put(('error',str(e)))
        threading.Thread(target=work,daemon=True).start()
    def poll(self):
        try:
            while True:
                kind,value=self.events.get_nowait()
                if kind=='progress': self.status.set(value)
                elif kind=='done':
                    self.busy=False; self.refresh(); self.status.set(f'完成：待校對 {value["pending"]}、既有 {value["skipped"]}、失敗 {value["failed"]}'+('；已取消後續頁面。' if value['cancelled'] else '。'))
                elif kind=='error': self.busy=False; self.refresh(); self.status.set('處理未完成：'+value); messagebox.showerror('批次 OCR',value,parent=self)
        except queue.Empty: pass
        if self.winfo_exists(): self.after(150,self.poll)
    def select(self,event=None):
        selected=self.tree.selection()
        if not selected: return
        row=self.rows[int(selected[0])]; self.text.delete('1.0','end'); self.text.insert('1.0',row[5] if row[3]!='failed' else '辨識失敗：'+row[4])
    def approve(self):
        selected=self.tree.selection()
        if not selected: return
        row=self.rows[int(selected[0])]
        if row[3]=='failed': messagebox.showinfo('無法儲存','此頁辨識失敗，請重新執行。',parent=self); return
        try:
            ocr_batch.approve(self.db,self.path,row[0],self.text.get('1.0','end'))
            self.refresh(); self.status.set(f'第 {row[1]} 頁已加入搜尋，AI 可引用。')
            manager=self.master.master
            if hasattr(manager,'refresh'): manager.refresh()
            if hasattr(manager.master,'refresh_metrics'): manager.master.refresh_metrics()
        except Exception as e: messagebox.showerror('無法儲存',str(e),parent=self)
    def close(self):
        if self.busy: messagebox.showinfo('辨識中','請先取消或等待本頁處理完成。',parent=self); return
        self.destroy()
