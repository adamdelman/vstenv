"""realtime: the host's rtprio limit and RTKit decide the doctor's verdict."""
import unittest
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

    def test_no_limit_warns_unless_rtkit_runs(self):
        ok, why = realtime.summary(self._s("rtprio=0\nrtkit=1\n")); self.assertTrue(ok); self.assertIn("RTKit only", why)   # the usual desktop: OK
        ok, why = realtime.summary(self._s("rtprio=0\nrtkit=0\n")); self.assertFalse(ok); self.assertIn("normal priority", why)

    def test_unreadable_is_not_a_failure(self):
        self.assertTrue(realtime.summary(self._s(""))[0])


if __name__ == "__main__":
    unittest.main()
