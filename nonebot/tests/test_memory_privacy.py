import unittest
from src.memory_privacy import redact

class MemoryPrivacyTests(unittest.TestCase):
    def test_common_credentials(self):
        for text in ['sk-'+'a'*24,'Bearer '+'b'*24,'password=abcdefgh123','密码：abcdefgh123','access_token="abcdefgh123"']:
            with self.subTest(text=text):
                self.assertIn('[已隐藏凭据]',redact(text))
                self.assertNotIn('abcdefgh123',redact(text))

    def test_normal_conversation_and_identity_unchanged(self):
        text='QQ202说周日聚餐，我对花生过敏。密码怎么设置？'
        self.assertEqual(redact(text),text)

    def test_redaction_is_idempotent(self):
        text='API_KEY=sk-'+'a'*24
        self.assertEqual(redact(redact(text)),redact(text))
