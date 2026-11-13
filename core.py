"""Local-only, read-only ingestion and SQLite full-text search."""
import csv
import ctypes
import hashlib
import io
import itertools
import os
import re
import sqlite3
import stat
import sys
import uuid
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

SUPPORTED = {'.md', '.txt', '.html', '.htm', '.csv', '.pdf', '.docx', '.xlsx'}
PLACEHOLDER = 0x1000 | 0x40000 | 0x400000
REPARSE = 0x400
MAX_SIZE = 250 * 1024 * 1024
PARSER_VERSION = '1'
AI_REVIEW_STATES={
    'AI 網路研究（指令未經裝置驗證）':('AI 網路研究（指令未經裝置驗證）','AI 網路研究（已人工核對，未經裝置實測）'),
    'AI 網路研究（已人工核對，未經裝置實測）':('AI 網路研究（指令未經裝置驗證）','AI 網路研究（已人工核對，未經裝置實測）'),
    'AI 裝置觀察紀錄（尚未人工校對）':('AI 裝置觀察紀錄（尚未人工校對）','使用者校對的 AI 裝置操作紀錄'),
    '使用者校對的 AI 裝置操作紀錄':('AI 裝置觀察紀錄（尚未人工校對）','使用者校對的 AI 裝置操作紀錄'),
}

def contained(path, root):
    try:
        return os.path.commonpath([os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(root))]) == os.path.normcase(os.path.realpath(root))
    except ValueError:
        return False

def connect(path):
    db = sqlite3.connect(path)
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS documents(id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL,
      extension TEXT NOT NULL, size INTEGER, mtime_ns INTEGER, sha256 TEXT,
      status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', parser_version TEXT);
    CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
      locator TEXT NOT NULL, text TEXT NOT NULL);
    CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(text, tokenize='trigram');
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS document_revisions(id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id),
      sha256 TEXT NOT NULL, size INTEGER, mtime_ns INTEGER, recorded_at TEXT NOT NULL, UNIQUE(document_id,sha256));
    CREATE TABLE IF NOT EXISTS revision_chunks(revision_id INTEGER NOT NULL REFERENCES document_revisions(id),
      position INTEGER NOT NULL, locator TEXT NOT NULL, text TEXT NOT NULL, PRIMARY KEY(revision_id,position));
    CREATE TABLE IF NOT EXISTS pdf_derived(id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id),
      source_sha256 TEXT NOT NULL, page INTEGER NOT NULL, kind TEXT NOT NULL, locator TEXT NOT NULL,
      text TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(document_id,source_sha256,page,kind));
    CREATE VIRTUAL TABLE IF NOT EXISTS pdf_derived_fts USING fts5(text, tokenize='trigram');
    CREATE TABLE IF NOT EXISTS pdf_derived_revisions(id INTEGER PRIMARY KEY, derived_id INTEGER NOT NULL REFERENCES pdf_derived(id),
      text TEXT NOT NULL, recorded_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS pdf_ocr_queue(id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id),
      source_sha256 TEXT NOT NULL, page INTEGER NOT NULL, language TEXT NOT NULL, text TEXT NOT NULL,
      status TEXT NOT NULL, detail TEXT NOT NULL, updated_at TEXT NOT NULL,
      UNIQUE(document_id,source_sha256,page,language));
    CREATE TABLE IF NOT EXISTS device_captures(id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id),
      png BLOB NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ai_card_reviews(id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id),
      content_sha256 TEXT NOT NULL, previous_detail TEXT NOT NULL, new_detail TEXT NOT NULL, reviewed_at TEXT NOT NULL);
    ''')
    return db

def _text(data):
    for enc in ('utf-8-sig', 'utf-16', 'cp950', 'gb18030'):
        try:
            return data.decode(enc), enc
        except (UnicodeError, LookupError):
            pass
    raise ValueError('無法判定文字編碼')

class HTMLBlocks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ignore = 0; self.heading = ''; self.parts = []; self.blocks = []; self.line = 1
    def flush(self):
        t = ' '.join(' '.join(self.parts).split())
        if t:
            self.blocks.append((f'{self.heading or "HTML"}，約第 {self.line} 行', t))
        self.parts = []
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript'):
            self.ignore += 1; return
        if self.ignore: return
        if tag in ('h1','h2','h3','h4','h5','h6','p','li','tr','pre','section','article'):
            self.flush(); self.line = self.getpos()[0]
        if tag in ('td','th'): self.parts.append(' | ')
    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'):
            self.ignore = max(0, self.ignore-1); return
        if self.ignore: return
        if tag in ('h1','h2','h3','h4','h5','h6'):
            self.heading = ' '.join(' '.join(self.parts).split()); self.flush()
        elif tag in ('p','li','tr','pre','section','article'): self.flush()
    def handle_data(self, data):
        if not self.ignore: self.parts.append(data)

def parse(path, data):
    ext = Path(path).suffix.lower()
    if ext == '.xlsx':
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries=archive.infolist()
            if len(entries)>10000 or sum(i.file_size for i in entries)>512*1024*1024: raise ValueError('Excel 展開後資料超過安全上限')
            for info in entries:
                if info.flag_bits & 1: raise ValueError('Excel 含加密項目')
                if info.filename.lower().endswith(('.xml','.rels')):
                    carry=b''
                    with archive.open(info) as f:
                        for block in iter(lambda:f.read(1024*1024),b''):
                            if b'<!' in carry+block or b'\x00' in block: raise ValueError('Excel XML 包含不支援的宣告或編碼')
                            carry=block[-1:]
        try:
            from openpyxl import load_workbook
        except ImportError:
            bundled=Path(os.environ.get('PKA_PYTHON_PACKAGES', str(Path(__file__).resolve().parent / 'vendor')))
            if bundled.is_dir(): sys.path.append(str(bundled))
            try: from openpyxl import load_workbook
            except ImportError: raise ValueError('缺少選用套件 openpyxl；Excel 尚未索引')
        workbook=load_workbook(io.BytesIO(data),read_only=True,data_only=False,keep_vba=False,keep_links=False)
        chunks=[]; cell_count=0; formulas=0
        try:
            if len(workbook.worksheets)>100: raise ValueError('Excel 工作表超過 100 張')
            for sheet in workbook.worksheets:
                for rowno,row in enumerate(sheet.iter_rows(values_only=True),1):
                    cell_count+=len(row)
                    if cell_count>1_000_000 or rowno>100_000: raise ValueError('Excel 儲存格數超過安全上限')
                    populated=[]
                    for i,value in enumerate(row,1):
                        if value is not None:
                            if isinstance(value,str) and value.startswith('='): formulas+=1
                            populated.append(f'第 {i} 欄: {str(value)}')
                    if populated: chunks.append((f'工作表 {sheet.title}，第 {rowno} 列','；'.join(populated)))
        finally: workbook.close()
        return chunks,f'Excel {len(workbook.worksheets)} 張工作表；公式 {formulas} 個（僅保存公式文字，不執行）；合併儲存格結構與格式未索引'
    if ext == '.docx':
        # Read only WordprocessingML; no macros, external relationships or media are executed.
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos=z.infolist()
            if len(infos)>10000: raise ValueError('DOCX 內含太多項目')
            try: body=z.getinfo('word/document.xml')
            except KeyError: raise ValueError('DOCX 缺少主要文件內容') from None
            if body.flag_bits & 1: raise ValueError('DOCX 主文件已加密')
            if body.file_size>80*1024*1024: raise ValueError('DOCX 文字結構超過 80 MiB 安全上限')
            xml=z.read(body)
            if re.search(br'<!\s*(DOCTYPE|ENTITY)',xml,re.I): raise ValueError('DOCX XML 包含不支援的宣告')
            tree=ET.fromstring(xml); media=sum(x.filename.startswith('word/media/') and not x.is_dir() for x in infos)
        ns='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        body=tree.find(ns+'body')
        if body is None: raise ValueError('DOCX 沒有文件主體')
        def text_of(node):
            parts=[]
            for item in node.iter():
                if item.tag==ns+'t' and item.text: parts.append(item.text)
                elif item.tag==ns+'tab': parts.append('\t')
                elif item.tag in (ns+'br',ns+'cr'): parts.append('\n')
            return ''.join(parts).strip()
        chunks=[]; paragraph=0; table=0; heading=''
        for node in body:
            if node.tag==ns+'p':
                paragraph+=1; text=text_of(node)
                if not text: continue
                style=node.find('./'+ns+'pPr/'+ns+'pStyle'); val=style.get(ns+'val','') if style is not None else ''
                if re.search(r'^(heading|title|標題|标题)',val,re.I): heading=text[:160]
                chunks.append((f'{heading or "文件"}，Word 第 {paragraph} 段',text))
            elif node.tag==ns+'tbl':
                table+=1
                for number,row in enumerate(node.findall(ns+'tr'),1):
                    cells=[text_of(cell) for cell in row.findall(ns+'tc')]
                    if any(cells): chunks.append((f'{heading or "文件"}，表格 {table} 第 {number} 列',' | '.join(cells)))
        return chunks, f'Word 主體 {paragraph} 段、{table} 個表格、{media} 個媒體項目；不執行巨集，頁碼、圖片及頁首頁尾未索引'
    if ext == '.pdf':
        try:
            from pypdf import PdfReader
        except ImportError:
            bundled = Path(os.environ.get('PKA_PYTHON_PACKAGES', str(Path(__file__).resolve().parent / 'vendor')))
            if bundled.is_dir(): sys.path.append(str(bundled))
            try: from pypdf import PdfReader
            except ImportError: raise ValueError('缺少選用套件 pypdf；PDF 尚未索引')
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(''):
            raise ValueError('PDF 受密碼保護')
        chunks=[]; low=[]; failed=[]; pages=len(reader.pages)
        for i in range(pages):
            try:
                text=reader.pages[i].extract_text() or ''
                if len(text.strip()) < 30: low.append(i+1)
                if text.strip(): chunks.append((f'PDF 第 {i+1} 頁', text))
            except Exception: failed.append(i+1)
        detail=f'PDF {pages} 頁；文字少於 30 字的頁數 {len(low)}；需人工檢查'
        if failed: detail+='；擷取失敗頁：'+','.join(map(str,failed[:50]))+(' 等' if len(failed)>50 else '')
        return chunks, detail
    text, enc = _text(data)
    if ext in ('.html','.htm'):
        parser=HTMLBlocks(); parser.feed(text); parser.flush()
        return parser.blocks, f'編碼 {enc}；靜態內容，圖片及 iframe 未擷取'
    if ext == '.csv':
        reader=csv.reader(io.StringIO(text)); header=next(reader, [])
        chunks=[]
        for row in reader:
            number=reader.line_num
            chunks.append((f'CSV 結束於第 {number} 行', '；'.join(f'{header[i] if i < len(header) else "欄"+str(i+1)}: {value}' for i,value in enumerate(row))))
        return chunks, f'編碼 {enc}；{len(chunks)} 資料列'
    chunks=[]; heading=''; buf=[]; start=1
    def flush():
        if buf:
            chunks.append((f'{heading or "文字"}，第 {start} 行', '\n'.join(buf)))
    for number,line in enumerate(text.splitlines(),1):
        if ext=='.md' and re.match(r'^#{1,6}\s+',line):
            flush(); buf=[]; heading=line.strip(); start=number
        if not buf: start=number
        buf.append(line)
        if len(buf)>=80:
            flush(); buf=[]
    flush()
    return chunks, f'編碼 {enc}'

def _record(db, path, ext, size, mtime, digest, status, detail, chunks=None):
    with db:
        previous=db.execute('SELECT id,sha256,size,mtime_ns FROM documents WHERE path=?',(path,)).fetchone()
        if chunks is not None and previous and previous[1] and digest and previous[1]!=digest:
            db.execute("INSERT OR IGNORE INTO document_revisions(document_id,sha256,size,mtime_ns,recorded_at) VALUES(?,?,?,?,datetime('now','localtime'))",previous)
            revision=db.execute('SELECT id FROM document_revisions WHERE document_id=? AND sha256=?',(previous[0],previous[1])).fetchone()
            if revision:
                old_chunks=db.execute('SELECT locator,text FROM chunks WHERE document_id=? ORDER BY id',(previous[0],)).fetchall()
                db.executemany('INSERT OR IGNORE INTO revision_chunks(revision_id,position,locator,text) VALUES(?,?,?,?)',[(revision[0],i,loc,text) for i,(loc,text) in enumerate(old_chunks)])
        db.execute('''INSERT INTO documents(path,extension,size,mtime_ns,sha256,status,detail,parser_version)
          VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET extension=excluded.extension,
          size=excluded.size,mtime_ns=excluded.mtime_ns,sha256=excluded.sha256,status=excluded.status,
          detail=excluded.detail,parser_version=excluded.parser_version''',
          (path,ext,size,mtime,digest,status,detail,PARSER_VERSION))
        docid=db.execute('SELECT id FROM documents WHERE path=?',(path,)).fetchone()[0]
        if chunks is not None:
            old=db.execute('SELECT id FROM chunks WHERE document_id=?',(docid,)).fetchall()
            db.executemany('DELETE FROM search_fts WHERE rowid=?',old)
            db.execute('DELETE FROM chunks WHERE document_id=?',(docid,))
            for locator, text in chunks:
                if not text.strip(): continue
                cursor=db.execute('INSERT INTO chunks(document_id,locator,text) VALUES(?,?,?)',(docid,locator,text))
                db.execute('INSERT INTO search_fts(rowid,text) VALUES(?,?)',(cursor.lastrowid,text))

def scan(root, db_path, progress=lambda msg:None, cancelled=lambda:False, fast=False):
    root=os.path.realpath(root); db_path=os.path.realpath(db_path)
    if not os.path.isdir(root): raise ValueError('來源資料夾不存在')
    if contained(db_path,root): raise ValueError('資料庫不能放在來源資料夾內')
    db=connect(db_path); seen=set(); counts={'indexed':0,'unchanged':0,'skipped':0,'error':0}; complete=True
    try:
        prior=db.execute("SELECT value FROM settings WHERE key='root'").fetchone()
        if prior and os.path.normcase(prior[0]) != os.path.normcase(root):
            raise ValueError('此資料庫已綁定其他來源資料夾；請使用另一個資料庫')
        with db: db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('root',?)",(root,))
        def walk_error(e):
            nonlocal complete
            complete=False; counts['error']+=1; progress(f'資料夾無法列舉：{e}')
        for directory, subdirs, names in os.walk(root, followlinks=False, onerror=walk_error):
            if cancelled(): complete=False; break
            kept=[]
            for name in subdirs:
                p=os.path.join(directory,name)
                try:
                    st=os.stat(p,follow_symlinks=False); attrs=getattr(st,'st_file_attributes',0)
                    if not (attrs & REPARSE or stat.S_ISLNK(st.st_mode)): kept.append(name)
                    else: progress(f'略過連結資料夾：{p}')
                except OSError as e: complete=False; progress(f'資料夾狀態錯誤：{p}: {e}')
            subdirs[:]=kept
            for name in names:
                if cancelled(): complete=False; break
                path=os.path.join(directory,name); seen.add(path); ext=Path(path).suffix.lower()
                try:
                    st=os.stat(path,follow_symlinks=False); attrs=getattr(st,'st_file_attributes',0)
                    if stat.S_ISLNK(st.st_mode) or attrs & (PLACEHOLDER|REPARSE):
                        _record(db,path,ext,st.st_size,st.st_mtime_ns,None,'skipped','雲端佔位或重新解析點，未讀取',[]); counts['skipped']+=1; continue
                    if ext not in SUPPORTED:
                        _record(db,path,ext,st.st_size,st.st_mtime_ns,None,'unsupported','格式尚未支援',[]); counts['skipped']+=1; continue
                    if st.st_size>MAX_SIZE:
                        _record(db,path,ext,st.st_size,st.st_mtime_ns,None,'skipped','超過 250 MiB 上限，未讀取',[]); counts['skipped']+=1; continue
                    if fast:
                        prior_file=db.execute('SELECT size,mtime_ns,status,parser_version FROM documents WHERE path=?',(path,)).fetchone()
                        if prior_file==(st.st_size,st.st_mtime_ns,'indexed',PARSER_VERSION):
                            counts['unchanged']+=1; continue
                    with open(path,'rb') as f: data=f.read(MAX_SIZE+1)
                    after=os.stat(path,follow_symlinks=False)
                    if len(data)>MAX_SIZE or st.st_size != after.st_size or st.st_mtime_ns != after.st_mtime_ns:
                        raise ValueError('讀取期間檔案變更或超過大小上限')
                    digest=hashlib.sha256(data).hexdigest()
                    existing=db.execute('SELECT sha256,status,parser_version FROM documents WHERE path=?',(path,)).fetchone()
                    if existing == (digest,'indexed',PARSER_VERSION): counts['unchanged']+=1; continue
                    chunks,detail=parse(path,data)
                    if not chunks: detail += '；未擷取到可搜尋文字'
                    _record(db,path,ext,st.st_size,st.st_mtime_ns,digest,'indexed',detail,chunks); counts['indexed']+=1
                except Exception as e:
                    counts['error']+=1
                    previous=db.execute('SELECT size,mtime_ns,sha256 FROM documents WHERE path=?',(path,)).fetchone()
                    _record(db,path,ext,*(previous or (None,None,None)),'error',str(e),None)
                finally: progress(f'{sum(counts.values())}：{os.path.relpath(path,root)}')
        if complete:
            for (path,) in db.execute('SELECT path FROM documents').fetchall():
                if contained(path,root) and path not in seen:
                    with db: db.execute("UPDATE documents SET status='missing',detail='本次掃描未發現來源；保留舊索引' WHERE path=?",(path,))
        return counts,complete
    finally: db.close()

def import_copy(db_path, source):
    """Copy one explicitly selected file into APP storage; never write to the source."""
    source=Path(source); st=os.stat(source,follow_symlinks=False)
    attrs=getattr(st,'st_file_attributes',0)
    if stat.S_ISLNK(st.st_mode) or attrs & (PLACEHOLDER|REPARSE): raise ValueError('雲端佔位檔或重新解析點不能自動匯入')
    if not stat.S_ISREG(st.st_mode) or st.st_size>MAX_SIZE: raise ValueError('不是一般檔案，或大於 250 MiB')
    ext=source.suffix.lower()
    if ext not in SUPPORTED: raise ValueError('目前不支援這種格式')
    db_path=Path(db_path).resolve(); folder=db_path.parent/'imported'; db=connect(str(db_path))
    try:
        row=db.execute("SELECT value FROM settings WHERE key='root'").fetchone()
        if not row or contained(folder,row[0]): raise ValueError('匯入區不可位於原始來源中')
        folder.mkdir(parents=True,exist_ok=True)
        if folder.is_symlink() or getattr(os.stat(folder,follow_symlinks=False),'st_file_attributes',0) & REPARSE: raise ValueError('匯入資料夾不能是重新解析點')
        with open(source,'rb') as f: data=f.read(MAX_SIZE+1)
        after=os.stat(source,follow_symlinks=False)
        if len(data)>MAX_SIZE or after.st_size!=st.st_size or after.st_mtime_ns!=st.st_mtime_ns: raise ValueError('讀取期間來源改變或超過大小上限')
        digest=hashlib.sha256(data).hexdigest()
        candidates=db.execute("SELECT path,size,mtime_ns FROM documents WHERE sha256=? AND status='indexed'",(digest,)).fetchall()
        for candidate,size,mtime in candidates:
            try:
                candidate=Path(candidate)
                if candidate.parent.resolve()!=folder.resolve(): continue
                candidate_st=os.stat(candidate,follow_symlinks=False); candidate_attrs=getattr(candidate_st,'st_file_attributes',0)
                if not stat.S_ISREG(candidate_st.st_mode) or candidate_attrs & (PLACEHOLDER|REPARSE): continue
                if candidate_st.st_size!=size or candidate_st.st_size!=len(data) or candidate_st.st_mtime_ns!=mtime: continue
                existing_hash=hashlib.sha256()
                with open(candidate,'rb') as existing:
                    for block in iter(lambda:existing.read(1024*1024),b''): existing_hash.update(block)
                after_candidate=os.stat(candidate,follow_symlinks=False)
                if after_candidate.st_size==candidate_st.st_size and after_candidate.st_mtime_ns==candidate_st.st_mtime_ns and existing_hash.hexdigest()==digest:
                    return str(candidate),False
            except OSError: continue
        filename=uuid.uuid4().hex+'--'+source.name
        # A long Windows path or name is possible; fall back to an ID plus extension.
        if len(filename)>180: filename=uuid.uuid4().hex+ext
        target=folder/filename; temp=folder/(uuid.uuid4().hex+'.tmp')
        try:
            with open(temp,'xb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
            os.replace(temp,target)
        finally:
            if temp.exists(): temp.unlink()
        copy_st=target.stat()
        try:
            chunks,detail=parse(target,data)
            if not chunks: detail+='；未擷取到可搜尋文字'
            _record(db,str(target),ext,copy_st.st_size,copy_st.st_mtime_ns,digest,'indexed','手動匯入副本；原始路徑：'+str(source)+'；'+detail,chunks)
        except Exception as e:
            _record(db,str(target),ext,copy_st.st_size,copy_st.st_mtime_ns,digest,'error','手動匯入副本；原始路徑：'+str(source)+'；'+str(e),[])
        return str(target),True
    finally: db.close()

def retry_import_copy(db_path,target,source_label):
    """Re-parse an unchanged APP-owned import; never rewrite the file."""
    db_path=Path(db_path).resolve(); folder=db_path.parent/'imported'; target=Path(target)
    if target.parent.resolve()!=folder.resolve() or target.suffix.lower() not in SUPPORTED:
        raise ValueError('只能重新解析 APP 管理的匯入副本')
    if not re.fullmatch(r'[0-9a-f]{32}(?:--.+)?\.[A-Za-z0-9]+',target.name): raise ValueError('匯入副本名稱無效')
    db=connect(str(db_path))
    try:
        root=db.execute("SELECT value FROM settings WHERE key='root'").fetchone()
        if not root or contained(folder,root[0]): raise ValueError('匯入區不可位於原始來源中')
        folder_st=os.stat(folder,follow_symlinks=False)
        if stat.S_ISLNK(folder_st.st_mode) or getattr(folder_st,'st_file_attributes',0)&REPARSE: raise ValueError('匯入資料夾不能是重新解析點')
        row=db.execute('SELECT status,sha256 FROM documents WHERE path=?',(str(target),)).fetchone()
        if not row or row[0] not in ('error','indexed') or not row[1]: raise ValueError('只有已記錄雜湊的匯入副本可以重試')
        before=os.stat(target,follow_symlinks=False); attrs=getattr(before,'st_file_attributes',0)
        if not stat.S_ISREG(before.st_mode) or attrs & (PLACEHOLDER|REPARSE) or before.st_size>MAX_SIZE:
            raise ValueError('匯入副本無法安全讀取')
        with open(target,'rb') as f: data=f.read(MAX_SIZE+1)
        after=os.stat(target,follow_symlinks=False)
        if (len(data)>MAX_SIZE or not stat.S_ISREG(after.st_mode) or getattr(after,'st_file_attributes',0)&(PLACEHOLDER|REPARSE)
                or before.st_size!=after.st_size or before.st_mtime_ns!=after.st_mtime_ns or hashlib.sha256(data).hexdigest()!=row[1]):
            raise ValueError('匯入副本已變更，未重新解析；請重新選擇來源匯入')
        if row[0]=='indexed': return str(target),False
        prefix='手動匯入副本；原始路徑：'+str(source_label)+'；'
        try:
            chunks,detail=parse(target,data)
            if not chunks: detail+='；未擷取到可搜尋文字'
            _record(db,str(target),target.suffix.lower(),after.st_size,after.st_mtime_ns,row[1],'indexed',prefix+detail,chunks)
        except Exception as e:
            _record(db,str(target),target.suffix.lower(),after.st_size,after.st_mtime_ns,row[1],'error',prefix+str(e),[])
        return str(target),True
    finally: db.close()

def save_card(db_path, title, content, existing=None):
    """Save a user-edited card outside the source and index it as a separate document."""
    title=title.strip(); content=content.strip()
    if not title or '\n' in title or len(title)>160: raise ValueError('請輸入 160 字以內的單行標題')
    if not content or len(content)>250000: raise ValueError('內容不能為空，且不可超過 250,000 字')
    db_path=Path(db_path).resolve(); cards=db_path.parent/'cards'
    db=connect(str(db_path))
    try:
        row=db.execute("SELECT value FROM settings WHERE key='root'").fetchone()
        if not row or contained(cards,row[0]): raise ValueError('知識卡資料夾不能放在原始資料夾內')
        cards.mkdir(parents=True,exist_ok=True)
        card_attrs=getattr(os.stat(cards,follow_symlinks=False),'st_file_attributes',0)
        if cards.is_symlink() or card_attrs & REPARSE: raise ValueError('知識卡資料夾不能是重新解析點')
        detail='使用者知識卡；非原始文件'
        if existing:
            path=Path(existing)
            if path.parent.resolve()!=cards.resolve() or path.suffix.lower()!='.md' or not re.fullmatch(r'[0-9a-f]{32}\.md',path.name):
                raise ValueError('只能編輯 APP 建立的知識卡')
            if path.is_symlink() or not path.is_file() or getattr(os.stat(path,follow_symlinks=False),'st_file_attributes',0) & REPARSE: raise ValueError('知識卡不存在或不可編輯')
            prior=db.execute('SELECT detail FROM documents WHERE path=?',(str(path),)).fetchone()
            if prior and prior[0] in AI_REVIEW_STATES: detail=AI_REVIEW_STATES[prior[0]][0]
        else: path=cards/(uuid.uuid4().hex+'.md')
        data=('# '+title+'\n\n'+content+'\n').encode('utf-8')
        temp=cards/(uuid.uuid4().hex+'.tmp')
        try:
            with open(temp,'xb') as f: f.write(data); f.flush(); os.fsync(f.fileno())
            os.replace(temp,path)
        finally:
            if temp.exists(): temp.unlink()
        st=path.stat(); chunks,_=parse(path,data)
        _record(db,str(path),'.md',st.st_size,st.st_mtime_ns,hashlib.sha256(data).hexdigest(),'indexed',detail,chunks)
        if existing and prior and prior[0] in AI_REVIEW_STATES and prior[0]!=detail:
            row=db.execute('SELECT id FROM documents WHERE path=?',(str(path),)).fetchone()
            with db: db.execute("INSERT INTO ai_card_reviews(document_id,content_sha256,previous_detail,new_detail,reviewed_at) VALUES(?,?,?,?,datetime('now','localtime'))",(row[0],hashlib.sha256(data).hexdigest(),prior[0],detail))
        return str(path)
    finally: db.close()

def review_ai_card(db_path,path,reviewed):
    """Record explicit human review against exact indexed card bytes."""
    if type(reviewed) is not bool: raise ValueError('審核狀態無效')
    db_path=Path(db_path).resolve(); cards=db_path.parent/'cards'; path=Path(path)
    if path.parent.resolve()!=cards.resolve() or not re.fullmatch(r'[0-9a-f]{32}\.md',path.name): raise ValueError('只能審核 APP 建立的 AI 知識卡')
    if path.is_symlink() or not path.is_file() or getattr(os.stat(path,follow_symlinks=False),'st_file_attributes',0)&REPARSE: raise ValueError('知識卡不存在或不可審核')
    data=path.read_bytes()
    if len(data)>1024*1024: raise ValueError('知識卡過大')
    digest=hashlib.sha256(data).hexdigest(); db=connect(str(db_path))
    try:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT id,sha256,detail,status FROM documents WHERE path=?',(str(path),)).fetchone()
        if not row or row[3]!='indexed' or row[1]!=digest: raise ValueError('知識卡已變更，請重新載入後再審核')
        if row[2] not in AI_REVIEW_STATES: raise ValueError('這不是可審核的 AI 產生知識卡')
        target=AI_REVIEW_STATES[row[2]][1 if reviewed else 0]
        if target!=row[2]:
            db.execute('UPDATE documents SET detail=? WHERE id=?',(target,row[0]))
            db.execute("INSERT INTO ai_card_reviews(document_id,content_sha256,previous_detail,new_detail,reviewed_at) VALUES(?,?,?,?,datetime('now','localtime'))",(row[0],digest,row[2],target))
        db.commit(); return target
    except Exception:
        db.rollback(); raise
    finally: db.close()

def chinese_variants(text):
    """Use Windows character mapping for recall; no network or extra index."""
    result=[text]
    if os.name!='nt' or not re.search(r'[\u3400-\u9fff]',text) or '\x00' in text: return result
    try:
        mapper=ctypes.WinDLL('kernel32',use_last_error=True).LCMapStringEx
        mapper.argtypes=[ctypes.c_wchar_p,ctypes.c_uint,ctypes.c_wchar_p,ctypes.c_int,ctypes.c_wchar_p,ctypes.c_int,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_ssize_t]
        mapper.restype=ctypes.c_int
        for flag in (0x02000000,0x04000000):
            size=mapper('zh-CN',flag,text,-1,None,0,None,None,0)
            if not size: continue
            buf=ctypes.create_unicode_buffer(size)
            if mapper('zh-CN',flag,text,-1,buf,size,None,None,0) and buf.value not in result: result.append(buf.value)
    except (OSError,ValueError): pass
    return result

def search(db_path, query, limit=100, extension_filter='全部格式', brand_filter='全部品牌'):
    results=[]; seen=set()
    for variant in chinese_variants(query):
        for row in _search_exact(db_path,variant,limit,extension_filter,brand_filter):
            key=(row[0],row[3],row[4])
            if key not in seen: seen.add(key); results.append(row)
            if len(results)>=limit: return results
    return results

def _search_exact(db_path, query, limit=100, extension_filter='全部格式', brand_filter='全部品牌'):
    query=query.strip()
    if not query: return []
    extensions={'PDF':['.pdf'],'HTML':['.html','.htm'],'Markdown':['.md'],'CSV':['.csv'],'TXT':['.txt'],'Word':['.docx'],'Excel':['.xlsx']}.get(extension_filter)
    filter_sql=' AND d.extension IN ('+','.join('?' for _ in extensions)+')' if extensions else ''
    params=list(extensions or [])
    if brand_filter!='全部品牌':
        term=brand_filter.lower().replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        filter_sql+=" AND (lower(d.path) LIKE ? ESCAPE '\\' OR lower(c.text) LIKE ? ESCAPE '\\')"
        params.extend(['%'+term+'%','%'+term+'%'])
    db=connect(db_path)
    try:
        if len(query)>=3:
            expression='"'+query.replace('"','""')+'"'
            rows=db.execute('''SELECT d.path,d.status,d.sha256,c.locator,c.text,d.mtime_ns
              FROM search_fts f JOIN chunks c ON c.id=f.rowid JOIN documents d ON d.id=c.document_id
              WHERE search_fts MATCH ?'''+filter_sql+' ORDER BY rank LIMIT ?', (expression,*params,limit)).fetchall()
            derived=db.execute('''SELECT d.path,d.status,d.sha256,c.locator,c.text,d.mtime_ns
              FROM pdf_derived_fts f JOIN pdf_derived c ON c.id=f.rowid JOIN documents d ON d.id=c.document_id
              WHERE pdf_derived_fts MATCH ? AND c.source_sha256=d.sha256'''+filter_sql+' ORDER BY rank LIMIT ?', (expression,*params,limit)).fetchall()
        else:
            escaped=query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
            rows=db.execute('''SELECT d.path,d.status,d.sha256,c.locator,c.text,d.mtime_ns
              FROM chunks c JOIN documents d ON d.id=c.document_id
              WHERE c.text LIKE ? ESCAPE '\\' '''+filter_sql+' LIMIT ?',('%'+escaped+'%',*params,limit)).fetchall()
            derived=db.execute('''SELECT d.path,d.status,d.sha256,c.locator,c.text,d.mtime_ns
              FROM pdf_derived c JOIN documents d ON d.id=c.document_id WHERE c.source_sha256=d.sha256 AND c.text LIKE ? ESCAPE '\\' '''+filter_sql+' LIMIT ?',('%'+escaped+'%',*params,limit)).fetchall()
        merged=[item for pair in itertools.zip_longest(rows,derived) for item in pair if item is not None]
        return merged[:limit]
    finally: db.close()
