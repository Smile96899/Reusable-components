#!/usr/bin/env python3
"""deploykit: 通用的多服务器发版工具。单文件，只依赖 Python 3.11+ 标准库。

每个项目在自己的仓库里放一份 deploy.toml，描述：
  - 本地先跑什么（测试、构建）
  - 要发哪些目录（overlay 覆盖变更文件 / swap 整目录原子切换）
  - 发版前备份什么（SQLite、MySQL、任意命令）
  - 怎么重启、怎么验证
  - 发到哪几台服务器（按顺序滚动发布，任何一台失败就停）

服务器端只需要 bash、tar、sha256sum、cmp、curl；SQLite 备份需要 sqlite3 或 python3。
用法见 README.md。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import getpass
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

__version__ = "0.1.0"

HERE = Path(__file__).resolve().parent


class DeployError(Exception):
    pass


def log(msg: str) -> None:
    print(f"[deploykit] {msg}", flush=True)


def q(value) -> str:
    return shlex.quote(str(value))


def render(text: str, ctx: dict) -> str:
    """替换 {{name}} 占位符；未知占位符直接报错，避免把空值带进 rm/mv。"""
    out = text
    for key, value in ctx.items():
        out = out.replace("{{" + key + "}}", str(value))
    if "{{" in out:
        start = out.index("{{")
        raise DeployError(f"未知占位符: {out[start:out.find('}}', start) + 2]}")
    return out


# ---------------------------------------------------------------- 配置

def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def load_config(path: Path) -> dict:
    path = path.resolve()
    if not path.is_file():
        raise DeployError(f"找不到配置文件: {path}")
    with path.open("rb") as fh:
        cfg = tomllib.load(fh)
    cfg["_root"] = path.parent
    cfg["_path"] = path

    if not cfg.get("name"):
        raise DeployError("配置缺少 name")
    cfg.setdefault("keep_releases", 5)
    cfg.setdefault("auto_rollback", True)
    cfg.setdefault("vars", {})
    cfg.setdefault("git", {})
    cfg.setdefault("steps", [])
    cfg.setdefault("backups", [])
    cfg.setdefault("checks", [])
    cfg.setdefault("hooks", {})
    cfg.setdefault("restart", {})

    targets = cfg.get("targets") or []
    if not targets:
        raise DeployError("配置至少需要一个 [[targets]]")
    seen = set()
    for t in targets:
        for key in ("name", "host", "releases_dir"):
            if not t.get(key):
                raise DeployError(f"target 缺少 {key}: {t}")
        if t["name"] in seen:
            raise DeployError(f"target 重名: {t['name']}")
        seen.add(t["name"])
        if not t["releases_dir"].startswith("/") and t["host"] != "local":
            raise DeployError(f"releases_dir 必须是绝对路径: {t['releases_dir']}")
        t.setdefault("vars", {})

    artifacts = cfg.get("artifacts") or []
    if not artifacts:
        raise DeployError("配置至少需要一个 [[artifacts]]")
    names = set()
    for a in artifacts:
        for key in ("name", "src", "dest"):
            if not a.get(key):
                raise DeployError(f"artifact 缺少 {key}: {a}")
        if a["name"] in names:
            raise DeployError(f"artifact 重名: {a['name']}")
        names.add(a["name"])
        a.setdefault("mode", "overlay")
        if a["mode"] not in ("overlay", "swap"):
            raise DeployError(f"artifact {a['name']} 的 mode 只能是 overlay 或 swap")
        a["include"] = _as_list(a.get("include"))
        a["exclude"] = _as_list(a.get("exclude"))
        a["require"] = _as_list(a.get("require"))
        a["verify"] = _as_list(a.get("verify"))
        a["after_activate"] = _as_list(a.get("after_activate"))

    for b in cfg["backups"]:
        if not b.get("name") or b.get("type") not in ("sqlite", "mysql", "command"):
            raise DeployError(f"backup 需要 name，type 只能是 sqlite/mysql/command: {b}")
    for c in cfg["checks"]:
        if not (c.get("url") or c.get("run")):
            raise DeployError(f"check 需要 url 或 run: {c}")
    return cfg


def select_targets(cfg: dict, names: list[str] | None) -> list[dict]:
    if not names:
        return cfg["targets"]
    by_name = {t["name"]: t for t in cfg["targets"]}
    missing = [n for n in names if n not in by_name]
    if missing:
        raise DeployError(f"未知 target: {', '.join(missing)}（可选: {', '.join(by_name)}）")
    return [by_name[n] for n in names]


# ---------------------------------------------------------------- 打包

def matches(rel: str, pattern: str) -> bool:
    """不带 / 的模式匹配任意层级的文件名（类似 .gitignore），带 / 的匹配相对路径。"""
    pattern = pattern.rstrip("/") if pattern.endswith("/") and "*" not in pattern else pattern
    if "/" not in pattern:
        return any(fnmatch.fnmatchcase(part, pattern) for part in rel.split("/"))
    return fnmatch.fnmatchcase(rel, pattern) or fnmatch.fnmatchcase(rel + "/", pattern)


def collect_files(src: Path, include: list[str], exclude: list[str]) -> list[str]:
    files = []
    for dirpath, dirnames, filenames in os.walk(src):
        base = Path(dirpath).relative_to(src).as_posix()
        base = "" if base == "." else base + "/"
        dirnames[:] = sorted(d for d in dirnames if not any(matches(base + d, p) for p in exclude))
        for name in sorted(filenames):
            rel = base + name
            if any(matches(rel, p) for p in exclude):
                continue
            if include and not any(matches(rel, p) for p in include):
                continue
            files.append(rel)
    return files


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def package_artifact(cfg: dict, art: dict, out_dir: Path) -> dict:
    src = (cfg["_root"] / art["src"]).resolve()
    if not src.is_dir():
        raise DeployError(f"artifact {art['name']} 的 src 不存在: {src}")
    files = collect_files(src, art["include"], art["exclude"])
    if not files:
        raise DeployError(f"artifact {art['name']} 没有任何文件（检查 include/exclude）")
    missing = [r for r in art["require"] if r not in files]
    if missing:
        raise DeployError(f"artifact {art['name']} 缺少必需文件: {', '.join(missing)}")
    tar_path = out_dir / f"{art['name']}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        for rel in files:
            info = tar.gettarinfo(str(src / rel), arcname=rel)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if info.isreg():
                with (src / rel).open("rb") as fh:
                    tar.addfile(info, fh)
            else:
                tar.addfile(info)
    return {"name": art["name"], "file": tar_path.name, "sha256": sha256_file(tar_path),
            "files": len(files), "bytes": tar_path.stat().st_size, "_list": files}


# ---------------------------------------------------------------- 本地准备

def git(cfg: dict, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=cfg["_root"], capture_output=True, text=True)
    if res.returncode != 0:
        raise DeployError(f"git {' '.join(args)} 失败: {res.stderr.strip()}")
    return res.stdout.strip()


def git_info(cfg: dict) -> dict:
    gcfg = cfg["git"]
    if gcfg.get("enabled", True) is False:
        return {"commit": "nogit", "branch": "", "dirty": False}
    try:
        commit = git(cfg, "rev-parse", "HEAD")
    except DeployError:
        if gcfg.get("required", False):
            raise
        return {"commit": "nogit", "branch": "", "dirty": False}
    branch = git(cfg, "rev-parse", "--abbrev-ref", "HEAD")
    untracked = "normal" if gcfg.get("include_untracked", False) else "no"
    dirty = bool(git(cfg, "status", "--porcelain", f"--untracked-files={untracked}"))
    info = {"commit": commit, "branch": branch, "dirty": dirty}

    if gcfg.get("require_clean", True) and dirty:
        raise DeployError("工作区有未提交的改动（git.require_clean = true）")
    if gcfg.get("branch") and branch != gcfg["branch"]:
        raise DeployError(f"当前分支是 {branch}，配置要求 {gcfg['branch']}")
    if gcfg.get("require_pushed", False):
        remote = gcfg.get("remote", "origin")
        git(cfg, "fetch", remote, branch)
        res = subprocess.run(["git", "merge-base", "--is-ancestor", "HEAD", f"{remote}/{branch}"],
                             cwd=cfg["_root"])
        if res.returncode != 0:
            raise DeployError(f"HEAD 还没推送到 {remote}/{branch}（git.require_pushed = true）")
    return info


def run_local_steps(cfg: dict) -> None:
    for step in cfg["steps"]:
        cwd = (cfg["_root"] / step.get("cwd", ".")).resolve()
        log(f"本地步骤: {step['run']}  (cwd={cwd})")
        res = subprocess.run(step["run"], cwd=cwd, shell=True)
        if res.returncode != 0:
            if step.get("allow_failure"):
                log(f"  步骤失败但 allow_failure = true，继续（退出码 {res.returncode}）")
                continue
            raise DeployError(f"本地步骤失败（退出码 {res.returncode}）: {step['run']}")


# ---------------------------------------------------------------- 远程执行

def find_bash() -> str:
    if os.name == "nt":
        for cand in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
            if os.path.exists(cand):
                return cand
    return shutil.which("bash") or "bash"


class Target:
    """host = "local" 时在本机 bash 里执行（用于测试 / 单机部署），否则走 ssh/scp。"""

    def __init__(self, cfg: dict, dry_run: bool = False):
        self.cfg = cfg
        self.name = cfg["name"]
        self.host = cfg["host"]
        self.dry_run = dry_run
        self.local = self.host == "local"

    def _ssh_opts(self, port_flag: str) -> list[str]:
        opts = ["-o", "BatchMode=yes"]
        if self.cfg.get("port"):
            opts += [port_flag, str(self.cfg["port"])]
        if self.cfg.get("identity"):
            opts += ["-i", os.path.expanduser(self.cfg["identity"])]
        for o in _as_list(self.cfg.get("ssh_options")):
            opts += ["-o", o]
        return opts

    def run(self, script: str, title: str, check: bool = True) -> int:
        script = "set -euo pipefail\n" + script
        if self.dry_run:
            print(f"\n----- [{self.name}] {title} (dry-run) -----\n{script}\n----- end -----")
            return 0
        cmd = [find_bash(), "-s"] if self.local else ["ssh", *self._ssh_opts("-p"), self.host, "bash -s"]
        res = subprocess.run(cmd, input=script.encode())
        if check and res.returncode != 0:
            raise DeployError(f"[{self.name}] {title} 失败（退出码 {res.returncode}）")
        return res.returncode

    def upload(self, local: Path, remote: str) -> None:
        if self.dry_run:
            print(f"[{self.name}] (dry-run) upload {local} -> {remote}")
            return
        if self.local:
            shutil.copyfile(local, remote)
            return
        res = subprocess.run(["scp", "-q", *self._ssh_opts("-P"), str(local), f"{self.host}:{remote}"])
        if res.returncode != 0:
            raise DeployError(f"[{self.name}] 上传 {local.name} 失败")


# ---------------------------------------------------------------- 远程脚本生成

def context(cfg: dict, target: dict, release_id: str) -> dict:
    rd = target["releases_dir"].rstrip("/")
    ctx = {**cfg["vars"], **target["vars"]}
    ctx.update({"name": cfg["name"], "target": target["name"], "host": target["host"],
                "id": release_id, "releases_dir": rd, "release": f"{rd}/{release_id}"})
    return ctx


def script_prepare(ctx: dict) -> str:
    return f"""
RD={q(ctx['releases_dir'])}; R={q(ctx['release'])}
mkdir -p "$RD"
if ! mkdir "$RD/.lock" 2>/dev/null; then
  echo "另一个发版正在进行: $(cat "$RD/.lock/info" 2>/dev/null || echo 未知)" >&2
  echo "确认没有发版在跑后执行: deploykit unlock" >&2
  exit 3
fi
echo "{ctx['id']} by $(whoami)@$(hostname) at $(date -Is)" > "$RD/.lock/info"
mkdir -p "$R/pkg" "$R/stage" "$R/backup" "$R/overlay"
"""


def script_unlock(ctx: dict) -> str:
    return f'rm -rf {q(ctx["releases_dir"])}/.lock\n'


def script_stage(cfg: dict, ctx: dict, packages: list[dict]) -> str:
    out = [f"R={q(ctx['release'])}", 'cd "$R/pkg"']
    for pkg in packages:
        art = next(a for a in cfg["artifacts"] if a["name"] == pkg["name"])
        name = pkg["name"]
        out.append(f'echo "== 校验并解包 {name}"')
        out.append(f'echo {q(pkg["sha256"] + "  " + pkg["file"])} | sha256sum -c --quiet -')
        out.append(f'S="$R/stage/{name}"; rm -rf "$S"; mkdir -p "$S"')
        out.append(f'tar -xzf - -C "$S" < "$R/pkg/{pkg["file"]}"')
        for req in art["require"]:
            out.append(f'[ -e "$S/"{q(req)} ] || {{ echo "缺少必需文件 {req}" >&2; exit 1; }}')
        for cmd in art["verify"]:
            vctx = {**ctx, "stage": f"{ctx['release']}/stage/{name}", "dest": render(art["dest"], ctx)}
            out.append(f'echo "== verify {name}: "{q(render(cmd, vctx))}')
            out.append(f"( {render(cmd, vctx)} )")
    return "\n".join(out) + "\n"


_SQLITE_PY = r'''
import json, sqlite3, sys
src, dst, integrity, tables = sys.argv[1], sys.argv[2], sys.argv[3] == "1", sys.argv[4:]
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
d = sqlite3.connect(dst)
s.backup(d)
s.close()
result = {}
if integrity:
    result["integrity_check"] = d.execute("PRAGMA integrity_check").fetchone()[0]
counts = {}
for t in tables:
    counts[t] = d.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
result["counts"] = counts
d.close()
print(json.dumps(result, ensure_ascii=False))
if integrity and result["integrity_check"] != "ok":
    sys.exit(2)
'''


def script_backups(cfg: dict, ctx: dict) -> str:
    out = [f"R={q(ctx['release'])}", 'mkdir -p "$R/backup"']
    for b in cfg["backups"]:
        name = b["name"]
        out.append(f'echo "== 备份 {name}"')
        if b["type"] == "sqlite":
            db = render(b["path"], ctx)
            integrity = "1" if b.get("integrity_check", True) else "0"
            tables = " ".join(q(t) for t in _as_list(b.get("count_tables")))
            out.append(f'DB={q(db)}; OUT="$R/backup/{name}.sqlite"')
            out.append('[ -f "$DB" ] || { echo "数据库不存在: $DB" >&2; exit 1; }')
            out.append("if command -v python3 >/dev/null 2>&1; then")
            out.append(f'  python3 -c {q(_SQLITE_PY)} "$DB" "$OUT" {integrity} {tables} | tee "$R/backup/{name}.json"')
            out.append("elif command -v sqlite3 >/dev/null 2>&1; then")
            out.append("  sqlite3 \"$DB\" \".backup '$OUT'\"")
            if integrity == "1":
                out.append('  r=$(sqlite3 "$OUT" "PRAGMA integrity_check;"); echo "integrity_check: $r"')
                out.append('  [ "$r" = ok ] || exit 2')
            for t in _as_list(b.get("count_tables")):
                out.append(f'  echo "{t}: $(sqlite3 "$OUT" \'SELECT COUNT(*) FROM "{t}";\')"')
            out.append('else echo "服务器上没有 python3 或 sqlite3，无法做 SQLite 在线备份" >&2; exit 1; fi')
        elif b["type"] == "mysql":
            cmd = render(b.get("dump_command", "mysqldump"), ctx)
            opts = render(b.get("options", ""), ctx)
            out.append(f'{cmd} --single-transaction --routines --triggers {opts} | gzip > "$R/backup/{name}.sql.gz"')
            out.append(f'[ -s "$R/backup/{name}.sql.gz" ] || {{ echo "mysql 备份为空" >&2; exit 1; }}')
        else:
            bctx = {**ctx, "out": f"{ctx['release']}/backup/{name}"}
            out.append(f'mkdir -p "$R/backup/{name}"')
            out.append(f"( {render(b['run'], bctx)} )")
        out.append(f'du -sh "$R/backup/{name}"* 2>/dev/null || true')
    return "\n".join(out) + "\n"


def _art_vars(art: dict, ctx: dict) -> str:
    dest = render(art["dest"], ctx).rstrip("/")
    if not dest.startswith("/") and not (len(dest) > 2 and dest[1] == ":"):
        raise DeployError(f"artifact {art['name']} 的 dest 必须是绝对路径: {dest}")
    if dest.count("/") < 2:
        raise DeployError(f"artifact {art['name']} 的 dest 太浅，拒绝操作: {dest}")
    return f'R={q(ctx["release"])}; D={q(dest)}; N={q(art["name"])}; ID={q(ctx["id"])}\n'


def script_rollback(cfg: dict, ctx: dict) -> str:
    """回滚脚本在激活之前就写到服务器上，激活中途失败也能用。按相反顺序恢复。"""
    parts = ["#!/usr/bin/env bash", "set -euo pipefail", f"# deploykit rollback for {ctx['id']}"]
    for art in reversed(cfg["artifacts"]):
        parts.append(f"# ---- {art['name']} ({art['mode']})")
        parts.append(_art_vars(art, ctx).strip())
        if art["mode"] == "overlay":
            parts.append('if [ -f "$R/overlay/$N.new" ]; then')
            parts.append('  while IFS= read -r f; do [ -n "$f" ] && rm -f "$D/$f"; done < "$R/overlay/$N.new"')
            parts.append("fi")
            parts.append('if [ -d "$R/overlay/$N" ] && [ -n "$(ls -A "$R/overlay/$N")" ]; then cp -a "$R/overlay/$N/." "$D/"; fi')
        else:
            parts.append('if [ -e "$D-backup-$ID" ]; then')
            parts.append('  rm -rf "$D-failed-$ID"; [ -e "$D" ] && mv "$D" "$D-failed-$ID"')
            parts.append('  mv "$D-backup-$ID" "$D"')
            parts.append('elif [ -f "$R/swap-$N.fresh" ] && [ -e "$D" ]; then')
            parts.append('  rm -rf "$D-failed-$ID"; mv "$D" "$D-failed-$ID"')
            parts.append("fi")
            parts.append('echo "$D-failed-$ID" >> "$R/cleanup.list"')
        parts.append(f'echo "已回滚 {art["name"]}"')
    parts.append('touch "$R/ROLLED_BACK"; rm -f "$R/ACTIVE"')
    body = "\n".join(parts) + "\n"
    return (f"R={q(ctx['release'])}\n"
            f"cat > \"$R/rollback.sh\" <<'DEPLOYKIT_EOF'\n{body}DEPLOYKIT_EOF\n"
            'chmod +x "$R/rollback.sh"\n')


def script_activate(cfg: dict, ctx: dict) -> str:
    out = []
    for hook in _as_list(cfg["hooks"].get("before_activate")):
        out.append(f"( {render(hook, ctx)} )")
    for art in cfg["artifacts"]:
        out.append(f'echo "== 激活 {art["name"]} ({art["mode"]})"')
        out.append(_art_vars(art, ctx).strip())
        if art["mode"] == "overlay":
            out.append(r'''S="$R/stage/$N"; B="$R/overlay/$N"
mkdir -p "$B" "$D"; : > "$R/overlay/$N.new"; : > "$R/overlay/$N.changed"
( cd "$S" && find . \( -type f -o -type l \) | sed 's|^\./||' ) | while IFS= read -r f; do
  if [ -e "$D/$f" ] || [ -L "$D/$f" ]; then
    if cmp -s "$S/$f" "$D/$f"; then continue; fi
    mkdir -p "$B/$(dirname "$f")"; cp -a "$D/$f" "$B/$f"; echo "$f" >> "$R/overlay/$N.changed"
  else
    echo "$f" >> "$R/overlay/$N.new"
  fi
  mkdir -p "$D/$(dirname "$f")"
  cp -a "$S/$f" "$D/$f.deploykit-tmp" && mv -f "$D/$f.deploykit-tmp" "$D/$f"
done
echo "  变更 $(wc -l < "$R/overlay/$N.changed") 个，新增 $(wc -l < "$R/overlay/$N.new") 个文件"''')
        else:
            out.append(r'''NX="$D-next-$ID"; rm -rf "$NX"; mkdir -p "$(dirname "$D")"
cp -a "$R/stage/$N" "$NX"''')
            for req in art["require"]:
                out.append(f'[ -e "$NX/"{q(req)} ] || {{ echo "切换前检查失败：缺少 {req}" >&2; exit 1; }}')
            out.append(r'''if [ -e "$D" ]; then mv "$D" "$D-backup-$ID"; echo "$D-backup-$ID" >> "$R/cleanup.list"; else touch "$R/swap-$N.fresh"; fi
mv "$NX" "$D"
echo "  已切换，旧目录保留在 $D-backup-$ID"''')
        actx = {**ctx, "dest": render(art["dest"], ctx)}
        for cmd in art["after_activate"]:
            out.append(f"( {render(cmd, actx)} )")
    for hook in _as_list(cfg["hooks"].get("after_activate")):
        out.append(f"( {render(hook, ctx)} )")
    out.append(f'touch {q(ctx["release"])}/ACTIVE')
    return "\n".join(out) + "\n"


def script_restart(cfg: dict, ctx: dict) -> str:
    rcfg = cfg["restart"]
    out = []
    for cmd in _as_list(rcfg.get("run")):
        out.append(f'echo "== 重启: "{q(render(cmd, ctx))}')
        out.append(render(cmd, ctx))
    for cmd in _as_list(rcfg.get("wait")):
        tries = int(rcfg.get("wait_retries", 15))
        out.append(f"ok=0; for i in $(seq 1 {tries}); do if ( {render(cmd, ctx)} ) >/dev/null 2>&1; then ok=1; break; fi; sleep 2; done")
        out.append(f'[ "$ok" = 1 ] || {{ echo "等待失败: "{q(render(cmd, ctx))} >&2; exit 1; }}')
    return "\n".join(out) + "\n"


def script_checks(cfg: dict, ctx: dict) -> str:
    out = [f"R={q(ctx['release'])}", "fail=0"]
    for c in cfg["checks"]:
        if c.get("where", "remote") != "remote":
            continue
        tries, interval = int(c.get("retries", 10)), int(c.get("interval", 3))
        label = c.get("name") or c.get("url") or c.get("run")
        if c.get("url"):
            headers = " ".join(f"-H {q(f'{k}: {v}')}" for k, v in (c.get("headers") or {}).items())
            expect = str(c.get("status", 200))
            contains = c.get("contains")
            test = f'[ "$code" = {q(expect)} ]'
            if contains:
                test += f' && grep -qF -- {q(contains)} "$R/check.out"'
            out.append(f'''ok=0; for i in $(seq 1 {tries}); do
  code=$(curl -sS -o "$R/check.out" -w '%{{http_code}}' --max-time {int(c.get('timeout', 10))} {headers} {q(render(c['url'], ctx))} || true)
  if {test}; then ok=1; break; fi; sleep {interval}
done''')
        else:
            out.append(f'''ok=0; for i in $(seq 1 {tries}); do
  if ( {render(c['run'], ctx)} ); then ok=1; break; fi; sleep {interval}
done''')
        out.append(f'if [ "$ok" = 1 ]; then echo "  ✓ "{q(label)}; else echo "  ✗ "{q(label)} >&2; fail=1; fi')
    out.append('[ "$fail" = 0 ]')
    return "\n".join(out) + "\n"


def local_checks(cfg: dict, ctx: dict, dry_run: bool) -> None:
    for c in cfg["checks"]:
        if c.get("where") != "local":
            continue
        url = render(c["url"], ctx)
        if dry_run:
            print(f"(dry-run) 本地检查 {url}")
            continue
        expect, contains = int(c.get("status", 200)), c.get("contains")
        last = ""
        for _ in range(int(c.get("retries", 10))):
            try:
                req = urllib.request.Request(url, headers=c.get("headers") or {})
                with urllib.request.urlopen(req, timeout=int(c.get("timeout", 10))) as resp:
                    body = resp.read().decode("utf-8", "replace")
                    if resp.status == expect and (not contains or contains in body):
                        log(f"  ✓ {c.get('name') or url}")
                        break
                    last = f"HTTP {resp.status}"
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
                if e.code == expect and not contains:
                    log(f"  ✓ {c.get('name') or url}")
                    break
            except Exception as e:  # noqa: BLE001 网络错误全部当成失败重试
                last = str(e)
            time.sleep(int(c.get("interval", 3)))
        else:
            raise DeployError(f"本地检查失败 {url}: {last}")


def script_finish(cfg: dict, ctx: dict) -> str:
    keep = int(cfg["keep_releases"])
    return f"""RD={q(ctx['releases_dir'])}; R={q(ctx['release'])}
rm -rf "$R/stage" "$R/pkg"
date -Is > "$R/FINISHED"
# 只清理已结束的旧版本；保留最近 {keep} 个
ls -1 "$RD" | grep -v '^\\.' | sort | head -n -{keep} | while IFS= read -r old; do
  [ -f "$RD/$old/FINISHED" ] || [ -f "$RD/$old/ROLLED_BACK" ] || continue
  if [ -f "$RD/$old/cleanup.list" ]; then
    while IFS= read -r p; do case "$p" in /*/*|?:/*/*) rm -rf "$p";; esac; done < "$RD/$old/cleanup.list"
  fi
  rm -rf "$RD/$old"; echo "  清理旧版本 $old"
done
"""


# ---------------------------------------------------------------- 命令

def make_release_id(info: dict) -> str:
    now = _dt.datetime.now()
    return f"{now:%Y%m%d-%H%M%S}{now.microsecond // 1000:03d}-{info['commit'][:8]}"


def build_packages(cfg: dict, tmp: Path) -> list[dict]:
    pkgs = []
    for art in cfg["artifacts"]:
        pkg = package_artifact(cfg, art, tmp)
        log(f"打包 {pkg['name']}: {pkg['files']} 个文件, {pkg['bytes'] / 1024:.0f} KB, sha256 {pkg['sha256'][:12]}")
        pkgs.append(pkg)
    return pkgs


def deploy_target(cfg: dict, target: dict, release_id: str, pkgs: list[dict], manifest: Path,
                  dry_run: bool) -> None:
    t = Target(target, dry_run)
    ctx = context(cfg, target, release_id)
    log(f"===== {t.name} ({t.host}) → {ctx['release']}")
    t.run(script_prepare(ctx), "准备")
    activated = False
    try:
        for pkg in pkgs:
            t.upload(Path(pkg["_path"]), f"{ctx['release']}/pkg/{pkg['file']}")
        t.upload(manifest, f"{ctx['release']}/manifest.json")
        t.run(script_stage(cfg, ctx, pkgs), "校验/解包/verify")
        if cfg["backups"]:
            t.run(script_backups(cfg, ctx), "备份")
        t.run(script_rollback(cfg, ctx), "写入回滚脚本")
        activated = True
        t.run(script_activate(cfg, ctx), "激活")
        t.run(script_restart(cfg, ctx), "重启")
        t.run(script_checks(cfg, ctx), "发版后检查")
        local_checks(cfg, ctx, dry_run)
        log(f"✓ {t.name} 发布成功: {release_id}")
        # 收尾失败（清理旧版本）不影响已经验证通过的线上版本，不触发回滚
        if t.run(script_finish(cfg, ctx), "收尾", check=False) != 0:
            log("  收尾清理失败，可稍后手动清理旧版本")
    except DeployError as err:
        log(f"✗ {t.name}: {err}")
        if activated and cfg["auto_rollback"]:
            log(f"自动回滚 {t.name} …")
            try:
                t.run(f'bash {q(ctx["release"])}/rollback.sh', "回滚")
                t.run(script_restart(cfg, ctx), "回滚后重启")
                t.run(script_checks(cfg, ctx), "回滚后检查", check=False)
                log("回滚完成（数据库不会自动回滚，备份在 release/backup/ 里）")
            except DeployError as rb_err:
                log(f"!!! 回滚也失败了，需要人工处理: {rb_err}")
        elif activated:
            log(f"auto_rollback = false，未回滚。手动回滚: bash {ctx['release']}/rollback.sh")
        else:
            log("失败发生在激活之前，线上没有任何文件被改动")
        raise
    finally:
        t.run(script_unlock(ctx), "解锁", check=False)


def confirm(question: str, assume_yes: bool) -> None:
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise DeployError("非交互环境请加 --yes 确认")
    if input(f"{question} [y/N] ").strip().lower() not in ("y", "yes"):
        raise DeployError("已取消")


def cmd_deploy(cfg: dict, args) -> None:
    targets = select_targets(cfg, args.target)
    info = git_info(cfg)
    if not args.skip_steps:
        run_local_steps(cfg)
    release_id = make_release_id(info)
    with tempfile.TemporaryDirectory(prefix="deploykit-") as tmpdir:
        tmp = Path(tmpdir)
        pkgs = build_packages(cfg, tmp)
        for p in pkgs:
            p["_path"] = str(tmp / p["file"])
        manifest = tmp / "manifest.json"
        manifest.write_text(json.dumps({
            "project": cfg["name"], "id": release_id, "deploykit": __version__,
            "git": info, "by": f"{getpass.getuser()}@{os.environ.get('COMPUTERNAME') or os.uname().nodename}",
            "time": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "artifacts": [{k: v for k, v in p.items() if not k.startswith("_")} for p in pkgs],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"版本 {release_id}，目标: {', '.join(t['name'] for t in targets)}")
        if not args.dry_run:
            confirm(f"发布到 {', '.join(t['name'] + '(' + t['host'] + ')' for t in targets)}？", args.yes)
        for target in targets:
            deploy_target(cfg, target, release_id, pkgs, manifest, args.dry_run)
    log("全部完成")


def cmd_package(cfg: dict, args) -> None:
    out = Path(args.output).resolve() if args.output else None
    with tempfile.TemporaryDirectory(prefix="deploykit-") as tmpdir:
        tmp = out or Path(tmpdir)
        tmp.mkdir(parents=True, exist_ok=True)
        for p in build_packages(cfg, tmp):
            if args.list:
                for rel in p["_list"]:
                    print(f"  {p['name']}: {rel}")
        if out:
            log(f"已输出到 {out}")


def _latest_active_script(ctx: dict, release: str | None) -> str:
    pick = (f'ID={q(release)}' if release else
            'ID=$(ls -1 "$RD" | grep -v "^\\." | sort | while read d; do '
            '[ -f "$RD/$d/ACTIVE" ] && echo "$d"; done | tail -n 1)')
    return f"""RD={q(ctx['releases_dir'])}
{pick}
[ -n "$ID" ] || {{ echo "没有可回滚的版本" >&2; exit 1; }}
LATEST=$(ls -1 "$RD" | grep -v "^\\." | sort | while read d; do [ -f "$RD/$d/ACTIVE" ] && echo "$d"; done | tail -n 1)
[ "$ID" = "$LATEST" ] || {{ echo "只能回滚最新的生效版本 $LATEST（要回滚更早的版本，请逐个往回滚）" >&2; exit 1; }}
"""


def cmd_rollback(cfg: dict, args) -> None:
    targets = select_targets(cfg, args.target)
    confirm(f"回滚 {', '.join(t['name'] for t in targets)} 的最新版本？", args.yes or args.dry_run)
    for target in targets:
        t = Target(target, args.dry_run)
        ctx = context(cfg, target, "ROLLBACK")
        log(f"===== 回滚 {t.name}")
        t.run(script_prepare({**ctx, "release": ctx["releases_dir"] + "/.rollback-tmp"}), "加锁")
        try:
            t.run(_latest_active_script(ctx, args.release) +
                  'echo "回滚版本 $ID"; bash "$RD/$ID/rollback.sh"', "回滚")
            t.run(script_restart(cfg, ctx), "重启")
            t.run(script_checks({**cfg}, {**ctx, "release": ctx["releases_dir"] + "/.rollback-tmp"}), "检查")
            log(f"✓ {t.name} 回滚完成（数据库不会回滚）")
        finally:
            t.run(f'rm -rf {q(ctx["releases_dir"])}/.rollback-tmp\n' + script_unlock(ctx), "解锁", check=False)


def cmd_status(cfg: dict, args) -> None:
    for target in select_targets(cfg, args.target):
        t = Target(target, args.dry_run)
        ctx = context(cfg, target, "STATUS")
        log(f"===== {t.name} ({t.host})")
        t.run(f"""RD={q(ctx['releases_dir'])}
[ -d "$RD" ] || {{ echo "  还没有发布过"; exit 0; }}
[ -d "$RD/.lock" ] && echo "  ⚠ 已加锁: $(cat "$RD/.lock/info" 2>/dev/null)"
ls -1 "$RD" | grep -v '^\\.' | sort | while read d; do
  s=未完成; [ -f "$RD/$d/FINISHED" ] && s=已完成; [ -f "$RD/$d/ROLLED_BACK" ] && s=已回滚
  a=""; [ -f "$RD/$d/ACTIVE" ] && a=" (生效中)"
  echo "  $d  $s$a"
done | tail -n 20
""", "状态")


def cmd_unlock(cfg: dict, args) -> None:
    for target in select_targets(cfg, args.target):
        t = Target(target, args.dry_run)
        t.run(script_unlock(context(cfg, target, "UNLOCK")), "解锁")
        log(f"{t.name} 已解锁")


def cmd_install_monitor(cfg: dict, args) -> None:
    files = {
        "monitor/deploykit-monitor.sh": "/usr/local/sbin/deploykit-monitor",
        "monitor/deploykit-monitor.service": "/etc/systemd/system/deploykit-monitor.service",
        "monitor/deploykit-monitor.timer": "/etc/systemd/system/deploykit-monitor.timer",
        "monitor/monitor.conf.example": "/etc/deploykit/monitor.conf.example",
        "monitor/checks.list.example": "/etc/deploykit/checks.list.example",
    }
    for target in select_targets(cfg, args.target):
        t = Target(target, args.dry_run)
        t.run("mkdir -p /etc/deploykit /var/lib/deploykit-monitor", "创建目录")
        for src, dest in files.items():
            t.upload(HERE / src, dest)
        t.run("""chmod 755 /usr/local/sbin/deploykit-monitor
for f in monitor.conf checks.list; do
  [ -f /etc/deploykit/$f ] || cp /etc/deploykit/$f.example /etc/deploykit/$f
done
chmod 600 /etc/deploykit/monitor.conf
systemctl daemon-reload
systemctl enable --now deploykit-monitor.timer
echo "已安装。编辑 /etc/deploykit/monitor.conf（告警渠道）和 /etc/deploykit/checks.list（检查项）"
echo "手动试跑: /usr/local/sbin/deploykit-monitor --test"
""", "安装监控")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deploykit", description="通用多服务器发版工具")
    parser.add_argument("-c", "--config", default="deploy.toml", help="配置文件（默认 ./deploy.toml）")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p, targets=True):
        if targets:
            p.add_argument("-t", "--target", action="append", help="只操作指定 target，可重复")
        p.add_argument("--dry-run", action="store_true", help="只打印将执行的远程脚本")
        p.add_argument("-y", "--yes", action="store_true", help="跳过确认")
        return p

    p = common(sub.add_parser("deploy", help="测试 → 打包 → 逐台发布"))
    p.add_argument("--skip-steps", action="store_true", help="跳过本地 steps（测试/构建）")
    p = common(sub.add_parser("rollback", help="回滚最新生效的版本"))
    p.add_argument("--release", help="要回滚的版本 ID（必须是最新的生效版本）")
    common(sub.add_parser("status", help="查看各服务器的发布记录"))
    common(sub.add_parser("unlock", help="清除残留的发版锁"))
    common(sub.add_parser("install-monitor", help="在服务器上安装健康监控 + 告警"))
    p = sub.add_parser("package", help="只在本地打包，检查会发哪些文件")
    p.add_argument("--list", action="store_true", help="列出每个文件")
    p.add_argument("-o", "--output", help="把包输出到目录")

    args = parser.parse_args(argv)
    try:
        cfg = load_config(Path(args.config))
        {"deploy": cmd_deploy, "rollback": cmd_rollback, "status": cmd_status,
         "unlock": cmd_unlock, "install-monitor": cmd_install_monitor,
         "package": cmd_package}[args.cmd](cfg, args)
    except DeployError as err:
        log(f"错误: {err}")
        return 1
    except KeyboardInterrupt:
        log("已中断")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
