"""加解密单元测试。"""
import unittest
import os

from app.core.crypto import encrypt, decrypt


class CryptoTest(unittest.TestCase):
    """Fernet 对称加解密"""

    def test_encrypt_decrypt_roundtrip(self):
        plain = "secret-password-123"
        cipher = encrypt(plain)
        self.assertNotEqual(cipher, plain)
        back = decrypt(cipher)
        self.assertEqual(back, plain)

    def test_encrypt_empty(self):
        self.assertEqual(encrypt(""), "")
        self.assertEqual(encrypt(None), "")

    def test_decrypt_empty(self):
        self.assertEqual(decrypt(""), "")
        self.assertEqual(decrypt(None), "")

    def test_non_ascii_plaintext(self):
        plain = "中文密码@¥€"
        cipher = encrypt(plain)
        self.assertNotEqual(cipher, plain)
        self.assertEqual(decrypt(cipher), plain)

    def test_decrypt_corrupt_raises(self):
        with self.assertRaises(Exception):
            decrypt("not-valid-fernet-token")


if __name__ == "__main__":
    unittest.main()
