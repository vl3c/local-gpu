#!/usr/bin/env python3
"""See which local AI server holds the GPU, and switch between them.

  gpu.py status [--json]
  gpu.py switch <name> [start args...] [--yes] [--force] [--timeout S]
  gpu.py stop <name>|all [--yes] [--force]

The servers are listed in servers.json, plus servers.local.json if it exists (not tracked
by git: your own servers, or changes to the shared entries, field by field). Each one is
stopped through its own stop.cmd
and started through its own start.cmd, never by killing processes by name. Anything that
stops a running server asks first; without a console to ask in (an agent's shell) it
prints the plan and exits with code 2, and the caller reruns with --yes once the user
has agreed.

Per-process VRAM: nvidia-smi reports [N/A] for it on Windows (WDDM), so each server's
figure is the "GPU Process Memory / Dedicated Usage" performance counter summed over the
process listening on its port and that process's children. Only the card total comes
from nvidia-smi. Standard library only.
"""
import argparse
import base64
import ctypes
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
GB = 2**30
OTHER_MIN = 0.5 * GB      # other GPU programs below this are not worth listing
IGNORE = {"dwm.exe"}      # its Dedicated Usage counter is meaningless (larger than the card)
STOP_WAIT = 20            # seconds for a server to be gone after its stop.cmd


def load_registry(here: Path = HERE) -> dict:
    data = json.loads((here / "servers.json").read_text(encoding="utf-8"))
    local = here / "servers.local.json"
    if local.is_file():
        extra = json.loads(local.read_text(encoding="utf-8"))
        data["skills_dir"] = extra.get("skills_dir", data["skills_dir"])
        for name, s in extra.get("servers", {}).items():
            data["servers"][name] = dict(data["servers"].get(name, {}), **s)
    skills = Path(os.path.expanduser(data["skills_dir"]))
    servers = {}
    for name, s in data["servers"].items():
        folder = skills / s.get("folder", name)
        # A process counts as this install's if its command line or executable contains
        # <folder>\<owner>, through the junction or the real path (Windows reports whichever
        # path started it). A worktree of the same tool lives deeper and doesn't match.
        roots = {str(folder), os.path.realpath(folder)}
        owner = s["owner"].replace("/", "\\")
        servers[name] = dict(s, name=name, folder=folder,
                             marks=[(r.rstrip("\\") + "\\" + owner).lower() for r in roots])
    return servers


def owns(s: dict, proc: dict | None) -> bool:
    text = ((proc or {}).get("cmd") or "") + "\n" + ((proc or {}).get("exe") or "")
    return any(m in text.lower() for m in s["marks"])


# ---------------------------------------------------------------- reading the machine

# One PowerShell call: listening sockets on the servers' ports, every process (with parent
# and start time), and per-process dedicated GPU memory. Numbers go out as JSON, so the
# Windows locale's number format never matters.
PS_QUERY = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
$ports = @(%PORTS%)
$epoch = [datetime]'1970-01-01'
$listen = @(Get-NetTCPConnection -State Listen | Where-Object { $ports -contains $_.LocalPort } |
  ForEach-Object { @{ addr = [string]$_.LocalAddress; port = [int]$_.LocalPort; pid = [int]$_.OwningProcess } })
$lp = @($listen | ForEach-Object { $_.pid })
$procs = @(Get-CimInstance Win32_Process | ForEach-Object {
  $s = 0; if ($_.CreationDate) { $s = [int64]($_.CreationDate.ToUniversalTime() - $epoch).TotalSeconds }
  $c = $null; $e = $null
  if ($_.Name -in 'python.exe', 'llama-server.exe' -or $lp -contains [int]$_.ProcessId) { $c = $_.CommandLine; $e = $_.ExecutablePath }
  @{ pid = [int]$_.ProcessId; ppid = [int]$_.ParentProcessId; name = [string]$_.Name; start = $s; cmd = $c; exe = $e } })
$gpu = @{}
(Get-Counter '\GPU Process Memory(*)\Dedicated Usage').CounterSamples | ForEach-Object {
  if ($_.InstanceName -match '^pid_(\d+)_') { $gpu[$matches[1]] = [double]$gpu[$matches[1]] + $_.CookedValue } }
@{ listen = $listen; procs = $procs; gpu = $gpu } | ConvertTo-Json -Depth 4 -Compress
"""


def query_windows(ports) -> dict:
    script = PS_QUERY.replace("%PORTS%", ",".join(str(p) for p in ports))
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                         capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
    data = json.loads(out[out.index("{"):])
    as_list = lambda v: v if isinstance(v, list) else ([v] if v else [])  # noqa: E731
    return {"listen": as_list(data.get("listen")), "procs": as_list(data.get("procs")),
            "gpu": {int(k): v for k, v in (data.get("gpu") or {}).items()}}


def nvidia_smi() -> dict | None:
    """Card name, used and total VRAM in bytes. Retries once: it sometimes answers 'ERROR:'."""
    for _ in range(2):
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total",
                                  "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=15).stdout
            name, used, total = [x.strip() for x in out.splitlines()[0].split(",")]
            return {"name": name, "used": int(used) * 2**20, "total": int(total) * 2**20}
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            time.sleep(1)
    return None


def http_get(url: str, timeout: float = 3):
    """(status, parsed JSON or None); status None when nothing answered."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read()
            try:
                return r.status, json.loads(body)
            except ValueError:
                return r.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return None, None


def pid_alive(pid: int) -> bool:
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    k.GetExitCodeProcess(h, ctypes.byref(code))
    k.CloseHandle(h)
    return code.value == 259  # STILL_ACTIVE


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------- status

def busy_of(s: dict, base: str, health: dict | None):
    """True / False when the server can tell whether a job is in flight, None when it can't."""
    kind = s.get("busy")
    if not kind:
        return None
    if kind.startswith("health:"):
        return bool((health or {}).get(kind[7:])) if health else None
    if kind == "comfy-queue":
        code, q = http_get(base + "/queue")
        return bool(q.get("queue_running") or q.get("queue_pending")) if code == 200 and q else None
    if kind == "llama-slots":
        code, slots = http_get(base + "/slots")
        return any(x.get("is_processing") for x in slots) if code == 200 and isinstance(slots, list) else None
    return None


def snapshot(servers: dict) -> dict:
    win = query_windows(sorted({s["port"] for s in servers.values()}))
    procs = {p["pid"]: p for p in win["procs"]}
    kids: dict[int, list[int]] = {}
    for p in win["procs"]:
        parent = procs.get(p["ppid"])
        if parent and p["pid"] != p["ppid"] and p["start"] >= parent["start"]:  # not a recycled PID
            kids.setdefault(p["ppid"], []).append(p["pid"])

    def tree(pid):
        out, todo = [], [pid]
        while todo:
            x = todo.pop()
            out.append(x)
            todo += kids.get(x, [])
        return out

    now, gpu_ok, owned, rows = time.time(), bool(win["gpu"]), set(), {}
    for name, s in servers.items():
        row = {"name": name, "state": "down", "pid": None, "host": None, "vram": None,
               "uptime_s": None, "busy": None, "other": None}
        hosts = s.get("hosts", ["127.0.0.1"])
        mine = [x for x in win["listen"] if x["port"] == s["port"]]
        if mine and not owns(s, procs.get(mine[0]["pid"])):
            # Something else holds the port: another copy (a worktree) or another program.
            p = procs.get(mine[0]["pid"], {})
            row.update(state="other install", pid=mine[0]["pid"],
                       other=(p.get("cmd") or p.get("exe") or p.get("name") or "?")[:120])
        elif mine:
            addr = mine[0]["addr"]
            row["pid"] = mine[0]["pid"]
            row["host"] = addr if addr in hosts else hosts[0]
            base = f"http://{row['host']}:{s['port']}"
            code, health = http_get(base + s["health"])
            row["state"] = {200: "up", 503: "loading"}.get(code, "not answering")
            if code == 200:
                row["busy"] = busy_of(s, base, health)
        elif s.get("pid_file"):
            # Loads its models before opening the port: during warmup only the PID file finds it.
            try:
                pid = int((s["folder"] / s["pid_file"]).read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                pid = None
            p = procs.get(pid)
            if p and re.search(s["pid_cmd"], p.get("cmd") or ""):
                row.update(state="starting", pid=pid, host=hosts[0])
        if row["pid"]:
            pids = tree(row["pid"])
            owned.update(pids)
            if gpu_ok:
                row["vram"] = sum(win["gpu"].get(x, 0) for x in pids)
            if procs.get(row["pid"], {}).get("start"):
                row["uptime_s"] = int(now - procs[row["pid"]]["start"])
        rows[name] = row

    others = [{"pid": pid, "name": procs.get(pid, {}).get("name", "?"), "vram": b}
              for pid, b in win["gpu"].items()
              if b >= OTHER_MIN and pid not in owned and procs.get(pid, {}).get("name") not in IGNORE]
    others.sort(key=lambda o: -o["vram"])
    return {"gpu": nvidia_smi(), "gpu_counter_ok": gpu_ok, "servers": rows, "others": others}


def gb(b) -> str:
    return "?" if b is None else f"{b / GB:.1f} GB"


def ago(s) -> str:
    if s is None:
        return ""
    h, m = divmod(s // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m" if m else f"{s}s"


def print_status(snap: dict, servers: dict) -> None:
    g = snap["gpu"]
    if g:
        print(f"GPU  {g['name']}: {gb(g['used'])} of {gb(g['total'])} used, {gb(g['total'] - g['used'])} free")
    else:
        print("GPU  nvidia-smi did not answer")
    if not snap["gpu_counter_ok"]:
        print("     (per-process GPU counter unavailable: per-server VRAM shown as ?)")
    print(f"\n{'server':<14}{'state':<15}{'VRAM':>9}  {'PID':>6}  {'up':>8}  port")
    for name, r in snap["servers"].items():
        s = servers[name]
        if r["state"] == "down":
            print(f"{name:<14}{'down':<15}{'':>9}  {'':>6}  {'':>8}  {s['port']}   typical: {s['typical']}")
            continue
        note = {True: "busy: job running", False: "idle"}.get(r["busy"], "")
        if r["other"]:
            note = f"not this install, not stopped by gpu.py: {r['other']}"
        where = f"{r['host']}:{s['port']}" if r["host"] not in (None, "127.0.0.1") else str(s["port"])
        print(f"{name:<14}{r['state']:<15}{gb(r['vram']):>9}  {r['pid'] or '':>6}  {ago(r['uptime_s']):>8}  {where}   {note}")
    if snap["others"]:
        print("\nother GPU programs: " + ", ".join(f"{o['name']} ({o['pid']}) {gb(o['vram'])}" for o in snap["others"]))


# ---------------------------------------------------------------- stop / start

def running(row: dict) -> bool:
    """This install's server is running (in any state): one gpu.py may stop."""
    return row["state"] not in ("down", "other install")


def foreign_note(name: str, s: dict, row: dict) -> str:
    return (f"port {s['port']} ({name}) is held by something that is not this install"
            f" (PID {row['pid']}: {row['other']}). gpu.py doesn't stop it; ask the user.")


def has_console() -> bool:
    """A console someone can type into. isatty() is not enough: on Windows it is also true
    for NUL, which is what an agent's shell often hands a program as stdin."""
    try:
        import msvcrt
        mode = ctypes.c_ulong()
        return bool(ctypes.windll.kernel32.GetConsoleMode(msvcrt.get_osfhandle(sys.stdin.fileno()),
                                                          ctypes.byref(mode)))
    except (OSError, ValueError, AttributeError):
        return False


def confirm(args, question: str) -> bool:
    if args.yes:
        return True
    if not has_console():
        print("\nNothing done yet: this needs the user's confirmation. Ask them, then rerun with --yes.")
        sys.exit(2)
    try:
        return input(f"\n{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def run_cmd(path: Path, args=(), timeout=90):
    """Run a .cmd through cmd.exe with its output in a temp file (a start.cmd's new window
    must not keep a pipe of ours open). Returns (exit code, output)."""
    line = f'cmd /d /s /c ""{path}" {" ".join(args)}"'
    with tempfile.TemporaryFile() as f:
        p = subprocess.run(line, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT, timeout=timeout)
        f.seek(0)
        return p.returncode, f.read().decode("oem", "replace").strip()


def stop_one(s: dict, row: dict) -> bool:
    """Run the server's own stop.cmd, then check that it is really gone: stop.cmd exit codes
    are not reliable for every tool, so the port and the processes are checked here."""
    print(f"\nStopping {s['name']} (its stop.cmd)...")
    code, out = run_cmd(s["folder"] / "stop.cmd")
    for line in out.splitlines():
        print("  | " + line)
    pid = row["pid"]
    deadline = time.time() + STOP_WAIT
    while time.time() < deadline:
        if not port_open(row["host"] or "127.0.0.1", s["port"]) and not (pid and pid_alive(pid)):
            print(f"  {s['name']} is stopped.")
            return True
        time.sleep(1)
    print(f"  {s['name']} is STILL RUNNING (port {s['port']} open or PID {pid} alive) after its stop.cmd"
          f" (exit code {code}). Close its window, or check Task Manager.")
    return False


def stop_many(names: list, snap: dict, servers: dict) -> bool:
    before = (snap["gpu"] or {}).get("used")
    known = [snap["servers"][n]["vram"] for n in names]
    expect = sum(v for v in known if v) if all(v is not None for v in known) else None
    ok = all([stop_one(servers[n], snap["servers"][n]) for n in names])
    if before is not None:
        after = before
        for _ in range(10):  # the driver can take a moment to hand the memory back
            g = nvidia_smi()
            after = g["used"] if g else after
            if expect is None or before - after >= 0.8 * expect:
                break
            time.sleep(1)
        print(f"\nVRAM in use: {gb(before)} -> {gb(after)}" + (f" (expected to free ~{gb(expect)})" if expect else ""))
        if expect and before - after < 0.5 * expect:
            print("  Less came back than expected: run `gpu.py status` to see what still holds it.")
    return ok


def wait_ready(s: dict, timeout: int) -> bool:
    t0 = last = time.time()
    hosts = s.get("hosts", ["127.0.0.1"])
    while time.time() - t0 < timeout:
        for h in hosts:
            if http_get(f"http://{h}:{s['port']}{s['health']}", timeout=2)[0] == 200:
                print(f"  {s['name']} is ready after {int(time.time() - t0)} s.")
                return True
        if time.time() - last >= 20:
            last = time.time()
            print(f"  still waiting ({int(last - t0)} s)...")
        time.sleep(2)
    print(f"  {s['name']} did not answer within {timeout} s. Look at its window and its logs\\server.log.")
    return False


def plan_lines(names: list, snap: dict) -> list:
    lines = []
    for n in names:
        r = snap["servers"][n]
        busy = "  BUSY: a job is running" if r["busy"] else ""
        lines.append(f"  stop  {n:<14} {r['state']:<14} {gb(r['vram']):>9}  PID {r['pid']}{busy}")
    return lines


def refuse_busy(names: list, snap: dict, args) -> None:
    busy = [n for n in names if snap["servers"][n]["busy"]]
    if busy and not args.force:
        print(f"\nRefusing: {', '.join(busy)} {'is' if len(busy) == 1 else 'are'} in the middle of a job."
              " Wait for it to finish, or rerun with --force (stops it anyway, after confirming).")
        sys.exit(1)


def cmd_status(args, servers):
    snap = snapshot(servers)
    if args.json:
        print(json.dumps(snap, indent=2))
    else:
        print_status(snap, servers)


def cmd_stop(args, servers):
    snap = snapshot(servers)
    names = [n for n, r in snap["servers"].items() if running(r)] if args.name == "all" else [args.name]
    if args.name != "all" and snap["servers"][args.name]["state"] == "other install":
        print("Not stopping: " + foreign_note(args.name, servers[args.name], snap["servers"][args.name]))
        return 1
    names = [n for n in names if running(snap["servers"][n])]
    if not names:
        print(f"Nothing to stop: {'no server is' if args.name == 'all' else args.name + ' is not'} running.")
        return 0
    print("Plan:")
    print("\n".join(plan_lines(names, snap)))
    refuse_busy(names, snap, args)
    if not confirm(args, f"Stop {', '.join(names)}?"):
        print("Nothing stopped.")
        return 1
    return 0 if stop_many(names, snap, servers) else 1


def cmd_switch(args, servers):
    s = servers[args.name]
    snap = snapshot(servers)
    me = snap["servers"][args.name]
    names = [n for n, r in snap["servers"].items() if n != args.name and running(r)]
    if me["state"] == "other install":
        print(f"Not switching: {foreign_note(args.name, s, me)}")
        return 1
    foreign = [n for n, r in snap["servers"].items() if n != args.name and r["state"] == "other install"]
    for n in foreign:
        print(f"Note: {foreign_note(n, servers[n], snap['servers'][n])} It holds {gb(snap['servers'][n]['vram'])}.")
    if running(me) and args.extra:
        print(f"Note: {args.name} is already running, so {' '.join(args.extra)} is ignored."
              f" To restart it with different options, stop it first.")
    if names:
        print("Plan:")
        print("\n".join(plan_lines(names, snap)))
        if not running(me):
            print(f"  start {args.name:<14} start.cmd {' '.join(s['start_args'] + args.extra)}")
        if snap["others"]:
            print("  (not touched) other GPU programs: "
                  + ", ".join(f"{o['name']} {gb(o['vram'])}" for o in snap["others"]))
        refuse_busy(names, snap, args)
        if not confirm(args, f"Stop {', '.join(names)}" + ("" if running(me) else f" and start {args.name}") + "?"):
            print("Nothing changed.")
            return 1
        if not stop_many(names, snap, servers):
            print(f"\nNot starting {args.name}: something did not stop.")
            return 1
    if running(me):
        if me["state"] == "up":
            print(f"{args.name} is already running and ready.")
            return 0
        print(f"{args.name} is already running ({me['state']}); waiting for it to be ready...")
        return 0 if wait_ready(s, args.timeout or s["warmup_s"]) else 1
    print(f"\nStarting {args.name} (start.cmd opens its own window)...")
    code, out = run_cmd(s["folder"] / "start.cmd", s["start_args"] + args.extra, timeout=60)
    for line in out.splitlines():
        print("  | " + line)
    if code:
        print(f"  start.cmd exited with code {code}.")
        return 1
    if not wait_ready(s, args.timeout or s["warmup_s"]):
        return 1
    after = snapshot(servers)
    r, g = after["servers"][args.name], after["gpu"]
    if r["state"] != "up":
        # Health answered, but not from this install: another copy took the port first.
        print(f"  But the server answering is not the one just started: {foreign_note(args.name, s, r)}"
              f" This install's start probably failed on the port: see its logs\\server.log.")
        return 1
    print(f"  holding {gb(r['vram'])}" + (f"; GPU {gb(g['used'])} of {gb(g['total'])} used." if g else "."))
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    servers = load_registry()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("status", help="what holds the GPU")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("switch", help="stop the other servers (after confirming) and start this one")
    p.add_argument("name", choices=list(servers))
    p.add_argument("extra", nargs="*", help="passed to start.cmd (put options starting with -- after --)")
    p.add_argument("--timeout", type=int, help="seconds to wait for it to be ready (default: per server)")
    p = sub.add_parser("stop", help="stop one server, or all (after confirming)")
    p.add_argument("name", choices=list(servers) + ["all"])
    for p in sub.choices.values():
        if p.prog.split()[-1] != "status":
            p.add_argument("--yes", action="store_true", help="the user already confirmed")
            p.add_argument("--force", action="store_true", help="stop a server even while it runs a job")
    args = ap.parse_args()
    return {"status": cmd_status, "switch": cmd_switch, "stop": cmd_stop}[args.cmd](args, servers) or 0


if __name__ == "__main__":
    sys.exit(main())
