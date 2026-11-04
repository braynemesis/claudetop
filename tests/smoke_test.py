#!/usr/bin/env python3
"""Smoke test do claudetop: monta um ~/.claude fake e valida snapshot + json."""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "claudetop.py")

SID = "11111111-2222-3333-4444-555555555555"
SUB1 = "a1111111111111111"
SUB2 = "a2222222222222222"


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def assistant(model, usage, stop="tool_use", blocks=None):
    return {
        "type": "assistant",
        "timestamp": iso(time.time()),
        "sessionId": SID,
        "cwd": "/Users/demo/app",
        "gitBranch": "feat/swarm",
        "version": "2.1.311",
        "isSidechain": False,
        "message": {
            "model": model,
            "role": "assistant",
            "stop_reason": stop,
            "usage": usage,
            "content": blocks or [{"type": "text", "text": "trabalhando"}],
        },
    }


def user(text, side=False, agent=None):
    entry = {
        "type": "user",
        "timestamp": iso(time.time()),
        "sessionId": SID,
        "cwd": "/Users/demo/app",
        "gitBranch": "feat/swarm",
        "isSidechain": side,
        "message": {"role": "user", "content": text},
    }
    if agent:
        entry["agentId"] = agent
    return entry


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def build(root):
    proj = os.path.join(root, "projects", "-Users-demo-app")
    write_jsonl(os.path.join(proj, SID + ".jsonl"), [
        user("Implemente o entrypoint do checkout"),
        {"type": "ai-title", "aiTitle": "Checkout entrypoint", "sessionId": SID},
        assistant("claude-sonnet-5-5", {"input_tokens": 12, "cache_read_input_tokens": 90000,
                                        "cache_creation_input_tokens": 500, "output_tokens": 800},
                  stop="tool_use",
                  blocks=[{"type": "tool_use", "name": "Grep", "input": {"pattern": "entrypoint"}}]),
    ])
    for sub, label, state in ((SUB1, "mapear o repo", "done"), (SUB2, "auditar call sites", "working")):
        rows = [user(label, side=True, agent=sub)]
        rows.append(assistant("claude-haiku-4-5", {"input_tokens": 3, "cache_read_input_tokens": 20000,
                                                   "cache_creation_input_tokens": 100, "output_tokens": 300},
                              stop="end_turn" if state == "done" else "tool_use"))
        write_jsonl(os.path.join(proj, SID, "subagents", "agent-" + sub + ".jsonl"), rows)

    job = os.path.join(root, "jobs", "abc12345")
    os.makedirs(job, exist_ok=True)
    now_ms = int(time.time() * 1000)
    with open(os.path.join(job, "state.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "state": "blocked",
            "detail": "preciso de aprovacao para rodar a migration",
            "name": "Migration do checkout",
            "sessionId": SID,
            "daemonShort": "abc12345",
            "cwd": "/Users/demo/app",
            "template": "bg",
            "respawnFlags": ["--model", "sonnet"],
            "createdAt": iso(time.time() - 1200),
            "updatedAt": iso(time.time()),
            "tokens": 300520,
            "inFlight": {"tasks": 1, "queued": 0},
            "fan": [
                {"id": SUB1, "kind": "agent", "label": "mapear o repo", "startedAt": now_ms - 90000,
                 "doneAt": now_ms - 30000},
                {"id": SUB2, "kind": "agent", "label": "auditar call sites", "startedAt": now_ms - 20000},
            ],
        }, fh)
    with open(os.path.join(job, "timeline.jsonl"), "w", encoding="utf-8") as fh:
        for i in range(6):
            fh.write(json.dumps({"at": iso(time.time() - 300 + i * 30),
                                 "state": "working" if i < 5 else "blocked",
                                 "detail": f"passo {i}"}) + "\n")


def check_open_helpers(failures):
    """Valida o comando de retomada e a deteccao de terminal (sem abrir janela)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("claudetop", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class Fake:
        session_id = "abc-123"
        cwd = "/Users/demo/Meu Projeto (v2)"

    cmd = mod.resume_command(Fake())
    class NoSid:
        session_id = ""
        cwd = "/tmp"
    cmd_sem_sid = mod.resume_command(NoSid())

    os.environ["CLAUDETOP_OPEN_DRY"] = "1"
    os.environ["TERM_PROGRAM"] = "iTerm.app"
    ok, err, name = mod.open_in_terminal(cmd)
    os.environ["TERM_PROGRAM"] = "tmux"
    ok_tmux, _, name_tmux = mod.open_in_terminal(cmd)
    os.environ["CLAUDETOP_OPEN"] = "iterm"
    forcar = mod.detect_terminal()
    del os.environ["CLAUDETOP_OPEN"]
    os.environ.pop("CLAUDETOP_OPEN_DRY", None)

    checks = [
        ("cmd cita o session id", "claude --resume abc-123" in cmd),
        ("cmd faz cd para o cwd", "cd '" in cmd and "Meu Projeto (v2)'" in cmd),
        ("cmd sem sessao nao usa --resume", "--resume" not in cmd_sem_sid),
        ("dry-run nao abre janela", ok and "dry-run" in name),
        ("iTerm detectado por TERM_PROGRAM", name == "iTerm2 (dry-run)" and ok),
        ("tmux cai no Terminal.app", name_tmux.startswith("Terminal.app")),
        ("CLAUDETOP_OPEN tem prioridade", forcar == "iterm"),
    ]
    for label, good in checks:
        print(("  ok   " if good else "  FAIL ") + label)
        failures += 0 if good else 1
    return failures


def check_metrics_and_git(failures):
    """Valida ritmo (delta de tokens), historico local e URLs de branch/PR."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("claudetop", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class Fake:
        def __init__(self, key="s:a", tokens=0, cost=0.0, state="working", subs=()):
            self.key = key
            self.tokens = tokens
            self.cost = cost
            self.state = state
            self.subs = list(subs)
            self.cwd = "/cwd/que/nao/existe"
            self.branch = ""

    agent = Fake(tokens=1600, cost=0.16)
    keeper = mod.RateKeeper()
    keeper.samples["s:a"] = [(time.time() - 60, 1000, 0.10)]
    keeper.update([agent])
    rate = keeper.get("s:a") or (0, 0)

    tmp = tempfile.mkdtemp(prefix="claudetop-hist-")
    hist = mod.History(os.path.join(tmp, "history.jsonl"))
    hist.sample([agent], force=True)
    agent.state, agent.tokens = "blocked", 2000
    hist.sample([agent], force=True)
    since = hist.state_since("s:a", "blocked")
    with open(hist.path, encoding="utf-8") as fh:
        lines = [line for line in fh.read().splitlines() if line.strip()]

    ok_git = True
    try:
        repo = tempfile.mkdtemp(prefix="claudetop-git-")
        subprocess.run(["git", "init", "-q", repo], check=True, timeout=10)
        subprocess.run(["git", "-C", repo, "remote", "add", "origin",
                        "git@bitbucket.org:demo/repo.git"], check=True, timeout=10)
        parsed = mod.repo_info(repo)[0]
        agent.cwd, agent.branch = repo, "feat/teste"
        branch_url = mod.repo_url(agent, "branch")[0] or ""
        pr_url = mod.repo_url(agent, "pr")[0] or ""
    except (OSError, subprocess.SubprocessError):
        parsed = mod.parse_remote("git@bitbucket.org:demo/repo.git")
        branch_url = mod.repo_branch_url("bitbucket.org", "demo/repo", "feat/teste")
        pr_url = mod.repo_pr_url("bitbucket.org", "demo/repo", "feat/teste")
        ok_git = False

    checks = [
        ("ritmo calcula tokens/min", abs(rate[0] - 600) < 1),
        ("ritmo calcula custo/hora", abs(rate[1] - 3.6) < 0.01),
        ("historico grava amostras", len(lines) == 2),
        ("historico sabe desde quando esta blocked", since is not None),
        ("parse remote ssh", mod.parse_remote("git@github.com:o/r.git") == ("github.com", "o/r")),
        ("parse remote https", mod.parse_remote("https://gitlab.com/g/sub/r.git") == ("gitlab.com", "g/sub/r")),
        ("remote do repo detectado", parsed == ("bitbucket.org", "demo/repo")),
        ("url de branch bitbucket", branch_url ==
         "https://bitbucket.org/demo/repo/src/feat/teste/"),
        ("url de PR bitbucket", pr_url ==
         "https://bitbucket.org/demo/repo/pull-requests/new?source=feat/teste"),
        ("sem remote explica no rodape", mod.repo_url(Fake(), "branch")[0] is None),
    ]
    if not ok_git:
        print("  (git indisponivel: testando so os parsers)")
    for label, good in checks:
        print(("  ok   " if good else "  FAIL ") + label)
        failures += 0 if good else 1
    return failures


def main():
    failures = 0
    with tempfile.TemporaryDirectory() as root:
        build(root)
        env = dict(os.environ, CLAUDETOP_CLAUDE_DIR=root, COLUMNS="140", TERM="dumb")

        out = subprocess.run([sys.executable, TOOL, "--once"], env=env,
                             capture_output=True, text=True, timeout=120)
        text = out.stdout
        print(text)
        checks = [
            ("exit 0", out.returncode == 0),
            ("mostra job", "Migration do checkout" in text),
            ("mostra blocked", "blocked" in text),
            ("mostra detalhe do bloqueio", "aprovacao" in text),
            ("mostra subagente", "auditar call sites" in text),
            ("mostra modelo", "sonnet 5.5" in text),
            ("sem traceback", "Traceback" not in out.stderr),
        ]
        for name, ok in checks:
            print(("  ok   " if ok else "  FAIL ") + name)
            failures += 0 if ok else 1

        js = subprocess.run([sys.executable, TOOL, "--json"], env=env,
                            capture_output=True, text=True, timeout=120)
        try:
            data = json.loads(js.stdout)
            agents = {a["short"]: a for a in data}
            job = agents.get("abc12345", {})
            checks2 = [
                ("json valido", isinstance(data, list) and len(data) >= 1),
                ("job blocked no json", job.get("state") == "blocked"),
                ("ctx calculado", job.get("ctxTokens", 0) > 0),
                ("subagentes no json", len(job.get("subagents") or []) == 2),
            ]
        except ValueError as exc:
            print("  FAIL json invalido:", exc)
            failures += 1
            checks2 = []
        for name, ok in checks2:
            print(("  ok   " if ok else "  FAIL ") + name)
            failures += 0 if ok else 1

    failures = check_open_helpers(failures)
    failures = check_metrics_and_git(failures)

    print("\n", "TUDO OK" if failures == 0 else f"{failures} falha(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
