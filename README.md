# local-gpu

One command to see which local AI model server holds the GPU, and to switch between them.

A 16 GB card fits one model stack at a time: a voice model, a sound-effects model, an image
model or a local LLM. Load two and nothing fails; Windows quietly pages the overflow into
system RAM and both run several times slower. When a project uses several of these tools
(an animated short needs voices, sound effects and images), someone has to keep track of
what's running, stop it, start the next one and wait for it to warm up. `gpu.py` does that,
and asks before stopping anything.

Built for [local-voice](https://github.com/vl3c/local-voice),
[local-sounds](https://github.com/vl3c/local-sounds) and
[local-image](https://github.com/vl3c/local-image), and for Claude Code agents that use them
(see [SKILL.md](SKILL.md)). Any server that follows the same contract can be added.

```
> python gpu.py status
GPU  NVIDIA GeForce RTX 5080: 10.3 GB of 15.9 GB used, 5.6 GB free

server        state               VRAM     PID        up  port
local-voice   up                8.7 GB   24356       51s  8190
local-sounds  down                                        8191   typical: ~4 GB SA3, ~14 GB peak with MOSS too
local-image   down                                        8188   typical: small until the first image, then ~15.5 GB

other GPU programs: firefox.exe (29916) 0.7 GB
```

## Commands

| Command | What it does |
|---|---|
| `gpu.py status [--json]` | Card total from nvidia-smi; per server: state, VRAM, PID, uptime, busy/idle; other programs holding over 0.5 GB. Read-only. |
| `gpu.py switch <name> [args...]` | Lists what would be stopped and how much VRAM that frees, refuses if one of them is mid-job, asks, stops them through their own `stop.cmd`, checks the port closed and VRAM came back, starts the target with `start.cmd --no-browser [args]`, waits until it answers. |
| `gpu.py stop <name>\|all` | The same, without starting anything. |
| `gpu.py check <name> [--speed] [--json]` | Is the server up and holding its model in dedicated VRAM? Exit 1 when it is down, not ready yet, **spilling** (1 GB or more of its memory outside VRAM: in shared memory, or evicted to system RAM, which the `GPU Process Memory` counters show as Total Committed minus Dedicated and Shared Usage) or **contended** (another program, often another registered model server, has 1 GB or more evicted: the card is oversubscribed). Either way everything on the card runs several times slower: measured here, two copies of a 27B model generating at once fell from about 70 to 7 tok/s each. `--speed` times a 128-token completion on llama.cpp servers (never while busy) and also fails below the server's `min_tok_s`. Read-only. |

Options: `--yes` (the user already confirmed), `--force` (stop a server even while it runs a
job), `--timeout S` (how long `switch` waits for the target). Arguments after the name go to
the target's `start.cmd`; ones that start with `--` go after `--`, e.g.
`gpu.py switch local-voice -- --safe`.

### Confirmation

Nothing that's running is stopped without a yes. In a console, `switch` and `stop` ask
`[y/N]`. Without one (an agent's shell, a pipe, `< NUL`) they print the plan and exit with
code **2**; the caller shows the plan to the user and reruns with `--yes` once they agree.
A piped `y` is not a yes.

Exit codes: 0 done, 1 refused or failed (a server mid-job, a stop that didn't take, a
target that never became ready), 2 needs confirmation.

## Install

Windows 10/11, an NVIDIA GPU, Python 3.10+ (standard library only), and the tools you want
it to manage installed as Claude Code skills under `~/.claude/skills/<name>` (a folder or a
junction to one).

```powershell
git clone https://github.com/vl3c/local-gpu.git
New-Item -ItemType Junction -Path "$env:USERPROFILE\.claude\skills\local-gpu" -Target "$PWD\local-gpu"
python local-gpu\gpu.py status
```

The junction makes [SKILL.md](SKILL.md) visible to Claude Code; `gpu.py` itself runs from
anywhere.

## How it works

- **Which process is the server:** the one listening on the server's port, found with
  `Get-NetTCPConnection`. It counts as this install's server only if its command line or
  executable contains `<skills folder>\<name>\<owner>` (through the junction or the real
  path). Anything else on that port (a worktree copy under test, another program) shows as
  `other install` and is never stopped or started over.
- **Servers that load before they listen** (local-voice, local-sounds) are found during
  warmup through the PID file they write at startup, and show as `starting`.
- **Per-server VRAM:** on Windows with the WDDM driver, `nvidia-smi --query-compute-apps`
  reports `[N/A]` for each process's memory, and the servers' own health figures are either
  card-wide or miss their worker processes. `gpu.py` reads the Windows performance counter
  `\GPU Process Memory(*)\Dedicated Usage` and sums it over the server process and its
  children (engine workers, ComfyUI's re-exec). On the machine it was built on, the counter's
  per-process values summed to within 0.2 GB of nvidia-smi's total. `dwm.exe` reports a
  meaningless value (larger than the card) and is ignored.
- **Stopping** always runs the tool's own `stop.cmd`, never a kill by process name. Not every
  `stop.cmd` returns a reliable exit code, so afterwards `gpu.py` checks for itself that the
  port closed and the process is gone, then watches nvidia-smi for the VRAM to come back.
- **Starting** runs `start.cmd` with its output going to a temporary file rather than a pipe:
  the server window it opens inherits the handles, and an inherited pipe would stay open
  for the server's lifetime.

## Registry

[servers.json](servers.json) lists the shared servers. Your own servers, or changes to the
shared ones, go in `servers.local.json` next to it (ignored by git), with the same shape;
entries are merged field by field:

```json
{
  "servers": {
    "my-llm": {
      "owner": "bin/",
      "port": 8080,
      "health": "/health",
      "busy": "llama-slots",
      "start_args": ["--no-browser"],
      "warmup_s": 300,
      "typical": "~12 GB"
    },
    "local-image": { "warmup_s": 300 }
  }
}
```

| Field | Meaning |
|---|---|
| `port` | TCP port the server listens on. |
| `hosts` | Addresses to try for health, default `["127.0.0.1"]`. |
| `health` | GET path that answers 200 when the server is ready (503 shows as `loading`). |
| `owner` | Path under the tool's folder that the listening process's command line or executable must contain. |
| `busy` | How to tell a job is in flight: `health:<field>` (a boolean in the health JSON), `comfy-queue` (ComfyUI's `/queue`) or `llama-slots` (llama.cpp's `/slots`). Optional. |
| `pid_file`, `pid_cmd` | For servers that load their models before opening the port: the PID file (relative to the folder) and a regex its command line must match. Optional. |
| `start_args` | Passed to `start.cmd`. |
| `warmup_s` | How long `switch` waits for health. |
| `typical` | Shown by `status` while the server is down. |
| `min_tok_s` | Optional: `check --speed` fails when the measured speed is below this (tokens/s). |
| `folder` | Folder name under `skills_dir`, if it isn't the server's name. |

### What a server must provide

- `start.cmd` that opens the server in its own window and returns at once (`--no-browser`
  for agents).
- `stop.cmd` that stops only this install's server.
- An HTTP health endpoint on a fixed port.

## Safety

- `gpu.py` never stops a server you didn't confirm, never stops anything that isn't the
  registered install, and never touches other GPU programs.
- It talks only to the local machine: loopback HTTP, PowerShell, nvidia-smi.
- `scripts/check_repo_safe.py --history` scans the tracked files and every commit for local
  paths, addresses, emails and credential-shaped strings; CI runs it on every push.

## License

[MIT](LICENSE)
