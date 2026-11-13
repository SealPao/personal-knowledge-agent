import io
import math
import queue
import threading
import tkinter as tk
from tkinter import ttk,messagebox,filedialog
from pathlib import Path
import core,pdf_features,workspace

FONT='Microsoft JhengHei UI'; WHITE='#ffffff'; BG='#f1f5f9'; INK='#1b2c40'; MUTED='#64748b'; BLUE='#2563eb'
def lab(p,text='',**kw):
    kw.setdefault('font',(FONT,10)); kw.setdefault('bg',WHITE); kw.setdefault('fg',INK)
    return tk.Label(p,text=text,**kw)
def btn(p,text,command,primary=False):
    return tk.Button(p,text=text,command=command,bd=0,padx=12,pady=7,cursor='hand2',font=(FONT,10,'bold'),bg=BLUE if primary else '#e9f0fb',fg=WHITE if primary else '#1d4ed8')

class PDFWindow(tk.Toplevel):
    def __init__(self,master,db_path,path):
        super().__init__(master); self.title('PDF 頁面與擷取'); self.geometry('1120x780'); self.minsize(900,650); self.configure(bg=BG)
        self.db_path=Path(db_path); self.path=path; self.events=queue.Queue(); self.busy=False; self.loaded=None; self.result=None; self.photo=None
        self.page=tk.StringVar(value='1'); self.image_index=tk.StringVar(value='1'); self.language=tk.StringVar(); self.kind=tk.StringVar(value='OCR 文字'); self.status=tk.StringVar(value='開啟 PDF 頁面預覽中…')
        head=tk.Frame(self,bg=BG); head.pack(fill='x',padx=20,pady=(16,6))
        lab(head,'PDF 頁面與擷取',bg=BG,font=(FONT,16,'bold')).pack(side='left')
        lab(self,Path(path).name,bg=BG,fg=MUTED,wraplength=1050,anchor='w').pack(fill='x',padx=20)
        bottom=tk.Frame(self,bg=BG); bottom.pack(side='bottom',fill='x',padx=20,pady=(0,12))
        lab(bottom,textvariable=self.status,bg=BG,fg=MUTED,anchor='w').pack(fill='x')
        controls=tk.Frame(self,bg=WHITE); controls.pack(fill='x',padx=20,pady=10)
        lab(controls,'頁碼').pack(side='left',padx=(12,5),pady=10)
        tk.Entry(controls,textvariable=self.page,width=6,font=(FONT,10)).pack(side='left',ipady=4)
        btn(controls,'顯示頁面',self.render,True).pack(side='left',padx=8)
        btn(controls,'批次 OCR 與校對',self.open_batch).pack(side='right',padx=12)
        self.page_count=tk.StringVar(value='')
        lab(controls,textvariable=self.page_count,fg=MUTED).pack(side='left')
        body=tk.PanedWindow(self,orient='horizontal',bg=BG,bd=0,sashwidth=8); body.pack(fill='both',expand=True,padx=20,pady=(0,10))
        left=tk.Frame(body,bg=WHITE,highlightbackground='#dbe4ef',highlightthickness=1); right=tk.Frame(body,bg=WHITE,highlightbackground='#dbe4ef',highlightthickness=1)
        body.add(left,minsize=350,stretch='always'); body.add(right,minsize=350,stretch='always')
        lab(left,'頁面圖片',font=(FONT,12,'bold')).pack(anchor='w',padx=14,pady=10)
        imgbar=tk.Frame(left,bg=WHITE); imgbar.pack(side='bottom',fill='x',padx=12,pady=(0,10))
        lab(imgbar,'內嵌圖片編號').pack(side='left'); tk.Entry(imgbar,textvariable=self.image_index,width=4,font=(FONT,10)).pack(side='left',padx=5,ipady=3)
        btn(imgbar,'擷取並儲存圖片',self.image_extract).pack(side='left')
        self.picture=lab(left,'頁面載入中',bg='#edf2f7',fg=MUTED); self.picture.pack(fill='both',expand=True,padx=12,pady=(0,12))
        lab(right,'文字與表格',font=(FONT,12,'bold')).pack(anchor='w',padx=14,pady=(10,4))
        lab(right,'結果可能有辨識錯誤；請校對後再加入搜尋。原始 PDF 不會被修改。',fg=MUTED,font=(FONT,9),wraplength=500,justify='left').pack(anchor='w',padx=14)
        tools=tk.Frame(right,bg=WHITE); tools.pack(fill='x',padx=14,pady=7)
        self.langbox=ttk.Combobox(tools,textvariable=self.language,state='readonly',width=22); self.langbox.pack(side='left')
        btn(tools,'OCR 此頁',self.ocr).pack(side='left',padx=6); btn(tools,'擷取表格',self.tables).pack(side='left')
        btn(tools,'掃描表格 OCR',self.scan_table).pack(side='left',padx=6)
        self.language_note=tk.StringVar()
        lab(right,textvariable=self.language_note,fg=MUTED,font=(FONT,9),wraplength=500,justify='left').pack(anchor='w',padx=14,pady=(0,4))
        bottomright=tk.Frame(right,bg=WHITE); bottomright.pack(side='bottom',fill='x',padx=14,pady=(0,12))
        btn(bottomright,'校對後儲存並加入搜尋',self.save,True).pack(side='right')
        ttk.Combobox(bottomright,textvariable=self.kind,values=('OCR 文字','表格','掃描表格'),state='readonly',width=10).pack(side='left')
        btn(bottomright,'載入已儲存內容',self.load_saved).pack(side='left',padx=6)
        btn(bottomright,'校對歷史',self.history).pack(side='left')
        textframe=tk.Frame(right,bg=WHITE); textframe.pack(fill='both',expand=True,padx=14,pady=(0,10))
        self.text=tk.Text(textframe,wrap='word',font=(FONT,10),fg=INK,bg='#f8fafc',relief='flat',padx=10,pady=10)
        scroll=ttk.Scrollbar(textframe,command=self.text.yview); self.text.configure(yscrollcommand=scroll.set); self.text.pack(side='left',fill='both',expand=True); scroll.pack(side='right',fill='y')
        self.tessdata=workspace.application_base()/'data'/'ocr'/'tessdata'
        try: langs=pdf_features.languages(self.tessdata)
        except Exception: langs=[]
        self.langbox.configure(values=langs)
        traditional=next((x for x in langs if x.lower().startswith(('windows:zh-hant','windows:zh-tw','windows:zh-hk'))),None)
        simplified=next((x for x in langs if x.lower().startswith(('windows:zh-hans','windows:zh-cn','windows:zh-sg'))),None)
        if langs: self.language.set(traditional or ('chi_tra' if 'chi_tra' in langs else 'eng' if 'eng' in langs else langs[0]))
        missing=[]
        if 'chi_tra' not in langs and not traditional: missing.append('繁體中文')
        if 'chi_sim' not in langs and not simplified: missing.append('簡體中文')
        self.language_note.set(('尚未安裝：'+'、'.join(missing)+' OCR 語言資料。' if missing else '已偵測到繁、簡中文 OCR 語言資料。')+'本程式不會自動下載。')
        self.protocol('WM_DELETE_WINDOW',self.close); self.after(150,self.poll); self.render()

    def _start(self,kind,work):
        if self.busy: return
        self.busy=True; self.status.set('正在本地處理，請稍候…')
        def wrapped():
            try: self.events.put((kind,work()))
            except Exception as e: self.events.put(('error',str(e)))
        threading.Thread(target=wrapped,daemon=True).start()
    def render(self):
        try: page=int(self.page.get())
        except ValueError: messagebox.showerror('頁碼錯誤','請輸入正整數頁碼。',parent=self); return
        if page<1: messagebox.showerror('頁碼錯誤','請輸入正整數頁碼。',parent=self); return
        def work():
            data,sha=pdf_features.load_source(self.db_path,self.path); png,count=pdf_features.render_page(data,page)
            return data,sha,page,png,count
        self._start('render',work)
    def _valid_page(self):
        if not self.loaded: messagebox.showinfo('請先顯示頁面','請先顯示要處理的 PDF 頁面。',parent=self); return False
        try: same=int(self.page.get())==self.loaded[2]
        except ValueError: same=False
        if not same: messagebox.showinfo('請重新顯示頁面','頁碼已變更，請先按「顯示頁面」。',parent=self)
        return same
    def ocr(self):
        if not self._valid_page(): return
        language=self.language.get()
        if not language: messagebox.showinfo('OCR 尚未就緒','沒有可用的本機 OCR 語言資料；程式不會自動下載。',parent=self); return
        data,sha,page,png,count=self.loaded
        def work(): return sha,page,'ocr',pdf_features.run_ocr(png,language,self.db_path.parent/'cache',self.tessdata)
        self._start('extracted',work)
    def open_batch(self):
        if not self.language.get(): messagebox.showinfo('OCR 尚未就緒','沒有可用的本機 OCR 語言資料。',parent=self); return
        import batch_ui
        batch_ui.BatchWindow(self,self.db_path,self.path,self.language.get(),self.tessdata)
    def tables(self):
        if not self._valid_page(): return
        data,sha,page,png,count=self.loaded
        def work():
            text,number=pdf_features.extract_tables(data,page)
            if not number or not text: raise ValueError('此頁未找到可擷取的表格；影像型表格需要其他處理方式')
            return sha,page,'table',text
        self._start('extracted',work)
    def scan_table(self):
        if not self._valid_page(): return
        language=self.language.get()
        if not language: messagebox.showinfo('OCR 尚未就緒','沒有可用的本機 OCR 語言資料。',parent=self); return
        _,sha,page,png,_=self.loaded
        def work(): return sha,page,'scanned_table',pdf_features.scanned_table(png,language,self.db_path.parent/'cache',self.tessdata)
        self._start('extracted',work)
    def image_extract(self):
        if not self._valid_page(): return
        try: index=int(self.image_index.get())
        except ValueError: messagebox.showerror('圖片編號錯誤','請輸入正整數圖片編號。',parent=self); return
        data,sha,page,png,count=self.loaded
        def work():
            current,current_sha=pdf_features.load_source(self.db_path,self.path)
            if current_sha!=sha: raise ValueError('來源已變更，請重新顯示頁面')
            return pdf_features.extract_embedded_image(current,page,index)
        self._start('image',work)
    def load_saved(self):
        if not self._valid_page(): return
        _,sha,page,_,_=self.loaded; kind={'OCR 文字':'ocr','表格':'table','掃描表格':'scanned_table'}[self.kind.get()]
        db=core.connect(str(self.db_path))
        try: row=db.execute('''SELECT c.text FROM pdf_derived c JOIN documents d ON d.id=c.document_id
              WHERE d.path=? AND c.source_sha256=? AND c.page=? AND c.kind=?''',(self.path,sha,page,kind)).fetchone()
        finally: db.close()
        if not row: messagebox.showinfo('沒有已儲存內容','此頁尚未儲存這種類型的內容。',parent=self); return
        self.result=(sha,page,kind); self.text.delete('1.0','end'); self.text.insert('1.0',row[0]); self.status.set('已載入先前校對的衍生內容。')
    def save(self):
        if not self._valid_page() or not self.result: return
        sha,page,kind=self.result
        if page!=self.loaded[2]: messagebox.showerror('頁面不一致','請重新擷取目前頁面。',parent=self); return
        text=self.text.get('1.0','end').strip()
        def work(): return pdf_features.save_derivative(self.db_path,self.path,sha,page,kind,text)
        self._start('saved',work)
    def history(self):
        if not self._valid_page(): return
        _,sha,page,_,_=self.loaded; kind={'OCR 文字':'ocr','表格':'table','掃描表格':'scanned_table'}[self.kind.get()]
        db=core.connect(str(self.db_path))
        try:
            rows=db.execute('''SELECT r.id,r.recorded_at,r.text FROM pdf_derived_revisions r
              JOIN pdf_derived c ON c.id=r.derived_id JOIN documents d ON d.id=c.document_id
              WHERE d.path=? AND c.source_sha256=? AND c.page=? AND c.kind=? ORDER BY r.id DESC''',(self.path,sha,page,kind)).fetchall()
        finally: db.close()
        if not rows: messagebox.showinfo('校對歷史','此頁目前沒有舊版校對文字。',parent=self); return
        w=tk.Toplevel(self); w.title('校對歷史'); w.geometry('760x550'); w.transient(self)
        lab(w,'選擇歷史版本，再載入編輯；載入不會立即覆蓋現有內容。',wraplength=700).pack(anchor='w',padx=16,pady=12)
        listing=ttk.Treeview(w,columns=('time',),show='headings',height=5); listing.heading('time',text='保留時間'); listing.pack(fill='x',padx=16)
        for i,(_,when,_) in enumerate(rows): listing.insert('','end',iid=str(i),values=(when,))
        preview=tk.Text(w,wrap='word',font=(FONT,10),bg='#f8fafc',bd=0); preview.pack(fill='both',expand=True,padx=16,pady=10)
        def show(event=None):
            selected=listing.selection()
            if selected: preview.delete('1.0','end'); preview.insert('1.0',rows[int(selected[0])][2])
        def restore():
            selected=listing.selection()
            if not selected: return
            self.result=(sha,page,kind); self.text.delete('1.0','end'); self.text.insert('1.0',rows[int(selected[0])][2]); w.destroy()
            self.status.set('已載入舊版；確認後按儲存才會更新搜尋內容。')
        listing.bind('<<TreeviewSelect>>',show); btn(w,'載入此版本編輯',restore,True).pack(anchor='e',padx=16,pady=(0,12))
    def poll(self):
        try:
            while True:
                kind,value=self.events.get_nowait(); self.busy=False
                if kind=='error': self.status.set('操作未完成：'+value); messagebox.showerror('無法處理 PDF',value,parent=self)
                elif kind=='render':
                    self.loaded=value; self.result=None; self.text.delete('1.0','end'); png=value[3]
                    image=tk.PhotoImage(data=png); factor=max(1,math.ceil(image.width()/460),math.ceil(image.height()/510))
                    self.photo=image.subsample(factor,factor); self.picture.configure(image=self.photo,text='')
                    self.page_count.set(f'共 {value[4]} 頁'); self.status.set('頁面預覽完成；OCR 與表格結果請校對。')
                elif kind=='extracted':
                    sha,page,result_kind,text=value; self.result=(sha,page,result_kind); self.kind.set({'ocr':'OCR 文字','table':'表格','scanned_table':'掃描表格'}[result_kind])
                    self.text.delete('1.0','end'); self.text.insert('1.0',text)
                    self.status.set('已擷取；請核對並按「校對後儲存並加入搜尋」。' if text else '此頁未辨識出文字。')
                elif kind=='saved':
                    self.status.set(value+' 已儲存至 APP 資料庫並可搜尋。')
                    if hasattr(self.master,'refresh'): self.master.refresh()
                    if hasattr(self.master.master,'refresh_metrics'): self.master.master.refresh_metrics()
                elif kind=='image':
                    image,extension,count=value
                    path=filedialog.asksaveasfilename(parent=self,title=f'儲存內嵌圖片（此頁共 {count} 張）',defaultextension=extension,filetypes=[('圖片','*'+extension)])
                    if path:
                        try:
                            roots=workspace.protected_roots(workspace.application_base(),self.db_path)
                            pdf_features.save_image(self.db_path,path,image,roots); self.status.set(f'圖片已儲存；此頁共 {count} 張內嵌圖片。')
                        except Exception as e: messagebox.showerror('無法儲存圖片',str(e),parent=self)
        except queue.Empty: pass
        if self.winfo_exists(): self.after(150,self.poll)
    def close(self):
        if self.busy: messagebox.showinfo('處理中','請等待此頁處理完成後再關閉。',parent=self); return
        if any(getattr(child,'busy',False) for child in self.winfo_children()): messagebox.showinfo('批次處理中','請先取消或等待批次辨識完成。',parent=self); return
        self.destroy()
