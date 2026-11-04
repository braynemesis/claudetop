#!/usr/bin/env python3
"""Gera um ~/.claude ficticio (projetos, agentes, subagentes e um job bloqueado).

Serve para demos e screenshots sem expor dados reais. Uso:

    python3 tests/make_demo_home.py /tmp/claudetop-demo --live

Com ``--live`` o script fica acrescentando mensagens na sessao em destaque, para
que o painel mostre ritmo de tokens e a linha de estado "running".
"""
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE_SID = "7f3a91c2-5d64-4b18-9a0e-2c6d4f8b1a37"

PROJECTS = [
    {
        "dir": "-Users-demo-code-acme-api",
        "cwd": "/Users/demo/code/acme-api",
        "branch": "feat/auth-middleware",
        "sessions": [
            {
                "sid": LIVE_SID,
                "title": "Refatorar middleware de auth",
                "model": "claude-sonnet-4-6",
                "age": 20,
                "stop": "tool_use",
                "ctx": 148_400,
                "cache_read": 142_000,
                "cache_write": 4_800,
                "tools": [("Read", "src/middleware/auth.ts"), ("Grep", "requireSession"),
                          ("Edit", "src/middleware/auth.ts"), ("Task", "revisar guardas de sessao")],
                "events": [
                    ("you", "Quero extrair o middleware de auth para um modulo testavel."),
                    ("ai", "Vou mapear os pontos de entrada e as guardas de sessao."),
                    ("tool", "Grep requireSession"),
                    ("tool", "Read src/middleware/auth.ts"),
                    ("ai", "Encontrei 4 guardas duplicadas e 2 chamadas sem fallback."),
                    ("tool", "Task revisar guardas de sessao"),
                    ("you", "Cobre também o caminho de tenant nulo."),
                    ("ai", "Adicionei o caso e um teste de regressão."),
                    ("you", "Roda a suíte completa antes de fechar."),
                    ("tool", "Bash pytest tests/auth -q"),
                ],
                "subs": [
                    {"id": "a1", "label": "mapear guardas de sessao", "status": "working",
                     "age": 25, "model": "claude-haiku-4-5", "tokens": 9_800},
                    {"id": "a2", "label": "propor testes do middleware", "status": "done",
                     "age": 240, "model": "claude-haiku-4-5", "tokens": 14_600},
                ],
            },
            {
                "sid": "b28d4c11-0f7a-4c55-8e39-6a1b2c3d4e5f",
                "title": "Corrigir race no cache de sessao",
                "model": "claude-sonnet-4-6",
                "age": 420,
                "stop": "end_turn",
                "ctx": 92_400,
                "cache_read": 88_000,
                "cache_write": 2_100,
                "tools": [("Read", "src/cache/session.ts"), ("Bash", "pytest tests/cache")],
                "events": [
                    ("you", "O cache de sessao some quando duas requests chegam juntas."),
                    ("tool", "Read src/cache/session.ts"),
                    ("ai", "Era um check-then-set sem lock; troquei por set-if-absent."),
                    ("tool", "Bash pytest tests/cache -q"),
                    ("ai", "12 testes passando, incluindo o caso concorrente."),
                ],
                "subs": [
                ],
            },
            {
                "sid": "c9e0a7d3-1b2c-4d5e-8f90-a1b2c3d4e5f6",
                "title": "Atualizar SDK interno para v3",
                "model": "claude-haiku-4-5",
                "age": 9000,
                "stop": "end_turn",
                "ctx": 88_000,
                "cache_read": 86_000,
                "cache_write": 900,
                "tools": [("Edit", "package.json"), ("Bash", "npm test")],
                "events": [
                    ("you", "Sobe o SDK interno para a v3 e ajusta os imports."),
                    ("tool", "Edit package.json"),
                    ("tool", "Bash npm test"),
                    ("ai", "Build e testes verdes; nada mais usa a API antiga."),
                ],
                "subs": [],
            },
        ],
    },
    {
        "dir": "-Users-demo-code-acme-checkout",
        "cwd": "/Users/demo/code/acme-checkout",
        "branch": "feat/pix",
        "sessions": [
            {
                "sid": "d4f1b8a2-6c3d-4e5f-9a0b-1c2d3e4f5a6b",
                "title": "Fluxo de pagamento PIX",
                "model": "claude-opus-4-8",
                "age": 55,
                "stop": "tool_use",
                "ctx": 164_200,
                "cache_read": 158_000,
                "cache_write": 6_200,
                "tools": [("Read", "src/payments/pix.ts"), ("Grep", "webhook"),
                          ("Task", "validar contrato do PSP")],
                "events": [
                    ("you", "Implementa o fluxo de PIX com confirmacao por webhook."),
                    ("ai", "Separei criacao da cobranca e conciliacao do webhook."),
                    ("tool", "Read src/payments/pix.ts"),
                    ("tool", "Grep webhook"),
                    ("tool", "Task validar contrato do PSP"),
                    ("you", "Garante idempotência no reenvio do webhook."),
                    ("ai", "A chave de idempotência agora vem do PSP."),
                ],
                "subs": [
                    {"id": "a4", "label": "validar contrato do PSP", "status": "working",
                     "age": 40, "model": "claude-haiku-4-5", "tokens": 12_900},
                    {"id": "a5", "label": "revisar idempotencia", "status": "working",
                     "age": 30, "model": "claude-sonnet-4-6", "tokens": 8_400},
                ],
            },
        ],
    },
    {
        "dir": "-Users-demo-code-acme-dashboard",
        "cwd": "/Users/demo/code/acme-dashboard",
        "branch": "feat/revenue-chart",
        "sessions": [
            {
                "sid": "e5a2c9b1-7d4e-4f60-8b1c-3d4e5f6a7b8c",
                "title": "Novo grafico de receita",
                "model": "claude-sonnet-4-6",
                "age": 5400,
                "stop": "end_turn",
                "ctx": 132_000,
                "cache_read": 129_000,
                "cache_write": 1_700,
                "tools": [("Read", "src/charts/Revenue.tsx"), ("Edit", "src/charts/Revenue.tsx"),
                          ("Bash", "pnpm test")],
                "events": [
                    ("you", "Cria o grafico de receita mensal com comparativo do ano anterior."),
                    ("tool", "Read src/charts/Revenue.tsx"),
                    ("ai", "Usei a mesma escala dos outros graficos e adicionei tooltip."),
                    ("tool", "Bash pnpm test"),
                    ("ai", "Testes e storybook ok."),
                ],
                "subs": [],
            },
            {
                "sid": "f6b3d0c2-8e5f-4a71-9c2d-4e5f6a7b8c9d",
                "title": "Virtual scroll na tabela de pedidos",
                "model": "claude-sonnet-4-6",
                "age": 330,
                "stop": "end_turn",
                "ctx": 176_000,
                "cache_read": 173_000,
                "cache_write": 2_300,
                "tools": [("Read", "src/table/Orders.tsx"), ("Bash", "pnpm test")],
                "events": [
                    ("you", "A tabela trava com 5 mil pedidos; virtualiza as linhas."),
                    ("tool", "Read src/table/Orders.tsx"),
                    ("ai", "Troquei o map por virtualizer com altura dinamica."),
                    ("tool", "Bash pnpm test"),
                    ("ai", "Scroll estável; falta so revisar o focus ring."),
                ],
                "subs": [
                ],
            },
            {
                "sid": "a7c4e1d3-9f6a-4b82-ad3e-5f6a7b8c9d0e",
                "title": "Code review do PR 128",
                "model": "claude-haiku-4-5",
                "age": 7600,
                "stop": "end_turn",
                "ctx": 64_000,
                "cache_read": 62_000,
                "cache_write": 700,
                "tools": [("Read", "diff.patch"), ("Grep", "useEffect")],
                "events": [
                    ("you", "Revisa o PR 128 com foco em vazamento de efeito."),
                    ("tool", "Read diff.patch"),
                    ("ai", "Dois useEffect sem cleanup; deixei comentarios objetivos."),
                ],
                "subs": [],
            },
        ],
    },
    {
        "dir": "-Users-demo-code-acme-docs",
        "cwd": "/Users/demo/code/acme-docs",
        "branch": "main",
        "sessions": [
            {
                "sid": "b8d5f2e4-0a7b-4c93-be4f-6a7b8c9d0e1f",
                "title": "Atualizar guia de deploy",
                "model": "claude-haiku-4-5",
                "age": 11000,
                "stop": "end_turn",
                "ctx": 51_000,
                "cache_read": 49_000,
                "cache_write": 600,
                "tools": [("Read", "docs/deploy.md"), ("Edit", "docs/deploy.md")],
                "events": [
                    ("you", "Atualiza o guia de deploy para o novo pipeline."),
                    ("tool", "Read docs/deploy.md"),
                    ("tool", "Edit docs/deploy.md"),
                    ("ai", "Removi passos obsoletos e documentei o rollback."),
                ],
                "subs": [],
            },
        ],
    },
    {
        "dir": "-Users-demo-code-acme-mobile",
        "cwd": "/Users/demo/code/acme-mobile",
        "branch": "feat/push",
        "sessions": [
            {
                "sid": "c9e6a3f5-1b8c-4da4-cf5a-7b8c9d0e1f2a",
                "title": "Push notifications no iOS",
                "model": "claude-sonnet-4-6",
                "age": 6200,
                "stop": "end_turn",
                "ctx": 148_000,
                "cache_read": 145_000,
                "cache_write": 1_900,
                "tools": [("Read", "ios/AppDelegate.swift"), ("Bash", "xcodebuild test")],
                "events": [
                    ("you", "Configura push notifications e o handler de permissao."),
                    ("tool", "Read ios/AppDelegate.swift"),
                    ("ai", "Registro do device token e fallback para negacao."),
                    ("tool", "Bash xcodebuild test"),
                ],
                "subs": [],
            },
            {
                "sid": "d0f7b4a6-2c9d-4eb5-d06b-8c9d0e1f2a3b",
                "title": "Deep link para o checkout",
                "model": "claude-opus-4-8",
                "age": 780,
                "stop": "end_turn",
                "ctx": 172_800,
                "cache_read": 169_000,
                "cache_write": 3_800,
                "tools": [("Read", "ios/Router.swift"), ("Edit", "ios/Router.swift")],
                "events": [
                    ("you", "Abre o checkout direto por deep link, com fallback para a home."),
                    ("tool", "Read ios/Router.swift"),
                    ("ai", "Roteamento por host + path; fallback cobre link invalido."),
                    ("tool", "Edit ios/Router.swift"),
                ],
                "subs": [
                ],
            },
        ],
    },
]


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def base_entry(kind, ts, sid, cwd, branch, **extra):
    entry = {
        "type": kind,
        "timestamp": iso(ts),
        "sessionId": sid,
        "cwd": cwd,
        "gitBranch": branch,
        "version": "2.1.311",
        "isSidechain": extra.pop("side", False),
    }
    entry.update(extra)
    return entry


def user(ts, sid, cwd, branch, text, side=False, agent=None):
    entry = base_entry("user", ts, sid, cwd, branch, side=side,
                       message={"role": "user", "content": text})
    if agent:
        entry["agentId"] = agent
    return entry


def assistant(ts, sid, cwd, branch, model, usage, text="", stop="tool_use",
              tools=(), thinking=None):
    blocks = []
    if thinking:
        blocks.append({"type": "thinking", "thinking": thinking})
    if text:
        blocks.append({"type": "text", "text": text})
    for name, target in tools:
        blocks.append({"type": "tool_use", "id": "toolu_" + name.lower(),
                       "name": name, "input": summarize(name, target)})
    if not blocks:
        blocks = [{"type": "text", "text": "ok"}]
    return base_entry("assistant", ts, sid, cwd, branch,
                      message={"model": model, "role": "assistant",
                               "stop_reason": stop, "usage": usage,
                               "content": blocks})


def summarize(name, target):
    if name == "Read":
        return {"file_path": target}
    if name == "Grep":
        return {"pattern": target}
    if name == "Edit":
        return {"file_path": target, "old_string": "a", "new_string": "b"}
    if name == "Bash":
        return {"command": target}
    return {"description": target, "prompt": target}


def usage(cache_read, cache_write, output=900, inbox=120):
    return {"input_tokens": inbox, "output_tokens": output,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write}


def write_rows(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def build_session(root, project, spec):
    sid = spec["sid"]
    cwd, branch = project["cwd"], project["branch"]
    end = time.time() - spec["age"]
    rows = []
    n_events = len(spec["events"])
    span = min(spec["age"], 1500)
    start = end - span
    rows.append(user(start - 30, sid, cwd, branch,
                     spec["events"][0][1] if spec["events"] else spec["title"]))
    rows.append(base_entry("ai-title", start - 25, sid, cwd, branch, aiTitle=spec["title"]))
    tools = list(spec["tools"])
    model = spec["model"]
    cache_read = spec["cache_read"]
    cache_write = spec["cache_write"]
    for i, (tag, text) in enumerate(spec["events"][1:], start=1):
        ts = start + span * (i + 1) / (n_events + 1)
        stop = spec["stop"] if i == n_events - 1 else "tool_use"
        if tag == "you":
            rows.append(user(ts, sid, cwd, branch, text))
        elif tag == "tool":
            name, target = tools.pop(0) if tools else ("Read", "src/index.ts")
            rows.append(assistant(ts, sid, cwd, branch, model,
                                  usage(cache_read, cache_write),
                                  stop="tool_use", tools=[(name, target)]))
        elif tag == "think":
            rows.append(assistant(ts, sid, cwd, branch, model,
                                  usage(cache_read, cache_write),
                                  stop=stop, thinking=text))
        else:
            rows.append(assistant(ts, sid, cwd, branch, model,
                                  usage(cache_read, cache_write),
                                  text=text, stop=stop))
    path = os.path.join(root, "projects", project["dir"], sid + ".jsonl")
    write_rows(path, rows)

    for sub in spec["subs"]:
        sub_end = time.time() - sub["age"]
        sub_start = sub_end - min(sub["age"], 600)
        first_w = int(sub["tokens"] * 0.65)
        rest_w = max(200, sub["tokens"] - first_w)
        sub_rows = [
            user(sub_start, sid, cwd, branch, sub["label"], side=True, agent=sub["id"]),
            base_entry("agent-name", sub_start + 1, sid, cwd, branch,
                       side=True, agentName=sub["label"]),
            assistant(sub_start + 5, sid, cwd, branch, sub["model"],
                      usage(9_000, first_w, output=1200),
                      text="Analisando os arquivos relevantes.",
                      stop="tool_use", tools=[("Read", "src/index.ts")]),
            assistant(sub_end, sid, cwd, branch, sub["model"],
                      usage(9_000, rest_w, output=800),
                      text="Concluido; resumo enviado ao agente principal.",
                      stop="end_turn" if sub["status"] == "done" else "tool_use"),
        ]
        write_rows(os.path.join(root, "projects", project["dir"], sid,
                                "subagents", "agent-" + sub["id"] + ".jsonl"), sub_rows)


def build_job(root):
    job = os.path.join(root, "jobs", "9c4e1f2a")
    os.makedirs(job, exist_ok=True)
    now = time.time()
    state = {
        "state": "blocked",
        "detail": "aguardando aprovacao para rodar a migration de orders",
        "name": "Migration da tabela orders",
        "sessionId": "aa11bb22-cc33-4d44-8e55-ff66aa77bb88",
        "daemonShort": "9c4e1f2a",
        "cwd": "/Users/demo/code/acme-checkout",
        "template": "bg",
        "respawnFlags": ["--model", "sonnet", "--permission-mode", "ask"],
        "createdAt": iso(now - 2400),
        "updatedAt": iso(now - 180),
        "tokens": 214_800,
        "inFlight": {"tasks": 1, "queued": 2},
        "fan": [
            {"id": "a9", "kind": "agent", "label": "planejar a migration",
             "startedAt": int((now - 900) * 1000), "doneAt": int((now - 600) * 1000)},
            {"id": "b1", "kind": "agent", "label": "ensaiar em staging",
             "startedAt": int((now - 300) * 1000)},
        ],
    }
    with open(os.path.join(job, "state.json"), "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    with open(os.path.join(job, "timeline.jsonl"), "w", encoding="utf-8") as fh:
        for i, (text, st) in enumerate([
            ("lendo schema atual", "working"),
            ("gerando migration", "working"),
            ("ensaiando em staging", "working"),
            ("aguardando aprovacao", "blocked"),
        ]):
            fh.write(json.dumps({"at": iso(now - 900 + i * 240), "state": st,
                                 "detail": text}) + "\n")


def live_tick(root, i):
    """Acrescenta uma resposta na sessao em destaque para alimentar o ritmo."""
    path = os.path.join(root, "projects", PROJECTS[0]["dir"], LIVE_SID + ".jsonl")
    now = time.time()
    cache_read = 142_000 + i * 320
    texts = [
        "Extrai a guarda de sessao para um helper unico.",
        "Os testes cobrem o caminho sem cookie e o expirado.",
        "Removi a duplicacao entre as rotas privadas.",
        "Ajustei o tipo do contexto para incluir o tenant.",
    ]
    tools = [("Read", "src/middleware/auth.ts"), ("Grep", "requireSession"),
             ("Edit", "src/middleware/tenant.ts"), ("Bash", "pytest tests/auth -q")]
    rows = [
        assistant(now, LIVE_SID, PROJECTS[0]["cwd"], PROJECTS[0]["branch"],
                  "claude-sonnet-4-6", usage(cache_read, 260, output=190, inbox=30),
                  text=texts[i % len(texts)],
                  stop="tool_use", tools=[tools[i % len(tools)]]),
    ]
    with open(path, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def build(root):
    os.makedirs(os.path.join(root, "projects"), exist_ok=True)
    os.makedirs(os.path.join(root, "jobs"), exist_ok=True)
    for project in PROJECTS:
        for spec in project["sessions"]:
            build_session(root, project, spec)
    build_job(root)


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    root = args[0] if args else "/tmp/claudetop-demo"
    build(root)
    print(root)
    if "--live" not in argv:
        return 0
    try:
        i = 0
        while True:
            time.sleep(6)
            live_tick(root, i)
            i += 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
