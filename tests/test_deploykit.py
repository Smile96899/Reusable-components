"""deploykit 测试。端到端用例用 host = "local" 在本机 bash 里跑完整的发版 / 失败回滚 / 手动回滚。

运行: python -m unittest discover -s tests -v
"""
import io
import contextlib
import json
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deploykit as dk  # noqa: E402

HAS_BASH = Path(dk.find_bash()).exists() or shutil.which("bash")


def posix(p: Path) -> str:
    return p.resolve().as_posix()


class MatchTests(unittest.TestCase):
    def test_basename_pattern_matches_any_level(self):
        self.assertTrue(dk.matches("a/b/node_modules", "node_modules"))
        self.assertTrue(dk.matches("x/app.log", "*.log"))
        self.assertTrue(dk.matches(".env", ".env"))
        self.assertFalse(dk.matches(".env.example", ".env"))

    def test_path_pattern_is_anchored(self):
        self.assertTrue(dk.matches("data/x.json", "data/**"))
        self.assertTrue(dk.matches("data", "data/**"))
        self.assertFalse(dk.matches("services/data/x.json", "data/**"))

    def test_collect_prunes_and_filters(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for rel in ["app.js", ".env", "node_modules/x/i.js", "data/a.db", "svc/data/keep.js",
                        "database.sqlite-wal", "views/a.ejs"]:
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text("x")
            got = dk.collect_files(root, [], [".env", "node_modules", "data/**", "database.sqlite*"])
            self.assertEqual(got, ["app.js", "svc/data/keep.js", "views/a.ejs"])
            self.assertEqual(dk.collect_files(root, ["*.ejs"], ["node_modules"]), ["views/a.ejs"])


class RenderAndConfigTests(unittest.TestCase):
    def test_unknown_placeholder_fails(self):
        with self.assertRaises(dk.DeployError):
            dk.render("rm -rf {{nope}}/x", {})

    def test_shallow_dest_refused(self):
        with self.assertRaises(dk.DeployError):
            dk._art_vars({"name": "a", "dest": "/opt"}, {"release": "/r/1", "id": "1"})

    def test_config_validation(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "deploy.toml"
            p.write_text('name = "x"\n', encoding="utf-8")
            with self.assertRaises(dk.DeployError):
                dk.load_config(p)

    def test_example_configs_parse_and_render(self):
        for name in ("deploy.example.toml", "iconiq-canada.toml"):
            cfg = dk.load_config(Path(__file__).resolve().parents[1] / "examples" / name)
            for t in cfg["targets"]:
                ctx = dk.context(cfg, t, "20260101-000000-abcdef12")
                pkgs = [{"name": a["name"], "file": a["name"] + ".tar.gz", "sha256": "0" * 64}
                        for a in cfg["artifacts"]]
                for script in (dk.script_stage(cfg, ctx, pkgs), dk.script_backups(cfg, ctx),
                               dk.script_rollback(cfg, ctx), dk.script_activate(cfg, ctx),
                               dk.script_restart(cfg, ctx), dk.script_checks(cfg, ctx),
                               dk.script_finish(cfg, ctx)):
                    self.assertNotIn("{{", script)


@unittest.skipUnless(HAS_BASH, "需要 bash")
class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dk-test-"))
        self.proj = self.tmp / "proj"
        self.live = self.tmp / "live"
        (self.proj / "api" / "lib").mkdir(parents=True)
        (self.proj / "web" / "dist" / "assets").mkdir(parents=True)
        self.write("api/app.js", "v1")
        self.write("api/lib/a.js", "a1")
        self.write("api/.env", "LOCAL-SECRET")
        self.write("web/dist/index.html", "index-v1")
        self.write("web/dist/assets/app-1.js", "js1")
        (self.live / "api").mkdir(parents=True)
        (self.live / "api" / ".env").write_text("PROD-SECRET")
        (self.live / "api" / "runtime.txt").write_text("keep me")
        self.flag = self.tmp / "healthy"
        self.flag.write_text("1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, text):
        p = self.proj / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def config(self) -> Path:
        cfg = textwrap.dedent(f"""
            name = "demo"
            keep_releases = 2
            [git]
            enabled = false
            [vars]
            live = "{posix(self.live)}"
            [[artifacts]]
            name = "api"
            src = "api"
            dest = "{{{{live}}}}/api"
            exclude = [".env"]
            require = ["app.js"]
            verify = ["test -f {{{{stage}}}}/app.js"]
            [[artifacts]]
            name = "web"
            src = "web/dist"
            dest = "{{{{live}}}}/web/dist"
            mode = "swap"
            require = ["index.html"]
            [restart]
            run = ["echo restarted >> {{{{live}}}}/restarts"]
            [[checks]]
            name = "healthy flag"
            run = "test -f {posix(self.flag)}"
            retries = 1
            interval = 0
            [[targets]]
            name = "local"
            host = "local"
            releases_dir = "{posix(self.tmp / 'releases')}"
        """)
        p = self.proj / "deploy.toml"
        p.write_text(cfg, encoding="utf-8")
        return p

    def run_cli(self, *args) -> int:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = dk.main(["-c", str(self.config()), *args])
        self.last_output = buf.getvalue()
        return rc

    def read(self, rel):
        return (self.live / rel).read_text()

    def test_deploy_update_and_rollback(self):
        self.assertEqual(self.run_cli("deploy", "--yes"), 0, self.last_output)
        self.assertEqual(self.read("api/app.js"), "v1")
        self.assertEqual(self.read("api/.env"), "PROD-SECRET")
        self.assertEqual(self.read("api/runtime.txt"), "keep me")
        self.assertEqual(self.read("web/dist/index.html"), "index-v1")
        self.assertFalse((self.live / "web" / "dist" / "dist").exists())

        self.write("api/app.js", "v2")
        self.write("api/lib/new.js", "new")
        self.write("web/dist/index.html", "index-v2")
        (self.proj / "web/dist/assets/app-1.js").unlink()
        self.write("web/dist/assets/app-2.js", "js2")
        self.assertEqual(self.run_cli("deploy", "--yes"), 0, self.last_output)
        self.assertEqual(self.read("api/app.js"), "v2")
        self.assertTrue((self.live / "api/lib/new.js").exists())
        self.assertFalse((self.live / "web/dist/assets/app-1.js").exists())

        self.assertEqual(self.run_cli("rollback", "--yes"), 0, self.last_output)
        self.assertEqual(self.read("api/app.js"), "v1")
        self.assertFalse((self.live / "api/lib/new.js").exists())
        self.assertEqual(self.read("api/.env"), "PROD-SECRET")
        self.assertEqual(self.read("web/dist/index.html"), "index-v1")
        self.assertTrue((self.live / "web/dist/assets/app-1.js").exists())

    def test_failed_check_rolls_back_automatically(self):
        self.assertEqual(self.run_cli("deploy", "--yes"), 0, self.last_output)
        self.write("api/app.js", "broken")
        self.write("web/dist/index.html", "index-broken")
        self.flag.unlink()
        self.assertEqual(self.run_cli("deploy", "--yes"), 1)
        self.assertEqual(self.read("api/app.js"), "v1")
        self.assertEqual(self.read("web/dist/index.html"), "index-v1")
        self.assertFalse((self.tmp / "releases" / ".lock").exists())

    def test_failure_before_activation_changes_nothing(self):
        self.assertEqual(self.run_cli("deploy", "--yes"), 0, self.last_output)
        (self.proj / "api/app.js").unlink()
        self.write("api/other.js", "x")
        self.assertEqual(self.run_cli("deploy", "--yes"), 1)
        self.assertEqual(self.read("api/app.js"), "v1")

    def test_keep_releases_prunes_old_swap_backups(self):
        for i in range(4):
            self.write("web/dist/index.html", f"index-{i}")
            self.assertEqual(self.run_cli("deploy", "--yes"), 0, self.last_output)
        releases = [p for p in (self.tmp / "releases").iterdir() if not p.name.startswith(".")]
        self.assertEqual(len(releases), 2)
        backups = list((self.live / "web").glob("dist-backup-*"))
        self.assertEqual(len(backups), 2)

    def test_concurrent_deploy_is_locked(self):
        (self.tmp / "releases" / ".lock").mkdir(parents=True)
        self.assertEqual(self.run_cli("deploy", "--yes"), 1)
        self.assertFalse((self.live / "api" / "app.js").exists())


if __name__ == "__main__":
    unittest.main()
