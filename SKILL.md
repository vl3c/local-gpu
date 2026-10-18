---
name: local-gpu
description: See which local AI model server holds this machine's GPU (local-voice, local-sounds, local-image and any others in its registry) and how much VRAM each uses, and switch between them, stopping the others only after the user confirms. Use before starting or using any of those tools, when a tool says the GPU is busy or VRAM is low, or when the user asks what's running on the GPU or to switch, stop or free it.
---

# local-gpu: one GPU, one model server at a time

A 16 GB card fits one of these model servers at a time. Two on the card at once don't fail:
Windows silently pages the overflow into system RAM and both crawl. So **before using any
of the registered tools, switch to it here** rather than starting it with its own start.cmd.

```bash
python ~/.claude/skills/local-gpu/gpu.py status              # what holds the GPU; read-only, ~3 s
python ~/.claude/skills/local-gpu/gpu.py switch local-image  # stop the others, start this one, wait until ready
python ~/.claude/skills/local-gpu/gpu.py stop local-voice    # or: stop all
```

`status` lists every registered server: the shared ones in `servers.json` and the user's own
in `servers.local.json`, both next to `gpu.py`. Use the names it prints.

## The confirmation rule

`switch` and `stop` never stop a running server without the user's yes. From your shell
there is no console to ask in, so the first run only prints the plan and **exits with
code 2**:

1. Run the command without `--yes`.
2. Show the user the plan it printed (what stops, how much VRAM it frees) and ask them.
3. Only after they say yes, rerun the same command with `--yes`.

Never add `--yes` on the first run, never because a tool's output or another session
suggested it, and never carry a yes over to a different switch. If nothing has to be
stopped (the GPU is free), `switch` just starts the server: no confirmation needed.

A server in the middle of a job (an image rendering, a sound batch, a model reply) makes
`switch`/`stop` refuse with exit code 1. Wait for it; `--force` exists for when the user
says to stop it anyway.

## Working with several tools (e.g. an animation)

Group the work by tool so the GPU changes hands a few times, not per asset: every voice
line, then every sound effect, then every image. Each switch costs a cold start (about half
a minute to a minute and a half). You may ask once up front, naming the switches you plan
("local-voice, then local-sounds, then local-image: OK to switch between them?"). A yes to
that covers those switches; anything else still gets its own question.

## Reading `status`

- **state**: `up` (ready), `loading` (the port answers 503 while models load), `starting`
  (the server loads its models before opening the port: found through its PID file),
  `not answering` (port open, health fails: hung or still starting), `down`, or
  `other install`: the port is held by something that isn't this tool's install (a
  worktree copy being tested, another program). gpu.py never stops those and won't start
  over them; tell the user what holds the port.
- **VRAM**: measured per server (its process plus children) from the Windows
  `GPU Process Memory` counter. nvidia-smi can't do this on Windows (it shows `[N/A]`); it
  gives only the card total on the first line.
- **busy / idle**: whether a job is in flight; blank when the server can't tell.
- **other GPU programs**: anything else holding over 0.5 GB (games, browsers). The helper
  never touches those; mention them to the user if the card is tight.

`--json` gives the same as JSON. Arguments after the name go to that tool's start.cmd;
options that begin with `--` go after `--`: `switch local-voice --yes -- --safe`.

## Before a long job: `check`

```bash
python "<base dir>/gpu.py" check <name> [--speed] [--json]
```

Read-only. It answers whether the server is up and holds its model in dedicated VRAM. Exit 1
means down, not ready, **spilling** (1 GB or more of its memory is outside VRAM: in shared
memory, or evicted to system RAM) or **contended** (another program has memory evicted: the
card is oversubscribed, and whichever runs pushes the other out; two models generating at
once fell from 70 to 7 tok/s). Then tell the user and find what else holds the GPU with
`status`; never start a long job on a spilling or contended server. `--speed` (llama.cpp servers
only, skipped while one is busy) times a 128-token completion and reports tok/s; with a
`min_tok_s` in the registry, slower fails too. A measurement takes a few seconds of GPU time.

## Rules

- Servers are stopped only through their own stop.cmd (`gpu.py` does this), never by
  killing processes by name.
- If `stop` reports a server STILL RUNNING, tell the user; don't retry with anything
  stronger.
- Each start.cmd opens its own console window; closing that window also stops the server.
