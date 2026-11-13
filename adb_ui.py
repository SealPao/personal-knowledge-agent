"""User-initiated device observation; no autonomous device actions."""
import io
import queue
import threading
import tkinter as tk
from tkinter import ttk,messagebox
from PIL import Image,ImageTk,ImageDraw
import adb_service
import ai_service
import device_agent
import budget
import ai_ui

FONT='Microsoft JhengHei UI'; WHITE='#ffffff'; INK='#1b2c40'; MUTED='#64748b'; BLUE='#2563eb'
def label(p,text='',**kw):
    kw.setdefault('font',(FONT,10)); kw.setdefault('bg',WHITE); kw.setdefault('fg',INK); return tk.Label(p,text=text,**kw)
def button(p,text,fn):
    return tk.Button(p,text=text,command=fn,font=(FONT,10,'bold'),bg='#e9f0fb',fg='#1d4ed8',bd=0,padx=12,pady=7,cursor='hand2')
def show_image(parent,png,title='閱讀器截圖'):
    with Image.open(io.BytesIO(png)) as image:
        if image.width*image.height>25_000_000: raise ValueError('截圖尺寸過大')
        image.thumbnail((760,560)); photo=ImageTk.PhotoImage(image.copy())
    w=tk.Toplevel(parent); w.title(title); w.configure(bg=WHITE)
    widget=label(w,image=photo); widget.image=photo; widget.pack(padx=16,pady=16)
    return w

def show_action(parent,png,item):
    with Image.open(io.BytesIO(png)) as original:
        im=original.convert('RGB'); draw=ImageDraw.Draw(im); w,h=im.size
        def point(x,y): return (round(x*(w-1)/1000),round(y*(h-1)/1000))
        if item['type']=='tap':
            x,y=point(item['x'],item['y']); r=max(8,min(w,h)//30); draw.ellipse((x-r,y-r,x+r,y+r),outline='red',width=max(3,r//5))
        elif item['type']=='swipe':
            start=point(item['x1'],item['y1']); end=point(item['x2'],item['y2']); draw.line((start,end),fill='red',width=max(4,min(w,h)//100)); r=max(7,min(w,h)//40); draw.ellipse((end[0]-r,end[1]-r,end[0]+r,end[1]+r),outline='red',width=4)
        buf=io.BytesIO(); im.save(buf,format='PNG')
    return show_image(parent,buf.getvalue(),'本次操作位置（紅色標記）')

def show_captures(parent,images):
    w=tk.Toplevel(parent); w.title('閱讀器操作截圖'); w.configure(bg=WHITE)
    index=tk.IntVar(value=0); status=tk.StringVar(); pic=label(w); pic.pack(padx=16,pady=10)
    row=tk.Frame(w,bg=WHITE); row.pack(pady=10); label(row,textvariable=status).pack(side='left',padx=12)
    def display(delta=0):
        index.set(max(0,min(len(images)-1,index.get()+delta)))
        with Image.open(io.BytesIO(images[index.get()])) as im:
            if im.width*im.height>25_000_000: raise ValueError('截圖尺寸過大')
            im.thumbnail((760,560)); photo=ImageTk.PhotoImage(im.copy())
        pic.configure(image=photo); pic.image=photo; status.set(f'{index.get()+1} / {len(images)}')
    button(row,'上一張',lambda:display(-1)).pack(side='left'); button(row,'下一張',lambda:display(1)).pack(side='left',padx=8)
    display()

def show_result(parent,answer,images,saved):
    w=tk.Toplevel(parent); w.title('閱讀器操作路徑與回答'); w.geometry('780x650'); w.configure(bg=WHITE)
    label(w,'找到的操作路徑',font=(FONT,16,'bold')).pack(anchor='w',padx=20,pady=(16,5))
    label(w,'以下是 AI 根據裝置畫面整理的回答，請核對選單、機型與實際操作結果。',fg=MUTED,wraplength=730,justify='left').pack(anchor='w',padx=20)
    bar=tk.Frame(w,bg=WHITE); bar.pack(side='bottom',fill='x',padx=20,pady=15)
    button(bar,f'查看 {len(images)} 張過程截圖',lambda:show_captures(w,images)).pack(side='left')
    label(bar,'紀錄已存入知識庫（待人工校對）' if saved else '紀錄尚未保存，請檢查操作畫面',fg=MUTED).pack(side='left',padx=12)
    text=tk.Text(w,wrap='word',font=(FONT,11),fg=INK,bg='#f8fafc',bd=0,padx=12,pady=12); text.pack(fill='both',expand=True,padx=20,pady=12)
    text.insert('1.0',answer); text.configure(state='disabled')

class DeviceWindow(tk.Toplevel):
    def __init__(self,master,db,initial_goal=''):
        super().__init__(master); self.title('閱讀器連線與紀錄'); self.geometry('780x650'); self.configure(bg=WHITE)
        self.db=db; self.busy=False; self.events=queue.Queue(); self.png=None; self.capture_serial=None; self.rows=[]; self.agent_active=False; self.history=[]; self.images=[]; self.agent_serial=None; self.agent_config=None; self.goal=''; self.session_path=None; self.agent_cancel=threading.Event()
        label(self,'閱讀器連線與紀錄',font=(FONT,16,'bold')).pack(anchor='w',padx=20,pady=(18,5))
        label(self,'AI 可根據截圖提出操作，每步確認後執行；支援點擊、滑動、返回及首頁。',fg=MUTED,wraplength=720,justify='left').pack(anchor='w',padx=20)
        row=tk.Frame(self,bg=WHITE); row.pack(fill='x',padx=20,pady=10)
        button(row,'重新偵測裝置',self.detect).pack(side='left'); button(row,'擷取所選裝置畫面',self.capture).pack(side='left',padx=8)
        button(row,'一鍵找答案',self.quick_start).pack(side='left'); button(row,'停止 AI',self.stop_agent).pack(side='left',padx=8)
        self.list=ttk.Treeview(self,columns=('serial','status','info'),show='headings',height=3,selectmode='browse')
        for name,title,width in [('serial','裝置',190),('status','狀態',100),('info','資訊',420)]: self.list.heading(name,text=title); self.list.column(name,width=width)
        self.list.pack(fill='x',padx=20)
        self.status=tk.StringVar(value='請連接閱讀器、開啟 USB 偵錯並在裝置上授權。'); label(self,textvariable=self.status,fg=MUTED,wraplength=720,justify='left').pack(anchor='w',padx=20,pady=8)
        label(self,'操作目標（AI 將依此觀察畫面）',font=(FONT,10,'bold')).pack(anchor='w',padx=20)
        self.goalvar=tk.StringVar(value=initial_goal[:1000]); tk.Entry(self,textvariable=self.goalvar,font=(FONT,11)).pack(fill='x',padx=20,pady=(3,8),ipady=4)
        self.screen_ocr=tk.BooleanVar(value=True)
        tk.Checkbutton(self,text='以本機 OCR 輔助辨識畫面文字（可能有錯字）',variable=self.screen_ocr,bg=WHITE,fg=INK,font=(FONT,9),anchor='w').pack(anchor='w',padx=20)
        label(self,'紀錄標題',font=(FONT,10,'bold')).pack(anchor='w',padx=20)
        self.titlevar=tk.StringVar(value='閱讀器操作紀錄'); tk.Entry(self,textvariable=self.titlevar,font=(FONT,11)).pack(fill='x',padx=20,pady=(3,8),ipady=4)
        label(self,'已確認的操作步驟或觀察',font=(FONT,10,'bold')).pack(anchor='w',padx=20)
        bar=tk.Frame(self,bg=WHITE); bar.pack(side='bottom',fill='x',padx=20,pady=16)
        button(bar,'研究 ADB 指令',self.open_research).pack(side='left')
        button(bar,'儲存紀錄並加入搜尋',self.save).pack(side='right'); button(bar,'預覽目前截圖',self.preview).pack(side='right',padx=8)
        self.note=tk.Text(self,wrap='word',font=(FONT,11),bg='#f8fafc',bd=1,padx=8,pady=8); self.note.pack(fill='both',expand=True,padx=20,pady=6)
        self.poll_id=self.after(150,self.poll); self.protocol('WM_DELETE_WINDOW',self.close)
    def work(self,kind,fn):
        if self.busy: return
        self.busy=True; self.status.set('正在連線，請稍候…')
        def worker():
            try: self.events.put((kind,fn()))
            except Exception as e: self.events.put(('error',str(e)))
        threading.Thread(target=worker,daemon=True).start()
    def open_research(self):
        import research_ui
        selected=self.list.selection()
        serial=self.rows[int(selected[0])][0] if selected and self.rows[int(selected[0])][1]=='device' else None
        research_ui.ResearchWindow(self.master,self.db,serial)
    def detect(self):
        if not self.agent_active: self.work('devices',adb_service.devices)
    def capture(self):
        if self.agent_active: return
        selected=self.list.selection()
        if not selected: messagebox.showinfo('選擇裝置','請先偵測並選擇已授權的裝置。',parent=self); return
        serial,state,_=self.rows[int(selected[0])]
        if state!='device': messagebox.showwarning('裝置未就緒','請在閱讀器確認 USB 偵錯授權，並重新偵測。',parent=self); return
        self.work('capture',lambda:(serial,adb_service.screenshot(serial)))
    def preview(self):
        if not self.png: messagebox.showinfo('尚無截圖','請先擷取裝置畫面。',parent=self); return
        try: show_image(self,self.png)
        except Exception as e: messagebox.showerror('無法顯示截圖',str(e),parent=self)
    def quick_start(self):
        if self.busy or self.agent_active: return
        goal=self.goalvar.get().strip()
        if not goal or len(goal)>1000: messagebox.showinfo('請填入問題','請輸入 1000 字以內的問題。',parent=self); return
        selected=self.list.selection()
        chosen=self.rows[int(selected[0])][0] if selected and int(selected[0])<len(self.rows) else None
        def prepare():
            rows=adb_service.devices(); ready=[x for x in rows if x[1]=='device']
            if chosen:
                target=next((x for x in ready if x[0]==chosen),None)
            else: target=ready[0] if len(ready)==1 else None
            return rows,target,adb_service.screenshot(target[0]) if target else None
        self.work('quick_ready',prepare)
    def stop_agent(self):
        self.agent_cancel.set(); self.agent_active=False; self.note.insert('end','\n探索已由使用者停止；未完成的目標尚待確認。\n')
        self.checkpoint(); self.status.set('已要求停止；執行中的連線結束後不會繼續操作。')
    def checkpoint(self,reviewed=False):
        if not self.images or not self.goal: return True
        try:
            self.session_path=adb_service.save_session(self.db,'AI 閱讀器探索：'+self.goal[:120],self.note.get('1.0','end'),self.images,reviewed=reviewed,existing=self.session_path)
            return True
        except Exception as e:
            self.agent_active=False; self.status.set('無法保存探索紀錄，已停止：'+str(e)); return False
    def start_agent(self):
        if self.busy or self.agent_active: return
        selected=self.list.selection(); goal=self.goalvar.get().strip()
        if not selected or self.rows[int(selected[0])][1]!='device': messagebox.showinfo('選擇裝置','請先偵測並選擇已授權的裝置。',parent=self); return
        if not self.png: messagebox.showinfo('請先截圖','請先擷取並預覽目前畫面。',parent=self); return
        if self.capture_serial!=self.rows[int(selected[0])][0]: messagebox.showinfo('裝置已變更','請重新擷取所選裝置的畫面。',parent=self); return
        if not goal or len(goal)>1000: messagebox.showinfo('請填入目標','請輸入 1000 字以內的操作目標。',parent=self); return
        config=ai_ui.read_config(self.db.parent/'ai-settings.json'); provider=config['provider']
        if provider=='openai':
            try: valid=config['network_enabled'] and bool(ai_service.load_key())
            except Exception as e: messagebox.showerror('無法讀取 Key',str(e),parent=self); return
            if not valid: messagebox.showerror('AI 尚未設定','請先開啟外網並設定 OpenAI Key，或改用本地 AI。',parent=self); return
        if provider=='local' and not config['local_model']: messagebox.showerror('AI 尚未設定','請先設定本地視覺模型。',parent=self); return
        destination='OpenAI（可能產生費用）' if provider=='openai' else '本機視覺模型'
        overlay=show_image(self,self.png,'即將提供 AI 的畫面')
        approved=messagebox.askyesno('確認傳送畫面',f'開始後會把目標、操作紀錄、每一步截圖及啟用時的畫面 OCR 文字傳送到{destination}，畫面可能含私人資料。AI 每一步操作前均須確認；最多 10 步。確定開始嗎？',parent=overlay)
        overlay.destroy()
        if not approved: return
        self.agent_cancel.clear(); self.agent_serial=self.rows[int(selected[0])][0]; self.agent_config=dict(config); self.goal=goal; self.agent_active=True; self.history=[]; self.images=[self.png]; self.session_path=None
        self.note.insert('end','\n操作目標：'+goal+'\n以下是已確認送出的操作；畫面觀察由 AI 產生，請校對。\n')
        self.note.insert('end','探索尚在進行；以下紀錄並不表示操作已完成。\n')
        if not self.checkpoint(): return
        self.next_plan()
    def next_plan(self):
        if not self.agent_active: return
        if len(self.history)>=10:
            self.agent_active=False; self.note.insert('end','\n已達步數上限，探索未完成。\n'); self.checkpoint(); self.status.set('已達 10 步上限，已保存未完成紀錄。'); return
        current=self.png; config=self.agent_config; goal=self.goal; history=list(self.history); use_ocr=self.screen_ocr.get()
        def planner():
            current_config=ai_ui.read_config(self.db.parent/'ai-settings.json')
            if current_config!=config: raise ValueError('AI 設定已變更，已停止')
            ocr=''; ocr_error=''
            if use_ocr:
                try: ocr=adb_service.screen_text(current,self.db.parent/'cache')
                except Exception: ocr_error='畫面 OCR 無法使用，本次僅使用截圖。'
            if self.agent_cancel.is_set(): raise RuntimeError('探索已停止，畫面未傳送至 AI。')
            key=ai_service.load_key() if config['provider']=='openai' else None; ticket=None
            try:
                if config['provider']=='openai': ticket=budget.reserve(self.db,goal,[{'id':1,'excerpt':str(history)+'\n'+ocr}],config['openai_model'],min(800,int(config['max_tokens'])),extra_input_tokens=8192)
                if self.agent_cancel.is_set():
                    if ticket: budget.exclude(self.db,ticket); ticket=None
                    raise RuntimeError('探索已停止，畫面未傳送至 AI。')
                result=device_agent.plan(current,goal,history,config,key,screen_text=ocr)
                if ticket: budget.finish(self.db,ticket,result[1])
                return result[0],ocr_error
            except Exception:
                if ticket: budget.uncertain(self.db,ticket)
                raise
        self.work('plan',planner)
    def apply_plan(self,item):
        if not self.agent_active: return
        if item['type']=='finish':
            self.agent_active=False
            answer=item.get('answer','').strip()
            if not answer: self.status.set('AI 未提供有效回答，請檢查紀錄。'); return
            self.note.insert('end','\nAI 回答（尚未人工校對）：\n'+answer+'\n\n最後畫面觀察：'+item['observation']+'\n'+item['reason']+'\n')
            saved=False
            try:
                saved=self.checkpoint()
                if not saved: raise ValueError('無法更新探索紀錄')
                if hasattr(self.master,'refresh_metrics'): self.master.refresh_metrics()
                self.status.set('AI 回答及截圖已儲存為待校對紀錄，可供搜尋。')
            except Exception as e:
                self.status.set('回答已產生，但紀錄未保存：'+str(e))
            show_result(self,answer,self.images,saved)
            return
        detail=f"動作：{item['type']}\n說明：{item['reason']}\n畫面觀察：{item['observation']}\n座標："+str({k:item[k] for k in ('x','y','x1','y1','x2','y2') if k in item})
        overlay=show_action(self,self.png,item)
        approved=messagebox.askyesno('確認閱讀器操作',detail+'\n\n請檢查截圖中的紅色標記；要執行這一步嗎？',parent=overlay)
        overlay.destroy()
        if not approved: self.stop_agent(); return
        before=self.png; size,digest=adb_service.image_info(before); serial=self.agent_serial
        def execute():
            adb_service.perform(serial,item,size,digest)
            try: return item,adb_service.screenshot(serial),None
            except Exception as e: return item,None,str(e)
        self.work('acted',execute)
    def save(self):
        if not self.png: messagebox.showinfo('尚無截圖','請先擷取裝置畫面。',parent=self); return
        try:
            if self.busy or self.agent_active: raise ValueError('請先停止 AI 並等待目前操作結束')
            notes=self.note.get('1.0','end').strip()
            if self.images: adb_service.save_session(self.db,self.titlevar.get(),notes,self.images,existing=self.session_path)
            else: adb_service.save_note(self.db,self.titlevar.get(),notes,self.png)
            if hasattr(self.master,'refresh_metrics'): self.master.refresh_metrics()
            self.status.set('已儲存紀錄及截圖；AI 探索紀錄仍需至文件管理中人工審核。' if self.images else '已儲存使用者紀錄及截圖；文字已加入搜尋，可供 AI 引用。')
        except Exception as e: messagebox.showerror('無法儲存紀錄',str(e),parent=self)
    def poll(self):
        try:
            while True:
                kind,value=self.events.get_nowait(); self.busy=False
                if kind=='error':
                    self.agent_active=False; self.note.insert('end','\n探索中斷，尚未確認最終答案：'+value+'\n'); self.checkpoint(); self.status.set('操作未完成：'+value)
                elif kind=='devices':
                    self.rows=value; self.list.delete(*self.list.get_children())
                    for i,row in enumerate(value): self.list.insert('','end',iid=str(i),values=row)
                    self.status.set(f'找到 {len(value)} 個裝置；狀態為 device 才可截圖。')
                elif kind=='capture': self.capture_serial,self.png=value; self.status.set('截圖已取得；請預覽並填寫步驟後儲存。')
                elif kind=='quick_ready':
                    rows,target,png=value; self.rows=rows; self.list.delete(*self.list.get_children())
                    for i,row in enumerate(rows): self.list.insert('','end',iid=str(i),values=row)
                    if not target:
                        self.status.set('請選擇已授權的裝置；若沒有裝置，請檢查連線與 USB 偵錯。')
                        messagebox.showinfo('請選擇裝置','若有多個裝置，請在清單中選擇；若沒有，請檢查連線與授權。',parent=self)
                    else:
                        index=rows.index(target); self.list.selection_set(str(index)); self.capture_serial=target[0]; self.png=png
                        self.start_agent()
                elif kind=='plan':
                    item,ocr_error=value
                    if ocr_error: self.note.insert('end','\n'+ocr_error+'\n')
                    self.apply_plan(item)
                elif kind=='acted':
                    item,after,error=value
                    if after: self.png=after; self.images.append(after)
                    self.history.append({'type':item['type'],'reason':item['reason'],'observation':item['observation']})
                    self.note.insert('end',f"\n步驟 {len(self.history)}（操作前畫面 {len(self.images)-1 if after else len(self.images)}）：{item['type']} — {item['reason']}\nAI 觀察（請核對）：{item['observation']}\n")
                    if not self.checkpoint(): continue
                    if error: self.agent_active=False; self.status.set('操作已送出，但無法取得後續畫面；請檢查裝置：'+error)
                    elif self.agent_active: self.next_plan()
                    else: self.status.set('已停止。請檢查並儲存紀錄。')
        except queue.Empty: pass
        if self.winfo_exists(): self.poll_id=self.after(150,self.poll)
    def close(self):
        if self.busy: messagebox.showinfo('處理中','請等待 ADB 操作結束。',parent=self); return
        self.destroy()
    def destroy(self):
        if getattr(self,'poll_id',None):
            try: self.after_cancel(self.poll_id)
            except tk.TclError: pass
            self.poll_id=None
        super().destroy()
