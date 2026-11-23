import io,json,unittest
from unittest.mock import patch
import web_search,ai_service,local_ai

HTML=b'''<html><a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Farticle">Manual</a><td class="result-snippet">Press <b>settings</b> for refresh.</td><a class="result-link" href="http://127.0.0.1/admin">bad</a><td class="result-snippet">private</td></html>'''
class Response(io.BytesIO):
    status=200
class WebSearchTests(unittest.TestCase):
    def test_search_parses_snippets_and_never_fetches_result_pages(self):
        calls=[]
        def opener(req,timeout): calls.append(req.full_url);return Response(HTML)
        results=web_search.search('BOOX refresh',opener=opener)
        self.assertEqual(len(calls),1); self.assertTrue(calls[0].startswith(web_search.SEARCH_URL)); self.assertEqual(results,[{'url':'https://example.org/article','title':'Manual','snippet':'Press settings for refresh.'}])
        sources=web_search.to_sources(results,4); self.assertEqual(sources[0]['id'],4); self.assertEqual(ai_service.source_state(sources[0]),'')
    def test_unsafe_urls_never_opened(self):
        for url in ('http://127.0.0.1/admin','http://192.168.1.1','http://localhost/','https://example.org@127.0.0.1','javascript:alert(1)'):
            self.assertFalse(web_search.safe_public_url(url)); self.assertNotEqual(ai_service.source_state({'kind':web_search.KIND,'url':url,'path':url}),'')
    def test_missing_or_blocked_results_fail_explicitly(self):
        with self.assertRaises(RuntimeError): web_search.search('query',opener=lambda req,timeout: Response(b'<html>challenge</html>'))
    def test_unsupported_english_mode_name_is_not_treated_as_evidence(self):
        quotes=[(1,'We provide different refresh modes.')]
        self.assertIn('Regal',ai_service._web_unsupported_terms('可以使用 Regal Mode。',quotes))
        self.assertEqual(ai_service._web_unsupported_terms('可以調整 refresh modes。',quotes),[])
    def test_web_snippets_are_marked_and_not_sent_to_search(self):
        sources=web_search.to_sources([{'url':'https://example.org/doc','title':'Official','snippet':'Open settings.'}])
        content,segments=ai_service._local_segment_input('How?',sources)
        self.assertIn('網路搜尋摘要（未驗證）',content); self.assertEqual(segments[1],(1,'Open settings.'))
        self.assertTrue(ai_service.matches_model('BOOX Go 7 refresh','go7')); self.assertFalse(ai_service.matches_model('BOOX Go 70 refresh','go7'))
if __name__=='__main__':unittest.main()
