"""Resumable document preparation; source files remain read-only."""
import json, sqlite3, uuid
from pathlib import Path
import core, ai_service, ai_ui, budget, pdf_features, ocr_batch, workspace

QUESTION='請整理此文件片段中記載的產品、功能、操作步驟與注意事項。僅使用來源內容，保留條件並附引用；不清楚的內容不要推測。'

def connect(path):
    db=core.connect(str(path))
    db.executescript('''CREATE TABLE IF NOT EXISTS organize_jobs(id TEXT PRIMARY KEY, config TEXT NOT NULL, token_limit INTEGER NOT NULL, reserved INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS organize_items(id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES organize_jobs(id), document_id INTEGER NOT NULL, path TEXT NOT NULL, sha TEXT, size INTEGER, mtime INTEGER, locator TEXT NOT NULL, text TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, result TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '', reserved INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX IF NOT EXISTS organize_pending_lookup ON organize_items(job_id,state,document_id,id);''')
    return db

def pack_csv_chunks(chunks,limit=3000):
    # Keep every row's locator with its text while limiting each request.
    grouped=[]; parts=[]; first='';last='';length=0
    for loc,body in chunks:
        for offset in range(0,len(body),2000):
            piece=body[offset:offset+2000]; item=f'[{loc}] {piece}'
            if parts and length+len(item)+1>limit:
                grouped.append((first if first==last else first+' 至 '+last,'\n'.join(parts)))
                parts=[];length=0
            if not parts:first=loc
            last=loc;parts.append(item);length+=len(item)+1
    if parts:grouped.append((first if first==last else first+' 至 '+last,'\n'.join(parts)))
    return grouped

def create(path,config,limit):
    limit=int(limit)
    if limit<1000 or limit>100000000: raise ValueError('整個工作 Token 上限須介於 1,000 與 100,000,000。')
    if config.get('provider') not in ('local','openai'): raise ValueError('請先設定 AI。')
    if config['provider']=='openai' and not config.get('network_enabled'): raise ValueError('請先允許 OpenAI 外網連線。')
    model=config.get('openai_model') if config['provider']=='openai' else config.get('local_model')
    if config['provider']=='openai' and not ai_service.load_key(): raise ValueError('請先設定 OpenAI API Key。')
    if config['provider']=='local' and model==ai_ui.local_ai.MODEL and not ai_ui.local_ai.running(): raise ValueError('請先啟動內建本地 AI。')
    if not model: raise ValueError('請先設定 AI 模型。')
    db=connect(path); ident=uuid.uuid4().hex
    try:
        rows=db.execute('SELECT id,path,sha256,size,mtime_ns,status,extension,detail FROM documents ORDER BY path').fetchall()
        if not rows: raise ValueError('尚無文件，請先匯入並建立索引。')
        with db:
            db.execute("INSERT INTO organize_jobs(id,config,token_limit,state) VALUES(?,?,?,'paused')",(ident,json.dumps(config,ensure_ascii=False),limit))
            for did,name,sha,size,mtime,status,ext,detail in rows:
                chunks=db.execute('SELECT locator,text FROM chunks WHERE document_id=? ORDER BY id',(did,)).fetchall() if status=='indexed' else []
                if chunks:
                    chunks=pack_csv_chunks(chunks)
                    for loc,body in chunks:
                        for pos in range(0,len(body),3000):
                            db.execute("INSERT INTO organize_items(job_id,document_id,path,sha,size,mtime,locator,text,kind,state) VALUES(?,?,?,?,?,?,?,?,?,?)",(ident,did,name,sha,size,mtime,loc,body[pos:pos+3000],'csv_group' if ext.lower()=='.csv' else 'text_group','pending'))
                else:
                    kind='ocr' if status=='indexed' and ext.lower()=='.pdf' and sha else 'unavailable'
                    state='pending' if kind=='ocr' else 'skipped'
                    db.execute("INSERT INTO organize_items(job_id,document_id,path,sha,size,mtime,locator,text,kind,state,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(ident,did,name,sha,size,mtime,'整份文件','',kind,state,detail[:500]))
        return ident
    finally: db.close()

def latest(path):
    db=connect(path)
    try:
        row=db.execute('SELECT id FROM organize_jobs ORDER BY rowid DESC LIMIT 1').fetchone();return row[0] if row else None
    finally: db.close()

def stats(path,job):
    db=connect(path)
    try:
        row=db.execute('SELECT state,token_limit,reserved FROM organize_jobs WHERE id=?',(job,)).fetchone()
        if not row: raise ValueError('工作不存在')
        counts=dict(db.execute('SELECT state,count(*) FROM organize_items WHERE job_id=? GROUP BY state',(job,)).fetchall())
        docs=db.execute('SELECT count(DISTINCT document_id) FROM organize_items WHERE job_id=?',(job,)).fetchone()[0]
        ready=db.execute("SELECT count(*) FROM (SELECT document_id FROM organize_items WHERE job_id=? GROUP BY document_id HAVING sum(CASE WHEN state IN ('pending','running','unknown') THEN 1 ELSE 0 END)=0)",(job,)).fetchone()[0]
        return {'state':row[0],'limit':row[1],'reserved':row[2],'counts':counts,'documents':docs,'processed':ready}
    finally: db.close()

def set_limit(path,job,value):
    value=int(value)
    if not 1000<=value<=100000000: raise ValueError('Token 上限須介於 1,000 與 100,000,000。')
    db=connect(path)
    try:
        with db:
            row=db.execute('SELECT state,reserved FROM organize_jobs WHERE id=?',(job,)).fetchone()
            if not row or row[0]=='running' or value<row[1]: raise ValueError('工作進行中或上限低於已預留數量。')
            db.execute('UPDATE organize_jobs SET token_limit=? WHERE id=?',(value,job))
    finally:db.close()

def compact_pending_text(db,job):
    # Transaction owned by caller; only unprocessed legacy rows are merged.
    docs=db.execute("SELECT DISTINCT i.document_id FROM organize_items i JOIN documents d ON d.id=i.document_id WHERE i.job_id=? AND i.state='pending' AND i.kind='text'",(job,)).fetchall()
    for (did,) in docs:
        rows=db.execute("SELECT id,path,sha,size,mtime,locator,text FROM organize_items WHERE job_id=? AND document_id=? AND state='pending' AND kind='text' ORDER BY id",(job,did)).fetchall()
        if not rows:continue
        # Never combine rows from different source snapshots.
        meta=(rows[0][1],rows[0][2],rows[0][3],rows[0][4])
        if any(tuple(r[1:5])!=meta for r in rows):continue
        packed=pack_csv_chunks([(r[5],r[6]) for r in rows])
        db.executemany('DELETE FROM organize_items WHERE id=?',[(r[0],) for r in rows])
        db.executemany("INSERT INTO organize_items(job_id,document_id,path,sha,size,mtime,locator,text,kind,state) VALUES(?,?,?,?,?,?,?,?,'text_group','pending')",[(job,did,*meta,loc,text) for loc,text in packed])

def compact_pending_csv(db,job):
    return compact_pending_text(db,job)

def retry_failed(path,job):
    db=connect(path)
    try:
        with db:
            state=db.execute('SELECT state FROM organize_jobs WHERE id=?',(job,)).fetchone()
            if not state or state[0]=='running': raise ValueError('工作正在執行或不存在。')
            db.execute("UPDATE organize_items SET state='pending',detail='' WHERE job_id=? AND state='failed'",(job,))
    finally: db.close()

def recover(path,job):
    db=connect(path)
    try:
        with db:
            db.execute("UPDATE organize_items SET state='unknown',detail='上次中斷於處理中；請檢查用量與結果，不自動重試。' WHERE job_id=? AND state='running'",(job,))
            db.execute("UPDATE organize_jobs SET state='paused' WHERE id=? AND state='running'",(job,))
    finally: db.close()

def model_is_builtin(config):
    return config.get('local_model')==ai_ui.local_ai.MODEL

def run(path,job,cancelled=lambda:False,progress=lambda x:None):
    db=connect(path)
    try:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT config,token_limit,reserved,state FROM organize_jobs WHERE id=?',(job,)).fetchone()
        if not row or row[3]=='running': raise ValueError('工作不存在或正在執行。')
        config=json.loads(row[0]);limit=row[1]
        if ai_ui.read_config(Path(path).parent/'ai-settings.json')!=config: raise ValueError('AI 設定已變更；請建立新工作，避免混用模型。')
        if db.execute("SELECT count(*) FROM organize_items WHERE job_id=? AND state='unknown'",(job,)).fetchone()[0]: raise ValueError('有中斷後用量未知的項目；請先人工檢查，不能自動重試。')
        progress('正在整理既有佇列，保留已完成草稿…')
        compact_pending_text(db,job)
        db.execute("UPDATE organize_jobs SET state='running' WHERE id=?",(job,));db.commit()
        previous_doc=0
        while not cancelled():
            query="SELECT id,path,sha,size,mtime,locator,text,kind,document_id FROM organize_items WHERE job_id=? AND state='pending' "
            item=db.execute(query+"AND document_id>? ORDER BY document_id,id LIMIT 1",(job,previous_doc)).fetchone()
            if not item: item=db.execute(query+"ORDER BY document_id,id LIMIT 1",(job,)).fetchone()
            if not item: break
            ident,name,sha,size,mtime,loc,body,kind,previous_doc=item
            source={'id':1,'path':name,'sha256':sha,'size':size,'mtime_ns':mtime,'locator':loc,'excerpt':body,'kind':'來源文件（待確認）'}
            if ai_service.source_state(source):
                with db: db.execute("UPDATE organize_items SET state='failed',detail='來源已變更或無法讀取，請重新掃描。' WHERE id=?",(ident,))
                continue
            progress('正在處理：'+Path(name).name)
            if kind=='ocr':
                with db: db.execute("UPDATE organize_items SET state='running' WHERE id=?",(ident,))
                try:
                    extra=workspace.application_base()/'data'/'ocr'/'tessdata'; langs=pdf_features.languages(extra)
                    lang=next((x for x in ('chi_tra','Windows:zh-TW','chi_sim','Windows:zh-CN','eng','Windows:en-US') if x in langs),None)
                    if not lang: raise ValueError('沒有可用 OCR 語言；請先安裝或設定語言資料。')
                    data,_=pdf_features.load_source(path,name);_,total=pdf_features.render_page(data,1)
                    if total>200: raise ValueError('PDF 超過自動辨識 200 頁上限，請分批處理。')
                    failed=0
                    for page in range(1,total+1):
                        if cancelled(): break
                        result=ocr_batch.run(path,name,page,page,lang,extra,progress,cancelled)
                        failed+=result['failed']
                    state='pending' if cancelled() else 'failed' if failed else 'review'
                    detail=f'OCR {lang}；共 {total} 頁，失敗 {failed}；結果須人工校對。'
                except Exception as e: state='failed';detail=str(e)[:500]
                with db: db.execute('UPDATE organize_items SET state=?,detail=? WHERE id=?',(state,detail,ident))
                continue
            maximum=int(config['max_tokens']); ceiling=(len((ai_service.INSTRUCTIONS+ai_service.make_input(QUESTION,[source])).encode('utf8'))+1024+maximum)*(2 if config['provider']=='local' and model_is_builtin(config) else 1)
            db.execute('BEGIN IMMEDIATE');used=db.execute('SELECT reserved FROM organize_jobs WHERE id=?',(job,)).fetchone()[0]
            if used+ceiling>limit:
                db.rollback();progress('達到整個工作的 Token 預留上限，已暫停。');break
            db.execute("UPDATE organize_items SET state='running',reserved=? WHERE id=?",(ceiling,ident));db.execute('UPDATE organize_jobs SET reserved=reserved+? WHERE id=?',(ceiling,job));db.commit()
            ticket=None
            try:
                key=ai_service.load_key() if config['provider']=='openai' else None
                model=config['openai_model'] if config['provider']=='openai' else config['local_model']
                if config['provider']=='openai': ticket=budget.reserve(path,QUESTION,[source],model,maximum)
                text,used_sources,usage=ai_service.answer(QUESTION,[source],config['provider'],model,key,config['local_base'],maximum,network_enabled=config['network_enabled'])
                if ticket: budget.finish(path,ticket,usage);ticket=None
                if not used_sources: raise ValueError('AI 草稿沒有可核對來源。')
                if ai_service.source_state(source): raise ValueError('處理後來源已變更；草稿未保存。')
                with db: db.execute("UPDATE organize_items SET state='review',result=?,detail='AI 草稿；尚未人工核對，未加入搜尋。' WHERE id=?",(text,ident))
            except Exception as e:
                if ticket:
                    try: budget.uncertain(path,ticket)
                    except Exception: pass
                with db: db.execute("UPDATE organize_items SET state=?,detail=? WHERE id=?",('unknown' if ticket else 'failed',str(e)[:500],ident))
                break
        with db:
            left=db.execute("SELECT count(*) FROM organize_items WHERE job_id=? AND state='pending'",(job,)).fetchone()[0]
            db.execute('UPDATE organize_jobs SET state=? WHERE id=?',('paused' if left or cancelled() else 'finished',job))
    finally: db.close()
