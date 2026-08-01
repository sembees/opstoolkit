"""认证与 JWT 单元测试。"""
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from app.core.auth import (
    hash_password,
    verify_password,
    _truncate,
    create_access_token,
    decode_access_token,
)


class AuthTest(unittest.TestCase):
    """密码哈希、截断、JWT 生成与解析"""

    def test_hash_and_verify(self):
        pw = "Test@1234"
        h = hash_password(pw)
        self.assertNotEqual(h, pw)
        self.assertTrue(verify_password(pw, h))
        self.assertFalse(verify_password("wrong", h))

    def test_truncate_72_bytes(self):
        short = _truncate("abc")
        self.assertEqual(short, b"abc")
        long = _truncate("a" * 100)
        self.assertEqual(len(long), 72)

    def test_truncate_unicode(self):
        s = "密码" * 30  # 2-char CJK * 3 bytes each * 30 = 180 bytes
        t = _truncate(s)
        self.assertLessEqual(len(t), 72)
        # should still verify correctly against re-hash of truncated
        h = hash_password(s)
        self.assertTrue(verify_password(s, h))

    def test_empty_password(self):
        self.assertEqual(_truncate(""), b"")
        h = hash_password("")
        self.assertTrue(verify_password("", h))

    def test_verify_bad_hashed(self):
        self.assertFalse(verify_password("x", "not-a-valid-hash"))
        self.assertFalse(verify_password("x", "$2b$12$invalidhashhere"))

    def test_jwt_create_and_decode(self):
        token = create_access_token("admin", {"role": "admin"})
        self.assertIsInstance(token, str)
        payload = decode_access_token(token)
        self.assertEqual(payload["sub"], "admin")
        self.assertEqual(payload["role"], "admin")
        self.assertIn("exp", payload)

    def test_jwt_invalid_token(self):
        from jose import JWTError
        with self.assertRaises(JWTError):
            decode_access_token("invalid.token.here")

    def test_jwt_expired_token(self):
        from jose import JWTError
        from app.config import settings
        from jose import jwt
        expired = datetime.now(timezone.utc) - timedelta(minutes=10)
        payload = {"sub": "admin", "exp": expired}
        token = jwt.encode(payload, settings.secret_key, algorithm="HS256")
        with self.assertRaises(JWTError):
            decode_access_token(token)


if __name__ == "__main__":
    unittest.main()
