"""User-initiated web research UI; reports are never executed as commands."""
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk,messagebox
import adb_research,adb_service,ai_service,ai_ui

FONT='Microsoft JhengHei UI'; WHITE='#ffffff'; INK='#1b2c40'; MUTED='#64748b'
def label(p,text='',**kw):
    kw.setdefault('font',(FONT,10)); kw.setdefault('bg',WHITE); kw.setdefault('fg',INK); return tk.Label(p,text=text,**kw)
def button(p,text,fn):
    return tk.Button(p,text=text,command=fn,font=(FONT,10,'bold'),bg='#e9f0fb',fg='#1d4ed8',bd=0,padx=10,pady=6,cursor='hand2')

class ResearchWindow(tk.Toplevel):
    def __init__(self,master,db,device_serial=None):
        super().__init__(master); self.db=db; self.device_serial=device_serial; self.busy=False; self.events=queue.Queue(); self.title('ADB 指令研究模式'); self.geometry('850x720'); self.configure(bg=WHITE)
        label(self,'ADB 指令研究模式',font=(FONT,16,'bold')).pack(anchor='w',padx=20,pady=(16,4))
        label(self,'查詢公開資料並保存待驗證筆記；不會自動讀取裝置或執行研究指令。',fg=MUTED,wraplength=780,justify='left').pack(anchor='w',padx=20)
        fields=tk.Frame(self,bg=WHITE); fields.pack(fill='x',padx=20,pady=10)
        self.brand=tk.StringVar(); self.model=tk.StringVar(); self.firmware=tk.StringVar(); self.question=tk.StringVar(value='如何透過 ADB 查詢或調整前光與刷新模式？')
        for col,(title,var) in enumerate([('品牌（可自行輸入）',self.brand),('型號（可留空）',self.model),('韌體／Android（可留空）',self.firmware)]):
            frame=tk.Frame(fields,bg=WHITE); frame.grid(row=0,column=col,sticky='ew',padx=(0,8)); fields.columnconfigure(col,weight=1)
            label(frame,title).pack(anchor='w')
            if col==0: ttk.Combobox(frame,textvariable=var,values=('BOOX','HyRead','Meebook'),state='normal').pack(fill='x',pady=4)
            else: tk.Entry(frame,textvariable=var,font=(FONT,10)).pack(fill='x',pady=4,ipady=3)
        label(self,'研究問題',font=(FONT,10,'bold')).pack(anchor='w',padx=20)
        tk.Entry(self,textvariable=self.question,font=(FONT,11)).pack(fill='x',padx=20,pady=5,ipady=5)
        row=tk.Frame(self,bg=WHITE); row.pack(fill='x',padx=20,pady=8)
        button(row,'搜尋並建立研究筆記',self.start).pack(side='left')
        button(row,'讀取已選裝置資訊',self.identify).pack(side='left',padx=6)
        self.status=tk.StringVar(value='研究需要 OpenAI 外網與支援網路搜尋的模型；搜尋費用可能另外計算。'); label(row,textvariable=self.status,fg=MUTED,wraplength=520,justify='left').pack(side='left',padx=12)
        self.text=tk.Text(self,wrap='word',font=(FONT,10),bg='#f8fafc',fg=INK,bd=0,padx=12,pady=12); self.text.pack(fill='both',expand=True,padx=20,pady=(0,20)); self.text.configure(state='disabled')
        self.after(150,self.poll); self.protocol('WM_DELETE_WINDOW',self.close)
    def identify(self):
        if self.busy: return
        if not self.device_serial:
            messagebox.showinfo('請先選擇裝置','請在閱讀器連線畫面先偵測並選擇已授權的裝置。',parent=self); return
        self.busy=True; self.status.set('正在唯讀取得品牌、型號及韌體…')
        def work():
            try: self.events.put(('device_info',adb_service.device_info(self.device_serial)))
            except Exception as e: self.events.put(('error',str(e)))
        threading.Thread(target=work,daemon=True).start()
    def start(self):
        if self.busy: return
        brand=self.brand.get().strip(); model=self.model.get().strip(); firmware=self.firmware.get().strip(); question=self.question.get().strip(); config=ai_ui.read_config(self.db.parent/'ai-settings.json')
        if not brand or not question: messagebox.showinfo('請輸入品牌及問題','請先填寫品牌和要研究的問題。',parent=self); return
        if config['provider']!='openai' or not config['network_enabled']:
            messagebox.showinfo('研究模式需要外網','請在 AI 設定選擇 OpenAI 並開啟外網；本地視覺模型不會自動執行網路搜尋。',parent=self); return
        detail=f'品牌：{brand}\n型號：{model or "未提供"}\n韌體：{firmware or "未提供"}\n問題：{question}'
        if not messagebox.askyesno('確認網路研究',detail+'\n\n上述內容將傳送至 OpenAI，並用網路搜尋公開資料；可能產生模型及搜尋費用。沒有包含閱讀器截圖或文件。要開始嗎？',parent=self): return
        self.busy=True; self.status.set('正在研究，請稍候…')
        def work():
            try:
                if ai_ui.read_config(self.db.parent/'ai-settings.json')!=config: raise ValueError('AI 設定已變更，未傳送')
                key=ai_service.load_key(); result=adb_research.research(self.db,brand,model,firmware,question,config,key)
                path=adb_research.save(self.db,result); self.events.put(('done',(result,path)))
            except Exception as e: self.events.put(('error',str(e)))
        threading.Thread(target=work,daemon=True).start()
    def poll(self):
        try:
            while True:
                kind,value=self.events.get_nowait(); self.busy=False
                if kind=='error': self.status.set('操作未完成：'+value)
                elif kind=='device_info':
                    self.brand.set(value['brand']); self.model.set(value['model'])
                    self.firmware.set(' / '.join(x for x in (('Android '+value['android']) if value['android'] else '',value['firmware']) if x))
                    self.status.set('已讀取裝置資訊；請確認內容後再選擇是否搜尋。')
                else:
                    result,path=value; self.text.configure(state='normal'); self.text.delete('1.0','end'); self.text.insert('end',result['text']+'\n\n來源（點擊開啟）：\n')
                    for i,source in enumerate(result['sources'],1):
                        tag='source'+str(i); marker=f'[{i}]'; pos='1.0'
                        while True:
                            pos=self.text.search(marker,pos,stopindex='end')
                            if not pos: break
                            end=f'{pos}+{len(marker)}c'; self.text.tag_add(tag,pos,end); pos=end
                        self.text.insert('end',f'[{i}] {source["title"]}\n{source["url"]}\n\n',tag)
                        self.text.tag_configure(tag,foreground='#1d4ed8',underline=True); self.text.tag_bind(tag,'<Button-1>',lambda e,url=source['url']:os.startfile(url))
                    if not result['sources']: self.text.insert('end','沒有可驗證的來源；所有內容均為未驗證。')
                    self.text.configure(state='disabled')
                    cost={'complete':'費用已依自行設定單價估算','unpriced':'單價未設定，費用未定價','unknown':'用量或搜尋次數未確認'}.get(result.get('cost_status'),'費用狀態未確認')
                    self.status.set('已保存待驗證筆記；'+cost+'，請核對官方用量。')
                    if hasattr(self.master,'refresh_metrics'): self.master.refresh_metrics()
        except queue.Empty: pass
        if self.winfo_exists(): self.after(150,self.poll)
    def close(self):
        if self.busy: messagebox.showinfo('研究中','請等待本次研究結束。',parent=self); return
        self.destroy()
