"""APP-owned import history; saved-copy references survive database relocation."""
import contextlib
import os
import re
import stat
import uuid
from pathlib import Path
import core

RETRY_STATES=('副本已保存，解析失敗','未完成','未處理')

@contextlib.contextmanager
def batch_lock(db_path,batch_id):
    if not re.fullmatch(r'[0-9a-f]{32}',str(batch_id)): raise ValueError('匯入批次識別碼無效')
    folder=Path(db_path).resolve().parent/'import-locks'; folder.mkdir(parents=True,exist_ok=True)
    st=os.stat(folder,follow_symlinks=False)
    if stat.S_ISLNK(st.st_mode) or getattr(st,'st_file_attributes',0)&(core.REPARSE|core.PLACEHOLDER): raise ValueError('匯入鎖定資料夾不可是連結或雲端佔位檔')
    lockfile=folder/(batch_id+'.lock')
    if lockfile.exists() or lockfile.is_symlink():
        st=os.stat(lockfile,follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode) or getattr(st,'st_file_attributes',0)&(core.REPARSE|core.PLACEHOLDER): raise ValueError('匯入鎖定檔不可是連結或雲端佔位檔')
    with lockfile.open('a+b') as handle:
        handle.seek(0)
        if os.name=='nt':
            import msvcrt
            try: msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            except OSError: raise RuntimeError('此匯入批次正在另一個視窗或 APP 處理。') from None
            try: yield
            finally: handle.seek(0); msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            try: fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError: raise RuntimeError('此匯入批次正在另一個視窗或 APP 處理。') from None
            try: yield
            finally: fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

class ImportHistory:
    def __init__(self,db_path):
        self.db_path=Path(db_path).resolve(); self.db=core.connect(str(self.db_path))
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS import_batches(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS import_batch_items(batch_id TEXT NOT NULL REFERENCES import_batches(id), position INTEGER NOT NULL,
            source TEXT NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL, target_saved INTEGER NOT NULL DEFAULT 0,
            target_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL, target_name TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(batch_id,position));
        CREATE TABLE IF NOT EXISTS import_item_events(id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, position INTEGER NOT NULL,
            recorded_at TEXT NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL,
            FOREIGN KEY(batch_id,position) REFERENCES import_batch_items(batch_id,position));
        ''')
    def __enter__(self): return self
    def __exit__(self,*args): self.db.close()
    def create(self,paths):
        paths=[str(p) for p in paths]
        if not paths or len(paths)>100000: raise ValueError('匯入清單為空或過大')
        ident=uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO import_batches VALUES(?,datetime('now','localtime'),datetime('now','localtime'))",(ident,))
            self.db.executemany("INSERT INTO import_batch_items(batch_id,position,source,state,detail) VALUES(?,?,?,'未處理','尚未開始或上次作業中斷')",[(ident,i,p) for i,p in enumerate(paths)])
        return ident
    def update(self,batch_id,index,item):
        target=str(item.get('target') or ''); name=''; document_id=None; saved=bool(target)
        if target:
            p=Path(target); folder=self.db_path.parent/'imported'
            if p.parent.resolve()!=folder.resolve() or not re.fullmatch(r'[0-9a-f]{32}(?:--.+)?\.[A-Za-z0-9]+',p.name):
                raise ValueError('匯入結果包含非 APP 管理的副本路徑')
            name=p.name
            row=self.db.execute('SELECT id FROM documents WHERE path=?',(str(p),)).fetchone()
            document_id=row[0] if row else None
        with self.db:
            cursor=self.db.execute('''UPDATE import_batch_items SET state=?,detail=?,target_saved=?,target_document_id=?,target_name=?
                WHERE batch_id=? AND position=?''',(item['state'],str(item.get('detail','')),int(saved),document_id,name,batch_id,index))
            if cursor.rowcount!=1: raise ValueError('匯入項目不存在')
            self.db.execute("INSERT INTO import_item_events(batch_id,position,recorded_at,state,detail) VALUES(?,?,datetime('now','localtime'),?,?)",(batch_id,index,item['state'],str(item.get('detail',''))))
            self.db.execute("UPDATE import_batches SET updated_at=datetime('now','localtime') WHERE id=?",(batch_id,))
    def batches(self):
        return self.db.execute('''SELECT b.id,b.created_at,b.updated_at,COUNT(i.position)
            FROM import_batches b LEFT JOIN import_batch_items i ON i.batch_id=b.id
            GROUP BY b.id ORDER BY b.rowid DESC''').fetchall()
    def cancel_pending(self,batch_id):
        with self.db:
            self.db.execute("""INSERT INTO import_item_events(batch_id,position,recorded_at,state,detail)
                SELECT batch_id,position,datetime('now','localtime'),'未處理','使用者已取消，尚未處理'
                FROM import_batch_items WHERE batch_id=? AND state='未處理'""",(batch_id,))
            self.db.execute("UPDATE import_batch_items SET detail='使用者已取消，尚未處理' WHERE batch_id=? AND state='未處理'",(batch_id,))
            self.db.execute("UPDATE import_batches SET updated_at=datetime('now','localtime') WHERE id=?",(batch_id,))
    def events(self,batch_id):
        return self.db.execute('''SELECT e.recorded_at,i.source,e.state,e.detail FROM import_item_events e
            JOIN import_batch_items i ON i.batch_id=e.batch_id AND i.position=e.position
            WHERE e.batch_id=? ORDER BY e.id''',(batch_id,)).fetchall()
    def load(self,batch_id):
        rows=self.db.execute('''SELECT i.source,i.state,i.detail,i.target_saved,i.target_name,d.path
            FROM import_batch_items i LEFT JOIN documents d ON d.id=i.target_document_id
            WHERE i.batch_id=? ORDER BY i.position''',(batch_id,)).fetchall()
        results=[]; folder=self.db_path.parent/'imported'
        for source,state,detail,saved,name,document_path in rows:
            target=''; missing=False
            if saved:
                try:
                    if not re.fullmatch(r'[0-9a-f]{32}(?:--.+)?\.[A-Za-z0-9]+',name): raise ValueError()
                    p=Path(document_path) if document_path else folder/name
                    if p.parent.resolve()!=folder.resolve() or p.name!=name: raise ValueError()
                    st=os.stat(p,follow_symlinks=False); attrs=getattr(st,'st_file_attributes',0)
                    if not stat.S_ISREG(st.st_mode) or attrs&(core.PLACEHOLDER|core.REPARSE): raise ValueError()
                    target=str(p)
                except (OSError,ValueError):
                    missing=True; detail+='；APP 副本無法存取，請檢查知識庫或重新選擇來源。'
            results.append({'source':source,'state':state,'detail':detail,'target':target,'target_missing':missing})
        return results
