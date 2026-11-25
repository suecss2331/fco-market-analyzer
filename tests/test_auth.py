"""대시보드 로그인: 해시 비교 · 서명 쿠키 · 잠금 (임시 설정 폴더에서)."""
import importlib
import os
import tempfile
import unittest


class Auth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FCO_CONFIG_DIR"] = self.tmp.name
        import fco.config, fco.auth
        importlib.reload(fco.config)
        self.auth = importlib.reload(fco.auth)
        self.auth.reset_limits()

    def tearDown(self):
        os.environ.pop("FCO_CONFIG_DIR", None)
        import fco.config, fco.auth
        importlib.reload(fco.config)
        importlib.reload(fco.auth)
        self.tmp.cleanup()

    def test_password_and_token(self):
        a = self.auth
        self.assertFalse(a.enabled())
        a.set_password("1234")
        self.assertTrue(a.enabled())
        self.assertTrue(a.check_password("1234"))
        self.assertFalse(a.check_password("1235"))
        self.assertNotIn("1234", a.SECRETS.read_text(encoding="utf-8"))   # 원문 저장 안 함
        t = a.make_token(now=1000)
        self.assertTrue(a.valid_token(t, now=2000))
        self.assertFalse(a.valid_token(t, now=1000 + 31 * 86400))         # 30일 지나면 만료
        self.assertFalse(a.valid_token(t[:-1] + ("0" if t[-1] != "0" else "1"), now=2000))  # 서명 위조
        a.logout_all()
        self.assertFalse(a.valid_token(t, now=2000))                       # 모든 기기 로그아웃

    def test_lockout(self):
        a = self.auth
        for i in range(a.IP_MAX_FAILS):
            self.assertEqual(a.locked("1.1.1.1", now=100), 0)
            a.record("1.1.1.1", False, now=100)
        self.assertGreater(a.locked("1.1.1.1", now=101), 0)               # 5번 틀리면 잠금
        self.assertEqual(a.locked("2.2.2.2", now=101), 0)                 # 다른 접속지는 아직
        for i in range(a.GLOBAL_MAX_FAILS):
            a.record(f"9.9.9.{i}", False, now=200)
        self.assertGreater(a.locked("3.3.3.3", now=201), 0)               # 전체 잠금


if __name__ == "__main__":
    unittest.main()
