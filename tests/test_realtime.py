"""realtime: the host's rtprio limit and RTKit decide the doctor's verdict."""
import os, subprocess, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from vstenv import realtime


class RealtimeTest(unittest.TestCase):
    def _s(self, out):
        with mock.patch("vstenv.host.sh", return_value=out):
            return realtime.status()

    def test_limit_gives_real_time(self):
        s = self._s("rtprio=98\nrtkit=0\n")
        self.assertEqual(s["rtprio"], 98); self.assertTrue(realtime.summary(s)[0])
        self.assertTrue(realtime.summary(self._s("rtprio=unlimited\nrtkit=0\n"))[0])

    def test_no_limit_warns(self):
        ok, why = realtime.summary(self._s("rtprio=0\nrtkit=1\n")); self.assertFalse(ok); self.assertIn("RTKit", why)
        ok, why = realtime.summary(self._s("rtprio=0\nrtkit=0\n")); self.assertFalse(ok); self.assertIn("normal priority", why)

    def test_unreadable_is_not_a_failure(self):
        self.assertTrue(realtime.summary(self._s(""))[0])



def _probe(**kw):
    d = dict(rtprio=0, rtkit=0, user="alice", conf=0, pkexec=1); d.update(kw)
    return "\n".join(f"{k}={v}" for k, v in d.items()) + "\n"


class EnableTest(unittest.TestCase):
    def test_enable_writes_the_limits_file_through_pkexec_on_the_host(self):
        probes = iter([_probe(), _probe(conf=1)])
        with mock.patch("vstenv.host.sh", side_effect=lambda *a, **k: next(probes)), \
             mock.patch("vstenv.host.run", return_value=SimpleNamespace(returncode=0, stderr="")) as run:
            ok, msg = realtime.enable()
        self.assertTrue(ok); self.assertIn("Log out and back in", msg)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:3], ["pkexec", "sh", "-c"]); self.assertEqual(cmd[4:], ["sh", "alice"])
        self.assertIn(realtime.CONF, cmd[3])

    def test_script_writes_both_limits_and_is_idempotent(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            conf = Path(tmp) / "rt.conf"
            script = realtime.ENABLE_SCRIPT.replace(realtime.CONF, str(conf))
            for _ in range(2): subprocess.run(["sh", "-c", script, "sh", "alice"], check=True)
            self.assertEqual(conf.read_text(), "alice - rtprio 95\nalice - memlock unlimited\n")
            for bad in ("", "Bob", "a b", "x;rm -rf /", "-x", "a\nb", "9z"):
                self.assertEqual(subprocess.run(["sh", "-c", script, "sh", bad], capture_output=True).returncode, 2, bad)
            self.assertEqual(conf.read_text(), "alice - rtprio 95\nalice - memlock unlimited\n")

    def test_bad_user_names_never_reach_pkexec(self):
        for bad in ("", "Bob", "a b", "x;id", "../x", "9z", "-x"):
            with mock.patch("vstenv.host.sh", return_value=_probe(user=bad)), mock.patch("vstenv.host.run") as run:
                ok, _ = realtime.enable()
            self.assertFalse(ok, bad); run.assert_not_called()

    def test_already_set_up_asks_nothing(self):
        for probe in (_probe(rtprio=95), _probe(conf=1)):
            with mock.patch("vstenv.host.sh", return_value=probe), mock.patch("vstenv.host.run") as run:
                self.assertTrue(realtime.enable()[0])
            run.assert_not_called()
        with mock.patch("vstenv.host.sh", return_value=_probe(conf=1)):
            s = realtime.status()
        ok, why = realtime.summary(s); self.assertFalse(ok); self.assertIn("Log out", why)
        self.assertFalse(realtime.can_enable(s)); self.assertIn("log out", realtime.fix(s))

    def test_a_cancelled_prompt_changes_nothing(self):
        with mock.patch("vstenv.host.sh", return_value=_probe()), \
             mock.patch("vstenv.host.run", return_value=SimpleNamespace(returncode=126, stderr="")):
            ok, msg = realtime.enable()
        self.assertFalse(ok); self.assertIn("cancelled", msg)

    def test_without_pkexec_it_says_what_to_write(self):
        with mock.patch("vstenv.host.sh", return_value=_probe(pkexec=0)):
            ok, msg = realtime.enable()
        self.assertFalse(ok); self.assertIn("alice - rtprio 95", msg)

    def test_a_no_is_remembered(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp, \
             mock.patch.object(realtime, "DECLINED", Path(tmp) / "d"), mock.patch("vstenv.host.sh", return_value=_probe()):
            self.assertTrue(realtime.should_offer()); self.assertIn("realtime enable", realtime.fix(realtime.status()))
            realtime.decline()
            self.assertFalse(realtime.should_offer())
        with mock.patch("vstenv.host.sh", return_value=_probe(rtprio=95)):
            self.assertFalse(realtime.can_enable(realtime.status()))


if __name__ == "__main__":
    unittest.main()
