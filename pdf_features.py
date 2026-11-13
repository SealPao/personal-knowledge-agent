"""On-demand PDF rendering, local OCR and user-reviewed searchable derivatives."""
import hashlib
import base64
import csv
import io
import json
import os
import re
import shutil
import stat
import statistics
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
import core

BUNDLE=Path(os.environ.get('PKA_PYTHON_PACKAGES', str(Path(__file__).resolve().parent / 'vendor')))
_PDFIUM_LOCK=threading.Lock()
WINOCR_LIST=r'''
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null=[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
[Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object { [Console]::WriteLine($_.LanguageTag) }
'''
WINOCR_RUN=r'''
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null=[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$null=[Windows.Storage.FileAccessMode,Windows.Storage,ContentType=WindowsRuntime]
$null=[Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation,ContentType=WindowsRuntime]
$null=[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null=[Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]
$method=[System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
function Await($operation,$type) { $task=$method.MakeGenericMethod($type).Invoke($null,@($operation)); return $task.GetAwaiter().GetResult() }
$file=Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($env:PKA_OCR_IMAGE)) ([Windows.Storage.StorageFile])
$stream=Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder=Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap=Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$language=New-Object Windows.Globalization.Language $env:PKA_OCR_LANG
$engine=[Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if ($null -eq $engine) { throw 'OCR language unavailable' }
$result=Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
foreach ($line in $result.Lines) { [Console]::WriteLine($line.Text) }
'''
WINOCR_LAYOUT=WINOCR_RUN.replace("foreach ($line in $result.Lines) { [Console]::WriteLine($line.Text) }",r'''
$words=@(foreach ($line in $result.Lines) { foreach ($word in $line.Words) {
  [pscustomobject]@{text=$word.Text;x=$word.BoundingRect.X;y=$word.BoundingRect.Y;w=$word.BoundingRect.Width;h=$word.BoundingRect.Height}
} })
[Console]::Write((ConvertTo-Json -InputObject $words -Depth 4 -Compress))
''')
def _dependencies():
    if BUNDLE.is_dir() and str(BUNDLE) not in sys.path: sys.path.append(str(BUNDLE))

def load_source(db_path,path):
    db=core.connect(str(db_path))
    try: row=db.execute('SELECT id,sha256,size,mtime_ns,status FROM documents WHERE path=? AND extension=?',(str(path),'.pdf')).fetchone()
    finally: db.close()
    if not row or not row[1] or row[4]!='indexed': raise ValueError('PDF 尚未完成索引，請先重新掃描')
    st=os.stat(path,follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode) or getattr(st,'st_file_attributes',0)&(core.PLACEHOLDER|core.REPARSE): raise ValueError('PDF 是雲端佔位檔或連結，已停止處理')
    if st.st_size>core.MAX_SIZE: raise ValueError('PDF 超過 250 MiB 安全上限')
    with open(path,'rb') as f: data=f.read(core.MAX_SIZE+1)
    after=os.stat(path,follow_symlinks=False)
    if len(data)>core.MAX_SIZE or (st.st_size,st.st_mtime_ns)!=(after.st_size,after.st_mtime_ns) or hashlib.sha256(data).hexdigest()!=row[1]:
        raise ValueError('PDF 已變更，請先重新掃描再處理')
    return data,row[1]

def render_page(data,page_number):
    # PDFium is not thread-safe; all PDFium work in this process is serialized.
    with _PDFIUM_LOCK: return _render_page(data,page_number)

def _render_page(data,page_number):
    _dependencies()
    try: import pypdfium2 as pdfium
    except ImportError: raise RuntimeError('缺少 PDF 頁面顯示元件') from None
    doc=pdfium.PdfDocument(data)
    try:
        count=len(doc)
        if page_number<1 or page_number>count: raise ValueError(f'頁碼必須介於 1 和 {count}')
        page=doc[page_number-1]
        try:
            width,height=page.get_size()
            if width<=0 or height<=0: raise ValueError('PDF 頁面尺寸無效')
            scale=min(2.0,2200/max(width,height),(5_000_000/(width*height))**.5)
            bitmap=page.render(scale=scale)
            try:
                image=bitmap.to_pil(); buf=io.BytesIO(); image.save(buf,format='PNG'); return buf.getvalue(),count
            finally: bitmap.close()
        finally: page.close()
    finally: doc.close()

def extract_tables(data,page_number):
    _dependencies()
    try: import pdfplumber
    except ImportError: raise RuntimeError('缺少 PDF 表格辨識元件') from None
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        if page_number<1 or page_number>len(pdf.pages): raise ValueError(f'頁碼必須介於 1 和 {len(pdf.pages)}')
        tables=pdf.pages[page_number-1].extract_tables()
        lines=[]; count=0
        for i,table in enumerate(tables,1):
            lines.append(f'表格 {i}（每欄以 | 分隔）')
            for row in table:
                count+=1
                if count>2000: raise ValueError('此頁表格列數超過安全上限')
                if len(row)>100: raise ValueError('此頁表格欄數超過安全上限')
                lines.append(' | '.join((str(cell or '').replace('\n',' ')[:10000]) for cell in row))
            lines.append('')
    return '\n'.join(lines).strip(),len(tables)

def extract_embedded_image(data,page_number,index):
    _dependencies()
    try: from pypdf import PdfReader
    except ImportError: raise RuntimeError('缺少 PDF 圖片擷取元件') from None
    reader=PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(''): raise ValueError('PDF 受密碼保護')
    if page_number<1 or page_number>len(reader.pages): raise ValueError('PDF 頁碼超出範圍')
    images=reader.pages[page_number-1].images; count=len(images)
    if index<1 or index>count: raise ValueError(f'此頁有 {count} 張可擷取的內嵌圖片，請輸入有效編號')
    image=images[index-1]; data=image.data
    if len(data)>32*1024*1024: raise ValueError('圖片超過 32 MiB 安全上限')
    if data.startswith(b'\x89PNG\r\n\x1a\n'): extension='.png'
    elif data.startswith(b'\xff\xd8\xff'): extension='.jpg'
    else: raise ValueError('此圖片格式目前不支援安全輸出')
    return data,extension,count

def save_image(db_path,destination,image_data,protected_roots=()):
    dest=Path(destination).resolve(); db=core.connect(str(db_path))
    try: row=db.execute("SELECT value FROM settings WHERE key='root'").fetchone()
    finally: db.close()
    roots=list(protected_roots)+([row[0]] if row else [])
    if any(core.contained(dest,root) for root in roots): raise ValueError('圖片不能儲存在原始知識庫內')
    if len(image_data)>32*1024*1024 or not image_data: raise ValueError('圖片資料無效')
    if dest.exists(): raise ValueError('目的檔案已存在，請選擇新的檔名')
    with tempfile.TemporaryDirectory(prefix='pka-image-',dir=dest.parent) as td:
        temp=Path(td)/'image'
        with open(temp,'xb') as f: f.write(image_data); f.flush(); os.fsync(f.fileno())
        try:
            if os.name=='nt': os.rename(temp,dest)
            else: os.link(temp,dest)
        except FileExistsError: raise ValueError('目的檔案已存在，請選擇新的檔名') from None
    return dest

def tesseract_path():
    # The portable edition keeps its OCR engine next to the application.
    program=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent
    bundled=program/'ocr'/'tesseract'/'tesseract.exe'
    if bundled.is_file(): return str(bundled)
    candidate=shutil.which('tesseract')
    if candidate: return candidate
    default=Path('C:/Program Files/Tesseract-OCR/tesseract.exe')
    return str(default) if default.is_file() else None

def _windows_command(script,env=None,timeout=30):
    exe=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32'/'WindowsPowerShell'/'v1.0'/'powershell.exe'
    if os.name!='nt' or not exe.is_file(): raise RuntimeError('Windows OCR 不可用')
    encoded=base64.b64encode(script.encode('utf-16le')).decode('ascii')
    return subprocess.run([str(exe),'-NoProfile','-NonInteractive','-EncodedCommand',encoded],env=env,capture_output=True,timeout=timeout,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))

def windows_languages():
    try:
        result=_windows_command(WINOCR_LIST,timeout=15)
        if result.returncode: return []
        return [x.strip() for x in result.stdout.decode('utf-8','replace').splitlines() if re.fullmatch(r'[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*',x.strip())]
    except (OSError,subprocess.SubprocessError,RuntimeError): return []

def languages(extra_dir=None):
    exe=tesseract_path()
    windows=['Windows:'+x for x in windows_languages()]
    if not exe: return windows
    flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
    result=subprocess.run([exe,'--list-langs'],capture_output=True,timeout=15,creationflags=flags)
    if result.returncode!=0: return windows
    found=[x.strip() for x in result.stdout.decode('utf-8','replace').splitlines()[1:] if re.fullmatch(r'[A-Za-z0-9_]+',x.strip()) and x.strip()!='osd']
    if extra_dir and Path(extra_dir).is_dir():
        for path in Path(extra_dir).glob('*.traineddata'):
            if re.fullmatch(r'[A-Za-z0-9_]+',path.stem) and not path.is_symlink(): found.append(path.stem)
    return windows+list(dict.fromkeys(found))

def run_ocr(png,language,temp_root,extra_dir=None,available=None):
    available=available if available is not None else languages(extra_dir)
    if language not in available: raise ValueError('此 OCR 語言尚未安裝；程式不會自動下載語言資料')
    temp_root=Path(temp_root); temp_root.mkdir(parents=True,exist_ok=True)
    # tempfile creates a new directory in app storage; never writes next to the PDF.
    with tempfile.TemporaryDirectory(prefix='ocr-',dir=temp_root) as td:
        image=Path(td)/'page.png'; image.write_bytes(png)
        if language.startswith('Windows:'):
            env=dict(os.environ,PKA_OCR_IMAGE=str(image.resolve()),PKA_OCR_LANG=language.split(':',1)[1])
            result=_windows_command(WINOCR_RUN,env=env,timeout=120)
        else:
            exe=tesseract_path()
            if not exe: raise RuntimeError('此電腦未安裝 Tesseract OCR')
            cmd=[exe,str(image),'stdout','-l',language]
            if extra_dir and (Path(extra_dir)/(language+'.traineddata')).is_file(): cmd.extend(['--tessdata-dir',str(extra_dir)])
            result=subprocess.run(cmd,capture_output=True,timeout=120,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode: raise RuntimeError('OCR 執行失敗，請檢查語言資料及圖片')
        if len(result.stdout)>2_000_000: raise ValueError('OCR 結果超過安全上限')
        text=result.stdout.decode('utf-8','replace').strip()
        if language.startswith('Windows:') or language.startswith('chi_'): text=re.sub(r'(?<=[\u3400-\u9fff])[ \t]+(?=[\u3400-\u9fff])','',text)
        return text

def _layout_table(words):
    words=[w for w in words if str(w.get('text','')).strip() and float(w.get('w',0))>0 and float(w.get('h',0))>0]
    if not words: raise ValueError('未辨識出可用文字，無法推測表格')
    if len(words)>10000: raise ValueError('此頁辨識文字過多')
    median=statistics.median(float(w['h']) for w in words); tolerance=max(4.0,median*.65)
    rows=[]
    for w in sorted(words,key=lambda v:(float(v['y'])+float(v['h'])/2,float(v['x']))):
        center=float(w['y'])+float(w['h'])/2
        if rows and abs(center-rows[-1]['y'])<=tolerance:
            rows[-1]['words'].append(w)
        else: rows.append({'y':center,'words':[w]})
    if len(rows)>500: raise ValueError('此頁辨識列數過多')
    grouped=[]; starts=[]; gap_limit=max(12.0,median*1.15)
    for row in rows:
        cells=[]
        for w in sorted(row['words'],key=lambda z:float(z['x'])):
            x=float(w['x']); right=x+float(w['w']); text=str(w['text']).strip()
            if cells and x-cells[-1]['right']<gap_limit:
                cells[-1]['parts'].append(text); cells[-1]['right']=max(cells[-1]['right'],right)
            else: cells.append({'x':x,'right':right,'parts':[text]})
        grouped.append(cells); starts.extend(c['x'] for c in cells)
    clusters=[]
    for x in sorted(starts):
        if clusters and x-statistics.median(clusters[-1])<max(18,median*1.4): clusters[-1].append(x)
        else: clusters.append([x])
    anchors=[statistics.median(c) for c in clusters]
    if len(anchors)>50: raise ValueError('推測欄數過多，請縮小範圍或手動整理')
    lines=[]
    for cells in grouped:
        values=['']*len(anchors)
        for c in cells:
            idx=min(range(len(anchors)),key=lambda j:abs(anchors[j]-c['x']))
            value=' '.join(c['parts']); value=re.sub(r'(?<=[\u3400-\u9fff])[ \t]+(?=[\u3400-\u9fff])','',value)
            values[idx]=(values[idx]+' '+value).strip()
        lines.append(' | '.join(values).rstrip(' |'))
    return '掃描表格 OCR 推測（欄位可能錯置，請核對）\n'+'\n'.join(lines)

def scanned_table(png,language,temp_root,extra_dir=None):
    if language not in languages(extra_dir): raise ValueError('所選 OCR 語言尚未安裝')
    temp_root=Path(temp_root); temp_root.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ocr-table-',dir=temp_root) as td:
        image=Path(td)/'page.png'; image.write_bytes(png)
        if language.startswith('Windows:'):
            env=dict(os.environ,PKA_OCR_IMAGE=str(image.resolve()),PKA_OCR_LANG=language.split(':',1)[1])
            result=_windows_command(WINOCR_LAYOUT,env=env,timeout=120)
            if result.returncode: raise RuntimeError('Windows OCR 表格辨識失敗')
            if len(result.stdout)>5_000_000: raise ValueError('OCR 結果超過安全上限')
            words=json.loads(result.stdout.decode('utf-8','replace'))
            if isinstance(words,dict): words=[words]
        else:
            exe=tesseract_path()
            if not exe: raise RuntimeError('此電腦未安裝 Tesseract OCR')
            cmd=[exe,str(image),'stdout','-l',language]
            if extra_dir and (Path(extra_dir)/(language+'.traineddata')).is_file(): cmd.extend(['--tessdata-dir',str(extra_dir)])
            cmd.extend(['--psm','6','-c','tessedit_create_tsv=1']); result=subprocess.run(cmd,capture_output=True,timeout=120,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if result.returncode: raise RuntimeError('OCR 表格辨識失敗')
            if len(result.stdout)>5_000_000: raise ValueError('OCR 結果超過安全上限')
            words=[]
            for row in csv.DictReader(io.StringIO(result.stdout.decode('utf-8','replace')),delimiter='\t'):
                if (row.get('text') or '').strip():
                    try: words.append({'text':row['text'],'x':int(row['left']),'y':int(row['top']),'w':int(row['width']),'h':int(row['height'])})
                    except (ValueError,KeyError): continue
        return _layout_table(words)

def save_derivative(db_path,path,source_sha,page,kind,text):
    if kind not in ('ocr','table','scanned_table') or not 1<=int(page)<=100000: raise ValueError('PDF 頁碼或內容種類無效')
    text=text.strip()
    if not text or len(text)>250000: raise ValueError('內容不可為空或超過 250,000 字元')
    # Recheck the exact source bytes at save time so stale work is never attached to a newer file.
    _,current_sha=load_source(db_path,path)
    if current_sha!=source_sha: raise ValueError('來源已變更，請重新擷取')
    db=core.connect(str(db_path))
    try:
        doc=db.execute('SELECT id,sha256 FROM documents WHERE path=?',(str(path),)).fetchone()
        if not doc or doc[1]!=source_sha: raise ValueError('來源索引已變更，請重新擷取')
        label={'ocr':'OCR 文字（使用者校對）','table':'擷取表格（使用者校對）','scanned_table':'掃描表格 OCR（使用者校對）'}[kind]
        locator=f'PDF 第 {page} 頁 · '+label
        with db:
            existing=db.execute('SELECT id,text FROM pdf_derived WHERE document_id=? AND source_sha256=? AND page=? AND kind=?',(doc[0],source_sha,int(page),kind)).fetchone()
            if existing and existing[1]!=text:
                db.execute("INSERT INTO pdf_derived_revisions(derived_id,text,recorded_at) VALUES(?,?,datetime('now','localtime'))",(existing[0],existing[1]))
            db.execute('''INSERT INTO pdf_derived(document_id,source_sha256,page,kind,locator,text,updated_at)
              VALUES(?,?,?,?,?,?,datetime('now','localtime')) ON CONFLICT(document_id,source_sha256,page,kind)
              DO UPDATE SET locator=excluded.locator,text=excluded.text,updated_at=excluded.updated_at''',(doc[0],source_sha,int(page),kind,locator,text))
            ident=db.execute('SELECT id FROM pdf_derived WHERE document_id=? AND source_sha256=? AND page=? AND kind=?',(doc[0],source_sha,int(page),kind)).fetchone()[0]
            db.execute('DELETE FROM pdf_derived_fts WHERE rowid=?',(ident,)); db.execute('INSERT INTO pdf_derived_fts(rowid,text) VALUES(?,?)',(ident,text))
        return locator
    finally: db.close()
