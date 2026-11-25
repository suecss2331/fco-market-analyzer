"""카카오 나에게 보내기: 설정 · state 확인 · 토큰 저장 · 전송 (네트워크는 가짜로)."""
import importlib
import json
import os
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse


class Kakao(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FCO_CONFIG_DIR"] = self.tmp.name
        import fco.config, fco.kakao
        importlib.reload(fco.config)
        self.k = importlib.reload(fco.kakao)
        self.calls = []

        def fake_post(url, form, token=None):
            self.calls.append((url, form, token))
            if url == self.k.TOKEN_URL:
                return {"access_token": "AT", "expires_in": 21599, "refresh_token": "RT", "refresh_token_expires_in": 5183999}
            return {"result_code": 0}
        self.k._post = fake_post
        self.k.pc_kakao_running = lambda: True

    def tearDown(self):
        os.environ.pop("FCO_CONFIG_DIR", None)
        import fco.config, fco.kakao
        importlib.reload(fco.config)
        importlib.reload(fco.kakao)
        self.tmp.cleanup()

    def test_flow(self):
        k = self.k
        self.assertEqual(k.notify_tunnel_url("https://a.trycloudflare.com"), "카카오 미연결 — 건너뜀")
        k.set_keys(" KEY ")
        url = k.login_url()
        q = parse_qs(urlparse(url).query)
        self.assertEqual(q["client_id"], ["KEY"])
        self.assertEqual(q["scope"], ["talk_message"])
        self.assertFalse(k.valid_state("forged"))
        self.assertTrue(k.valid_state(q["state"][0]))
        self.assertFalse(k.valid_state(q["state"][0]))           # 1회용
        k.exchange_code("CODE")
        self.assertTrue(k.status()["connected"])
        self.assertEqual(k.notify_tunnel_url("https://a.trycloudflare.com"), "카카오 전송 완료")
        url_, form, token = self.calls[-1]
        self.assertEqual((url_, token), (k.MEMO_URL, "AT"))
        tmpl = json.loads(form["template_object"])
        self.assertEqual(tmpl["text"], "https://a.trycloudflare.com")   # 링크만
        self.assertNotIn("button_title", tmpl)

    def test_only_when_running(self):
        k = self.k
        k.set_keys("KEY")
        k.exchange_code("CODE")
        k.pc_kakao_running = lambda: False
        k.time.sleep = lambda s: None                            # 5분 기다림 건너뛰기
        self.assertIn("꺼져", k.notify_tunnel_url("https://b.trycloudflare.com"))
        k.set_only_when_running(False)
        self.assertEqual(k.notify_tunnel_url("https://b.trycloudflare.com"), "카카오 전송 완료")

    def test_blank_fields_keep_saved(self):
        k = self.k
        k.set_keys("KEY", "SECRET")
        k.set_keys("", "")                                       # 빈 칸으로 다시 저장해도 유지
        self.assertEqual((k.cfg()["rest_api_key"], k.cfg()["client_secret"]), ("KEY", "SECRET"))
        k.exchange_code("CODE")
        self.assertEqual(self.calls[-1][1]["client_secret"], "SECRET")

    def test_key_change_resets_connection(self):
        k = self.k
        k.set_keys("KEY")
        k.exchange_code("CODE")
        k.set_keys("OTHER")
        self.assertFalse(k.status()["connected"])


if __name__ == "__main__":
    unittest.main()
