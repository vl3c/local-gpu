"""Tests for gpu.py. No GPU, no servers: the machine readings (listening sockets, processes,
the GPU counter, nvidia-smi, HTTP) are faked, and stop/start scripts are never the real ones."""
import argparse
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gpu", ROOT / "gpu.py")
gpu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gpu)

GB = 2**30
_TMPS = []


def tearDownModule():
    for t in _TMPS:
        t.cleanup()


class Machine:
    """A fake machine: tmp skills folder, a registry in it, and the readings snapshot() takes."""

    def __init__(self, servers: dict, local: dict | None = None):
        self.tmp = tempfile.TemporaryDirectory()
        _TMPS.append(self.tmp)
        self.root = Path(self.tmp.name)
        self.skills = self.root / "skills"
        (self.root / "servers.json").write_text(json.dumps(
            {"skills_dir": str(self.skills), "servers": servers}), encoding="utf-8")
        if local is not None:
            (self.root / "servers.local.json").write_text(json.dumps(local), encoding="utf-8")
        self.listen, self.procs, self.gpu = [], [], {}
        self.health = {}  # port -> (status, json)
        self.extra = {}   # url suffix -> (status, json), e.g. "/queue"
        self.smi = {"name": "Test GPU", "used": 10 * GB, "total": 16 * GB}
        self.shared = {}  # pid -> shared GPU memory; None: a query result from before it was read
        self.completion = (200, {"timings": {"predicted_per_second": 71.3, "predicted_n": 128}})
        self.posts = []

    def registry(self):
        return gpu.load_registry(self.root)

    def folder(self, name):
        return str(self.skills / name)

    def proc(self, pid, cmd, ppid=1, start=1000, name="python.exe", vram=0.0, shared=0.0):
        self.procs.append({"pid": pid, "ppid": ppid, "name": name, "start": start, "cmd": cmd, "exe": None})
        if vram:
            self.gpu[pid] = vram * GB
        if shared:
            self.shared[pid] = shared * GB

    def http_post_json(self, url, data, timeout=120):
        self.posts.append((url, data))
        return self.completion

    def query(self, ports):
        result = {"listen": self.listen, "procs": self.procs, "gpu": self.gpu}
        if self.shared is not None:
            result["shared"] = self.shared
        return result

    def http_get(self, url, timeout=3):
        for port, answer in self.health.items():
            if f":{port}/" in url and not any(url.endswith(k) for k in self.extra):
                return answer
        for suffix, answer in self.extra.items():
            if url.endswith(suffix):
                return answer
        return None, None

    def patches(self):
        return [mock.patch.object(gpu, "query_windows", self.query),
                mock.patch.object(gpu, "nvidia_smi", lambda: self.smi),
                mock.patch.object(gpu, "http_get", self.http_get),
                mock.patch.object(gpu, "http_post_json", self.http_post_json)]

    def snapshot(self):
        with ExitStack() as stack:
            for p in self.patches():
                stack.enter_context(p)
            return gpu.snapshot(self.registry())


VOICE = {"owner": "server/app.py", "port": 8190, "health": "/api/health",
         "pid_file": "output/.server.pid", "pid_cmd": "server.app\\.py",
         "start_args": ["--no-browser"], "warmup_s": 60, "typical": "8 GB"}
SOUNDS = dict(VOICE, port=8191, busy="health:busy", typical="4 GB")
IMAGE = {"owner": "ComfyUI/main.py", "port": 8188, "health": "/system_stats", "busy": "comfy-queue",
         "start_args": ["--no-browser"], "warmup_s": 60, "typical": "15 GB"}


class RegistryTest(unittest.TestCase):
    def test_local_file_adds_servers_and_overrides_fields(self):
        m = Machine({"local-voice": VOICE}, local={"servers": {
            "mine": dict(VOICE, port=9000), "local-voice": {"warmup_s": 5}}})
        reg = m.registry()
        self.assertEqual(sorted(reg), ["local-voice", "mine"])
        self.assertEqual(reg["local-voice"]["warmup_s"], 5)
        self.assertEqual(reg["local-voice"]["port"], 8190)  # untouched fields kept

    def test_owner_marks_use_backslashes_and_lower_case(self):
        reg = Machine({"local-image": IMAGE}).registry()
        self.assertTrue(all(mk.endswith("\\local-image\\comfyui\\main.py") for mk in reg["local-image"]["marks"]))


class OwnsTest(unittest.TestCase):
    def setUp(self):
        self.m = Machine({"local-image": IMAGE})
        self.s = self.m.registry()["local-image"]
        self.base = self.m.folder("local-image")

    def test_this_install(self):
        self.assertTrue(gpu.owns(self.s, {"cmd": f'python.exe "{self.base}\\ComfyUI\\main.py" --port 8188'}))

    def test_case_and_exe_path(self):
        self.assertTrue(gpu.owns(self.s, {"cmd": None, "exe": f"{self.base.upper()}\\COMFYUI\\MAIN.PY"}))

    def test_worktree_copy_is_not_this_install(self):
        wt = f"{self.base}\\.claude\\worktrees\\x\\ComfyUI\\main.py"
        self.assertFalse(gpu.owns(self.s, {"cmd": f"python.exe {wt}"}))

    def test_unrelated_program(self):
        self.assertFalse(gpu.owns(self.s, {"cmd": "python -m http.server 8188"}))
        self.assertFalse(gpu.owns(self.s, None))


class SnapshotTest(unittest.TestCase):
    def test_states_vram_and_others(self):
        m = Machine({"local-voice": VOICE, "local-sounds": SOUNDS, "local-image": IMAGE})
        # local-sounds: up and busy; its engine worker (a child) holds most of the VRAM.
        m.listen.append({"addr": "127.0.0.1", "port": 8191, "pid": 100})
        m.proc(100, f"py {m.folder('local-sounds')}\\server\\app.py", start=1000, vram=1.0)
        m.proc(101, "py worker.py", ppid=100, start=1001, vram=6.0)
        m.proc(102, "py stale.py", ppid=100, start=900, vram=3.0)  # recycled PID: older than its "parent"
        m.health[8191] = (200, {"ok": True, "busy": True})
        # local-image: port held by a worktree copy.
        m.listen.append({"addr": "127.0.0.1", "port": 8188, "pid": 200})
        m.proc(200, f"py {m.folder('local-image')}\\.claude\\worktrees\\x\\ComfyUI\\main.py", vram=0.5)
        # other GPU programs: a game counts, dwm and small ones don't.
        m.proc(300, None, name="game.exe", vram=2.0)
        m.proc(301, None, name="dwm.exe", vram=30.0)
        m.proc(302, None, name="tiny.exe", vram=0.1)

        snap = m.snapshot()
        voice, sounds, image = (snap["servers"][n] for n in ("local-voice", "local-sounds", "local-image"))
        self.assertEqual(voice["state"], "down")
        self.assertEqual(sounds["state"], "up")
        self.assertTrue(sounds["busy"])
        self.assertAlmostEqual(sounds["vram"] / GB, 7.0)  # server + worker, not the recycled PID
        self.assertEqual(image["state"], "other install")
        self.assertIn("worktrees", image["other"])
        # The recycled PID isn't the server's, so it is listed as another GPU program; the
        # worktree copy is accounted to its row; dwm and anything under 0.5 GB are left out.
        self.assertEqual([(o["pid"], o["name"]) for o in snap["others"]], [(102, "python.exe"), (300, "game.exe")])

    def test_loading_and_not_answering(self):
        m = Machine({"a": dict(VOICE, port=8190), "b": dict(VOICE, port=8191, folder="b")})
        for pid, port, name in ((1, 8190, "a"), (2, 8191, "b")):
            m.listen.append({"addr": "127.0.0.1", "port": port, "pid": pid})
            m.proc(pid, f"py {m.folder(name)}\\server\\app.py")
        m.health[8190] = (503, None)
        snap = m.snapshot()
        self.assertEqual(snap["servers"]["a"]["state"], "loading")
        self.assertEqual(snap["servers"]["b"]["state"], "not answering")

    def test_starting_found_through_pid_file(self):
        m = Machine({"local-voice": VOICE})
        out = m.skills / "local-voice" / "output"
        out.mkdir(parents=True)
        (out / ".server.pid").write_text("42", encoding="ascii")
        m.proc(42, f"py {m.folder('local-voice')}\\server\\app.py", vram=1.5)
        snap = m.snapshot()
        self.assertEqual(snap["servers"]["local-voice"]["state"], "starting")
        self.assertAlmostEqual(snap["servers"]["local-voice"]["vram"] / GB, 1.5)

    def test_stale_pid_file_is_down(self):
        m = Machine({"local-voice": VOICE})
        out = m.skills / "local-voice" / "output"
        out.mkdir(parents=True)
        (out / ".server.pid").write_text("42", encoding="ascii")
        m.proc(42, "notepad.exe", name="notepad.exe")  # the PID now belongs to something else
        self.assertEqual(m.snapshot()["servers"]["local-voice"]["state"], "down")

    def test_comfy_queue_busy(self):
        m = Machine({"local-image": IMAGE})
        m.listen.append({"addr": "127.0.0.1", "port": 8188, "pid": 5})
        m.proc(5, f"py {m.folder('local-image')}\\ComfyUI\\main.py")
        m.health[8188] = (200, {})
        m.extra["/queue"] = (200, {"queue_running": [], "queue_pending": [["job"]]})
        self.assertTrue(m.snapshot()["servers"]["local-image"]["busy"])


def args(**kw):
    base = {"yes": False, "force": False, "extra": [], "timeout": None, "json": False}
    return argparse.Namespace(**dict(base, **kw))


def row(state, pid=None, vram=None, busy=None, other=None):
    return {"state": state, "pid": pid, "host": "127.0.0.1", "vram": vram, "uptime_s": 1,
            "busy": busy, "other": other}


class ConfirmTest(unittest.TestCase):
    def test_yes_skips_the_question(self):
        self.assertTrue(gpu.confirm(args(yes=True), "?"))

    def test_without_a_console_exits_2(self):
        with mock.patch.object(gpu, "has_console", lambda: False), redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit) as e:
                gpu.confirm(args(), "Stop?")
        self.assertEqual(e.exception.code, 2)
        self.assertIn("--yes", out.getvalue())

    def test_console_answer(self):
        with mock.patch.object(gpu, "has_console", lambda: True):
            with mock.patch("builtins.input", lambda q: "y"):
                self.assertTrue(gpu.confirm(args(), "?"))
            with mock.patch("builtins.input", lambda q: ""):
                self.assertFalse(gpu.confirm(args(), "?"))


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.m = Machine({"local-voice": VOICE, "local-sounds": SOUNDS, "local-image": IMAGE})
        self.reg = self.m.registry()
        self.snaps = []
        self.stopped, self.started = [], []

    def run_cmd(self, fn, a):
        snaps = iter(self.snaps)
        with mock.patch.object(gpu, "snapshot", lambda servers: next(snaps)), \
             mock.patch.object(gpu, "stop_many", lambda names, snap, servers: self.stopped.extend(names) or True), \
             mock.patch.object(gpu, "run_cmd", lambda path, a=(), timeout=90: self.started.append((path.parent.name, list(a))) or (0, "")), \
             mock.patch.object(gpu, "wait_ready", lambda s, t: True), \
             mock.patch.object(gpu, "has_console", lambda: False), \
             redirect_stdout(io.StringIO()) as out:
            try:
                code = fn(a, self.reg)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def snap(self, **rows):
        servers = {n: row("down") for n in self.reg}
        servers.update(rows)
        return {"gpu": self.m.smi, "gpu_counter_ok": True, "servers": servers, "others": []}

    def test_switch_needs_confirmation_and_changes_nothing(self):
        self.snaps = [self.snap(**{"local-voice": row("up", 1, 8 * GB)})]
        code, out = self.run_cmd(gpu.cmd_switch, args(name="local-image"))
        self.assertEqual(code, 2)
        self.assertIn("stop  local-voice", out)
        self.assertEqual((self.stopped, self.started), ([], []))

    def test_switch_with_yes_stops_then_starts(self):
        self.snaps = [self.snap(**{"local-voice": row("up", 1, 8 * GB)}),
                      self.snap(**{"local-image": row("up", 2, 0.2 * GB)})]
        code, out = self.run_cmd(gpu.cmd_switch, args(name="local-image", yes=True, extra=["--safe"]))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.stopped, ["local-voice"])
        self.assertEqual(self.started, [("local-image", ["--no-browser", "--safe"])])

    def test_switch_on_free_gpu_starts_without_asking(self):
        self.snaps = [self.snap(), self.snap(**{"local-voice": row("up", 2)})]
        code, _ = self.run_cmd(gpu.cmd_switch, args(name="local-voice"))
        self.assertEqual(code, 0)
        self.assertEqual(self.stopped, [])
        self.assertEqual(len(self.started), 1)

    def test_switch_refuses_while_a_job_runs(self):
        self.snaps = [self.snap(**{"local-sounds": row("up", 1, busy=True)})]
        code, out = self.run_cmd(gpu.cmd_switch, args(name="local-image", yes=True))
        self.assertEqual(code, 1)
        self.assertIn("middle of a job", out)
        self.assertEqual(self.stopped, [])

    def test_switch_force_stops_a_busy_server(self):
        self.snaps = [self.snap(**{"local-sounds": row("up", 1, busy=True)}),
                      self.snap(**{"local-image": row("up", 2)})]
        code, _ = self.run_cmd(gpu.cmd_switch, args(name="local-image", yes=True, force=True))
        self.assertEqual(code, 0)
        self.assertEqual(self.stopped, ["local-sounds"])

    def test_switch_wont_start_over_another_install(self):
        self.snaps = [self.snap(**{"local-image": row("other install", 9, other="worktree copy")})]
        code, out = self.run_cmd(gpu.cmd_switch, args(name="local-image", yes=True))
        self.assertEqual(code, 1)
        self.assertIn("not this install", out)
        self.assertEqual((self.stopped, self.started), ([], []))

    def test_switch_notices_another_install_took_the_port(self):
        # The collision found in the first live test: another copy answered health first.
        self.snaps = [self.snap(), self.snap(**{"local-image": row("other install", 9, other="worktree copy")})]
        code, out = self.run_cmd(gpu.cmd_switch, args(name="local-image"))
        self.assertEqual(code, 1)
        self.assertIn("not the one just started", out)

    def test_other_installs_are_never_stopped(self):
        self.snaps = [self.snap(**{"local-voice": row("other install", 9, other="x")}),
                      self.snap(**{"local-image": row("up", 2)})]
        code, _ = self.run_cmd(gpu.cmd_switch, args(name="local-image", yes=True))
        self.assertEqual(code, 0)
        self.assertEqual(self.stopped, [])

    def test_stop_all(self):
        self.snaps = [self.snap(**{"local-voice": row("up", 1), "local-sounds": row("starting", 2),
                                   "local-image": row("other install", 3, other="x")})]
        code, _ = self.run_cmd(gpu.cmd_stop, args(name="all", yes=True))
        self.assertEqual(code, 0)
        self.assertEqual(self.stopped, ["local-voice", "local-sounds"])

    def test_stop_nothing_running(self):
        self.snaps = [self.snap()]
        code, out = self.run_cmd(gpu.cmd_stop, args(name="local-voice", yes=True))
        self.assertEqual(code, 0)
        self.assertIn("Nothing to stop", out)


class StopOneTest(unittest.TestCase):
    def setUp(self):
        self.s = Machine({"local-voice": VOICE}).registry()["local-voice"]

    def stop(self, port_open, alive):
        with mock.patch.object(gpu, "run_cmd", lambda p, a=(), timeout=90: (0, "  Stopped.")), \
             mock.patch.object(gpu, "port_open", lambda h, p: port_open), \
             mock.patch.object(gpu, "pid_alive", lambda pid: alive), \
             mock.patch.object(gpu, "STOP_WAIT", 0.3), redirect_stdout(io.StringIO()) as out:
            return gpu.stop_one(self.s, row("up", 1)), out.getvalue()

    def test_gone(self):
        ok, out = self.stop(False, False)
        self.assertTrue(ok)
        self.assertIn("| " + "  Stopped.", out)

    def test_survivor_is_reported_not_retried(self):
        ok, out = self.stop(True, False)
        self.assertFalse(ok)
        self.assertIn("STILL RUNNING", out)


@unittest.skipUnless(os.name == "nt", "runs cmd.exe")
class RunCmdTest(unittest.TestCase):
    def test_path_with_spaces_and_arguments(self):
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "a folder" / "start.cmd"
            script.parent.mkdir()
            script.write_text("@echo off\r\necho args=%*\r\nexit /b 3\r\n", encoding="ascii")
            code, out = gpu.run_cmd(script, ["--no-browser", "fast"])
        self.assertEqual(code, 3)
        self.assertEqual(out, "args=--no-browser fast")


class FormatTest(unittest.TestCase):
    def test_gb_and_ago(self):
        self.assertEqual(gpu.gb(None), "?")
        self.assertEqual(gpu.gb(1.5 * GB), "1.5 GB")
        self.assertEqual(gpu.ago(45), "45s")
        self.assertEqual(gpu.ago(125), "2m")
        self.assertEqual(gpu.ago(3 * 3600 + 5 * 60), "3h 05m")


LLAMA = {"owner": "models/", "port": 8080, "hosts": ["127.0.0.1", "127.0.0.2"], "health": "/health",
         "busy": "llama-slots", "start_args": ["--no-browser"], "warmup_s": 60, "typical": "14 GB"}


class CheckTest(unittest.TestCase):
    """`check` runs through the real snapshot(): only the machine readings are faked."""

    def setUp(self):
        self.m = Machine({"local-agent": LLAMA, "local-image": IMAGE})

    def llama_up(self, shared=(0.2, 0.1), addr="127.0.0.2", busy=False, vram=(1.0, 12.0)):
        # The server process and its child; dedicated and shared memory are summed over both.
        self.m.listen.append({"addr": addr, "port": 8080, "pid": 50})
        self.m.proc(50, f"{self.m.folder('local-agent')}\\models\\bin\\llama-server.exe --port 8080",
                    name="llama-server.exe", vram=vram[0], shared=shared[0])
        self.m.proc(51, "helper", ppid=50, start=1001, vram=vram[1], shared=shared[1])
        self.m.health[8080] = (200, {"status": "ok"})
        self.m.extra["/slots"] = (200, [{"is_processing": busy}])

    def check(self, name="local-agent", reg=None, **kw):
        with ExitStack() as stack:
            for p in self.m.patches():
                stack.enter_context(p)
            with redirect_stdout(io.StringIO()) as out:
                code = gpu.cmd_check(args(name=name, speed=kw.get("speed", False), json=kw.get("json", False)),
                                     reg or self.m.registry())
        return code, out.getvalue()

    def test_no_spill_sums_the_process_tree(self):
        self.llama_up()
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn("local-agent: up (pid 50), 13.0 GB dedicated, 0.3 GB shared - no spill", out)
        self.assertIn("card: 6.0 GB of 16.0 GB free", out)
        self.assertEqual(self.m.posts, [])  # no --speed: nothing is sent to the server

    def test_spill_fails_and_says_what_to_do(self):
        self.llama_up(shared=(2.5, 0.5))
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn("3.0 GB shared - SPILLING", out)
        self.assertIn("several times slower", out)
        self.assertIn("gpu.py status", out)

    def test_just_under_the_spill_line_is_fine(self):
        self.llama_up(shared=(0.9, 0.0))
        self.assertEqual(self.check()[0], 0)

    def test_speed_is_measured_on_the_host_that_answered(self):
        self.llama_up(addr="127.0.0.2")
        code, out = self.check(speed=True)
        self.assertEqual(code, 0, out)
        self.assertIn("speed: 71.3 tok/s (128 tokens)", out)
        self.assertEqual(self.m.posts, [("http://127.0.0.2:8080/completion", gpu.SPEED_REQUEST)])

    def test_a_min_tok_s_floor_fails_a_slow_server(self):
        self.llama_up()
        reg = self.m.registry()
        reg["local-agent"]["min_tok_s"] = 80
        code, out = self.check(reg=reg, speed=True)
        self.assertEqual(code, 1)
        self.assertIn("BELOW the expected 80 tok/s", out)
        reg["local-agent"]["min_tok_s"] = 60
        self.assertEqual(self.check(reg=reg, speed=True)[0], 0)

    def test_a_busy_server_is_not_measured(self):
        self.llama_up(busy=True)
        code, out = self.check(speed=True)
        self.assertEqual(code, 0, out)
        self.assertIn("speed: not measured (busy with another request)", out)
        self.assertEqual(self.m.posts, [])

    def test_a_failed_speed_request_is_reported_not_fatal(self):
        self.llama_up()
        self.m.completion = (500, None)
        code, out = self.check(speed=True)
        self.assertEqual(code, 0, out)
        self.assertIn("speed: not measured (the request failed: 500)", out)

    def test_speed_only_for_llama_servers(self):
        self.m.listen.append({"addr": "127.0.0.1", "port": 8188, "pid": 60})
        self.m.proc(60, f"py {self.m.folder('local-image')}\\ComfyUI\\main.py", vram=9.0)
        self.m.health[8188] = (200, {})
        self.m.extra["/queue"] = (200, {"queue_running": [], "queue_pending": []})
        code, out = self.check("local-image", speed=True)
        self.assertEqual(code, 0, out)
        self.assertIn("speed: not measured (not a llama.cpp server)", out)
        self.assertEqual(self.m.posts, [])

    def test_a_server_that_is_down_fails(self):
        code, out = self.check(speed=True)
        self.assertEqual(code, 1)
        self.assertIn("local-agent: not running (down).", out)
        self.assertEqual(self.m.posts, [])

    def test_an_older_query_result_without_shared_counters(self):
        self.m.shared = None
        self.llama_up(shared=(0, 0))
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn("0.0 GB shared - no spill", out)  # an empty map: nothing shared

    def test_no_gpu_counters_does_not_crash(self):
        self.llama_up(shared=(0, 0), vram=(0, 0))
        code, out = self.check(json=True)
        data = json.loads(out)
        self.assertEqual((code, data["dedicated_bytes"], data["shared_bytes"], data["spilling"]), (0, None, None, False))
        code, out = self.check()
        self.assertIn("? dedicated, ? shared - spill unknown (no GPU counters)", out)

    def test_json_result(self):
        self.llama_up(shared=(3.0, 0.0))
        code, out = self.check(speed=True, json=True)
        data = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual({k: data[k] for k in ("name", "state", "running", "pid", "spilling", "tok_s", "tokens", "ok")},
                         {"name": "local-agent", "state": "up", "running": True, "pid": 50, "spilling": True,
                          "tok_s": 71.3, "tokens": 128, "ok": False})
        self.assertEqual(data["shared_bytes"], 3 * GB)
        self.assertEqual(data["card_free_bytes"], 6 * GB)

    def test_the_command_is_wired_read_only(self):
        seen, reg = {}, self.m.registry()
        with mock.patch.object(gpu, "load_registry", lambda *a: reg), \
                mock.patch.object(gpu, "cmd_check", lambda a, s: seen.update(vars(a)) or 3), \
                mock.patch("sys.argv", ["gpu.py", "check", "local-agent", "--speed", "--json"]):
            self.assertEqual(gpu.main(), 3)
        self.assertEqual((seen["name"], seen["speed"], seen["json"]), ("local-agent", True, True))
        self.assertNotIn("yes", seen)  # nothing to confirm: check never stops or starts anything
        self.assertNotIn("force", seen)


if __name__ == "__main__":
    unittest.main()
