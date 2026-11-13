import os,queue,sys,threading,tkinter as tk
from tkinter import filedialog,messagebox
from pathlib import Path
import installer_core

FONT='Microsoft JhengHei UI'; WHITE='#ffffff'; INK='#1b2c40'; MUTED='#64748b'; BLUE='#2563eb'
def label(p,text='',**kw):
    kw.setdefault('font',(FONT,10)); kw.setdefault('bg',WHITE); kw.setdefault('fg',INK); return tk.Label(p,text=text,**kw)
def button(p,text,cmd,primary=False):
    return tk.Button(p,text=text,command=cmd,bd=0,padx=14,pady=8,font=(FONT,10,'bold'),bg=BLUE if primary else '#e9f0fb',fg=WHITE if primary else '#1d4ed8',cursor='hand2')
def program_base(): return Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent
def payload_base(): return Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent))/'payload'

class Installer(tk.Tk):
    def __init__(self):
        super().__init__(); self.title('安裝私人知識助理'); self.geometry('760x680'); self.minsize(680,600); self.configure(bg=WHITE)
        local=Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData'/'Local')))
        self.program=tk.StringVar(value=str(local/'Programs'/'PersonalKnowledgeAgent'))
        self.home=tk.StringVar(value=str(local/'PersonalKnowledgeAgentData'))
        base=program_base(); self.prototype=tk.StringVar(value=str(base) if (base/'data'/'knowledge.sqlite3').is_file() else '')
        self.status=tk.StringVar(value='確認位置後開始安裝。'); self.events=queue.Queue(); self.busy=False
        body=tk.Frame(self,bg=WHITE); body.pack(fill='both',expand=True,padx=28,pady=24)
        label(body,'安裝私人知識助理',font=(FONT,19,'bold')).pack(anchor='w')
        label(body,'程式與知識庫分開存放；更新程式不會覆蓋知識庫。',fg=MUTED).pack(anchor='w',pady=(5,14))
        def field(text,var):
            label(body,text,font=(FONT,10,'bold')).pack(anchor='w',pady=(10,4))
            row=tk.Frame(body,bg=WHITE); row.pack(fill='x')
            tk.Entry(row,textvariable=var,font=(FONT,10),relief='solid',bd=1).pack(side='left',fill='x',expand=True,ipady=5)
            button(row,'選擇',lambda:self.choose(var)).pack(side='left',padx=(8,0))
        field('程式安裝位置',self.program); field('知識庫資料位置',self.home); field('複製既有原型知識庫（選填：原型 APP 資料夾）',self.prototype)
        label(body,'若資料位置已有知識庫，安裝只更新程式並保留原資料。若選擇原型資料夾，會複製索引與工作副本，原始文件不會移動。',fg=MUTED,justify='left',wraplength=680).pack(anchor='w',pady=(18,7))
        label(body,'本安裝程式尚未簽章，也尚未提供自動更新及系統解除安裝登錄。簡體中文 OCR 仍需此電腦已安裝 Tesseract。',fg=MUTED,justify='left',wraplength=680).pack(anchor='w')
        actions=tk.Frame(body,bg=WHITE); actions.pack(side='bottom',fill='x',pady=(0,4))
        self.install_button=button(actions,'開始安裝',self.start,True); self.install_button.pack(side='right')
        label(actions,textvariable=self.status,fg=MUTED,wraplength=500,justify='left').pack(side='left')
        self.protocol('WM_DELETE_WINDOW',self.close); self.after(150,self.poll)
    def choose(self,var):
        selected=filedialog.askdirectory(parent=self)
        if selected: var.set(selected)
    def start(self):
        if self.busy: return
        program=self.program.get().strip(); home=self.home.get().strip(); prototype=self.prototype.get().strip() or None
        if not program or not home or not Path(program).is_absolute() or not Path(home).is_absolute(): messagebox.showerror('位置錯誤','請使用絕對路徑。',parent=self); return
        base=payload_base(); executable=base/'PersonalKnowledgeAgent.exe'; model=base/'chi_sim.traineddata'
        self.busy=True; self.install_button.configure(state='disabled'); self.status.set('安裝中，請稍候…')
        def work():
            try: self.events.put(('done',installer_core.install(executable,program,home,model if model.exists() else None,prototype,program_base())))
            except Exception as e: self.events.put(('error',str(e)))
        threading.Thread(target=work,daemon=True).start()
    def poll(self):
        try:
            while True:
                kind,value=self.events.get_nowait(); self.busy=False; self.install_button.configure(state='normal')
                if kind=='error': self.status.set('安裝未完成。'); messagebox.showerror('安裝失敗',value,parent=self)
                else:
                    self.status.set('安裝完成；資料保存在獨立位置。')
                    if messagebox.askyesno('安裝完成',f'程式：{value["program"]}\n資料：{value["data"]}\n\n現在開啟應用程式嗎？',parent=self): os.startfile(value['program'])
        except queue.Empty: pass
        self.after(150,self.poll)
    def close(self):
        if self.busy: messagebox.showinfo('安裝中','請等待目前的安裝完成。',parent=self); return
        self.destroy()
if __name__=='__main__': Installer().mainloop()
