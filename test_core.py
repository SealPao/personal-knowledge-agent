import hashlib
import os
import sqlite3
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from pathlib import Path
import core

class PrototypeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.base=Path(self.tmp.name); self.source=self.base/'source'; self.source.mkdir(); self.db=self.base/'db.sqlite3'
        (self.source/'guide.md').write_text('# BOOX Nova\n設定無線網路的操作步驟\n',encoding='utf-8')
        (self.source/'manual.html').write_text('<html><head><title>測試</title></head><body><h1>充電</h1><p>使用五伏特充電器</p><script>不應索引</script><table><tr><td>功率</td><td>10W</td></tr></table></body></html>',encoding='utf-8')
        (self.source/'table.csv').write_text('機型,規格\nNova,5V\n',encoding='utf-8')
        (self.source/'other.bin').write_bytes(b'test')
    def tearDown(self): self.tmp.cleanup()
    def snapshot(self):
        return {p.name:(hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_mtime_ns) for p in self.source.iterdir()}
    def test_scan_search_incremental_source_unchanged(self):
        before=self.snapshot(); counts,complete=core.scan(str(self.source),str(self.db)); self.assertTrue(complete)
        self.assertEqual(counts['indexed'],3); self.assertEqual(counts['skipped'],1); self.assertEqual(before,self.snapshot())
        for word,locator in [('無線網路','第 1 行'),('充電器','充電'),('5V','CSV')]:
            hits=core.search(str(self.db),word); self.assertTrue(hits,word); self.assertIn(locator,hits[0][3])
        self.assertFalse(core.search(str(self.db),'不應索引'))
        again,_=core.scan(str(self.source),str(self.db)); self.assertEqual(again['unchanged'],3); self.assertEqual(before,self.snapshot())
    def test_missing_preserves_previous_citation(self):
        core.scan(str(self.source),str(self.db)); (self.source/'guide.md').unlink()
        core.scan(str(self.source),str(self.db)); hits=core.search(str(self.db),'無線網路'); self.assertEqual(hits[0][1],'missing')
    @unittest.skipUnless(os.name=='nt','Windows character conversion required')
    def test_chinese_script_variants_search_without_changing_sources(self):
        (self.source/'simplified.txt').write_text('电子阅读器充电操作说明',encoding='utf-8')
        before=self.snapshot(); core.scan(str(self.source),str(self.db))
        self.assertTrue(any('simplified.txt' in x[0] for x in core.search(str(self.db),'閱讀器充電')))
        self.assertTrue(any('manual.html' in x[0] for x in core.search(str(self.db),'充电器')))
        hits=core.search(str(self.db),'充電'); self.assertEqual(len(hits),len({(x[0],x[3],x[4]) for x in hits}))
        self.assertEqual(before,self.snapshot())
    def test_database_inside_source_rejected(self):
        with self.assertRaises(ValueError): core.scan(str(self.source),str(self.source/'bad.db'))
        self.assertFalse((self.source/'bad.db').exists())
    def test_changed_file_replaces_fts(self):
        core.scan(str(self.source),str(self.db)); (self.source/'guide.md').write_text('# 新版\n藍牙重新配對說明',encoding='utf-8')
        core.scan(str(self.source),str(self.db)); self.assertFalse(core.search(str(self.db),'無線網路')); self.assertTrue(core.search(str(self.db),'重新配對'))
        db=sqlite3.connect(self.db)
        try: old=db.execute('SELECT text FROM revision_chunks').fetchall()
        finally: db.close()
        self.assertTrue(any('無線網路' in row[0] for row in old))
    def test_cancel_does_not_mark_files_missing(self):
        core.scan(str(self.source),str(self.db)); core.scan(str(self.source),str(self.db),cancelled=lambda:True)
        self.assertEqual(core.search(str(self.db),'無線網路')[0][1],'indexed')
    def test_root_binding(self):
        core.scan(str(self.source),str(self.db)); other=self.base/'other'; other.mkdir()
        with self.assertRaises(ValueError): core.scan(str(other),str(self.db))
    def test_cards_are_separate_searchable_and_survive_source_rescan(self):
        before=self.snapshot(); core.scan(str(self.source),str(self.db))
        card=core.save_card(str(self.db),'手工筆記','特別關鍵字測試內容')
        self.assertFalse(core.contained(card,self.source)); self.assertEqual(before,self.snapshot())
        self.assertEqual(core.search(str(self.db),'特別關鍵字')[0][0],card)
        core.scan(str(self.source),str(self.db)); self.assertEqual(core.search(str(self.db),'特別關鍵字')[0][1],'indexed')
        core.save_card(str(self.db),'手工筆記','修改後的獨立知識卡',existing=card)
        self.assertFalse(core.search(str(self.db),'特別關鍵字')); self.assertTrue(core.search(str(self.db),'獨立知識卡'))
        self.assertEqual(before,self.snapshot())
        with self.assertRaises(ValueError): core.save_card(str(self.db),'x','y',existing=str(self.source/'guide.md'))
    def test_explicit_import_copy_is_separate_and_deduplicated(self):
        core.scan(str(self.source),str(self.db)); other=self.base/'外部操作.txt'; other.write_text('外部新增的專屬操作方式',encoding='utf-8')
        before=other.read_bytes(); path,created=core.import_copy(str(self.db),str(other))
        self.assertTrue(created); self.assertEqual(other.read_bytes(),before); self.assertFalse(core.contained(path,self.source))
        self.assertEqual(core.search(str(self.db),'專屬操作')[0][0],path)
        same,created=core.import_copy(str(self.db),str(other)); self.assertFalse(created); self.assertEqual(path,same)
        core.scan(str(self.source),str(self.db)); self.assertEqual(core.search(str(self.db),'專屬操作')[0][1],'indexed')
    def test_automatic_scan_skips_unchanged_source_reads(self):
        core.scan(str(self.source),str(self.db)); original_open=open
        def guarded(path,*args,**kwargs):
            if core.contained(path,self.source): raise AssertionError('unchanged source content was opened')
            return original_open(path,*args,**kwargs)
        with patch('builtins.open',side_effect=guarded):
            counts,complete=core.scan(str(self.source),str(self.db),fast=True)
        self.assertTrue(complete); self.assertEqual(counts['unchanged'],3)
    def test_docx_paragraph_table_and_no_macro_execution(self):
        path=self.source/'manual.docx'
        xml='''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>設定指南</w:t></w:r></w:p><w:p><w:r><w:t>測試無線網路</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>電壓</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>5V</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>'''
        with zipfile.ZipFile(path,'w') as z: z.writestr('word/document.xml',xml); z.writestr('word/vbaProject.bin',b'not executed')
        before=path.read_bytes(); core.scan(str(self.source),str(self.db)); self.assertEqual(before,path.read_bytes())
        hits=core.search(str(self.db),'無線網路',extension_filter='Word'); self.assertTrue(hits); self.assertIn('Word 第 2 段',hits[0][3])
        table=core.search(str(self.db),'電壓',extension_filter='Word'); self.assertIn('表格 1 第 1 列',table[0][3])
    def test_docx_dtd_rejected(self):
        path=self.source/'bad.docx'
        with zipfile.ZipFile(path,'w') as z: z.writestr('word/document.xml','<!DOCTYPE x><x/>')
        with self.assertRaises(ValueError): core.parse(path,path.read_bytes())
    def test_excel_formula_remains_literal_and_source_is_unchanged(self):
        import sys
        try: from openpyxl import Workbook
        except ImportError: self.skipTest('openpyxl unavailable')
        path=self.source/'spec.xlsx'; workbook=Workbook(); sheet=workbook.active; sheet.title='電池規格'
        sheet.append(['型號','電壓']); sheet.append(['測試設備',5]); sheet.append(['公式','=1+1']); workbook.save(path); workbook.close()
        before=path.read_bytes(); core.scan(str(self.source),str(self.db)); self.assertEqual(before,path.read_bytes())
        hits=core.search(str(self.db),'測試設備',extension_filter='Excel'); self.assertTrue(hits); self.assertIn('工作表 電池規格，第 2 列',hits[0][3])
        formula=core.search(str(self.db),'=1+1',extension_filter='Excel'); self.assertTrue(formula); self.assertIn('=1+1',formula[0][4])
    def test_pdf_partial_page_error_preserves_other_pages(self):
        import sys,types
        class Page:
            def __init__(self,value): self.value=value
            def extract_text(self):
                if self.value is None: raise ValueError('broken page')
                return self.value
        fake=types.SimpleNamespace(PdfReader=lambda _:types.SimpleNamespace(is_encrypted=False,pages=[Page('正常文字'),Page(None)]))
        with patch.dict(sys.modules,{'pypdf':fake}): chunks,detail=core.parse('manual.pdf',b'fake')
        self.assertEqual(chunks,[('PDF 第 1 頁','正常文字')]); self.assertIn('擷取失敗頁：2',detail)
    def test_format_filters_apply_to_long_and_short_queries(self):
        core.scan(str(self.source),str(self.db))
        self.assertTrue(core.search(str(self.db),'充電器',extension_filter='HTML'))
        self.assertFalse(core.search(str(self.db),'充電器',extension_filter='PDF'))
        self.assertTrue(core.search(str(self.db),'5V',extension_filter='CSV'))
        self.assertFalse(core.search(str(self.db),'5V',extension_filter='Markdown'))
    def test_brand_filter_limits_before_results_are_returned(self):
        (self.source/'Kobo.md').write_text('# Kobo\n無線網路設定',encoding='utf-8')
        core.scan(str(self.source),str(self.db))
        results=core.search(str(self.db),'無線網路',brand_filter='BOOX')
        self.assertTrue(results); self.assertTrue(all('Kobo.md' not in row[0] for row in results))
        self.assertTrue(core.search(str(self.db),'無線網路',brand_filter='Kobo'))

if __name__=='__main__': unittest.main()
