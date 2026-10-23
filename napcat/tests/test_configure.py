"""只处理临时 JSON，不启动 QQ、容器或访问网络。"""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


spec = importlib.util.spec_from_file_location("napcat_configure", Path(__file__).parents[1] / "configure.py")
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def write(self, name, data):
        path = self.directory / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def read(self, name):
        return json.loads((self.directory / name).read_text())

    @staticmethod
    def client(token="onebot-test-token", url="ws://127.0.0.1:8080/onebot/v11/ws", **options):
        return {"name": "rws", "enable": True, "url": url, "token": token, **options}

    def test_fresh_setup_generates_private_webui_password_and_internal_url(self):
        changed = deployment.configure(self.directory)
        self.assertEqual(set(changed), {"webui.json", "onebot11.json"})
        webui = self.read("webui.json")
        self.assertEqual(webui["host"], "0.0.0.0")
        self.assertGreaterEqual(len(webui["token"]), 32)
        self.assertEqual((self.directory / "webui.json").stat().st_mode & 0o077, 0)
        client = self.read("onebot11.json")["network"]["websocketClients"][0]
        self.assertEqual(client["url"], deployment.DEFAULT_URL)
        self.assertFalse(client["reportSelfMessage"])

    def test_migration_preserves_credentials_other_connections_and_options(self):
        self.write("webui.json", {"host": "127.0.0.1", "port": 6099,
                                  "token": "webui-test-password", "loginRate": 7})
        remote = self.client("other-token", "ws://another-service:3001/onebot/v11/ws")
        network = {"websocketClients": [self.client(heartInterval=40000), remote],
                   "httpServers": [{"port": 8001}]}
        self.write("onebot11.json", {"network": network, "parseMultMsg": True})
        self.write("onebot11_123.json", {"network": {"websocketClients": [self.client("account-token")]}, "timeout": 8000})
        deployment.configure(self.directory)
        webui = self.read("webui.json")
        self.assertEqual(webui["token"], "webui-test-password")
        self.assertEqual(webui["loginRate"], 7)
        base = self.read("onebot11.json")
        self.assertTrue(base["parseMultMsg"])
        self.assertEqual(base["network"]["httpServers"], network["httpServers"])
        self.assertIn(remote, base["network"]["websocketClients"])
        managed = next(c for c in base["network"]["websocketClients"] if c["name"] == "ds-bot")
        self.assertEqual(managed["token"], "onebot-test-token")
        self.assertEqual(managed["heartInterval"], 40000)
        account = self.read("onebot11_123.json")
        self.assertEqual(account["timeout"], 8000)
        self.assertEqual(account["network"]["websocketClients"][0]["token"], "account-token")

    def test_repeated_start_is_idempotent_and_keeps_original_backup(self):
        path = self.write("webui.json", {"host": "127.0.0.1", "token": "test-password"})
        original = path.read_bytes()
        deployment.configure(self.directory)
        backup = path.with_name(path.name + ".pre-container")
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(backup.stat().st_mode & 0o077, 0)
        self.assertEqual(deployment.configure(self.directory), [])
        self.assertEqual(backup.read_bytes(), original)

    def test_duplicate_local_connections_are_replaced_by_one_managed_connection(self):
        self.write("onebot11.json", {"network": {"websocketClients": [
            self.client(), self.client(url="ws://localhost:8080/onebot/v11/ws"),
        ]}})
        deployment.configure(self.directory)
        clients = self.read("onebot11.json")["network"]["websocketClients"]
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0]["url"], deployment.DEFAULT_URL)

    def test_invalid_account_json_does_not_partially_modify_other_files(self):
        path = self.write("webui.json", {"host": "127.0.0.1", "token": "test-password"})
        original = path.read_bytes()
        (self.directory / "onebot11_123.json").write_text("{ invalid", encoding="utf-8")
        with self.assertRaises(ValueError):
            deployment.configure(self.directory)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.directory / "onebot11.json").exists())

    def test_placeholder_password_is_rejected_before_writing(self):
        self.write("webui.json", {"token": "replace_with_your_webui_token"})
        with self.assertRaisesRegex(ValueError, "占位值"):
            deployment.configure(self.directory)
        self.assertFalse((self.directory / "onebot11.json").exists())

    def test_account_without_project_connection_inherits_base_token(self):
        self.write("onebot11.json", {"network": {"websocketClients": [self.client()]}})
        self.write("onebot11_123.json", {"network": {"websocketClients": []}})
        deployment.configure(self.directory)
        self.assertEqual(self.read("onebot11_123.json")["network"]["websocketClients"][0]["token"], "onebot-test-token")

    def test_invalid_client_list_is_rejected_without_replacing_configuration(self):
        path = self.write("onebot11.json", {"network": {"websocketClients": "invalid"}})
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "websocketClients"):
            deployment.configure(self.directory)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.directory / "webui.json").exists())


if __name__ == "__main__":
    unittest.main()
