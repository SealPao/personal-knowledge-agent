import tkinter as tk
from tkinter import ttk,messagebox
from pathlib import Path
import budget

FONT='Microsoft JhengHei UI'; WHITE='#ffffff'; INK='#1b2c40'; MUTED='#64748b'; BLUE='#2563eb'
def lab(p,text='',**kw):
    kw.setdefault('font',(FONT,10)); kw.setdefault('bg',WHITE); kw.setdefault('fg',INK)
    return tk.Label(p,text=text,**kw)
def btn(p,text,command,primary=False):
    return tk.Button(p,text=text,command=command,bd=0,padx=12,pady=6,font=(FONT,10,'bold'),cursor='hand2',bg=BLUE if primary else '#e9f0fb',fg=WHITE if primary else '#1d4ed8')

class BudgetWindow(tk.Toplevel):
    def __init__(self,master,db_path,model):
        super().__init__(master); self.title('APP 預算與用量'); self.geometry('860x720'); self.configure(bg=WHITE)
        self.db=Path(db_path); self.model=model; self.rows=[]
        cfg=budget.get_settings(self.db); price=cfg.get('prices',{}).get(model,{})
        self.day=tk.StringVar(value=str(cfg.get('daily','0'))); self.month=tk.StringVar(value=str(cfg.get('monthly','0')))
        self.inprice=tk.StringVar(value=str(price.get('input','0'))); self.outprice=tk.StringVar(value=str(price.get('output','0')))
        self.webprice=tk.StringVar(value=str(cfg.get('web_search_price','0')))
        head=tk.Frame(self,bg=WHITE); head.pack(fill='x',padx=20,pady=(15,5))
        lab(head,'APP 預算與用量',font=(FONT,16,'bold')).pack(side='left')
        lab(self,'僅統計這個 APP 的 OpenAI 請求；不是官方帳戶用量、帳單或餘額。',fg=MUTED).pack(anchor='w',padx=20)
        lab(self,'模型單價需自行確認並填寫。預留額是估算；錯誤價格、其他費用或其他 APP 仍可能使實際費用不同。',fg=MUTED,wraplength=800,justify='left').pack(anchor='w',padx=20,pady=(3,10))
        form=tk.Frame(self,bg=WHITE); form.pack(fill='x',padx=20)
        def field(parent,text,var):
            box=tk.Frame(parent,bg=WHITE); box.pack(side='left',fill='x',expand=True,padx=(0,10))
            lab(box,text,font=(FONT,9,'bold')).pack(anchor='w'); tk.Entry(box,textvariable=var,font=(FONT,10),relief='solid',bd=1).pack(fill='x',ipady=4,pady=4)
        field(form,'每日預算 USD（0 = 不限制）',self.day); field(form,'每月預算 USD（0 = 不限制）',self.month)
        lab(self,'目前設定模型：'+model,font=(FONT,10,'bold')).pack(anchor='w',padx=20,pady=(8,2))
        prices=tk.Frame(self,bg=WHITE); prices.pack(fill='x',padx=20)
        field(prices,'每 100 萬輸入 Token 的 USD',self.inprice); field(prices,'每 100 萬輸出 Token 的 USD',self.outprice)
        webrow=tk.Frame(self,bg=WHITE); webrow.pack(fill='x',padx=20)
        field(webrow,'每次網路搜尋的 USD（依官方價格自行填寫）',self.webprice)
        controls=tk.Frame(self,bg=WHITE); controls.pack(fill='x',padx=20,pady=(5,10))
        btn(controls,'儲存預算與價格',self.save,True).pack(side='left')
        btn(controls,'排除選取的未確認紀錄',self.exclude).pack(side='left',padx=10)
        self.total_text=tk.StringVar(); lab(self,textvariable=self.total_text,fg=MUTED).pack(anchor='w',padx=20,pady=(0,3))
        lab(self,'近期紀錄（預留／未定價不等於實際費用）',font=(FONT,11,'bold')).pack(anchor='w',padx=20,pady=(3,5))
        frame=tk.Frame(self,bg=WHITE); frame.pack(fill='both',expand=True,padx=20,pady=(0,15))
        self.tree=ttk.Treeview(frame,columns=('date','model','status','tokens','cost'),show='headings')
        for col,title,width in [('date','日期',100),('model','模型',170),('status','狀態',150),('tokens','輸入／輸出',130),('cost','估算 USD',110)]:
            self.tree.heading(col,text=title); self.tree.column(col,width=width,minwidth=60)
        bar=ttk.Scrollbar(frame,command=self.tree.yview); self.tree.configure(yscrollcommand=bar.set); self.tree.pack(side='left',fill='both',expand=True); bar.pack(side='right',fill='y')
        self.refresh()
    def save(self):
        try: budget.save_settings(self.db,self.day.get(),self.month.get(),self.model,self.inprice.get(),self.outprice.get(),self.webprice.get()); self.refresh(); messagebox.showinfo('已儲存','預算、模型單價與網路搜尋單價已儲存。',parent=self)
        except Exception as e: messagebox.showerror('無法儲存',str(e),parent=self)
    def refresh(self):
        self.rows=budget.recent(self.db); self.tree.delete(*self.tree.get_children())
        daily,monthly,missing=budget.totals(self.db)
        cfg=budget.get_settings(self.db)
        def remaining(value,used):
            try:
                cap=budget.D(str(value))
                return '未限制' if cap<=0 else '$'+str(max(budget.D(0),cap-budget.D(used)/budget.NANO).quantize(budget.D('0.000001')))
            except Exception: return '設定錯誤'
        self.total_text.set(f'本日估算／預留：${daily/1_000_000_000:.6f}，APP 預算剩餘 {remaining(cfg.get("daily","0"),daily)}\n本月：${monthly/1_000_000_000:.6f}，APP 預算剩餘 {remaining(cfg.get("monthly","0"),monthly)}；未定價／未確認 {missing} 筆')
        names={'pending':'處理中／預留','unknown':'未確認','unpriced':'未定價','complete':'已估算'}
        for i,(day,model,status,inp,out,cost,excluded,ident) in enumerate(self.rows):
            state=('已排除 · ' if excluded else '')+names.get(status,status)
            amount='未定價' if status=='unpriced' or (status in ('unknown','pending') and cost==0) else f'{cost/1_000_000_000:.6f}'
            self.tree.insert('','end',iid=str(i),values=(day,model,state,f'{inp if inp is not None else "—"} / {out if out is not None else "—"}',amount))
    def exclude(self):
        selected=self.tree.selection()
        if not selected: return
        row=self.rows[int(selected[0])]
        if row[2] not in ('unknown','pending','unpriced'):
            messagebox.showinfo('無法排除','只能排除未確認、未定價或卡住的預留紀錄。',parent=self); return
        if messagebox.askyesno('排除紀錄','這只會讓 APP 預算不再計算這筆紀錄；官方仍可能已收費。確定排除嗎？',parent=self):
            budget.exclude(self.db,row[7]); self.refresh()
