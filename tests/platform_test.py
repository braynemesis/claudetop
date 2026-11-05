#!/usr/bin/env python3
"""Portabilidade do claudetop: Linux/Windows simulados via sys.platform e sem curses."""
import importlib.util
import os
import subprocess
import sys
import tempfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "claudetop.py")


def load(platform, env=None, which=None, no_curses=False):
    """Carrega o modulo como se rodasse em `platform`."""
    spec = importlib.util.spec_from_file_location("claudetop_" + platform, TOOL)
    mod = importlib.util.module_from_spec(spec)
    patches = [mock.patch.object(sys, "platform", platform),
               mock.patch.dict(os.environ, env or {}, clear=False)]
    if which is not None:
        patches.append(mock.patch("shutil.which", lambda exe: exe if exe in which else None))
    if no_curses:
        patches.append(mock.patch.dict(sys.modules, {"curses": None}))
    for p in patches:
        p.start()
    spec.loader.exec_module(mod)
    return mod, patches


def stop(patches):
    for p in patches:
        p.stop()


class Agent:
    session_id = "abc-123"
    cwd = "/home/u/meu proj"
    short = "ab12"


def main():
    checks = []

    mod, ps = load("linux", {"XDG_CACHE_HOME": "/xdg"}, which={"kitty", "xclip"})
    os.environ.pop("CLAUDETOP_CACHE_DIR", None)
    checks.append(("linux: terminal detectado", mod.detect_terminal() == "kitty"))
    checks.append(("linux: clipboard xclip", mod.clipboard_cmd("x")[0][0] == "xclip"))
    cmd = mod.resume_command(Agent())
    checks.append(("linux: cd/aspas posix", cmd == "cd '/home/u/meu proj' && claude --resume abc-123"))
    stop(ps)

    mod, ps = load("linux", which=set())
    checks.append(("linux: sem terminal -> erro claro",
                   mod.open_in_terminal("x")[0] is False and mod.clipboard_cmd("x")[0] == []))
    stop(ps)

    mod, ps = load("win32", {"LOCALAPPDATA": r"C:\L"}, which={"wt.exe"})
    cmd = mod.resume_command(Agent())
    checks.append(("win: cd /d com aspas duplas", cmd == 'cd /d "/home/u/meu proj" && claude --resume "abc-123"'))
    checks.append(("win: terminal wt", mod.detect_terminal() == "wt"))
    checks.append(("win: clipboard clip", mod.clipboard_cmd("x")[0] == ["clip"]))
    class Evil:
        session_id = "x&calc"
        cwd = "C:\\a%PATH%b"
        short = "ab&&del"
    cmd = mod.resume_command(Evil())
    checks.append(("win: id malicioso sanitizado, cwd com % vai pro home",
                   "calc" in cmd and "x&" not in cmd and "%PATH%" not in cmd and "claude --resume \"xcalc\"" in cmd))
    checks.append(("win: short sanitizado", mod.attach_command(Evil()).endswith('attach "abdel"')))
    class Amp:
        session_id = "s1"
        cwd = "C:\\a & b"
    class Acc:
        session_id = "s1"
        cwd = "C:\\Usu\u00e1rio\\Projetos (x86)"
    checks.append(("win: cwd com & vai pro home", "a & b" not in mod.resume_command(Amp())))
    checks.append(("win: cwd acentuado preservado", "Usu\u00e1rio" in mod.resume_command(Acc())))
    stop(ps)

    with tempfile.TemporaryDirectory() as home:
        env = dict(os.environ, CLAUDETOP_CLAUDE_DIR=home, CLAUDETOP_CACHE_DIR=home,
                   CLAUDETOP_NO_CLI="1", PYTHONPATH="")
        # curses ausente: --once/--json funcionam; TUI avisa e sai com 1
        stub = os.path.join(home, "sitecustomize.py")
        open(stub, "w").write("import sys; sys.modules['curses'] = None\n")
        env["PYTHONPATH"] = home
        for flag in ("--once", "--json"):
            r = subprocess.run([sys.executable, TOOL, flag], env=env, capture_output=True, text=True)
            checks.append((f"sem curses: {flag} ok", r.returncode == 0))
        r = subprocess.run([sys.executable, "-c",
                            "import runpy,sys;sys.argv=['claudetop'];"
                            "sys.stdout.isatty=lambda:True;"
                            f"runpy.run_path({TOOL!r},run_name='__main__')"],
                           env=env, capture_output=True, text=True)
        checks.append(("sem curses: TUI avisa com exit 1",
                       r.returncode == 1 and "windows-curses" in r.stderr))

    failures = 0
    for name, ok in checks:
        print(("  ok   " if ok else "  FAIL ") + name)
        failures += 0 if ok else 1
    print("\n", "TUDO OK" if failures == 0 else f"{failures} falha(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
