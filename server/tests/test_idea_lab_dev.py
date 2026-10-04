"""Isolation guards, with temporary files and no real server or provider calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import idea_lab_dev as dev


@unittest.skipUnless(os.name == "posix", "The server launcher runs on Linux")
class IdeaLabDevIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="idea-lab-guard-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "server-development"
        self.profile = self.base / dev.CLIENT_DIRECTORY / "idea-lab-dev-profile.json"

    def args(self, profile=None):
        return SimpleNamespace(client_profile=str(profile or self.profile), port=17851)

    def mark_server(self):
        self.root.mkdir()
        dev.write_private(self.root / "development.json", json.dumps({
            "kind": dev.MARKER, "root": str(self.root), "port": 17851,
            "token": "synthetic-test-token", "codex": "/bin/true",
        }))

    def test_installed_or_nonempty_unmarked_client_refuses_before_any_server_write(self):
        installed = self.base / "agentsdock-electron" / "idea-lab-dev-profile.json"
        with self.assertRaisesRegex(RuntimeError, "dedicated"):
            dev.prepare(self.args(installed), self.root)
        self.assertFalse(self.root.exists())
        self.assertFalse(installed.parent.exists())
        self.profile.parent.mkdir()
        sentinel = self.profile.parent / "settings.json"
        sentinel.write_text("existing settings")
        with self.assertRaisesRegex(RuntimeError, "nonempty client"):
            dev.prepare(self.args(), self.root)
        self.assertFalse(self.root.exists())
        self.assertFalse(self.profile.exists())
        self.assertEqual(sentinel.read_text(), "existing settings")

    def test_fresh_dedicated_profile_copies_only_native_login_into_private_home(self):
        native = self.base / "native-codex"
        native.mkdir()
        auth = native / "auth.json"
        auth.write_text('{"synthetic":"test-only"}')
        (native / "config.toml").write_text("must not be copied")
        with patch.dict(os.environ, {"CODEX_HOME": str(native)}), \
                patch.object(dev.shutil, "which", return_value="/bin/true"), \
                patch.object(dev, "owned_pid", return_value=None), \
                patch.object(dev.socket, "socket") as socket, \
                contextlib.redirect_stdout(io.StringIO()):
            socket.return_value.__enter__.return_value.connect_ex.return_value = 1
            dev.prepare(self.args(), self.root)
        copied = self.root / "home" / ".codex" / "auth.json"
        self.assertEqual(copied.read_bytes(), auth.read_bytes())
        self.assertNotEqual(copied.stat().st_ino, auth.stat().st_ino)
        self.assertFalse(copied.with_name("config.toml").exists())
        self.assertEqual(dev.read_json_regular(self.profile)["kind"], dev.MARKER)
        self.assertEqual(auth.read_text(), '{"synthetic":"test-only"}')

    def test_linked_client_directory_refuses_before_reading_or_writing_profile(self):
        original = self.base / "original-client"
        original.mkdir()
        self.profile.parent.symlink_to(original, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "symlinks"):
            dev.prepare(self.args(), self.root)
        self.assertFalse(self.root.exists())
        self.assertEqual(list(original.iterdir()), [])

    def test_nested_state_symlink_refuses_before_server_execution_or_directory_creation(self):
        self.mark_server()
        state = self.root / "home" / ".agentsdock-instances" / "idea-lab"
        state.mkdir(parents=True)
        original = self.base / "original-server-state"
        original.mkdir()
        (state / "admin").symlink_to(original, target_is_directory=True)
        with patch.object(dev, "owned_pid", return_value=None), \
                patch.object(dev.os, "execve") as execute:
            with self.assertRaisesRegex(RuntimeError, "symlinks"):
                dev.serve(self.root)
        execute.assert_not_called()
        self.assertFalse((self.root / "workspace").exists())
        self.assertFalse((self.root / "server.pid").exists())
        self.assertEqual(list(original.iterdir()), [])

    def test_linked_or_hardlinked_metadata_never_truncates_original(self):
        original = self.base / "original"
        original.write_text("retain this content")
        link = self.base / "linked"
        link.symlink_to(original)
        with self.assertRaisesRegex(RuntimeError, "symlinks"):
            dev.write_private(link, "replacement")
        link.unlink()
        os.link(original, link)
        with self.assertRaisesRegex(RuntimeError, "regular, unlinked"):
            dev.write_private(link, "replacement")
        self.assertEqual(original.read_text(), "retain this content")

    def test_marked_running_server_prepare_preserves_existing_private_auth(self):
        self.mark_server()
        auth = self.root / "home" / ".codex" / "auth.json"
        dev.write_private(auth, "existing private auth")
        with patch.object(dev, "owned_pid", return_value=12345), \
                patch.object(dev.socket, "socket") as socket, \
                contextlib.redirect_stdout(io.StringIO()):
            dev.prepare(self.args(), self.root)
        socket.assert_not_called()
        self.assertEqual(auth.read_text(), "existing private auth")

    def test_only_native_codex_executable_scratch_aliases_are_allowed(self):
        self.mark_server()
        scratch = self.root / "home/.codex/tmp/arg0/codex-arg0synthetic"
        scratch.mkdir(parents=True)
        alias = scratch / "codex-linux-sandbox"
        alias.symlink_to("/bin/true")
        dev.reject_linked_descendants(self.root, "/bin/true")
        with self.assertRaisesRegex(RuntimeError, "symlinks"):
            dev.reject_linked_descendants(self.root, "/bin/false")
        unknown = scratch / "auth.json"
        unknown.symlink_to("/bin/true")
        with self.assertRaisesRegex(RuntimeError, "symlinks"):
            dev.reject_linked_descendants(self.root, "/bin/true")


if __name__ == "__main__":
    unittest.main()
