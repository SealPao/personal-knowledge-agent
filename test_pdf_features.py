import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
import core,pdf_features,ai_service,package_service,ocr_batch,document_ui

pdf_features._dependencies()
try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    from PIL import Image,ImageDraw,ImageFont
except ImportError:
    canvas=None

@unittest.skipIf(canvas is None,'PDF fixture tools unavailable')
class PDFFeatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent); self.base=Path(self.tmp.name)
        self.src=self.base/'source'; self.src.mkdir(); self.pdf=self.src/'manual.pdf'
        c=canvas.Canvas(str(self.pdf),pagesize=(612,792)); c.setFont('Helvetica',14)
        c.drawString(60,720,'DEVICE SPECIFICATIONS')
        for x in (60,250,450): c.line(x,650,x,710)
        for y in (650,680,710): c.line(60,y,450,y)
        c.drawString(70,690,'Model'); c.drawString(260,690,'Power'); c.drawString(70,660,'READER'); c.drawString(260,660,'5 VOLTS'); c.showPage()
        image=Image.new('RGB',(1400,300),'white'); draw=ImageDraw.Draw(image)
        fontpath='C:/Windows/Fonts/arial.ttf'
        font=ImageFont.truetype(fontpath,80) if Path(fontpath).exists() else ImageFont.load_default()
        draw.text((40,60),'DEVICE CHARGING GUIDE',fill='black',font=font)
        c.drawImage(ImageReader(image),30,450,width=550,height=118); c.showPage(); c.save()
        self.original=self.pdf.read_bytes(); self.hash=hashlib.sha256(self.original).hexdigest()
        self.db=self.base/'data'/'knowledge.sqlite3'; self.db.parent.mkdir(); core.scan(str(self.src),str(self.db))
    def tearDown(self): self.tmp.cleanup()
    def test_render_table_save_search_and_backup(self):
        raw,sha=pdf_features.load_source(self.db,str(self.pdf)); png,pages=pdf_features.render_page(raw,1)
        self.assertTrue(png.startswith(b'\x89PNG')); self.assertEqual(pages,2)
        text,count=pdf_features.extract_tables(raw,1); self.assertGreaterEqual(count,1); self.assertIn('READER',text); self.assertIn('5 VOLTS',text)
        locator=pdf_features.save_derivative(self.db,str(self.pdf),sha,1,'table',text)
        hits=core.search(str(self.db),'5 VOLTS'); self.assertTrue(any('擷取表格' in h[3] for h in hits)); self.assertIn('第 1 頁',locator)
        copied=document_ui.indexed_card_copy(self.db,self.pdf)
        self.assertIn('已校對的 PDF 衍生文字',copied); self.assertIn('5 VOLTS',copied); self.assertIn(sha,copied)
        package=self.base/'backup.pka'; package_service.create_package(self.db,package); restored,_,_=package_service.restore_package(package,self.base,self.src)
        self.assertTrue(any('擷取表格' in h[3] for h in core.search(str(restored/'data'/'knowledge.sqlite3'),'5 VOLTS')))
        self.assertEqual(self.original,self.pdf.read_bytes())
    def test_real_english_ocr_and_citation(self):
        if 'eng' not in pdf_features.languages(): self.skipTest('English Tesseract data unavailable')
        raw,sha=pdf_features.load_source(self.db,str(self.pdf)); png,_=pdf_features.render_page(raw,2)
        text=pdf_features.run_ocr(png,'eng',self.base/'cache'); self.assertIn('CHARGING',text.upper())
        pdf_features.save_derivative(self.db,str(self.pdf),sha,2,'ocr',text)
        results=core.search(str(self.db),'CHARGING'); self.assertTrue(any('OCR' in hit[3] for hit in results))
        refs=ai_service.retrieve(str(self.db),'CHARGING'); self.assertTrue(any('OCR' in ref['kind'] for ref in refs))
        pdf_features.save_derivative(self.db,str(self.pdf),sha,2,'ocr',text+' corrected')
        import sqlite3
        conn=sqlite3.connect(self.db); old=conn.execute('SELECT text FROM pdf_derived_revisions').fetchall(); conn.close()
        self.assertIn((text,),old)
        self.assertEqual(self.original,self.pdf.read_bytes())
    def test_ocr_citation_survives_portable_move_and_export(self):
        import shutil
        if 'eng' not in pdf_features.languages(): self.skipTest('English Tesseract data unavailable')
        raw,sha=pdf_features.load_source(self.db,str(self.pdf)); png,_=pdf_features.render_page(raw,2)
        text=pdf_features.run_ocr(png,'eng',self.base/'cache')
        pdf_features.save_derivative(self.db,str(self.pdf),sha,2,'ocr',text)
        archive=self.base/'original.pka'; package_service.create_package(self.db,archive)
        import workspace
        a=self.base/'machine-a'; parent=workspace.library_parent(a); parent.mkdir(parents=True)
        library,_,_=package_service.restore_package(archive,parent,self.src)
        workspace.activate(a,library/'data'/'knowledge.sqlite3')
        b=self.base/'machine-b'; shutil.copytree(a,b); shutil.rmtree(a)
        moved=workspace.active_database(b)
        refs=ai_service.retrieve(str(moved),'CHARGING'); self.assertTrue(any('OCR' in ref['kind'] for ref in refs))
        result=next(hit for hit in core.search(str(moved),'CHARGING') if 'OCR' in hit[3])
        self.assertTrue(core.contained(result[0],b)); self.assertEqual(Path(result[0]).read_bytes(),self.original)
        pdf_features.load_source(moved,result[0])
        exported=self.base/'moved.pka'; package_service.create_package(moved,exported)
        restored,_,_=package_service.restore_package(exported,self.base,self.src)
        self.assertTrue(any('OCR' in ref['kind'] for ref in ai_service.retrieve(str(restored/'data'/'knowledge.sqlite3'),'CHARGING')))
        self.assertEqual(self.pdf.read_bytes(),self.original)
    def test_windows_traditional_chinese_ocr(self):
        langs=pdf_features.languages(); lang=next((x for x in langs if x.lower().startswith('windows:zh-hant')),None)
        if not lang: self.skipTest('Windows Traditional Chinese OCR unavailable')
        fontpath=Path('C:/Windows/Fonts/msjh.ttc')
        if not fontpath.exists(): self.skipTest('Traditional Chinese test font unavailable')
        import io
        image=Image.new('RGB',(1800,320),'white'); draw=ImageDraw.Draw(image)
        draw.text((50,80),'電子閱讀器充電操作說明',font=ImageFont.truetype(str(fontpath),88),fill='black')
        buffer=io.BytesIO(); image.save(buffer,format='PNG')
        text=pdf_features.run_ocr(buffer.getvalue(),lang,self.base/'cache')
        self.assertIn('閱讀器',text); self.assertIn('充電',text)
    def test_simplified_chinese_ocr_with_app_model(self):
        import io
        extra=Path(__file__).parent/'data'/'ocr'/'tessdata'
        if 'chi_sim' not in pdf_features.languages(extra): self.skipTest('Simplified Chinese OCR data unavailable')
        image=Image.new('RGB',(1800,320),'white'); draw=ImageDraw.Draw(image)
        draw.text((50,80),'电子阅读器充电操作说明',font=ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',88),fill='black')
        buffer=io.BytesIO(); image.save(buffer,format='PNG')
        text=pdf_features.run_ocr(buffer.getvalue(),'chi_sim',self.base/'cache',extra)
        self.assertIn('阅读器',text); self.assertIn('充电',text)
    def test_scanned_table_layout_and_ai_access(self):
        import io
        extra=Path(__file__).parent/'data'/'ocr'/'tessdata'
        if 'chi_sim' not in pdf_features.languages(extra): self.skipTest('Simplified Chinese OCR data unavailable')
        image=Image.new('RGB',(1600,520),'white'); draw=ImageDraw.Draw(image); font=ImageFont.truetype('C:/Windows/Fonts/msyh.ttc',72)
        draw.text((60,60),'型号',font=font,fill='black'); draw.text((750,60),'电压',font=font,fill='black')
        draw.text((60,240),'阅读器',font=font,fill='black'); draw.text((750,240),'五伏特',font=font,fill='black')
        stream=io.BytesIO(); image.save(stream,format='PNG')
        text=pdf_features.scanned_table(stream.getvalue(),'chi_sim',self.base/'cache',extra)
        self.assertIn('型号',text); self.assertIn('电压',text); self.assertIn(' | ',text)
        _,sha=pdf_features.load_source(self.db,str(self.pdf)); pdf_features.save_derivative(self.db,str(self.pdf),sha,2,'scanned_table',text)
        self.assertTrue(any('掃描表格 OCR' in x[3] for x in core.search(str(self.db),'阅读器')))
        self.assertTrue(any('掃描表格 OCR' in x['kind'] for x in ai_service.retrieve(str(self.db),'阅读器')))
    def test_windows_chinese_scanned_table(self):
        import io
        langs=pdf_features.languages(); lang=next((x for x in langs if x.lower().startswith('windows:zh-hant')),None)
        if not lang: self.skipTest('Windows Traditional Chinese OCR unavailable')
        image=Image.new('RGB',(1600,520),'white'); draw=ImageDraw.Draw(image); font=ImageFont.truetype('C:/Windows/Fonts/msjh.ttc',72)
        for xy,text in [((60,60),'型號'),((750,60),'電壓'),((60,240),'閱讀器'),((750,240),'五伏特')]: draw.text(xy,text,font=font,fill='black')
        stream=io.BytesIO(); image.save(stream,format='PNG')
        text=pdf_features.scanned_table(stream.getvalue(),lang,self.base/'cache')
        self.assertIn('型號',text); self.assertIn('電壓',text); self.assertIn(' | ',text)
    def test_batch_ocr_requires_review_before_search_and_ai(self):
        if 'eng' not in pdf_features.languages(): self.skipTest('English Tesseract data unavailable')
        stats=ocr_batch.run(self.db,str(self.pdf),2,2,'eng',None); self.assertEqual(stats['pending'],1)
        pages=ocr_batch.list_pages(self.db,str(self.pdf)); self.assertEqual(pages[0][3],'pending'); self.assertIn('CHARGING',pages[0][5].upper())
        self.assertFalse(core.search(str(self.db),'CHARGING')); self.assertFalse(ai_service.retrieve(str(self.db),'CHARGING'))
        archive=self.base/'pending.pka'; package_service.create_package(self.db,archive)
        restored,_,_=package_service.restore_package(archive,self.base,self.src)
        pending=ocr_batch.list_pages(restored/'data'/'knowledge.sqlite3',str(restored/'source'/'manual.pdf'))
        self.assertEqual(pending[0][3],'pending'); self.assertFalse(core.search(str(restored/'data'/'knowledge.sqlite3'),'CHARGING'))
        ocr_batch.approve(self.db,str(self.pdf),pages[0][0],pages[0][5]); self.assertTrue(core.search(str(self.db),'CHARGING'))
        self.assertTrue(any('OCR' in x['kind'] for x in ai_service.retrieve(str(self.db),'CHARGING')))
        self.assertEqual(ocr_batch.list_pages(self.db,str(self.pdf))[0][3],'reviewed')
        again=ocr_batch.run(self.db,str(self.pdf),2,2,'eng',None); self.assertEqual(again['skipped'],1)
        with self.assertRaises(ValueError): ocr_batch.run(self.db,str(self.pdf),1,30,'eng',None)
        self.assertEqual(self.original,self.pdf.read_bytes())
    def test_embedded_image_export_outside_source(self):
        raw,_=pdf_features.load_source(self.db,str(self.pdf)); image,extension,count=pdf_features.extract_embedded_image(raw,2,1)
        self.assertGreaterEqual(count,1); self.assertIn(extension,('.png','.jpg'))
        with self.assertRaises(ValueError): pdf_features.save_image(self.db,self.src/('unsafe'+extension),image)
        saved=pdf_features.save_image(self.db,self.base/('image'+extension),image)
        self.assertEqual(saved.read_bytes(),image); self.assertEqual(self.pdf.read_bytes(),self.original)
        with self.assertRaises(ValueError): pdf_features.save_image(self.db,saved,image)
    def test_stale_source_is_rejected(self):
        raw,sha=pdf_features.load_source(self.db,str(self.pdf))
        pdf_features.save_derivative(self.db,str(self.pdf),sha,2,'ocr','OLD OCR UNIQUE KEYWORD')
        self.assertTrue(any('OCR' in r[3] for r in core.search(str(self.db),'UNIQUE KEYWORD')))
        self.pdf.write_bytes(self.original+b'changed')
        with self.assertRaises(ValueError): pdf_features.save_derivative(self.db,str(self.pdf),sha,1,'ocr','old text')
        core.scan(str(self.src),str(self.db)); self.assertFalse(core.search(str(self.db),'UNIQUE KEYWORD'))
    def test_corrected_ocr_counts_as_searchable(self):
        from unittest.mock import patch
        from pypdf import PdfWriter
        import app,document_ui
        blank=self.src/'blank.pdf'; writer=PdfWriter(); writer.add_blank_page(width=100,height=100)
        with open(blank,'wb') as f: writer.write(f)
        core.scan(str(self.src),str(self.db)); _,sha=pdf_features.load_source(self.db,str(blank))
        with patch.object(app,'DB',self.db):
            a=app.App(); a.withdraw()
            try:
                self.assertIn('1 個可搜尋',a.metrics.get())
                pdf_features.save_derivative(self.db,str(blank),sha,1,'ocr','READABLE CONTENT')
                a.refresh_metrics(); self.assertIn('2 個可搜尋',a.metrics.get())
                w=document_ui.DocumentWindow(a,self.db); w.withdraw()
                self.assertTrue(any(row[0]==str(blank) and row[3] for row in w.rows)); w.destroy()
            finally: a.destroy()

if __name__=='__main__': unittest.main()
