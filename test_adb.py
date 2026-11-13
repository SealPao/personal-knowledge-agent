import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
import adb_service,ai_service,core,package_service,workspace,device_agent

class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).parent); self.base=Path(self.tmp.name)
        self.source=self.base/'source'; self.source.mkdir(); (self.source/'manual.txt').write_text('original',encoding='utf-8')
        self.db=self.base/'data'/'knowledge.sqlite3'; self.db.parent.mkdir(); core.scan(str(self.source),str(self.db))
        buffer=io.BytesIO(); Image.new('RGB',(30,30),'white').save(buffer,format='PNG'); self.png=buffer.getvalue()
    def tearDown(self): self.tmp.cleanup()
    def test_fixed_adb_arguments_and_device_states(self):
        calls=[]
        class Result: returncode=0; stdout=b'List of devices attached\nabc-123 device product:test\nxyz unauthorized\n'
        def runner(args,**kw): calls.append(args); return Result()
        with patch.object(adb_service,'executable',return_value='adb.exe'):
            self.assertEqual([x[1] for x in adb_service.devices(runner)],['device','unauthorized'])
            Result.stdout=self.png; self.assertEqual(adb_service.screenshot('abc-123',runner),self.png)
            with self.assertRaises(ValueError): adb_service.screenshot('abc; reboot',runner)
        self.assertEqual(calls[1],['adb.exe','-s','abc-123','exec-out','screencap','-p'])
    def test_device_info_reads_only_whitelisted_properties(self):
        calls=[]
        class Result: returncode=0; stdout=b'Test device\n'
        def runner(args,**kw): calls.append(args); return Result()
        with patch.object(adb_service,'executable',return_value='adb.exe'):
            info=adb_service.device_info('reader-123',runner)
            self.assertEqual(info['model'],'Test device'); self.assertEqual(len(calls),4)
            self.assertEqual({x[-1] for x in calls},{'ro.product.manufacturer','ro.product.model','ro.build.version.release','ro.build.display.id'})
            self.assertTrue(all(x[1:5]==['-s','reader-123','shell','getprop'] for x in calls))
            with self.assertRaises(ValueError): adb_service.device_info('reader;reboot',runner)
    def test_local_screen_ocr_uses_bundled_languages_and_temporary_copy(self):
        from unittest.mock import patch
        root=self.base/'engine'; data=root/'tessdata'; data.mkdir(parents=True); exe=root/'tesseract.exe'; exe.write_bytes(b'test')
        for name in ('chi_tra','chi_sim','eng'): (data/(name+'.traineddata')).write_bytes(b'test')
        calls=[]
        class Result: returncode=0; stdout='裝置設定'.encode('utf-8')
        def run(args,**kwargs):
            calls.append(args); self.assertTrue(Path(args[1]).is_file()); return Result()
        with patch.object(adb_service.pdf_features,'tesseract_path',return_value=str(exe)),patch.object(adb_service.subprocess,'run',side_effect=run):
            self.assertEqual(adb_service.screen_text(self.png,self.base/'cache'),'裝置設定')
        self.assertIn('chi_tra+chi_sim+eng',calls[0]); self.assertFalse(Path(calls[0][1]).exists())
    def test_reviewed_note_and_image_survive_backup(self):
        original=(self.source/'manual.txt').read_bytes()
        card=adb_service.save_note(self.db,'閱讀器設定紀錄','第一步開啟裝置的閱讀設定。第二步選擇文字大小。',self.png)
        self.assertEqual(adb_service.read_capture(self.db,card),self.png)
        refs=ai_service.retrieve(str(self.db),'閱讀設定'); self.assertTrue(any(x['path']==card for x in refs))
        archive=self.base/'backup.pka'; package_service.create_package(self.db,archive)
        restored,_,_=package_service.restore_package(archive,self.base,self.source)
        restored_db=restored/'data'/'knowledge.sqlite3'; rows=core.search(str(restored_db),'閱讀設定'); self.assertTrue(rows)
        self.assertEqual(adb_service.read_capture(restored_db,rows[0][0]),self.png)
        self.assertEqual((self.source/'manual.txt').read_bytes(),original)
    def test_invalid_screenshot_and_empty_note_rejected(self):
        with self.assertRaises(ValueError): adb_service.save_note(self.db,'title','test',b'bad')
        with self.assertRaises(ValueError): adb_service.save_note(self.db,'title','',self.png)
    def test_visual_plan_payload_and_restricted_actions(self):
        calls=[]
        def post(url,headers,payload):
            calls.append((url,headers,payload))
            return {'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'{"type":"tap","x":300,"y":500,"reason":"查看選項","observation":"顯示選單"}'}]}],'usage':{'input_tokens':10,'output_tokens':10}}
        config={'provider':'openai','network_enabled':True,'openai_model':'fake','max_tokens':800}
        action,usage=device_agent.plan(self.png,'查看設定',[],config,'test-key',post)
        self.assertEqual(action['type'],'tap'); self.assertEqual(calls[0][0],'https://api.openai.com/v1/responses')
        self.assertFalse(calls[0][2]['store']); self.assertTrue(calls[0][2]['input'][0]['content'][1]['image_url'].startswith('data:image/png;base64,'))
        config['network_enabled']=False
        with self.assertRaises(ValueError): device_agent.plan(self.png,'目標',[],config,'key',post)
        def bad(*args): return {'output':[{'type':'message','content':[{'type':'output_text','text':'{"type":"shell","reason":"x","observation":"x"}'}]}]}
        config['network_enabled']=True
        with self.assertRaises(ValueError): device_agent.plan(self.png,'目標',[],config,'key',bad)
        device_agent.plan(self.png,'查看設定',[],config,'test-key',post,screen_text='裝置設定')
        self.assertIn('裝置設定',calls[-1][2]['input'][0]['content'][0]['text'])
    def test_action_requires_unchanged_screen_and_valid_coordinates(self):
        size,digest=adb_service.image_info(self.png); calls=[]
        class Result: returncode=0; stdout=self.png
        def runner(args,**kw): calls.append(args); return Result()
        with patch.object(adb_service,'executable',return_value='adb.exe'):
            adb_service.perform('reader-1',{'type':'tap','x':500,'y':500},size,digest,runner)
            self.assertEqual(calls[-1],['adb.exe','-s','reader-1','shell','input','tap','14','14'])
            calls.clear()
            with self.assertRaises(ValueError): adb_service.perform('reader-1',{'type':'tap','x':500,'y':500},size,'wrong',runner)
            self.assertEqual(len(calls),1)
            with self.assertRaises(ValueError): adb_service.perform('reader-1',{'type':'tap','x':9999,'y':500},size,digest,runner)
    def test_multiple_session_images_survive_package(self):
        card=adb_service.save_session(self.db,'操作紀錄','閱讀器操作目標與已確認步驟',[self.png,self.png])
        self.assertTrue(any('尚未人工校對' in ref['kind'] for ref in ai_service.retrieve(str(self.db),'閱讀器操作目標')))
        archive=self.base/'session.pka'; package_service.create_package(self.db,archive)
        restored,_,_=package_service.restore_package(archive,self.base,self.source)
        db=restored/'data'/'knowledge.sqlite3'; path=core.search(str(db),'操作目標')[0][0]
        self.assertEqual(adb_service.read_captures(db,path),[self.png,self.png])
    def test_finish_requires_answer(self):
        config={'provider':'local','local_model':'fake','local_base':'http://127.0.0.1:11434/v1','max_tokens':800}
        def post(*args): return {'choices':[{'message':{'content':'{"type":"finish","reason":"找到","observation":"設定頁面"}'}}]}
        with self.assertRaises(ValueError): device_agent.plan(self.png,'去哪裡重置裝置',[],config,post=post)
    def test_checkpoint_updates_one_record_and_preserves_images(self):
        first=adb_service.save_session(self.db,'探索中','正在尋找重置入口',[self.png])
        again=adb_service.save_session(self.db,'探索中','已走過設定頁；尚未完成',[self.png,self.png],existing=first)
        self.assertEqual(first,again); self.assertEqual(adb_service.read_captures(self.db,first),[self.png,self.png])
        self.assertTrue(core.search(str(self.db),'尚未完成'))
        archive=self.base/'interrupted.pka'; package_service.create_package(self.db,archive)
        restored,_,_=package_service.restore_package(archive,self.base,self.source)
        db=restored/'data'/'knowledge.sqlite3'; path=core.search(str(db),'尚未完成')[0][0]
        self.assertEqual(adb_service.read_captures(db,path),[self.png,self.png])

if __name__=='__main__': unittest.main()
