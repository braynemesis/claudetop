#!/usr/bin/env python3
"""
claudetop — htop-style live monitor for Claude Code agents, sessions and subagent swarms.

Reads only local state (no network):
  ~/.claude/jobs/*/state.json + timeline.jsonl   -> background agents (daemon) and their fan-out
  ~/.claude/projects/**/<session>.jsonl          -> interactive sessions, models, tokens, tools
  ~/.claude/projects/**/<session>/subagents/*    -> subagent transcripts

Stdlib only (curses). Usage:
  claudetop              # live TUI
  claudetop --once       # one plain-text snapshot (no TUI)
  claudetop --json       # machine-readable snapshot
"""
from __future__ import annotations

import argparse
import calendar
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import unicodedata
from urllib.parse import quote

try:
    import curses
except ImportError:  # Windows sem windows-curses: --once/--json seguem funcionando
    curses = None

IS_WIN = sys.platform == "win32"

VERSION = "0.2.1"

CLAUDE_BIN = os.environ.get("CLAUDETOP_CLAUDE_BIN") or "claude"


def default_cache_dir():
    """macOS mantém ~/.cache; Linux respeita XDG_CACHE_HOME; Windows usa LOCALAPPDATA."""
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
    return os.path.join(base, "claudetop")


CACHE_DIR = os.path.expanduser(os.environ.get("CLAUDETOP_CACHE_DIR") or default_cache_dir())

CLAUDE_DIR = (
    os.environ.get("CLAUDETOP_CLAUDE_DIR")
    or os.environ.get("CLAUDE_CONFIG_DIR")
    or os.path.expanduser("~/.claude")
)
JOBS_DIR = os.path.join(CLAUDE_DIR, "jobs")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")

DEFAULT_CTX = 200_000
LOG_CAP = 240

SAMPLE_INTERVAL = 60.0          # segundos entre amostras do historico local
HISTORY_MAX_BYTES = 5 * 1024 * 1024
HISTORY_KEEP_LINES = 4000

# $ / MTok: (input, output, cache_read, cache_write)
PRICES = {
    "opus": (15.0, 75.0, 1.50, 18.75),
    "sonnet": (3.0, 15.0, 0.30, 3.75),
    "haiku": (0.80, 4.0, 0.08, 1.00),
}

STATE_ORDER = {
    "blocked": 0, "error": 1, "orphan": 2, "working": 3, "tool": 4,
    "idle": 5, "done": 6, "stale": 7, "unknown": 8,
}
STATE_GLYPH = {
    "blocked": "◆", "error": "✖", "orphan": "⚠", "working": "●", "tool": "▶",
    "idle": "○", "done": "✓", "stale": "·", "unknown": "?",
}
STATE_COLOR = {
    "blocked": 3, "error": 4, "orphan": 4, "working": 2, "tool": 1,
    "idle": 5, "done": 6, "stale": 7, "unknown": 7,
}
STATUS_SHORT = {
    "blocked": "block", "error": "error", "orphan": "orphn", "working": "run", "tool": "tool",
    "idle": "idle", "done": "done", "stale": "stale", "unknown": "?",
}
STATUS_LABEL = {
    "blocked": "blocked", "error": "error", "orphan": "orphan", "working": "running", "tool": "tool",
    "idle": "idle", "done": "done", "stale": "stale", "unknown": "?",
}

# ordenacao: tecla `s` cicla; a coluna ativa ganha a seta no header
SORT_KEYS = ("state", "age", "tokens", "name", "project")
SORT_COLUMN = {"state": "STATE", "age": "AGE", "tokens": "TOKENS", "name": "NAME"}
SORT_ARROW = {"state": "▼", "age": "▼", "tokens": "▼", "name": "▲", "project": "▼"}
SORT_HELP = {
    "state": "mais urgentes primeiro",
    "age": "atividade mais recente primeiro",
    "tokens": "mais tokens primeiro",
    "name": "A → Z",
    "project": "agrupado por projeto",
}


def sort_column(label, column, sort_key, width, align="<"):
    """Rótulo de coluna com seta quando ela é a coluna de ordenacao ativa."""
    arrow = SORT_ARROW.get(sort_key, "") if SORT_COLUMN.get(sort_key) == column else ""
    return f"{(label + arrow):{align}{width}}"


def sort_badge(sort_key):
    return f"[ordem: {sort_key} {SORT_ARROW.get(sort_key, '')}]"


STATE_FILTER_KEYS = {
    ord("1"): ({"blocked", "orphan", "error"}, "precisa de mim"),
    ord("2"): ({"working", "tool"}, "rodando"),
    ord("3"): ({"idle"}, "idle"),
    ord("4"): ({"done"}, "done"),
    ord("5"): ({"stale"}, "stale"),
    ord("0"): (set(), "todos"),
}

BARS = "▁▂▃▄▅▆▇█"


# ---------------------------------------------------------------- utilities

def now() -> float:
    return time.time()


def parse_ts(value) -> float | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return float(calendar.timegm(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return None


def fmt_dur(sec) -> str:
    if sec is None:
        return "-"
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec}s"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h:02d}h"


def fmt_tok(n) -> str:
    if n is None:
        return "-"
    n = int(n)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        v = n / 1000.0
        return f"{v:.0f}k" if v >= 10 else f"{v:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def dwidth(text: str) -> int:
    total = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def trunc(text: str, width: int) -> str:
    if width <= 0:
        return ""
    text = str(text)
    if dwidth(text) <= width:
        return text
    out, used = [], 0
    for ch in text:
        w = 0 if unicodedata.combining(ch) else (2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def clean_text(value, limit: int = 200) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, dict):
                parts.append(block.get("text") or block.get("content") or "")
            else:
                parts.append(str(block))
        value = " ".join(parts)
    text = str(value)
    text = re.sub(r"<pasted_content[^>]*>", " ", text)
    text = re.sub(r"</pasted_content>", " ", text)
    text = re.sub(r"<[^>]{1,60}>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return trunc(text, limit) if limit else text


def short_cwd(path: str, keep: int = 2) -> str:
    if not path:
        return ""
    home = os.path.expanduser("~")
    p = path.replace(home, "~", 1) if path.startswith(home) else path
    parts = [x for x in p.split("/") if x]
    if len(parts) <= keep + 1:
        return p
    return "…/" + "/".join(parts[-keep:])


def model_info(name: str):
    raw = (name or "").lower()
    family = "opus" if "opus" in raw else "sonnet" if "sonnet" in raw else "haiku" if "haiku" in raw else None
    label = re.sub(r"^claude-", "", raw)
    label = re.sub(r"-20\d{6}$", "", label)
    label = re.sub(r"^(\d+)-(\d+)-", r"\1.\2 ", label)
    label = label.replace("-", " ")
    label = re.sub(r"\b(\d+) (\d+)\b", r"\1.\2", label)
    label = re.sub(r"\s+", " ", label).strip() or "?"
    ctx = 1_000_000 if re.search(r"\[1m\]|1000000|1m-context", raw) else DEFAULT_CTX
    return label, family, ctx


def summarize_input(data) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("description", "file_path", "path", "command", "pattern", "query",
                "url", "prompt", "task", "subject", "notebook_path"):
        if data.get(key):
            return clean_text(data[key], 70)
    for value in data.values():
        if isinstance(value, str) and value.strip():
            return clean_text(value, 70)
    return ""


def sparkline(stamps, minutes: int = 20, buckets: int = 20) -> str:
    cutoff = now() - minutes * 60
    counts = [0] * buckets
    span = minutes * 60 / buckets
    for ts in stamps:
        if ts is None or ts < cutoff:
            continue
        idx = min(buckets - 1, int((ts - cutoff) / span))
        counts[idx] += 1
    top = max(counts) or 1
    return "".join(BARS[min(len(BARS) - 1, int(c / top * (len(BARS) - 1) + 0.5))] if c else " " for c in counts)


def is_temp_path(path: str) -> bool:
    return bool(re.match(r"^/(private/)?var/folders/|^/tmp/|^/private/tmp/", path or ""))


# ---------------------------------------------------------------- scanning

class Scan:
    """Incremental parser for one Claude Code transcript (.jsonl)."""

    def __init__(self, path: str):
        self.path = path
        self.reset()

    def reset(self):
        self.inode = None
        self.offset = 0
        self.partial = b""
        self.mtime = 0.0
        self.cwd = None
        self.branch = None
        self.title = None
        self.first_prompt = None
        self.last_prompt = None
        self.slug = None
        self.session_kind = None
        self.version = None
        self.model = None
        self.last_ts = None
        self.first_ts = None
        self.last_stop = None
        self.ctx_tokens = 0
        self.tok_in = 0
        self.tok_out = 0
        self.tok_cr = 0
        self.tok_cw = 0
        self.per_model = {}
        self.tools = {}
        self.last_tool = None
        self.last_text = ""
        self.pending = False
        self.events = []
        self.log = []
        self.turns = 0
        self.errors = 0
        self.lines = 0

    def update(self) -> bool:
        try:
            st = os.stat(self.path)
        except OSError:
            self.reset()
            return False
        if self.inode is not None and (st.st_ino != self.inode or st.st_size < self.offset):
            self.reset()
        if self.inode is None:
            self.inode = st.st_ino
        self.mtime = st.st_mtime
        if st.st_size == self.offset:
            return False
        chunk = b""
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                while True:
                    part = fh.read(8 * 1024 * 1024)
                    if not part:
                        break
                    chunk += part
                    if len(chunk) > 96 * 1024 * 1024:
                        break
                self.offset = fh.tell()
        except OSError:
            return False
        data = self.partial + chunk
        if b"\n" not in data:
            self.partial = data
            return True
        head, _, tail = data.rpartition(b"\n")
        self.partial = tail
        for raw in head.split(b"\n"):
            if raw.strip():
                self.lines += 1
                try:
                    self._entry(json.loads(raw.decode("utf-8", "replace")))
                except (ValueError, TypeError):
                    continue
        return True

    # ---- internals
    def _log(self, ts, tag, text):
        if not text:
            return
        self.log.append((ts, tag, text))
        if len(self.log) > LOG_CAP:
            del self.log[: len(self.log) - LOG_CAP]

    def _entry(self, d: dict):
        if not isinstance(d, dict):
            return
        etype = d.get("type")
        ts = parse_ts(d.get("timestamp"))
        if ts:
            self.last_ts = ts if self.last_ts is None else max(self.last_ts, ts)
            if self.first_ts is None:
                self.first_ts = ts
            self.events.append(ts)
            if len(self.events) > 3000:
                del self.events[:1000]

        if d.get("cwd"):
            self.cwd = d["cwd"]
        if d.get("gitBranch"):
            self.branch = d["gitBranch"]
        if d.get("slug"):
            self.slug = d["slug"]
        if d.get("sessionKind"):
            self.session_kind = d["sessionKind"]
        if d.get("version"):
            self.version = d["version"]

        if etype == "ai-title" and d.get("aiTitle"):
            self.title = clean_text(d["aiTitle"], 90)
            return
        if etype == "agent-name" and d.get("agentName"):
            self.title = self.title or clean_text(d["agentName"], 90)
            return
        if etype == "last-prompt" and d.get("lastPrompt"):
            self.last_prompt = clean_text(d["lastPrompt"], 160)
            return

        msg = d.get("message")
        if not isinstance(msg, dict):
            return
        content = msg.get("content")

        if etype == "user":
            self.turns += 1
            blocks = content if isinstance(content, list) else []
            has_result = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks)
            if has_result:
                self.pending = False
                for b in blocks:
                    if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                        self.errors += 1
                        self._log(ts, "err", "tool error: " + clean_text(b.get("content"), 90))
            else:
                text = clean_text(content, 160)
                if text:
                    if self.first_prompt is None and not d.get("isSidechain"):
                        self.first_prompt = text
                    self.last_prompt = text
                    self._log(ts, "you", text)
            return

        if etype != "assistant":
            return

        model = msg.get("model") or self.model
        if model:
            self.model = model
        stop = msg.get("stop_reason")
        if stop:
            self.last_stop = stop
        usage = msg.get("usage") or {}
        tin = int(usage.get("input_tokens") or 0)
        tout = int(usage.get("output_tokens") or 0)
        tcr = int(usage.get("cache_read_input_tokens") or 0)
        tcw = int(usage.get("cache_creation_input_tokens") or 0)
        ctx = tin + tcr + tcw
        if ctx:
            self.ctx_tokens = ctx
        self.tok_in += tin
        self.tok_out += tout
        self.tok_cr += tcr
        self.tok_cw += tcw
        if model:
            slot = self.per_model.setdefault(model, [0, 0, 0, 0])
            slot[0] += tin
            slot[1] += tout
            slot[2] += tcr
            slot[3] += tcw

        self.pending = stop == "tool_use"
        blocks = content if isinstance(content, list) else []
        texts = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                texts.append(clean_text(block["text"], 160))
            elif btype == "tool_use":
                name = block.get("name") or "tool"
                self.tools[name] = self.tools.get(name, 0) + 1
                self.last_tool = f"{name} {summarize_input(block.get('input'))}".strip()
                self._log(ts, "tool", self.last_tool)
            elif btype == "thinking" and block.get("thinking"):
                self._log(ts, "think", clean_text(block["thinking"], 110))
        for text in texts:
            self.last_text = text
            self._log(ts, "ai", text)


def derive_session_state(scan: Scan, done_after: float = 1800) -> str:
    last = scan.last_ts or scan.mtime or 0
    age = now() - last
    if scan.pending:
        return "tool" if age < 900 else "idle"
    if age < 90:
        return "working"
    if age < 900:
        return "idle"
    if scan.last_stop in ("end_turn", "stop_sequence") and age > done_after:
        return "done"
    return "idle" if age < 6 * 3600 else "stale"


def cost_of(per_model) -> float:
    total = 0.0
    for model, (tin, tout, tcr, tcw) in per_model.items():
        _, family, _ = model_info(model)
        pin, pout, pcr, pcw = PRICES.get(family or "", (0.0, 0.0, 0.0, 0.0))
        total += (tin * pin + tout * pout + tcr * pcr + tcw * pcw) / 1_000_000
    return total


class Sub:
    __slots__ = ("id", "label", "model", "started", "done", "status", "tokens", "ctx", "agent_type", "detail")

    def __init__(self, sid, label, model="", started=None, done=None, status="unknown",
                 tokens=0, ctx=0, agent_type="", detail=""):
        self.id = sid
        self.label = label
        self.model = model
        self.started = started
        self.done = done
        self.status = status
        self.tokens = tokens
        self.ctx = ctx
        self.agent_type = agent_type
        self.detail = detail


class Agent:
    def __init__(self):
        self.key = ""
        self.kind = "session"
        self.name = ""
        self.session_id = ""
        self.short = ""
        self.cwd = ""
        self.branch = ""
        self.state = "unknown"
        self.detail = ""
        self.model = ""
        self.ctx = 0
        self.ctx_window = DEFAULT_CTX
        self.tok_in = 0
        self.tok_out = 0
        self.tok_cr = 0
        self.tok_cw = 0
        self.tokens = 0
        self.cost = 0.0
        self.started = None
        self.updated = None
        self.template = ""
        self.flags = ""
        self.subs = []
        self.live = False
        self.pid = 0
        self.live_status = ""
        self.in_flight = 0
        self.queued = 0
        self.log = []
        self.log_path = ""
        self.log_kind = ""
        self.events = []
        self.tools = {}
        self.errors = 0
        self.turns = 0
        self.pending = False
        self.transcript = ""

    @property
    def elapsed(self):
        return None if not self.started else now() - self.started

    @property
    def ctx_pct(self):
        if not self.ctx_window:
            return 0
        return min(100, int(self.ctx / self.ctx_window * 100))

    @property
    def project(self):
        cwd = (self.cwd or "").rstrip("/")
        return os.path.basename(cwd) or cwd or "—"

    @property
    def running_subs(self):
        return sum(1 for s in self.subs if s.status in ("working", "tool"))

    @property
    def family(self):
        return model_info(self.model)[1]


class Scanner:
    """Discovers agents by polling ~/.claude. Keeps incremental caches."""

    def __init__(self, hours: float = 8.0):
        self.hours = hours
        self.scans: dict[str, Scan] = {}
        self.sub_scans: dict[str, Scan] = {}
        self.timelines: dict[str, tuple[float, list]] = {}

    # -- transcripts
    def _scan(self, path: str) -> Scan:
        scan = self.scans.get(path)
        if scan is None:
            scan = self.scans[path] = Scan(path)
        scan.update()
        return scan

    def _sub_scan(self, path: str) -> Scan:
        scan = self.sub_scans.get(path)
        if scan is None:
            scan = self.sub_scans[path] = Scan(path)
        scan.update()
        return scan

    def timeline(self, path: str):
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            return []
        cached = self.timelines.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        events = []
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 400 * 1024))
                blob = fh.read().decode("utf-8", "replace")
            for line in blob.splitlines():
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                events.append((parse_ts(d.get("at")), d.get("state") or "", clean_text(d.get("text") or d.get("detail"), 160)))
        except OSError:
            pass
        self.timelines[path] = (mtime, events[-400:])
        return self.timelines[path][1]

    # -- discovery
    def collect(self, show_all: bool = False):
        cutoff = now() - self.hours * 3600
        sessions: dict[str, dict] = {}
        project_dirs = []
        try:
            project_dirs = [os.path.join(PROJECTS_DIR, d) for d in os.listdir(PROJECTS_DIR)]
        except OSError:
            pass

        for pdir in project_dirs:
            try:
                entries = os.listdir(pdir)
            except OSError:
                continue
            for fname in entries:
                if not fname.endswith(".jsonl"):
                    continue
                path = os.path.join(pdir, fname)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if not show_all and st.st_mtime < cutoff:
                    continue
                scan = self._scan(path)
                sid = fname[:-6]
                subs = self._subagents(pdir, sid, cutoff, show_all)
                sessions[sid] = {"scan": scan, "subs": subs, "path": path, "mtime": st.st_mtime}

        agents: list[Agent] = []
        seen_jobs = set()

        for state_path in glob.glob(os.path.join(JOBS_DIR, "*", "state.json")):
            job = self._job(state_path, sessions, show_all, cutoff)
            if job is None:
                continue
            agents.append(job)
            seen_jobs.add(job.session_id)

        for sid, info in sessions.items():
            if sid in seen_jobs:
                continue
            scan = info["scan"]
            cwd = scan.cwd or ""
            if not show_all and is_temp_path(cwd):
                continue
            if not scan.model and not scan.tok_out and not info["subs"]:
                continue
            if not scan.model and (now() - (scan.last_ts or info["mtime"])) > 300:
                continue
            agent = self._session_agent(sid, scan, info)
            if not show_all and agent.state == "stale":
                continue
            agents.append(agent)

        return agents

    def _subagents(self, pdir: str, sid: str, cutoff: float, show_all: bool):
        subs = []
        subdir = os.path.join(pdir, sid, "subagents")
        try:
            names = sorted(os.listdir(subdir))
        except OSError:
            return subs
        for fname in names:
            if not fname.endswith(".jsonl") or not fname.startswith("agent-"):
                continue
            path = os.path.join(subdir, fname)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if not show_all and st.st_mtime < cutoff:
                continue
            scan = self._sub_scan(path)
            state = derive_session_state(scan, done_after=60)
            label = scan.first_prompt or scan.title or fname[6:-6]
            subs.append(Sub(
                sid=fname[6:-6],
                label=clean_text(label, 90),
                model=scan.model or "",
                started=scan.first_ts or getattr(st, "st_birthtime", st.st_mtime),
                done=scan.last_ts if state == "done" else None,
                status=state,
                tokens=scan.tok_in + scan.tok_out + scan.tok_cw,
                ctx=scan.ctx_tokens,
                detail=clean_text(scan.last_tool or scan.last_text or "", 90),
            ))
        return subs

    def _session_agent(self, sid: str, scan: Scan, info) -> Agent:
        agent = Agent()
        agent.kind = "session"
        agent.key = "s:" + sid
        agent.session_id = sid
        agent.short = sid[:8]
        agent.name = scan.title or scan.first_prompt or scan.slug or sid[:8]
        agent.name = clean_text(agent.name, 90)
        agent.cwd = scan.cwd or ""
        agent.branch = scan.branch or ""
        agent.model = scan.model or ""
        _, _, agent.ctx_window = model_info(agent.model)
        agent.ctx = scan.ctx_tokens
        agent.tok_in, agent.tok_out = scan.tok_in, scan.tok_out
        agent.tok_cr, agent.tok_cw = scan.tok_cr, scan.tok_cw
        agent.tokens = scan.tok_in + scan.tok_out + scan.tok_cw
        agent.cost = cost_of(scan.per_model)
        agent.started = scan.first_ts or info["mtime"]
        agent.updated = scan.last_ts or info["mtime"]
        agent.state = derive_session_state(scan)
        agent.detail = scan.last_tool or scan.last_text or scan.last_prompt or scan.first_prompt or ""
        agent.detail = clean_text(agent.detail, 200)
        agent.subs = info["subs"]
        agent.log = scan.log
        agent.log_kind = "transcript"
        agent.log_path = info["path"]
        agent.events = scan.events
        agent.tools = scan.tools
        agent.errors = scan.errors
        agent.turns = scan.turns
        agent.pending = scan.pending
        agent.transcript = info["path"]
        agent.template = scan.session_kind or ""
        if scan.branch:
            agent.flags = scan.branch
        return agent

    def _job(self, state_path: str, sessions, show_all: bool, cutoff: float):
        try:
            with open(state_path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return None
        sid = d.get("sessionId") or ""
        info = sessions.get(sid)
        mtime = 0.0
        try:
            mtime = os.stat(state_path).st_mtime
        except OSError:
            pass
        updated = parse_ts(d.get("updatedAt")) or mtime
        if not show_all and updated < cutoff:
            return None

        agent = Agent()
        agent.kind = "job"
        agent.key = "j:" + (d.get("daemonShort") or sid[:8] or state_path)
        agent.session_id = sid
        agent.short = d.get("daemonShort") or sid[:8]
        agent.name = clean_text(d.get("name") or d.get("intent") or sid[:8], 90)
        agent.cwd = d.get("cwd") or ""
        flags = d.get("respawnFlags") or []
        model = ""
        for i, flag in enumerate(flags):
            if flag == "--model" and i + 1 < len(flags):
                model = flags[i + 1]
        model = (d.get("providerEnv") or {}).get("ANTHROPIC_MODEL") or model
        if info and info["scan"].model and not re.search(r"\d", model or ""):
            model = info["scan"].model
        if not model and info:
            model = info["scan"].model or ""
        agent.model = model
        _, _, agent.ctx_window = model_info(model)
        agent.state = map_job_state(d.get("state"))
        needs = d.get("needs")
        detail = clean_text(d.get("detail") or (needs if isinstance(needs, str) else ""), 200)
        agent.detail = detail
        agent.started = parse_ts(d.get("createdAt")) or updated
        agent.updated = updated
        agent.template = d.get("template") or ""
        agent.flags = " ".join(flags)
        inflight = d.get("inFlight") or {}
        agent.in_flight = int(inflight.get("tasks") or 0)
        agent.queued = int(inflight.get("queued") or 0)
        agent.tokens = int(d.get("tokens") or 0)

        if info:
            scan = info["scan"]
            agent.ctx = scan.ctx_tokens
            agent.tok_in, agent.tok_out = scan.tok_in, scan.tok_out
            agent.tok_cr, agent.tok_cw = scan.tok_cr, scan.tok_cw
            agent.tokens = scan.tok_in + scan.tok_out + scan.tok_cw
            agent.cost = cost_of(scan.per_model)
            agent.log = scan.log
            agent.log_kind = "transcript"
            agent.log_path = info["path"]
            agent.events = scan.events
            agent.tools = scan.tools
            agent.errors = scan.errors
            agent.turns = scan.turns
            agent.pending = scan.pending
            agent.transcript = info["path"]
            if scan.branch:
                agent.branch = scan.branch
            if not agent.name:
                agent.name = scan.title or scan.first_prompt or ""

        by_id = {s.id: s for s in (info["subs"] if info else [])}
        for entry in (d.get("fan") or []):
            fid = str(entry.get("id") or "")
            sub = by_id.get(fid)
            if sub is None:
                started = None
                done_at = None
                if entry.get("startedAt"):
                    started = entry["startedAt"] / 1000.0
                if entry.get("doneAt"):
                    done_at = entry["doneAt"] / 1000.0
                status = "done" if done_at else "working"
                sub = Sub(fid, clean_text(entry.get("label"), 90), "", started, done_at, status)
            else:
                sub.label = clean_text(entry.get("label") or sub.label, 90)
                if entry.get("startedAt"):
                    sub.started = entry["startedAt"] / 1000.0
                if entry.get("doneAt"):
                    sub.done = entry["doneAt"] / 1000.0
                    sub.status = "done"
                elif sub.status == "done":
                    sub.status = "working"
            sub.agent_type = entry.get("kind") or sub.agent_type
            agent.subs.append(sub)
        if not agent.subs and info:
            agent.subs = info["subs"]

        timeline_path = os.path.join(os.path.dirname(state_path), "timeline.jsonl")
        if os.path.exists(timeline_path):
            agent.log_path = timeline_path
            agent.log_kind = "timeline"
            agent.log = self.timeline(timeline_path)
            agent.events = [ts for ts, _, _ in agent.log if ts]

        if agent.state in ("working", "tool") and agent.in_flight == 0 and agent.queued:
            agent.state = "working"
        return agent


def map_job_state(state) -> str:
    value = (state or "").lower()
    if value in ("working", "running", "active", "busy", "starting"):
        return "working"
    if value in ("blocked", "waiting", "needs_input", "needs-input", "paused", "ask"):
        return "blocked"
    if value in ("done", "completed", "finished", "settled"):
        return "done"
    if value in ("error", "failed", "crashed"):
        return "error"
    if value in ("idle", "sleeping"):
        return "idle"
    return "unknown"


# ---------------------------------------------------------------- rendering

class Row:
    __slots__ = ("agent", "sub", "level", "is_sub", "parent", "group", "group_agents")

    def __init__(self, agent, sub=None, level=0, parent=None, group="", group_agents=0):
        self.agent = agent
        self.sub = sub
        self.level = level
        self.is_sub = sub is not None
        self.parent = parent
        self.group = group
        self.group_agents = group_agents

    @property
    def is_group(self):
        return bool(self.group)

    @property
    def key(self):
        if self.is_sub:
            return f"{self.agent.key}/{self.sub.id}"
        return self.agent.key

    @property
    def state(self):
        return self.sub.status if self.is_sub else self.agent.state

    @property
    def name(self):
        return self.sub.label if self.is_sub else self.agent.name

    @property
    def model(self):
        return self.sub.model if self.is_sub else self.agent.model

    @property
    def updated(self):
        return (self.sub.done or self.sub.started or 0) if self.is_sub else (self.agent.updated or 0)


def sort_agents(agents, sort_key, pinned=None):
    pinned = pinned or set()
    pin = lambda a: 0 if a.key in pinned else 1  # noqa: E731
    if sort_key == "tokens":
        return sorted(agents, key=lambda a: (pin(a), -a.tokens))
    if sort_key == "name":
        return sorted(agents, key=lambda a: (pin(a), a.name.lower()))
    if sort_key == "age":
        return sorted(agents, key=lambda a: (pin(a), -(a.updated or 0)))
    if sort_key == "project":
        return sorted(agents, key=lambda a: (pin(a), a.project.lower(),
                                             STATE_ORDER.get(a.state, 9), -(a.updated or 0)))
    return sorted(agents, key=lambda a: (pin(a), STATE_ORDER.get(a.state, 9), -(a.updated or 0)))


def agent_matches(agent, needle):
    if not needle:
        return True
    haystack = " ".join([
        agent.name, agent.cwd, agent.detail, agent.model, agent.short,
        agent.template, agent.branch,
        " ".join(s.label for s in agent.subs),
    ]).lower()
    return needle in haystack


def build_rows(agents, expanded, filter_text, sort_key, pinned=None, hidden=None,
               state_filter=None):
    rows = []
    needle = (filter_text or "").lower()
    hidden = hidden or set()
    state_filter = state_filter or set()
    visible = []
    for agent in sort_agents(agents, sort_key, pinned):
        if agent.key in hidden:
            continue
        if state_filter and agent.state not in state_filter:
            continue
        if not agent_matches(agent, needle):
            continue
        visible.append(agent)

    for index, agent in enumerate(visible):
        if sort_key == "project":
            previous = visible[index - 1].project if index else None
            if agent.project != previous:
                count = sum(1 for a in visible if a.project == agent.project)
                rows.append(Row(agent, group=agent.project, group_agents=count))
        rows.append(Row(agent))
        if agent.subs and (agent.key in expanded):
            for sub in agent.subs:
                rows.append(Row(agent, sub, level=1, parent=agent))
    return rows


def stats(agents):
    counts = {"working": 0, "blocked": 0, "idle": 0, "done": 0, "error": 0, "tool": 0, "stale": 0, "unknown": 0}
    tokens = 0
    forks = 0
    cost = 0.0
    for agent in agents:
        counts[agent.state] = counts.get(agent.state, 0) + 1
        tokens += agent.tokens
        forks += len(agent.subs)
        cost += agent.cost
    return counts, tokens, forks, cost


# ---------------------------------------------------------------- snapshot mode

COLUMNS = ("STATE", "NAME", "MODEL", "CTX", "TOKENS", "AGE", "FAN", "CURRENT")


def snapshot(agents, expanded, filter_text, sort_key="project", width=150,
             pinned=None, hidden=None, state_filter=None):
    rows = build_rows(agents, expanded, filter_text, sort_key, pinned, hidden, state_filter)
    counts, tokens, forks, cost = stats(agents)
    w_name = max(24, min(40, width - 80))
    w_cur = max(16, width - (8 + w_name + 11 + 10 + 7 + 6 + 5 + 8))
    lines = []
    running = counts["working"] + counts["tool"]
    head = (f"CLAUDETOP  {running} running  {counts['blocked']} blocked  {counts['idle']} idle  "
            f"{counts['done']} done  {forks} forks  {fmt_tok(tokens)} tokens")
    lines.append(head)
    lines.append("-" * min(width, 160))
    lines.append(
        f"{sort_column('STATE', 'STATE', sort_key, 8)}"
        f"{sort_column('NAME', 'NAME', sort_key, w_name)}"
        f"{'MODEL':<11}{'CTX':<9}"
        f"{sort_column('TOKENS', 'TOKENS', sort_key, 7, '>')}  "
        f"{sort_column('AGE', 'AGE', sort_key, 6, '>')}  {'FAN':>4}  {'CURRENT':<{w_cur}}"
    )
    for row in rows:
        if row.is_group:
            label = f"── {row.group} ({row.group_agents}) "
            lines.append(label + "─" * max(0, min(width, 160) - len(label) - 1))
            continue
        glyph = STATE_GLYPH.get(row.state, "?")
        label = STATUS_SHORT.get(row.state, row.state)[:5]
        indent = "  " * row.level + ("└─ " if row.level else "")
        name = trunc(indent + row.name, w_name - 1)
        mlabel = trunc(model_info(row.model)[0], 10)
        if row.is_sub:
            ctx_s = " " * 9
            tok_s = fmt_tok(row.sub.tokens)
            age_s = fmt_dur((row.sub.done or now()) - row.sub.started) if row.sub.started else "-"
            fan_s = ""
            cur_s = row.sub.detail or ""
        else:
            agent = row.agent
            ctx_s = f"{bar(agent.ctx_pct, 5)} {agent.ctx_pct:>3}%"
            tok_s = fmt_tok(agent.tokens)
            age_s = fmt_dur(agent.elapsed)
            fan_s = f"{agent.running_subs}/{len(agent.subs)}" if agent.subs else ""
            cur_s = agent.detail
        lines.append(
            f"{glyph} {label:<6}{name:<{w_name}}{mlabel:<11}{ctx_s:<9}{tok_s:>7}  {age_s:>6}  {fan_s:>4}  {trunc(cur_s, w_cur)}"
        )
    lines.append("-" * min(width, 160))
    lines.append(
        f"agents: {len(agents)}   rows: {len(rows)}   "
        f"ordem: {sort_key} {SORT_ARROW.get(sort_key, '')} ({SORT_HELP.get(sort_key, '')})   "
        f"(custo estimado aparece no painel de detalhe)"
    )
    return "\n".join(lines)


def bar(pct, width=5):
    filled = int(round(pct / 100 * width))
    return "█" * filled + "░" * (width - filled)


class Prefs:
    """Pins e itens ocultos — ficam em ~/.cache/claudetop, nunca no ~/.claude."""

    def __init__(self, path=None):
        self.path = path or os.path.join(CACHE_DIR, "state.json")
        self.pinned: set[str] = set()
        self.hidden: set[str] = set()
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self.pinned = set(x for x in (data.get("pinned") or []) if isinstance(x, str))
            self.hidden = set(x for x in (data.get("hidden") or []) if isinstance(x, str))

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"pinned": sorted(self.pinned), "hidden": sorted(self.hidden)}, fh, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass


class RateKeeper:
    """Taxa de tokens e custo por agente numa janela movel de 60s.

    Medir o delta entre refreshes de 1s transforma cada turno num pico irreal
    (e volta a zero no segundo seguinte). A janela de 60s suaviza o numero.
    """

    WINDOW = 60.0
    MIN_SPAN = 5.0

    def __init__(self, window=WINDOW):
        self.window = window
        self.samples = {}
        self.rates = {}

    def update(self, agents):
        stamp = now()
        rates = {}
        for agent in agents:
            hist = self.samples.setdefault(agent.key, [])
            hist.append((stamp, agent.tokens, agent.cost))
            while len(hist) > 2 and stamp - hist[0][0] > self.window:
                hist.pop(0)
            first = hist[0]
            span = stamp - first[0]
            if span >= self.MIN_SPAN:
                dtok = max(0, agent.tokens - first[1])
                dcost = max(0.0, agent.cost - first[2])
                rates[agent.key] = (dtok / (span / 60.0), dcost / (span / 3600.0))
        self.rates = rates
        live = {agent.key for agent in agents}
        for key in list(self.samples):
            if key not in live:
                del self.samples[key]

    def get(self, key):
        return self.rates.get(key)


class History:
    """Amostras locais (~/.cache/claudetop/history.jsonl) — tempo em cada estado."""

    def __init__(self, path=None, interval=SAMPLE_INTERVAL):
        self.path = path or os.path.join(CACHE_DIR, "history.jsonl")
        self.interval = interval
        self.entries = []
        self.last_write = 0.0
        self.load()

    def load(self, limit=HISTORY_KEEP_LINES):
        try:
            with open(self.path, encoding="utf-8") as fh:
                lines = fh.readlines()[-limit:]
        except OSError:
            return
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                self.entries.append(item)

    def sample(self, agents, force=False):
        stamp = now()
        if not force and self.last_write and stamp - self.last_write < self.interval:
            return
        self.last_write = stamp
        rows = []
        for agent in agents:
            row = {
                "t": int(stamp), "k": agent.key, "s": agent.state,
                "tk": agent.tokens, "c": round(agent.cost, 6),
                "sub": [[sub.id, sub.status] for sub in agent.subs],
            }
            rows.append(row)
            self.entries.append(row)
        if not rows:
            return
        del self.entries[:-HISTORY_KEEP_LINES]
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        except OSError:
            return
        self.rotate()

    def rotate(self):
        try:
            if os.path.getsize(self.path) <= HISTORY_MAX_BYTES:
                return
            tmp = self.path + ".tmp"
            with open(self.path, encoding="utf-8") as fh:
                lines = fh.readlines()[-HISTORY_KEEP_LINES:]
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.writelines(lines)
            os.replace(tmp, self.path)
            self.entries = [json.loads(line) for line in lines if line.strip()][-HISTORY_KEEP_LINES:]
        except (OSError, ValueError):
            pass

    def state_since(self, key, state):
        """Timestamp em que a sequencia atual de `state` comecou (None se desconhecido)."""
        since = None
        for entry in reversed(self.entries):
            if entry.get("k") != key:
                continue
            if entry.get("s") != state:
                return since
            since = entry.get("t")
        return since


# ---------------------------------------------------------------- git: branch e PR
GIT_CACHE = {}


def git_output(cwd, argv, timeout=4):
    try:
        proc = subprocess.run(["git", "-C", cwd, *argv], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def parse_remote(remote):
    """`git@host:org/repo.git` ou `https://host/org/repo` -> (host, org/repo)."""
    url = (remote or "").strip()
    if not url:
        return None
    if url.endswith(".git"):
        url = url[:-4]
    match = re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+?)/?$", url)
    if not match:
        match = re.match(r"^(?:https?|ssh)://(?:[^@/]+@)?([^/]+)/(.+?)/?$", url)
    if not match:
        return None
    host, path = match.group(1), match.group(2)
    if not path or "/" not in path:
        return None
    return host, path


def repo_info(cwd):
    """(host, path, branch) do repo do agente — cacheado por cwd."""
    cwd = (cwd or "").strip()
    if not cwd or not os.path.isdir(cwd):
        return None, ""
    if cwd in GIT_CACHE:
        return GIT_CACHE[cwd]
    parsed = parse_remote(git_output(cwd, ["remote", "get-url", "origin"]))
    branch = git_output(cwd, ["rev-parse", "--abbrev-ref", "HEAD"])
    GIT_CACHE[cwd] = (parsed, branch)
    return GIT_CACHE[cwd]


def repo_branch_url(host, path, branch):
    q = quote(branch or "")
    if "bitbucket" in host:
        return f"https://{host}/{path}/src/{q}/"
    if "gitlab" in host:
        return f"https://{host}/{path}/-/tree/{q}"
    if "github" in host:
        return f"https://{host}/{path}/tree/{q}"
    return f"https://{host}/{path}"


def repo_pr_url(host, path, branch):
    q = quote(branch or "")
    if "bitbucket" in host:
        return f"https://{host}/{path}/pull-requests/new?source={q}"
    if "gitlab" in host:
        return f"https://{host}/{path}/-/merge_requests/new?merge_request%5Bsource_branch%5D={q}"
    if "github" in host:
        return f"https://{host}/{path}/compare/{q}?expand=1"
    return f"https://{host}/{path}"


def repo_url(agent, kind="branch"):
    """URL da branch (ou do formulario de PR) do agente, sem usar API."""
    parsed, detected = repo_info(agent.cwd)
    if not parsed:
        return None, "sem remote git em " + (short_cwd(agent.cwd or "", 2) or "—")
    host, path = parsed
    branch = agent.branch or detected or ""
    if kind == "pr" and not branch:
        return None, "sem branch para abrir PR"
    if not branch:
        return f"https://{host}/{path}", ""
    if kind == "pr":
        return repo_pr_url(host, path, branch), ""
    return repo_branch_url(host, path, branch), ""


def open_url(url):
    if os.environ.get("CLAUDETOP_OPEN_DRY"):
        return True, ""
    try:
        if IS_WIN:
            os.startfile(url)
            return True, ""
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        return False, str(exc)
    return True, ""


# ---------------------------------------------------------------- CLI oficial do claude

def cli_enabled():
    """Evita chamar o binario em testes (HOME falso) ou quando o usuario pediu para desligar."""
    if os.environ.get("CLAUDETOP_NO_CLI") or os.environ.get("CLAUDETOP_CLAUDE_DIR"):
        return False
    return shutil.which(CLAUDE_BIN) is not None


def claude_cli(argv, timeout=15):
    """Roda `claude <argv>`. Retorna (stdout, erro)."""
    if not cli_enabled():
        return None, "claude CLI indisponivel"
    try:
        proc = subprocess.run([CLAUDE_BIN, *argv], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        return None, clean_text(msg, 120) or f"exit {proc.returncode}"
    return proc.stdout, ""


def live_sessions():
    """`claude agents --json` -> {sessionId: item, 'id:<short>': item}."""
    out, _ = claude_cli(["agents", "--json"], timeout=10)
    if not out:
        return {}
    try:
        data = json.loads(out)
    except ValueError:
        return {}
    live = {}
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        sid = item.get("sessionId") or ""
        short = item.get("id") or ""
        if sid:
            live[sid] = item
        if short:
            live["id:" + short] = item
    return live


LIVE_STATE_MAP = {
    "blocked": "blocked", "done": "done", "error": "error", "failed": "error",
    "working": "working", "running": "working", "busy": "working", "idle": "idle",
}


def live_state(item):
    raw = (item.get("state") or item.get("status") or "").strip().lower()
    return LIVE_STATE_MAP.get(raw, "")


def apply_live(agents, live):
    """Casa os agentes com o estado vivo e marca jobs que dizem rodar sem processo."""
    if not live:
        return agents
    for agent in agents:
        item = live.get(agent.session_id) or (live.get("id:" + agent.short) if agent.kind == "job" else None)
        if item:
            agent.live = True
            agent.pid = int(item.get("pid") or 0)
            agent.live_status = item.get("state") or item.get("status") or ""
            mapped = live_state(item)
            # o CLI é a fonte de verdade para "precisa de mim" e para "acabou";
            # para o resto só corrige quando o arquivo está claramente velho
            if mapped in ("blocked", "done", "error"):
                agent.state = mapped
            elif mapped == "working" and agent.state in ("idle", "done", "stale", "unknown", "orphan"):
                agent.state = "working"
            elif mapped == "idle" and agent.state in ("working", "tool", "orphan"):
                agent.state = "idle"
    for agent in agents:
        if agent.kind == "job" and not agent.live and agent.state in ("working", "tool"):
            agent.state = "orphan"
    return agents


class LiveProbe:
    """Cache do `claude agents --json` — evita spawnar o CLI a cada refresh."""

    def __init__(self, ttl=15.0):
        self.ttl = ttl
        self.at = 0.0
        self.data = {}

    def get(self, force=False):
        if not cli_enabled():
            return {}
        if not force and self.at and now() - self.at < self.ttl:
            return self.data
        self.data = live_sessions()
        self.at = now()
        return self.data


# ---------------------------------------------------------------- abrir em outro terminal

TERMINAL_SCRIPTS = {
    "terminal": '''
on run argv
    set cmd to item 1 of argv
    tell application "Terminal"
        activate
        do script cmd
    end tell
end run
''',
    "iterm": '''
on run argv
    set cmd to item 1 of argv
    tell application "iTerm"
        activate
        create window with default profile command cmd
    end tell
end run
''',
}
TERMINAL_NAMES = {"terminal": "Terminal.app", "iterm": "iTerm2"}

# fora do macOS: (binario, template); a ordem e a preferencia de deteccao.
# {qcmd} = comando ja citado; no Windows `cmd /s` remove so as aspas externas de {cmd}.
if IS_WIN:
    TERMINAL_TEMPLATES = {
        "wt": ("wt.exe", 'wt.exe cmd /s /k "{cmd}"'),
        "cmd": ("cmd.exe", 'start "" cmd /s /k "{cmd}"'),
    }
else:
    TERMINAL_TEMPLATES = {
        "x-terminal-emulator": ("x-terminal-emulator", "x-terminal-emulator -e sh -c {qcmd}"),
        "gnome-terminal": ("gnome-terminal", "gnome-terminal -- sh -c {qcmd}"),
        "konsole": ("konsole", "konsole -e sh -c {qcmd}"),
        "kitty": ("kitty", "kitty sh -c {qcmd}"),
        "wezterm": ("wezterm", "wezterm start -- sh -c {qcmd}"),
        "xterm": ("xterm", "xterm -e sh -c {qcmd}"),
    }


def detect_terminal():
    """Terminal usado por `o`. CLAUDETOP_OPEN aceita 'terminal', 'iterm' ou um template com {cmd}."""
    override = (os.environ.get("CLAUDETOP_OPEN") or "").strip()
    if override:
        return override
    if sys.platform != "darwin":
        return next((k for k, (exe, _) in TERMINAL_TEMPLATES.items() if shutil.which(exe)), "")
    prog = os.environ.get("TERM_PROGRAM", "")
    if prog in ("iTerm.app", "iTerm2"):
        return "iterm"
    if prog in ("Apple_Terminal", "Terminal"):
        return "terminal"
    # tmux/ssh/IDE nao dizem o terminal do usuario: cai no Terminal.app,
    # que sempre existe. Para iTerm, use CLAUDETOP_OPEN=iterm.
    return "terminal"


def shq(text):
    """Cita um argumento para o shell do SO (cmd.exe: sempre entre aspas duplas)."""
    return f'"{text}"' if IS_WIN else shlex.quote(text)


def safe_id(text):
    """Ids vem de arquivos em ~/.claude: so [A-Za-z0-9_-] entra num comando de shell."""
    return re.sub(r"[^A-Za-z0-9_-]", "", text or "")


def cd_cmd(cwd):
    # o cmd externo le o caminho fora das aspas e expande %var%: allowlist, senao cai no home
    if IS_WIN:
        if not re.fullmatch(r"[\w \-.:\\/()]+", cwd):
            cwd = os.path.expanduser("~")
        return f"cd /d {shq(cwd)}"
    return f"cd {shlex.quote(cwd)}"


def resume_command(agent):
    """Comando de shell que reabre a sessão do agente no diretório dele."""
    sid = safe_id((agent.session_id or "").strip())
    cwd = (agent.cwd or "").strip() or os.path.expanduser("~")
    parts = [cd_cmd(cwd)]
    parts.append(f"claude --resume {shq(sid)}" if sid else "claude")
    return " && ".join(parts)


def attach_command(agent):
    """Job em background: `claude attach` (retoma com o output ao vivo)."""
    cwd = (agent.cwd or "").strip() or os.path.expanduser("~")
    return f"{cd_cmd(cwd)} && claude attach {shq(safe_id(agent.short))}"


def logs_command(agent):
    cwd = (agent.cwd or "").strip() or os.path.expanduser("~")
    return f"{cd_cmd(cwd)} && claude logs {shq(safe_id(agent.short))}"


def open_in_terminal(cmd, terminal=None):
    """Abre `cmd` num terminal novo. Retorna (ok, erro, nome_do_terminal)."""
    terminal = terminal or detect_terminal()
    name = TERMINAL_NAMES.get(terminal, terminal)
    if not terminal:
        return False, "nenhum terminal encontrado (defina CLAUDETOP_OPEN)", name
    if os.environ.get("CLAUDETOP_OPEN_DRY"):
        return True, "", f"{name} (dry-run)"

    if terminal in TERMINAL_SCRIPTS:
        try:
            proc = subprocess.run(["osascript", "-e", TERMINAL_SCRIPTS[terminal], cmd],
                                  capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, str(exc), name
        if proc.returncode == 0:
            return True, "", name
        return False, (proc.stderr or proc.stdout or "").strip(), name

    # template customizado: {cmd} = linha pronta para um shell; {qcmd} = a mesma linha
    # ja citada, para terminais que recebem o comando como um unico argumento
    script = TERMINAL_TEMPLATES.get(terminal, (None, terminal))[1]
    script = script.replace("{qcmd}", shlex.quote(cmd)).replace("{cmd}", cmd)
    try:
        proc = subprocess.run(script, shell=True,
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc), name
    if proc.returncode == 0:
        return True, "", name
    return False, (proc.stderr or "").strip(), name


def clipboard_cmd(text):
    """(argv, bytes) do utilitário de clipboard do SO; argv vazio se não houver nenhum."""
    if sys.platform == "darwin":
        return ["pbcopy"], text.encode()
    if IS_WIN:
        return ["clip"], text.encode("utf-16")
    for argv in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"]):
        if shutil.which(argv[0]):
            return argv, text.encode()
    return [], b""


# ---------------------------------------------------------------- TUI

class TUI:
    def __init__(self, scanner: Scanner, args):
        self.scanner = scanner
        self.args = args
        self.agents: list[Agent] = []
        self.rows: list[Row] = []
        self.sel = 0
        self.offset = 0
        self.filter_text = ""
        self.expanded: set[str] = set()
        self.paused = False
        self.show_all = args.all
        self.focus = "table"
        self.log_scroll = 0
        self.message = ""
        self.message_at = 0.0
        self.sort_key = getattr(args, "sort", "project") or "project"
        self.show_help = False
        self.pending = None
        self.prefs = Prefs()
        self.rates = RateKeeper()
        self.history = History()
        self.state_filter = set()
        self.state_filter_name = ""
        self.probe = LiveProbe()
        self.last_refresh = 0.0
        self.refresh()

    # -- data
    def refresh(self):
        self.agents = self.scanner.collect(show_all=self.show_all)
        self.agents = apply_live(self.agents, self.probe.get())
        if not self.expanded:
            for agent in self.agents:
                if agent.kind == "job" or (agent.subs and len(agent.subs) <= 6):
                    self.expanded.add(agent.key)
        self.rebuild()
        self.rates.update(self.agents)
        self.history.sample(self.agents)
        self.last_refresh = now()

    def flash(self, text):
        self.message = text
        self.message_at = now()

    def rebuild(self):
        self.rows = build_rows(self.agents, self.expanded, self.filter_text, self.sort_key,
                               self.prefs.pinned, self.prefs.hidden, self.state_filter)
        self.sel = max(0, min(self.sel, len(self.rows) - 1))
        if self.rows and self.rows[self.sel].is_group:
            self.move(1)

    def move(self, delta):
        """Move a selecao pulando os cabecalhos de grupo."""
        if not self.rows or not delta:
            return
        step = 1 if delta > 0 else -1
        index = self.sel
        for _ in range(abs(delta)):
            nxt = index + step
            while 0 <= nxt < len(self.rows) and self.rows[nxt].is_group:
                nxt += step
            if not (0 <= nxt < len(self.rows)):
                break
            index = nxt
        self.sel = index

    def move_edge(self, to_end):
        pick = [i for i, row in enumerate(self.rows) if not row.is_group]
        if pick:
            self.sel = pick[-1] if to_end else pick[0]

    def act_pin(self):
        row = self.selected()
        if not row:
            return
        key = row.agent.key
        if key in self.prefs.pinned:
            self.prefs.pinned.discard(key)
            self.flash("desafixado")
        else:
            self.prefs.pinned.add(key)
            self.flash(f"fixado no topo: {row.agent.name[:50]}")
        self.prefs.save()
        self.rebuild()
        for i, r in enumerate(self.rows):
            if not r.is_group and r.agent.key == key:
                self.sel = i
                break

    def act_hide(self):
        row = self.selected()
        if not row:
            return
        key = row.agent.key
        self.prefs.hidden.add(key)
        self.prefs.save()
        self.flash(f"oculto: {row.agent.name[:50]} — D restaura todos")
        self.rebuild()

    def act_unhide(self):
        if not self.prefs.hidden:
            self.flash("nada oculto")
            return
        total = len(self.prefs.hidden)
        self.prefs.hidden.clear()
        self.prefs.save()
        self.flash(f"{total} restaurado(s)")
        self.rebuild()

    def act_filter(self, states, name):
        if self.state_filter == states:
            self.state_filter, self.state_filter_name = set(), ""
        else:
            self.state_filter, self.state_filter_name = set(states), name
        self.rebuild()

    def selected(self):
        if not self.rows:
            return None
        return self.rows[min(self.sel, len(self.rows) - 1)]

    def toggle(self):
        row = self.selected()
        if not row or row.is_sub:
            return
        key = row.agent.key
        if key in self.expanded:
            self.expanded.discard(key)
        else:
            self.expanded.add(key)
        self.rebuild()

    # -- drawing
    def loop(self, stdscr):
        self.stdscr = stdscr
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.keypad(True)
        self.init_colors()
        interval = max(0.25, self.args.interval)
        while True:
            try:
                self.draw(stdscr)
            except curses.error:
                pass
            stdscr.timeout(-1 if self.paused else int(interval * 1000))
            try:
                ch = stdscr.getch()
            except KeyboardInterrupt:
                return
            if ch == -1:
                if not self.paused:
                    self.refresh()
                continue
            if not self.handle(ch):
                return
            if not self.paused and now() - self.last_refresh > interval:
                self.refresh()

    def init_colors(self):
        try:
            curses.start_color()
            curses.use_default_colors()
            # a ajuda usa fundo próprio para ficar dark mesmo em tema claro
            dark = 234 if curses.COLORS >= 256 else curses.COLOR_BLACK
            pairs = {
                1: (curses.COLOR_CYAN, -1),
                2: (curses.COLOR_GREEN, -1),
                3: (curses.COLOR_YELLOW, -1),
                4: (curses.COLOR_RED, -1),
                5: (curses.COLOR_BLUE, -1),
                6: (curses.COLOR_MAGENTA, -1),
                7: (curses.COLOR_WHITE, -1),
                8: (curses.COLOR_WHITE, dark),
                9: (curses.COLOR_CYAN, dark),
                10: (curses.COLOR_BLUE, dark),
            }
            for idx, (fg, bg) in pairs.items():
                curses.init_pair(idx, fg, bg)
            self.colors_ok = True
        except curses.error:
            self.colors_ok = False

    def attr(self, pair=0, bold=False, dim=False, rev=False):
        value = 0
        if self.colors_ok and pair:
            value |= curses.color_pair(pair)
        if bold:
            value |= curses.A_BOLD
        if dim:
            value |= curses.A_DIM
        if rev:
            value |= curses.A_REVERSE
        return value

    @staticmethod
    def put(win, y, x, text, attr=0, maxw=None):
        if y < 0 or x < 0:
            return
        height, width = win.getmaxyx()
        if y >= height or x >= width:
            return
        limit = width - x - 1
        if maxw is not None:
            limit = min(limit, maxw)
        if limit <= 0:
            return
        text = "".join(ch if ch.isprintable() else " " for ch in str(text))
        try:
            win.addnstr(y, x, text, limit, attr)
        except curses.error:
            pass

    def draw(self, stdscr):
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        if height < 12 or width < 60:
            self.put(stdscr, 0, 0, "terminal muito pequeno (min 60x12)", curses.A_BOLD)
            stdscr.refresh()
            return

        self.draw_header(stdscr, width)
        header_h, footer_h = 3, 2
        body_top = header_h
        body_h = height - header_h - footer_h
        if body_h < 5:
            body_h = height - header_h - 1
            footer_h = 1
        table_h = max(4, int(body_h * 0.62))
        table_h = min(table_h, body_h - 3) if body_h >= 8 else body_h
        detail_h = body_h - table_h

        self.draw_table(stdscr, body_top, table_h, width)
        if detail_h > 0:
            self.draw_detail(stdscr, body_top + table_h, detail_h, width)
        self.draw_footer(stdscr, height - footer_h, footer_h, width)
        if self.show_help:
            self.draw_help(stdscr, height, width)
        stdscr.refresh()

    def draw_header(self, win, width):
        counts, tokens, forks, cost = stats(self.agents)
        running = counts["working"] + counts["tool"]
        self.put(win, 0, 0, " CLAUDETOP ", self.attr(1, bold=True) | curses.A_REVERSE)
        self.put(win, 0, 11, " claude agent monitor", self.attr(7, dim=True))
        tags = []
        if any(s.status in ("working", "tool") for a in self.agents for s in a.subs):
            tags.append("[swarming]")
        if any(model_info(s.model)[1] == "opus" for a in self.agents for s in a.subs):
            tags.append("[advising]")
        if self.paused:
            tags.append("[paused]")
        tag_text = " ".join(tags)
        self.put(win, 0, max(32, width - len(tag_text) - 2), tag_text, self.attr(6, bold=True))

        clock = time.strftime("%d/%m %H:%M:%S")
        summary = f"{fmt_tok(tokens)} tokens · {forks} forks"
        right = f"{summary}  {clock}" if width >= 118 else (clock if width >= 92 else "")
        avail = width - len(right) - 2 if right else width - 2
        head = f"{len(self.agents)} agents"
        self.put(win, 1, 1, head, self.attr(7, bold=True))
        x = 1 + len(head) + 2
        counts["working"] += counts.get("tool", 0)
        for state, pair in (("working", 2), ("blocked", 3), ("idle", 5), ("done", 6), ("error", 4)):
            label = f"{STATE_GLYPH[state]} {counts.get(state, 0)} {STATUS_LABEL[state]}"
            if x + len(label) > avail:
                break
            self.put(win, 1, x, label, self.attr(pair, bold=state in ("working", "blocked")))
            x += len(label) + 2
        if right:
            self.put(win, 1, width - len(right) - 1, right, self.attr(7, dim=True))
        self.put(win, 2, 0, "─" * width, self.attr(7, dim=True))

    def column_layout(self, width):
        w_name = max(22, min(38, width - 88))
        w_cur = max(14, width - (8 + w_name + 11 + 10 + 7 + 6 + 5 + 10))
        return w_name, w_cur

    def draw_table(self, win, top, height, width):
        w_name, w_cur = self.column_layout(width)
        head = (f"{sort_column('STATE', 'STATE', self.sort_key, 8)}"
                f"{sort_column('NAME', 'NAME', self.sort_key, w_name)}"
                f"{'MODEL':<11}{'CTX':<10}"
                f"{sort_column('TOKENS', 'TOKENS', self.sort_key, 7, '>')} "
                f"{sort_column('AGE', 'AGE', self.sort_key, 6, '>')} "
                f"{'FAN':>4}  {'CURRENT':<{w_cur}}")
        self.put(win, top, 0, head, self.attr(7, bold=True), maxw=width - 1)
        self.put(win, top + 1, 0, "─" * width, self.attr(7, dim=True))

        visible = max(1, height - 2)
        if self.sel < self.offset:
            self.offset = self.sel
        if self.sel >= self.offset + visible:
            self.offset = self.sel - visible + 1
        max_offset = max(0, len(self.rows) - visible)
        self.offset = max(0, min(self.offset, max_offset))

        for index in range(visible):
            row_index = self.offset + index
            if row_index >= len(self.rows):
                break
            row = self.rows[row_index]
            y = top + 2 + index
            selected = row_index == self.sel
            base = curses.A_REVERSE if selected else 0
            if row.is_group:
                label = f"── {row.group} · {row.group_agents} agente(s) "
                self.put(win, y, 0, trunc(label + "─" * max(0, width - len(label) - 1), width - 1),
                         self.attr(6, bold=True))
                continue
            state = row.state
            pair = STATE_COLOR.get(state, 7)

            glyph = STATE_GLYPH.get(state, "?")
            self.put(win, y, 0, f"{glyph} {STATUS_SHORT.get(state, state)[:5]:<6}",
                     self.attr(pair, bold=state in ("working", "blocked", "error")) | base)

            if row.is_sub:
                indent = "  " * row.level + "└─ "
                label = trunc(indent + row.name, w_name - 1)
                self.put(win, y, 8, f"{label:<{w_name}}", self.attr(7) | base)
                self.put(win, y, 8 + w_name, f"{trunc(model_info(row.model)[0], 10):<11}", self.attr(6) | base)
                self.put(win, y, 8 + w_name + 11, " " * 10, base)
                dur = fmt_dur((row.sub.done or now()) - row.sub.started) if row.sub.started else "-"
                self.put(win, y, 8 + w_name + 21, f"{fmt_tok(row.sub.tokens):>7}", self.attr(7, dim=True) | base)
                self.put(win, y, 8 + w_name + 29, f"{dur:>6}", self.attr(7, dim=True) | base)
                self.put(win, y, 8 + w_name + 36, f"{'':>4}  {trunc(row.sub.detail or 'subagent', w_cur):<{w_cur}}", self.attr(7, dim=True) | base)
                continue

            agent = row.agent
            marker = "▾ " if (agent.subs and agent.key in self.expanded) else ("▸ " if agent.subs else "  ")
            indent = "  " * row.level
            label = trunc(indent + marker + agent.name, w_name - 1)
            self.put(win, y, 8, f"{label:<{w_name}}", self.attr(7, bold=selected) | base)
            self.put(win, y, 8 + w_name, f"{trunc(model_info(agent.model)[0], 10):<11}", self.attr(6) | base)
            pct = agent.ctx_pct
            ctx_pair = 4 if pct >= 90 else 3 if pct >= 70 else 7
            self.put(win, y, 8 + w_name + 11, f"{bar(pct, 5)} {pct:>3}%", self.attr(ctx_pair) | base)
            self.put(win, y, 8 + w_name + 21, f"{fmt_tok(agent.tokens):>7}", self.attr(7) | base)
            self.put(win, y, 8 + w_name + 29, f"{fmt_dur(agent.elapsed):>6}", self.attr(7, dim=True) | base)
            fan = f"{agent.running_subs}/{len(agent.subs)}" if agent.subs else ""
            self.put(win, y, 8 + w_name + 36, f"{fan:>4}  ", self.attr(2 if agent.running_subs else 7) | base)
            detail = agent.detail
            if agent.state == "blocked":
                detail = "⏸ " + detail
            self.put(win, y, 8 + w_name + 42, trunc(detail, w_cur), self.attr(3 if agent.state == "blocked" else 7) | base)

        if not self.rows:
            self.put(win, top + 2, 2, "nenhum agente ativo — tente 'a' para mostrar tudo", self.attr(7, dim=True))

    def draw_detail(self, win, top, height, width):
        row = self.selected()
        self.put(win, top, 0, "─" * width, self.attr(7, dim=True))
        inner_top = top + 1
        inner_h = height - 1
        if inner_h <= 0:
            return
        left_w = max(34, min(56, int(width * 0.42)))
        right_x = left_w + 1
        right_w = width - right_x - 1

        if row is None:
            return
        agent = row.agent
        y = inner_top
        if row.is_sub:
            sub = row.sub
            self.put(win, y, 1, "SUBAGENT", self.attr(6, bold=True))
            y += 1
            self.put(win, y, 1, trunc(sub.label, left_w - 2), self.attr(7, bold=True))
            y += 1
            self.put(win, y, 1, f"{model_info(sub.model)[0]} · {STATUS_LABEL.get(sub.status, sub.status)} · "
                                f"{fmt_dur((sub.done or now()) - sub.started) if sub.started else '-'} · {fmt_tok(sub.tokens)} tk",
                     self.attr(7))
            y += 1
            self.put(win, y, 1, f"parent: {trunc(agent.name, left_w - 10)}", self.attr(7, dim=True))
            y += 2
        else:
            self.put(win, y, 1, "AGENT", self.attr(1, bold=True))
            y += 1
            self.put(win, y, 1, trunc(agent.name, left_w - 2), self.attr(7, bold=True))
            y += 1
            state_pair = STATE_COLOR.get(agent.state, 7)
            self.put(win, y, 1, f"{STATE_GLYPH.get(agent.state, '?')} {STATUS_LABEL.get(agent.state, agent.state)}",
                     self.attr(state_pair, bold=True))
            self.put(win, y, 12, f"{trunc(short_cwd(agent.cwd, 3), left_w - 14)}", self.attr(7, dim=True))
            y += 1
            bits = [model_info(agent.model)[0] or "?"]
            if agent.template:
                bits.append(agent.template)
            if agent.branch and agent.branch != "main":
                bits.append(agent.branch)
            self.put(win, y, 1, trunc(" · ".join(bits), left_w - 2), self.attr(6))
            y += 1
            turns = f"{agent.turns} turn" + ("" if agent.turns == 1 else "s")
            self.put(win, y, 1, trunc(f"{agent.short} · {fmt_dur(agent.elapsed)} · {turns} · "
                                      f"{sum(agent.tools.values())} tools · {agent.errors} errors", left_w - 2), self.attr(7, dim=True))
            y += 1
            if agent.session_id:
                if agent.kind == "job":
                    hint = f"$ claude attach {agent.short}   [o] abre em outro terminal"
                else:
                    hint = f"$ claude --resume {(agent.session_id or agent.short)[:8]}   [o] abre em outro terminal"
                self.put(win, y, 1, trunc(hint, left_w - 2), self.attr(5))
                y += 1
            ctx_line = f"ctx {fmt_tok(agent.ctx)}/{fmt_tok(agent.ctx_window)} ({agent.ctx_pct}%)"
            if agent.cost >= 0.01:
                ctx_line += f" · ≈${agent.cost:,.2f}"
            self.put(win, y, 1, trunc(ctx_line, left_w - 2), self.attr(7, dim=True))
            y += 1
            if agent.live:
                live_line = f"vivo agora · pid {agent.pid or '?'}"
                if agent.live_status:
                    live_line += f" · status {agent.live_status}"
            elif agent.kind == "job" and agent.state == "orphan":
                live_line = "sem processo vivo — o job parou mas ainda está marcado como rodando (x para limpar)"
            else:
                live_line = "sem processo live"
            self.put(win, y, 1, trunc(live_line, left_w - 2), self.attr(4 if agent.state == "orphan" else 7, dim=True))
            y += 1
            rate = self.rates.get(agent.key)
            if rate is None:
                rate_line = "ritmo — (aguardando 2ª leitura)"
            elif rate[0] >= 1:
                rate_line = f"ritmo {fmt_tok(int(rate[0]))} tk/min · ≈${rate[1]:,.2f}/h"
            else:
                rate_line = "ritmo parado (sem tokens novos)"
            since = self.history.state_since(agent.key, agent.state)
            if since and now() - since >= 60:
                rate_line += f" · neste estado há {fmt_dur(now() - since)}"
            self.put(win, y, 1, trunc(rate_line, left_w - 2),
                     self.attr(2 if rate and rate[0] >= 1 else 7, dim=not (rate and rate[0] >= 1)))
            y += 1
            flow = f"out {fmt_tok(agent.tok_out)} · cache-read {fmt_tok(agent.tok_cr)} · cache-write {fmt_tok(agent.tok_cw)}"
            self.put(win, y, 1, trunc(flow, left_w - 2), self.attr(7, dim=True))
            y += 1
            if agent.tools:
                top_tools = sorted(agent.tools.items(), key=lambda kv: -kv[1])[:5]
                self.put(win, y, 1, trunc(" ".join(f"{k}×{v}" for k, v in top_tools), left_w - 2), self.attr(7, dim=True))
                y += 1
            if agent.log_kind == "transcript" and agent.events:
                self.put(win, y, 1, sparkline(agent.events), self.attr(2))
                y += 1
            y += 1
            if agent.subs:
                n_running = agent.running_subs
                self.put(win, y, 1, f"SUBAGENTS {n_running}/{len(agent.subs)} running", self.attr(6, bold=True))
                y += 1
                for sub in agent.subs[: max(0, inner_top + inner_h - y)]:
                    pair = STATE_COLOR.get(sub.status, 7)
                    dur = fmt_dur((sub.done or now()) - sub.started) if sub.started else "-"
                    line = f"{STATE_GLYPH.get(sub.status, '?')} {trunc(sub.label, left_w - 16)} {dur:>6}"
                    self.put(win, y, 2, line, self.attr(pair))
                    y += 1
        # log pane
        self.put(win, inner_top, right_x, "SESSION LOG", self.attr(1, bold=True))
        entries = agent.log or []
        avail = inner_h - 1
        if avail <= 0:
            return
        start = max(0, len(entries) - avail + self.log_scroll)
        end = max(0, len(entries) - self.log_scroll)
        visible = entries[start:end][-avail:]
        y = inner_top + 1
        for ts, tag, text in visible:
            if y >= inner_top + inner_h:
                break
            stamp = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "--:--:--"
            pair = {"err": 4, "tool": 1, "you": 5, "ai": 2, "think": 6}.get(tag, 7)
            self.put(win, y, right_x, f"{stamp} ", self.attr(7, dim=True))
            self.put(win, y, right_x + 9, f"{tag:<5}", self.attr(pair))
            self.put(win, y, right_x + 15, trunc(text, right_w - 16), self.attr(7))
            y += 1
        if not entries:
            self.put(win, inner_top + 1, right_x, "sem eventos recentes", self.attr(7, dim=True))

    def draw_footer(self, win, top, height, width):
        self.put(win, top, 0, "─" * width, self.attr(7, dim=True))
        keys = " q sair  ↑↓ mover  ⏎ expandir  o abrir  x parar  l logs  b branch  / filtrar  s ordenar  ? ajuda "
        if self.focus == "log":
            keys = " q sair  ↑↓ rolar log  tab voltar  p pausa  ? ajuda "
        right = ""
        if self.filter_text:
            right += f"/{self.filter_text}  "
        if self.state_filter_name:
            right += f"[{self.state_filter_name}] "
        if self.prefs.hidden:
            right += f"[ocultos {len(self.prefs.hidden)}] "
        if self.show_all:
            right += "[all] "
        badge = sort_badge(self.sort_key)
        right += badge
        room = width - len(right) - 2
        if room >= 24:
            self.put(win, top + 1, 0, trunc(keys, room), self.attr(7, dim=True))
            self.put(win, top + 1, width - len(right) - 1, right, self.attr(6, bold=True))
        elif width > len(badge) + 2:
            # sem espaco para os atalhos: a ordenacao continua visivel
            self.put(win, top + 1, width - len(badge) - 1, badge, self.attr(6, bold=True))
        if self.message and (now() - self.message_at) < 2.5:
            room_msg = room if room >= 24 else max(1, width - len(badge) - 2)
            self.put(win, top + 1, 0, f"{trunc(self.message, room_msg):<{room_msg}}", self.attr(3, bold=True))
        else:
            self.message = ""

    def draw_help(self, win, height, width):
        lines = [
            "CLAUDETOP — atalhos",
            "",
            "  ↑ ↓ / j k     mover seleção",
            "  ⏎ / espaço    expandir/recolher subagentes",
            "  /             filtrar (enter aplica, esc cancela)",
            "  p             pausar/retomar refresh",
            "",
            "  a             mostrar tudo (inclui stale e sessões temporárias)",
            "  f             fixar/desafixar no topo (persistido)",
            "  d / D         ocultar agente / restaurar todos os ocultos",
            "  1..5 / 0      filtrar: precisa de mim · rodando · idle · done · stale · todos",
            "  s             ordenar (state/age/tokens/name/project)",
            "  tab           foco no log (↑↓ rola, tab volta)",
            "  r             forçar releitura dos arquivos",
            "  o             abrir: job → claude attach · sessão → claude --resume",
            "  O             copiar o comando em vez de abrir",
            "  l             logs do job em outro terminal (claude logs)",
            "  b / B         abrir a branch no browser / formulário de PR",
            "  x             parar o job em background (claude stop, 2 toques)",
            "  X             apagar o job e o histórico (claude rm, 2 toques)",
            "  y             copiar caminho do transcript",
            "  q             sair",
            "",
            "  Fonte: ~/.claude/jobs/*/state.json + timeline.jsonl",
            "         ~/.claude/projects/**/<session>.jsonl (+ subagents/*.jsonl)",
            "",
            "  Estado: ● running  ▶ tool  ◆ blocked  ⚠ orphan  ○ idle  ✓ done  ✖ error",
            "  Custo é estimativa por preços de tabela, não é fatura.",
            "",
            "  pressione ? para fechar",
        ]

        def is_key(line):
            return line.startswith("  ") and line[14:16] == "  " and line[16:].strip()

        # fundo escuro cobrindo a tela toda (inclusive atrás do painel)
        dark = self.attr(8)
        if not self.colors_ok:
            dark |= curses.A_REVERSE
        for yy in range(height):
            self.put(win, yy, 0, " " * width, dark, maxw=width)

        w = min(width - 4, max(len(x) for x in lines) + 4)
        h = min(height - 2, len(lines) + 2)
        y0 = max(0, (height - h) // 2)
        x0 = max(0, (width - w) // 2)
        pad = " " * max(0, w)
        for i in range(h):
            self.put(win, y0 + i, x0, pad, dark, maxw=w)

        for i, line in enumerate(lines):
            if i >= h - 1:
                break
            y = y0 + 1 + i
            text = line[: max(0, w - 3)]
            if i == 0:
                self.put(win, y, x0 + 2, text, self.attr(9, bold=True), maxw=w - 3)
            elif is_key(line):
                self.put(win, y, x0 + 2, text[:16], self.attr(9), maxw=16)
                self.put(win, y, x0 + 18, text[16:], self.attr(8), maxw=w - 18)
            elif line.strip().startswith("pressione"):
                self.put(win, y, x0 + 2, text, self.attr(9, dim=True), maxw=w - 3)
            else:
                notes = line.startswith(("  Fonte", "  Custo", "  Estado"))
                self.put(win, y, x0 + 2, text, self.attr(8, dim=notes), maxw=w - 3)


    # -- input
    def handle(self, ch) -> bool:
        if ch in (ord("q"), 27):
            if self.show_help:
                self.show_help = False
                return True
            if ch == 27:
                return True
            return False
        if ch == ord("?"):
            self.show_help = not self.show_help
            return True
        if self.show_help:
            self.show_help = False
            return True
        if ch == ord("p"):
            self.paused = not self.paused
            return True
        if ch == ord("r"):
            self.scanner = Scanner(self.scanner.hours)
            self.refresh()
            self.flash("releitura completa")
            return True
        if ch == ord("s"):
            self.sort_key = SORT_KEYS[(SORT_KEYS.index(self.sort_key) + 1) % len(SORT_KEYS)]
            self.flash(f"ordem: {self.sort_key} {SORT_ARROW.get(self.sort_key, '')} · {SORT_HELP.get(self.sort_key, '')}")
            self.rebuild()
            return True
        if ch == ord("a"):
            self.show_all = not self.show_all
            self.refresh()
            return True
        if ch == ord("y"):
            row = self.selected()
            if row and row.agent.transcript:
                self.copy(row.agent.transcript)
            return True
        if ch == ord("o"):
            self.open_selected()
            return True
        if ch == ord("x"):
            self.act_stop()
            return True
        if ch == ord("X"):
            self.act_rm()
            return True
        if ch == ord("l"):
            self.act_logs()
            return True
        if ch == ord("f"):
            self.act_pin()
            return True
        if ch == ord("d"):
            self.act_hide()
            return True
        if ch == ord("D"):
            self.act_unhide()
            return True
        if ch == ord("b"):
            self.act_browser("branch")
            return True
        if ch == ord("B"):
            self.act_browser("pr")
            return True
        if ch == ord("O"):
            row = self.selected()
            if row:
                self.copy(resume_command(row.agent), "comando copiado")
            return True
        if ch == 9:  # tab
            self.focus = "log" if self.focus == "table" else "table"
            self.log_scroll = 0
            return True
        if ch == ord("/"):
            self.prompt_filter()
            return True
        if ch in STATE_FILTER_KEYS:
            self.act_filter(*STATE_FILTER_KEYS[ch])
            return True
        if self.focus == "log":
            if ch in (curses.KEY_UP, ord("k")):
                self.log_scroll = min(self.log_scroll + 1, 2000)
            elif ch in (curses.KEY_DOWN, ord("j")):
                self.log_scroll = max(0, self.log_scroll - 1)
            elif ch == curses.KEY_PPAGE:
                self.log_scroll = min(self.log_scroll + 10, 2000)
            elif ch == curses.KEY_NPAGE:
                self.log_scroll = max(0, self.log_scroll - 10)
            return True
        if ch in (curses.KEY_UP, ord("k")):
            self.move(-1)
            self.log_scroll = 0
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.move(1)
            self.log_scroll = 0
        elif ch == curses.KEY_PPAGE:
            self.move(-10)
        elif ch == curses.KEY_NPAGE:
            self.move(10)
        elif ch in (curses.KEY_HOME, ord("g")):
            self.move_edge(False)
        elif ch in (curses.KEY_END, ord("G")):
            self.move_edge(True)
        elif ch in (curses.KEY_ENTER, 10, 13, ord(" ")):
            self.toggle()
            self.log_scroll = 0
        return True

    def prompt_filter(self):
        win = getattr(self, "stdscr", None)
        if win is None:
            return
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        buf = self.filter_text
        height, width = win.getmaxyx()
        while True:
            self.put(win, height - 1, 0, " " * (width - 1), self.attr(7))
            self.put(win, height - 1, 1, "/" + buf, self.attr(1, bold=True))
            try:
                win.move(height - 1, min(width - 2, 2 + len(buf)))
            except curses.error:
                pass
            win.refresh()
            ch = win.getch()
            if ch in (10, 13, curses.KEY_ENTER):
                break
            if ch == 27:
                buf = ""
                break
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                buf = buf[:-1]
            elif ch == curses.KEY_RESIZE:
                height, width = win.getmaxyx()
            elif 32 <= ch < 127:
                buf += chr(ch)
        self.filter_text = buf
        self.sel = 0
        self.rebuild()
        try:
            curses.curs_set(0)
        except curses.error:
            pass

    def copy(self, text, label="caminho copiado"):
        argv, data = clipboard_cmd(text)
        if not argv:
            self.flash("clipboard indisponível")
            return
        try:
            subprocess.run(argv, input=data, check=False, timeout=2)
            self.flash(label)
        except (OSError, subprocess.SubprocessError):
            self.flash(f"{argv[0]} indisponível")

    def confirm(self, token, label):
        """Acao destrutiva: exige o mesmo toque duas vezes em 4s."""
        if self.pending and self.pending[0] == token and now() - self.pending[1] < 4:
            self.pending = None
            return True
        self.pending = (token, now())
        self.flash(f"{label}? tecle de novo em 4s")
        return False

    def target_job(self):
        """Agente selecionado, se for um job em background (subagente aponta para o pai)."""
        row = self.selected()
        if not row:
            return None, "nada selecionado"
        agent = row.agent
        if agent.kind != "job":
            return None, "só vale para jobs em background"
        return agent, ""

    def act_stop(self):
        agent, why = self.target_job()
        if not agent:
            self.flash(why)
            return
        if not self.confirm(("stop", agent.key), f"parar {agent.short}"):
            return
        _, err = claude_cli(["stop", agent.short], timeout=20)
        if err:
            self.flash(f"falhou parar {agent.short}: {err}")
            return
        self.flash(f"{agent.short} parado — retomável com o")
        self.probe.at = 0.0
        self.refresh()

    def act_rm(self):
        agent, why = self.target_job()
        if not agent:
            self.flash(why)
            return
        if not self.confirm(("rm", agent.key), f"apagar {agent.short} e o histórico (irreversível)"):
            return
        _, err = claude_cli(["rm", agent.short], timeout=20)
        if err:
            self.flash(f"falhou apagar {agent.short}: {err}")
            return
        self.flash(f"{agent.short} apagado")
        self.probe.at = 0.0
        self.refresh()

    def act_logs(self):
        agent, why = self.target_job()
        if not agent:
            self.flash(why)
            return
        ok, err, name = open_in_terminal(logs_command(agent))
        if ok:
            self.flash(f"logs de {agent.short} em {name}")
        else:
            self.flash(f"falhou abrir logs: {err}")

    def act_browser(self, kind):
        row = self.selected()
        if not row:
            return
        url, err = repo_url(row.agent, kind)
        if not url:
            self.flash(err)
            return
        ok, msg = open_url(url)
        if ok:
            self.flash(("PR" if kind == "pr" else "branch") + ": " + url)
        else:
            self.flash(f"falhou abrir: {msg}")

    def open_selected(self):
        row = self.selected()
        if not row:
            return
        if row.agent.kind == "job":
            cmd = attach_command(row.agent)
        else:
            cmd = resume_command(row.agent)
        ok, err, name = open_in_terminal(cmd)
        if ok:
            self.flash(f"abrindo em {name} · {cmd}")
        else:
            self.copy(cmd, "comando copiado (falha ao abrir terminal)")
            self.flash(f"falhou abrir {name}: {trunc(err, 80)}")


# ---------------------------------------------------------------- main

def json_snapshot(agents):
    out = []
    for agent in agents:
        out.append({
            "key": agent.key,
            "kind": agent.kind,
            "name": agent.name,
            "sessionId": agent.session_id,
            "short": agent.short,
            "state": agent.state,
            "detail": agent.detail,
            "model": agent.model,
            "cwd": agent.cwd,
            "branch": agent.branch,
            "live": agent.live,
            "pid": agent.pid,
            "liveStatus": agent.live_status,
            "ctxTokens": agent.ctx,
            "ctxWindow": agent.ctx_window,
            "ctxPct": agent.ctx_pct,
            "tokens": agent.tokens,
            "costEstimate": round(agent.cost, 4),
            "startedAt": agent.started,
            "updatedAt": agent.updated,
            "elapsedSec": int(agent.elapsed or 0),
            "template": agent.template,
            "inFlight": agent.in_flight,
            "queued": agent.queued,
            "tools": agent.tools,
            "errors": agent.errors,
            "turns": agent.turns,
            "subagents": [
                {
                    "id": sub.id,
                    "label": sub.label,
                    "model": sub.model,
                    "status": sub.status,
                    "tokens": sub.tokens,
                    "elapsedSec": int(((sub.done or now()) - sub.started) if sub.started else 0),
                }
                for sub in agent.subs
            ],
            "transcript": agent.transcript,
        })
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="claudetop",
        description="htop-style live monitor for Claude Code agents and subagent swarms",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--once", action="store_true", help="imprime um snapshot e sai")
    parser.add_argument("--json", action="store_true", help="snapshot em JSON")
    parser.add_argument("--all", action="store_true", help="inclui sessoes antigas e temporarias")
    parser.add_argument("--hours", type=float, default=8.0, help="janela de sessoes em horas (default 8)")
    parser.add_argument("--interval", type=float, default=1.0, help="intervalo de refresh em segundos")
    parser.add_argument("--filter", default="", help="filtro inicial")
    parser.add_argument("--sort", default="project", choices=SORT_KEYS,
                        help="ordenacao inicial: project|state|age|tokens|name (default project)")
    args = parser.parse_args(argv)

    scanner = Scanner(hours=args.hours)

    if args.json:
        agents = apply_live(scanner.collect(show_all=args.all), LiveProbe(ttl=0).get(force=True))
        print(json.dumps(json_snapshot(agents), indent=2, ensure_ascii=False))
        return 0

    if args.once:
        agents = apply_live(scanner.collect(show_all=args.all), LiveProbe(ttl=0).get(force=True))
        expanded = {a.key for a in agents if a.kind == "job" or (a.subs and len(a.subs) <= 6)}
        try:
            cols = os.get_terminal_size().columns
        except OSError:
            cols = 150
        width = max(100, min(200, cols))
        prefs = Prefs()
        print(snapshot(agents, expanded, args.filter, args.sort, width,
                       prefs.pinned, prefs.hidden))
        return 0

    if not sys.stdout.isatty():
        agents = scanner.collect(show_all=args.all)
        width = 150
        expanded = {a.key for a in agents if a.kind == "job"}
        prefs = Prefs()
        print(snapshot(agents, expanded, args.filter, args.sort, width,
                       prefs.pinned, prefs.hidden))
        return 0

    if curses is None:
        print("claudetop: curses indisponível (no Windows: pip install windows-curses); "
              "use --once ou --json", file=sys.stderr)
        return 1
    tui = TUI(scanner, args)
    curses.wrapper(tui.loop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
