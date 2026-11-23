"""Previewable knowledge organization and diagnostics export."""
import queue
import threading
import tkinter as tk
from tkinter import ttk,messagebox,filedialog
from pathlib import Path
import analysis_export
import ai_service
import budget

FONT='Microsoft JhengHei UI'; BG='#f1f5f9'; WHITE='#ffffff'; INK='#1b2c40'; MUTED='#64748b'

def label(parent,text='',**kwargs):
    kwargs.setdefault('font',(FONT,10));kwargs.setdefault('bg',WHITE);kwargs.setdefault('fg',INK)
    return tk.Label(parent,text=text,**kwargs)

def button(parent,text,command):
    return tk.Button(parent,text=text,command=command,font=(FONT,10,'bold'),bg='#e9f0fb',fg='#1d4ed8',bd=0,padx=12,pady=7,cursor='hand2')

class OrganizeWindow(tk.Toplevel):
    def __init__(self,master,db_path,session=None):
        super().__init__(master);self.db=Path(db_path);self.session=dict(session or {});self.events=queue.Queue();self.busy=False;self.preview_ready=False
        self.title('整理知識與匯出');self.geometry('920x760');self.minsize(720,580);self.configure(bg=WHITE)
        label(self,'整理知識與匯出',font=(FONT,17,'bold')).pack(anchor='w',padx=22,pady=(16,4))
        label(self,'分析索引與指定主題，產生可編輯預覽；不修改原始文件，也不會自動上傳報告。',fg=MUTED,wraplength=850,justify='left').pack(anchor='w',padx=22)
        form=tk.Frame(self,bg=WHITE);form.pack(fill='x',padx=22,pady=(12,4))
        label(form,'主題或問題（會寫入報告；留空只統計知識庫）',font=(FONT,10,'bold')).pack(anchor='w')
        self.topic=tk.StringVar(value=str(self.session.get('question',''))[:2000]);tk.Entry(form,textvariable=self.topic,font=(FONT,11)).pack(fill='x',ipady=5,pady=4)
        self.include_chat=tk.BooleanVar(value=bool(self.session.get('question')));self.include_sources=tk.BooleanVar(value=False);self.use_ai=tk.BooleanVar(value=False)
        for text,var in [('包含最近問題與回答（可能有個資）',self.include_chat),('包含候選檔名與文件片段（可能有機密）',self.include_sources),('使用目前 AI 整理草稿（OpenAI 模式會傳送候選片段）',self.use_ai)]:
            tk.Checkbutton(form,text=text,variable=var,bg=WHITE,fg=INK,font=(FONT,9),anchor='w').pack(anchor='w')
        label(form,'AI 草稿和索引片段都可能有錯；可在下方刪改後再匯出。',fg=MUTED,font=(FONT,9)).pack(anchor='w',pady=(4,0))
        row=tk.Frame(self,bg=WHITE);row.pack(fill='x',padx=22,pady=6)
        self.preview_button=button(row,'產生預覽',self.preview);self.preview_button.pack(side='left')
        self.save_button=button(row,'儲存預覽為 Markdown',self.save);self.save_button.pack(side='left',padx=8);self.save_button.configure(state='disabled')
        self.copy_button=button(row,'複製預覽',self.copy_preview);self.copy_button.pack(side='left');self.copy_button.configure(state='disabled')
        self.status=tk.StringVar(value='可先填入主題，再選擇是否包含內容或使用 AI。');label(row,textvariable=self.status,fg=MUTED,wraplength=480,justify='left').pack(side='left',padx=8)
        area=tk.Frame(self,bg=WHITE);area.pack(fill='both',expand=True,padx=22,pady=(5,16))
        self.text=tk.Text(area,wrap='word',font=(FONT,10),fg=INK,bg='#f8fafc',bd=0,padx=12,pady=10)
        bar=ttk.Scrollbar(area,command=self.text.yview);self.text.configure(yscrollcommand=bar.set);self.text.pack(side='left',fill='both',expand=True);bar.pack(side='right',fill='y')
        self.poll_id=self.after(150,self.poll);self.protocol('WM_DELETE_WINDOW',self.close)

    def preview(self):
        if self.busy:return
        topic=self.topic.get().strip();include_chat=self.include_chat.get();include_sources=self.include_sources.get();use_ai=self.use_ai.get()
        if len(topic)>ai_service.MAX_QUESTION:messagebox.showerror('主題太長','主題最多 2000 字。',parent=self);return
        if use_ai and not topic:messagebox.showinfo('請輸入主題','AI 整理需要先輸入主題。',parent=self);return
        import ai_ui
        config=ai_ui.read_config(self.db.parent/'ai-settings.json'); session=dict(self.session)
        self.busy=True;self.preview_ready=False;self.preview_button.configure(state='disabled');self.save_button.configure(state='disabled');self.copy_button.configure(state='disabled');self.status.set('正在分析索引及整理內容…')
        def work():
            ticket=None
            try:
                summary=analysis_export.inventory(self.db)
                sources=ai_service.retrieve_with_general_fallback(str(self.db),topic) if topic else []
                draft=None
                if use_ai:
                    if not sources:raise ValueError('主題沒有可用的搜尋來源，請換一個主題或先匯入文件。')
                    if any(ai_service.source_state(source) for source in sources):raise ValueError('來源已變更或無法存取，請重新掃描。')
                    if ai_ui.read_config(self.db.parent/'ai-settings.json')!=config:raise ValueError('AI 設定已變更，請重新預覽。')
                    provider=config['provider'];model=config['openai_model'] if provider=='openai' else config['local_model']
                    key=None
                    question='請根據文件整理與以下主題相關的產品、機型、操作步驟和常見問題；資料不足請說明：'+topic
                    if provider=='openai':
                        if analysis_export.redact(question)!=question or any(analysis_export.redact(source.get('excerpt',''))!=source.get('excerpt','') for source in sources):
                            raise ValueError('主題或來源片段含疑似 API Key；請移除敏感資訊或改用本地 AI。')
                        if not config['network_enabled']:raise ValueError('OpenAI 外網存取已關閉。')
                        key=ai_service.load_key()
                        if not key:raise ValueError('請先設定 OpenAI API Key。')
                        ticket=budget.reserve(self.db,question,sources,model,config['max_tokens'])
                    draft,used,usage=ai_service.answer(question,sources,provider,model,key,config['local_base'],config['max_tokens'],network_enabled=config['network_enabled'])
                    if ticket:budget.finish(self.db,ticket,usage);ticket=None
                    if not used: draft='資料不足，尚未產生可靠的 AI 整理草稿。'
                report=analysis_export.render_report(summary,topic,config,sources,session,include_chat,include_sources,draft)
                self.events.put(('preview',report))
            except Exception as e:
                if ticket:
                    try:
                        if isinstance(e,ai_service.IncompleteResponse):budget.finish(self.db,ticket,e.usage)
                        else:budget.uncertain(self.db,ticket)
                    except Exception:pass
                self.events.put(('error',str(e)))
        threading.Thread(target=work,daemon=True).start()

    def poll(self):
        try:
            while True:
                kind,value=self.events.get_nowait();self.busy=False;self.preview_button.configure(state='normal')
                if kind=='error':self.status.set('整理未完成：'+value);messagebox.showerror('整理未完成',value,parent=self)
                else:
                    self.text.delete('1.0','end');self.text.insert('1.0',value);self.preview_ready=True;self.save_button.configure(state='normal');self.copy_button.configure(state='normal');self.status.set('請檢查並刪改預覽，再儲存或複製。')
        except queue.Empty:pass
        if self.winfo_exists():self.poll_id=self.after(150,self.poll)

    def copy_preview(self):
        if self.busy or not self.preview_ready:return
        text=self.text.get('1.0','end-1c')
        if analysis_export.redact(text)!=text:
            messagebox.showerror('疑似敏感資訊','預覽可能包含 API Key，請先移除後再複製。',parent=self);return
        self.clipboard_clear();self.clipboard_append(text);self.status.set('已複製預覽；貼上分享前請確認內容。')

    def save(self):
        if self.busy or not self.preview_ready:return
        text=self.text.get('1.0','end-1c')
        if not text.strip():messagebox.showinfo('內容為空','請先產生或輸入整理內容。',parent=self);return
        path=filedialog.asksaveasfilename(parent=self,title='儲存整理預覽',defaultextension='.md',filetypes=[('Markdown','*.md')],initialfile='knowledge-analysis.md')
        if not path:return
        try:
            analysis_export.save_report(self.db,path,text);self.status.set('已儲存整理報告；請自行檢查後再分享。')
        except FileExistsError:messagebox.showerror('檔案已存在','為避免覆蓋，請選擇新的檔名。',parent=self)
        except Exception as e:messagebox.showerror('儲存失敗',str(e),parent=self)

    def close(self):
        if self.busy:messagebox.showinfo('整理中','請等待本次整理結束。',parent=self);return
        if self.poll_id:self.after_cancel(self.poll_id);self.poll_id=None
        self.destroy()
