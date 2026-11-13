"""Bounded, cancellable batch OCR. Results stay unindexed until reviewed."""
from pathlib import Path
import core,pdf_features

MAX_PAGES=25
def run(db_path,path,first,last,language,extra_dir,progress=lambda x:None,cancelled=lambda:False):
    first=int(first); last=int(last)
    if first<1 or last<first or last-first+1>MAX_PAGES: raise ValueError(f'頁碼範圍錯誤；每次最多 {MAX_PAGES} 頁')
    available=pdf_features.languages(extra_dir)
    if language not in available: raise ValueError('所選 OCR 語言未安裝')
    data,sha=pdf_features.load_source(db_path,path); cache=Path(db_path).parent/'cache'
    db=core.connect(str(db_path)); stats={'pending':0,'skipped':0,'failed':0,'cancelled':False}
    try:
        doc=db.execute('SELECT id,sha256 FROM documents WHERE path=?',(str(path),)).fetchone()
        if not doc or doc[1]!=sha: raise ValueError('文件索引已變更')
        for page in range(first,last+1):
            if cancelled(): stats['cancelled']=True; break
            prior=db.execute('SELECT status FROM pdf_ocr_queue WHERE document_id=? AND source_sha256=? AND page=? AND language=?',(doc[0],sha,page,language)).fetchone()
            if prior and prior[0] in ('pending','reviewed'):
                stats['skipped']+=1; progress(f'第 {page} 頁已有結果，保留原有內容'); continue
            try:
                png,total=pdf_features.render_page(data,page)
                text=pdf_features.run_ocr(png,language,cache,extra_dir,available)
                status='pending' if text else 'failed'; detail='' if text else '沒有辨識到文字'
            except Exception as e: text=''; status='failed'; detail=str(e)[:500]
            current=db.execute('SELECT sha256 FROM documents WHERE id=?',(doc[0],)).fetchone()
            if not current or current[0]!=sha: raise ValueError('來源索引已變更，已停止批次處理')
            with db:
                db.execute('''INSERT INTO pdf_ocr_queue(document_id,source_sha256,page,language,text,status,detail,updated_at)
                  VALUES(?,?,?,?,?,?,?,datetime('now','localtime')) ON CONFLICT(document_id,source_sha256,page,language)
                  DO UPDATE SET text=excluded.text,status=excluded.status,detail=excluded.detail,updated_at=excluded.updated_at
                  WHERE pdf_ocr_queue.status='failed' ''',(doc[0],sha,page,language,text,status,detail))
            stats[status if status=='failed' else 'pending']+=1
            progress(f'已處理第 {page} 頁 / {last} 頁')
        return stats
    finally: db.close()

def list_pages(db_path,path):
    db=core.connect(str(db_path))
    try:
        return db.execute('''SELECT q.id,q.page,q.language,q.status,q.detail,q.text,q.source_sha256
          FROM pdf_ocr_queue q JOIN documents d ON d.id=q.document_id
          WHERE d.path=? AND q.source_sha256=d.sha256 ORDER BY q.page,q.language''',(str(path),)).fetchall()
    finally: db.close()

def approve(db_path,path,queue_id,text):
    db=core.connect(str(db_path))
    try:
        row=db.execute('''SELECT q.page,q.source_sha256,q.status FROM pdf_ocr_queue q JOIN documents d ON d.id=q.document_id
          WHERE q.id=? AND d.path=? AND q.source_sha256=d.sha256''',(queue_id,str(path))).fetchone()
    finally: db.close()
    if not row or row[2] not in ('pending','reviewed'): raise ValueError('待校對內容不存在或來源已變更')
    locator=pdf_features.save_derivative(db_path,path,row[1],row[0],'ocr',text)
    db=core.connect(str(db_path))
    try:
        with db: db.execute("UPDATE pdf_ocr_queue SET status='reviewed',text=?,updated_at=datetime('now','localtime') WHERE id=?",(text.strip(),queue_id))
    finally: db.close()
    return locator
