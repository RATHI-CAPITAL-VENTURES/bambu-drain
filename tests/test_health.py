"""The health verdict is what everything downstream keys on.

RIA alerts on it, so it has two hard requirements: it must be **stable** when
nothing is wrong (or a change-detecting watch fires constantly), and it must
actually notice the failures this project is prone to — all of which are silent
by nature.
"""

import unittest

from bambu_drain import health

HEALTHY = {
    "gadget": {"exists": True, "bound": True, "media_present": True,
               "usb_state": "configured", "idle_seconds": 12.0},
    "drain": {"blocked_reason": None, "staging_pct": 0.3, "staging_bytes": 1},
    "archive": {"files_total": 4, "files_pending_ship": 0, "bytes_total": 1},
    "recent_events": [],
}


def with_(**over):
    p = {k: dict(v) if isinstance(v, dict) else list(v) for k, v in HEALTHY.items()}
    for k, v in over.items():
        if k in p and isinstance(p[k], dict):
            p[k].update(v)
        else:
            p[k] = v
    return p


class TestHealthy(unittest.TestCase):
    def test_healthy_is_exactly_ok(self):
        self.assertEqual(health.problems(HEALTHY), [])
        self.assertEqual(health.verdict(HEALTHY), "ok")

    def test_verdict_is_stable_across_normal_variation(self):
        # A watch fires when this string changes. Idle seconds, byte counts and
        # file counts all move constantly during normal operation and must not
        # move the verdict.
        a = health.verdict(with_(gadget={"idle_seconds": 3.0},
                                 archive={"files_total": 4}))
        b = health.verdict(with_(gadget={"idle_seconds": 9999.0},
                                 archive={"files_total": 4000}))
        self.assertEqual(a, b, "verdict must not change during healthy operation")

    def test_a_closed_idle_gate_is_not_a_problem(self):
        # "printer active" is the normal state during a print.
        p = with_(drain={"blocked_reason": "printer active (3s since last write)"})
        self.assertEqual(health.problems(p), [])


class TestTheSilentFailures(unittest.TestCase):
    """Each of these actually happened, and none announces itself."""

    def test_medium_ejected(self):
        p = with_(gadget={"media_present": False})
        self.assertIn("empty card reader", health.verdict(p))

    def test_charge_only_cable(self):
        p = with_(gadget={"usb_state": "not attached"})
        self.assertIn("charge-only cable", health.verdict(p))

    def test_gadget_unbound(self):
        self.assertIn("not bound", health.verdict(with_(gadget={"bound": False})))

    def test_staging_filling_because_the_mac_is_away(self):
        p = with_(drain={"staging_pct": 82.0})
        self.assertIn("staging 82% full", health.verdict(p))

    def test_staging_just_below_the_threshold_is_quiet(self):
        self.assertEqual(health.problems(with_(drain={"staging_pct": 74.9})), [])

    def test_drain_loop_stopped(self):
        p = with_(drain_loop_age_seconds=3600)
        self.assertIn("has not run for 60 minutes", health.verdict(p))

    def test_a_recent_drain_is_fine(self):
        self.assertEqual(health.problems(with_(drain_loop_age_seconds=45)), [])

    def test_data_integrity_event_surfaces(self):
        now = 1_000_000.0
        p = with_(ts=now, recent_events=[
            {"kind": "local_corrupt", "detail": "a.mp4 lost", "ts": now - 60},
        ])
        self.assertIn("data integrity", health.verdict(p))

    def test_problems_accumulate(self):
        p = with_(gadget={"media_present": False}, drain={"staging_pct": 90.0})
        self.assertEqual(len(health.problems(p)), 2)
        self.assertIn(";", health.verdict(p))


if __name__ == "__main__":
    unittest.main()


class TestEventsExpire(unittest.TestCase):
    """An incident is news for an hour, not forever.

    A `ship_mismatch` from a power cut was still the reported verdict hours
    after the cause was fixed. An alert that never clears is an alert you learn
    to ignore.
    """

    def _payload(self, age_seconds):
        now = 1_000_000.0
        return with_(ts=now, recent_events=[
            {"kind": "ship_mismatch", "detail": "a.mp4", "ts": now - age_seconds},
        ])

    def test_a_fresh_event_alarms(self):
        v = health.verdict(self._payload(120))
        self.assertIn("data integrity", v)
        self.assertIn("2m ago", v)

    def test_an_old_event_does_not(self):
        self.assertEqual(health.problems(self._payload(7200)), [])

    def test_the_boundary(self):
        self.assertEqual(health.problems(self._payload(3601)), [])
        self.assertNotEqual(health.problems(self._payload(3599)), [])

    def test_an_event_with_no_timestamp_is_treated_as_ancient(self):
        p = with_(ts=1_000_000.0,
                  recent_events=[{"kind": "local_corrupt", "detail": "x"}])
        self.assertEqual(health.problems(p), [])
