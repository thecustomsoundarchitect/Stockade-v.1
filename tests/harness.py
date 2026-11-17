"""Starts the fake stores and a bricksync-test process in a scratch folder, and drives cycles."""
import json, os, shutil, signal, subprocess, tempfile, time
from fakestores import FakeBrickLink, FakeBrickOwl

HERE = os.path.dirname(os.path.abspath(__file__))
BINARY = os.path.join(HERE, "..", "bricksync-test")
CONF = '''autocheck = 1;
bricklink.consumerkey = "ck0000";
bricklink.consumersecret = "cs0000";
bricklink.token = "tk0000";
bricklink.tokensecret = "ts0000";
brickowl.key = "bokey0000";
checkmessage = 0;
'''


class Env:
    def __init__(self, interval=3600):
        self.bl, self.bo = FakeBrickLink(), FakeBrickOwl()
        self.dir = tempfile.mkdtemp(prefix="bstest-")
        os.makedirs(os.path.join(self.dir, "data"))
        with open(os.path.join(self.dir, "data", "bricksync.conf.txt"), "w") as f:
            f.write(CONF + ("autosyncinterval = %d;\n" % interval if interval else ""))
        self.proc, self.out = None, None

    def path(self, *parts):
        return os.path.join(self.dir, *parts)

    def status(self):
        try:
            with open(self.path("data", "autosync.status.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"cycle": 0}

    def output(self):
        with open(self.path("stdout.txt"), errors="replace") as f:
            return f.read()

    def start(self, wait=True):
        """Start BrickSync. On a first start it asks before changing Brick Owl: the test answers yes."""
        env = dict(os.environ, BS_TEST_BL_PORT=str(self.bl.server.port), BS_TEST_BO_PORT=str(self.bo.server.port))
        self.out = open(self.path("stdout.txt"), "ab")
        # The status file of an earlier run must not be read as this run's
        if os.path.exists(self.path("data", "autosync.status.json")):
            os.remove(self.path("data", "autosync.status.json"))
        self.proc = subprocess.Popen([BINARY], cwd=self.dir, env=env, stdin=subprocess.PIPE, stdout=self.out, stderr=subprocess.STDOUT)
        if not os.path.exists(self.path("data", "bricksync.state")):
            deadline = time.time() + 60
            while "Do you agree to apply these changes" not in self.output():
                if time.time() > deadline or self.proc.poll() is not None:
                    raise RuntimeError("no first-run prompt:\n" + self.output()[-3000:])
                time.sleep(0.2)
            self.send("yes")
        if wait:
            self.wait_cycle(1)

    def send(self, line):
        self.proc.stdin.write((line + "\n").encode())
        self.proc.stdin.flush()

    def wait_cycle(self, target, timeout=120):
        """Wait until this process has finished its cycle number `target` (it counts from 1 at each start)."""
        deadline = time.time() + timeout
        while True:
            if self.status().get("cycle", 0) >= target:
                return self.status()
            if self.proc.poll() is not None:
                raise RuntimeError("bricksync exited:\n" + self.output()[-3000:])
            if time.time() > deadline:
                raise RuntimeError("timeout waiting for cycle %d:\n%s" % (target, self.output()[-3000:]))
            time.sleep(0.2)

    def cycle(self):
        """Ask for a cycle now (the check command) and wait for it to finish."""
        target = self.status().get("cycle", 0) + 1
        self.send("check")
        return self.wait_cycle(target)

    def kill(self):
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait()
        self.out.close()
        if os.path.exists(self.path("data", "bricksync.lock")):
            os.remove(self.path("data", "bricksync.lock"))

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.send("quit")
                self.proc.wait(timeout=20)
            except Exception:
                self.proc.kill()
                self.proc.wait()
        if self.out:
            self.out.close()

    def cleanup(self):
        self.stop()
        shutil.rmtree(self.dir, ignore_errors=True)
