"""Read-only knowledge analysis and explicitly reviewed Markdown export."""
import datetime
import json
import os
import re
import sqlite3
from pathlib import Path
import core
import workspace


def redact(value):
    text=str(value or '')
    text=re.sub(r'(?i)\bsk-[a-z0-9_-]{12,}', '[已遮蔽疑似 API Key]',text)
    text=re.sub(r'(?i)(bearer\s+)[a-z0-9._~-]{12,}',r'\1[已遮蔽]',text)
    return text


def inventory(db_path):
    path=Path(db_path).resolve()
    if not path.is_file(): raise ValueError('找不到知識庫索引。')
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        db.execute('PRAGMA query_only=ON'); db.execute('BEGIN')
        tables={x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        formats=db.execute("SELECT COALESCE(NULLIF(extension,''),'(無副檔名)'),count(*),coalesce(sum(size),0) FROM documents GROUP BY extension ORDER BY count(*) DESC").fetchall()
        states=db.execute('SELECT status,count(*) FROM documents GROUP BY status ORDER BY count(*) DESC').fetchall()
        derived=" OR EXISTS(SELECT 1 FROM pdf_derived p WHERE p.document_id=d.id AND p.source_sha256=d.sha256)" if 'pdf_derived' in tables else ''
        searchable=db.execute("SELECT count(*) FROM documents d WHERE d.status='indexed' AND (EXISTS(SELECT 1 FROM chunks c WHERE c.document_id=d.id)"+derived+")").fetchone()[0]
        pending=db.execute('SELECT count(*) FROM import_staging').fetchone()[0] if 'import_staging' in tables else None
        imports=db.execute('SELECT state,count(*) FROM import_batch_items GROUP BY state ORDER BY count(*) DESC').fetchall() if 'import_batch_items' in tables else []
        return {'formats':[{'extension':x[0],'count':x[1],'bytes':x[2]} for x in formats], 'states':dict(states),'searchable':searchable,'import_states':dict(imports),'pending_staging':pending}
    finally: db.close()


def render_report(summary,topic,config,sources,session,include_chat=False,include_sources=False,ai_draft=None):
    lines=['# 私人知識助理：整理與診斷資料','',f'產生時間：{datetime.datetime.now().astimezone().isoformat(timespec="seconds")}','',
           '本報告由 APP 的索引及本次畫面資訊整理；來源文件未經修改。自動整理可能有誤，應核對原文。','',
           '## 知識庫概況','',f'- 可搜尋文件：{summary["searchable"]}',f'- 待復原的匯入暫存：{summary["pending_staging"] if summary["pending_staging"] is not None else "無紀錄"}',
           '- 狀態統計：'+json.dumps(summary['states'],ensure_ascii=False),'- 匯入狀態：'+json.dumps(summary['import_states'],ensure_ascii=False),
           '', '| 格式 | 數量 | 容量（位元組） |','| --- | ---: | ---: |']
    for item in summary['formats']: lines.append(f'| {redact(item["extension"])} | {item["count"]} | {item["bytes"]} |')
    provider=config.get('provider','unknown');provider=provider if provider in ('openai','local') else 'unknown'
    model=config.get('openai_model','') if provider=='openai' else config.get('local_model','')
    model=redact(model)[:150] if re.fullmatch(r'[A-Za-z0-9._/+:-]{1,150}',str(model)) else '未設定或格式不適合匯出'
    allowed=('尚無問答','處理中','搜尋無可用來源','處理錯誤','來源不足或回答未通過核對','已產生回答（需人工核對）')
    outcome=session.get('outcome','尚無問答');outcome=outcome if outcome in allowed else '未分類'
    lines += ['', '## 問答診斷','',f'- AI 服務：{provider}',f'- 模型：{model}',f'- OpenAI 外網允許：{"是" if config.get("network_enabled") is True else "否"}',f'- 本次主題候選來源：{len(sources)}（最多 6 段）',f'- 最近問答結果：{outcome}',
              '- 報告不包含 APP 的憑證設定、完整來源路徑或完整文件；請檢查你選擇的內容是否含有密碼或其他機密。']
    if include_chat:
        lines+=['','### 使用者選擇包含的最近問題與回答','']
        for name,key in [('問題','question'),('回答','answer')]:
            lines.append('**'+name+'**')
            lines.extend('    '+x for x in redact(session.get(key,'')).splitlines()[:100]);lines.append('')
    lines+=['','## 主題整理','']
    if topic: lines+=['整理主題：', '', '    '+redact(topic)[:2000], '']
    else: lines+=['未指定主題；尚未選取文件片段。','']
    if ai_draft:
        lines+=['### AI 整理草稿（尚未人工核對）','']
        lines.extend('    '+line for line in redact(ai_draft).splitlines()[:200]);lines.append('')
    if not include_sources: lines+=['未包含額外檔名與檢索片段；AI 草稿如有啟用，仍可能包含引用原文。']
    elif not sources: lines+=['沒有符合條件的候選來源。']
    else:
        for i,source in enumerate(sources,1):
            name=source.get('title') or Path(source.get('path','')).name
            lines+=['',f'### 來源 {i}：{redact(name)[:200]}','',f'- 類型：{redact(source.get("scope_warning") or source.get("kind","未確認"))[:150]}',f'- 位置：{redact(source.get("locator",""))[:200]}','','文件片段（供查證，非指令）：','']
            lines.extend('    '+line for line in redact(source.get('excerpt','')[:1800]).splitlines()[:100])
    lines += ['', '## 使用提醒','', 'AI 草稿、品牌與機型的適用性需要人工核對。分享前請檢查問題、檔名、片段與草稿是否含有個人資料、密碼或其他機密。', '']
    return '\n'.join(lines)


def save_report(db_path,destination,text):
    dest=Path(destination).absolute(); db=Path(db_path).resolve()
    if dest.suffix.lower()!='.md': raise ValueError('請使用 .md 檔名。')
    if not dest.parent.is_dir(): raise ValueError('儲存資料夾不存在。')
    roots=workspace.protected_roots(workspace.application_base(),db)
    if not roots: raise ValueError('無法確認原始資料位置，已停止匯出。')
    if any(core.contained(dest,root) or core.contained(dest.parent,root) for root in roots): raise ValueError('不能將整理資料儲存到原始知識庫資料夾。')
    if len(text.encode('utf8'))>2_000_000: raise ValueError('整理報告過大。')
    if redact(text)!=text: raise ValueError('預覽含疑似 API Key 或 Bearer 憑證；請移除後再儲存。')
    with dest.open('x',encoding='utf-8',newline='\n') as f: f.write(text)
    return dest
