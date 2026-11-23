import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import core,analysis_export
class AnalysisExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent);self.base=Path(self.tmp.name);self.source=self.base/'source';self.source.mkdir()
        self.original=self.source/'secret-product.md';self.original.write_text('# 機型\n私人文件正文\n',encoding='utf8');self.before=self.original.read_bytes()
        self.db=self.base/'app'/'knowledge.sqlite3';self.db.parent.mkdir();core.scan(str(self.source),str(self.db))
    def tearDown(self):
        self.assertEqual(self.original.read_bytes(),self.before);self.tmp.cleanup()
    def test_default_aggregate_excludes_file_names_paths_and_content(self):
        stats=analysis_export.inventory(self.db);self.assertEqual(stats['searchable'],1)
        sources=[{'path':str(self.original),'kind':'來源文件','locator':'私人標題','excerpt':'私人文件正文'}]
        report=analysis_export.render_report(stats,'主題',{'provider':'local'},sources,{'question':'私密問題','answer':'私密回答'})
        for value in ('secret-product','私人文件正文','私密問題',str(self.source),'私人標題'):self.assertNotIn(value,report)
    def test_explicit_fields_render_and_redact_known_key_patterns(self):
        stats=analysis_export.inventory(self.db);key='sk-'+'x'*35
        sources=[{'path':str(self.original),'kind':'文件','locator':'段落','excerpt':'正文 '+key}]
        report=analysis_export.render_report(stats,'前光',{'provider':'local'},sources,{'question':'如何設定','answer':'回答 '+key},True,True,'草稿 '+key)
        self.assertIn('secret-product.md',report);self.assertIn('如何設定',report);self.assertIn('正文',report);self.assertIn('AI 整理草稿',report);self.assertNotIn(key,report)
    def test_save_refuses_source_and_existing_files(self):
        report='# 預覽\n';out=self.base/'out';out.mkdir()
        with self.assertRaisesRegex(ValueError,'原始知識庫'):analysis_export.save_report(self.db,self.source/'report.md',report)
        path=out/'result.md';analysis_export.save_report(self.db,path,report);self.assertEqual(path.read_text(encoding='utf8'),report)
        with self.assertRaises(FileExistsError):analysis_export.save_report(self.db,path,'other')
        self.assertEqual(path.read_text(encoding='utf8'),report)
    def test_save_rejects_key_added_after_preview(self):
        out=self.base/'out';out.mkdir()
        with self.assertRaisesRegex(ValueError,'疑似 API Key'):analysis_export.save_report(self.db,out/'x.md','sk-'+'x'*35)
        self.assertFalse((out/'x.md').exists())
if __name__=='__main__':unittest.main()
