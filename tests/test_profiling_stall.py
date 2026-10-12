"""Tests for the DCGM profiling-stall guard (cus.7).

The exporter script is not an importable module (hyphenated filename, runtime
deps that are not installed off-node), so load it by path with the runtime
modules stubbed. Only pure helpers and the re-arm bookkeeping are exercised;
the GPU paths that need a real node are covered by the canary procedure in
CLAUDE.md.
"""
import importlib.util
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, os.pardir, "slurm-job-exporter.py")


def _stub(name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def load_exporter():
    class _Family:
        def __init__(self, *a, **k):
            self.samples = []

        def add_metric(self, labels, value):
            self.samples.append((tuple(labels), value))

    _stub("psutil", process_iter=lambda: [], NoSuchProcess=Exception)
    _stub("prometheus_client", make_wsgi_app=lambda *a, **k: None)
    _stub("prometheus_client.core", GaugeMetricFamily=_Family, CounterMetricFamily=_Family)
    spec = importlib.util.spec_from_file_location("sje", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sje = load_exporter()


class ProfilingStallDetection(unittest.TestCase):
    def test_zero_sm_active_on_busy_device_is_a_stall(self):
        self.assertTrue(sje.profiling_stalled(sm_active=0.0, gpu_util=97))

    def test_tiny_kernel_workload_is_not_a_stall(self):
        # CLAUDE.md: tiny-kernel jobs legitimately read ~3% SM_ACTIVE at 100% util.
        self.assertFalse(sje.profiling_stalled(sm_active=0.03, gpu_util=100))

    def test_single_sm_wait_kernel_is_not_a_stall(self):
        # Seen on rudin-02: a DDP rank parked in a NCCL wait kernel keeps one SM of
        # 142 busy, so GR_ENGINE and nvidia-smi read 100% while SM_ACTIVE reads
        # 0.007. That is a true reading, not a stall.
        self.assertFalse(sje.profiling_stalled(sm_active=0.007, gpu_util=100))

    def test_any_nonzero_sm_active_is_not_a_stall(self):
        # One resident warp on a 188-SM card at 10% engine activity is ~0.0005.
        # A stalled session reads an exact 0.0; anything above zero is data.
        self.assertFalse(sje.profiling_stalled(sm_active=0.0005, gpu_util=100))

    def test_idle_gpu_is_not_a_stall(self):
        self.assertFalse(sje.profiling_stalled(sm_active=0.0, gpu_util=0))

    def test_missing_profiling_field_is_not_a_stall(self):
        # No sm_active at all is the existing non-profiling-card path, not a stall.
        self.assertFalse(sje.profiling_stalled(sm_active=None, gpu_util=100))

    def test_missing_device_util_cannot_be_judged(self):
        self.assertFalse(sje.profiling_stalled(sm_active=0.0, gpu_util=None))


class UtilizationSelection(unittest.TestCase):
    def test_healthy_profiling_reports_sm_active_in_percent(self):
        data = {"sm_active": 0.9, "dram_active": 0.6, "gpu_util": 100, "mem_copy_util": 50}
        self.assertEqual(sje.gpu_utilization_values(data), (90.0, 60.0, False))

    def test_stalled_profiling_substitutes_device_utilization(self):
        data = {"sm_active": 0.0, "dram_active": 0.0, "gpu_util": 97, "mem_copy_util": 61}
        self.assertEqual(sje.gpu_utilization_values(data), (97, 61, True))

    def test_no_profiling_data_yields_nothing(self):
        data = {"gpu_util": 100, "mem_copy_util": 40}
        self.assertEqual(sje.gpu_utilization_values(data), (None, None, False))


class _FakeSamples:
    def __init__(self):
        self.calls = []

    def WatchFields(self, field_group, update_freq_us, max_keep_age, max_keep_samples):
        self.calls.append((field_group, update_freq_us, max_keep_age, max_keep_samples))


class _FakeGroup:
    def __init__(self):
        self.samples = _FakeSamples()


def make_collector(interval=10):
    c = sje.SlurmJobCollector.__new__(sje.SlurmJobCollector)
    c.groups = {"gpu": _FakeGroup(), "mig": _FakeGroup()}
    c.field_groups = {"gpu": "gpu-fg", "mig": "mig-fg"}
    c._dcgm_update_interval = interval
    c._last_rearm = 0.0
    c._rearm_count = 0
    return c


class WatchRearm(unittest.TestCase):
    def test_rearm_reissues_watch_with_startup_parameters(self):
        c = make_collector(interval=10)
        self.assertTrue(c._rearm_dcgm_watches(now=1000.0, reason="test"))
        self.assertEqual(c.groups["gpu"].samples.calls, [("gpu-fg", 10 * 1000 * 1000, 20.0, 5)])
        self.assertEqual(c.groups["mig"].samples.calls, [("mig-fg", 10 * 1000 * 1000, 20.0, 5)])
        self.assertEqual(c._rearm_count, 1)

    def test_rearm_is_rate_limited(self):
        c = make_collector()
        self.assertTrue(c._rearm_dcgm_watches(now=1000.0, reason="test"))
        self.assertFalse(c._rearm_dcgm_watches(now=1030.0, reason="test"))
        self.assertEqual(len(c.groups["gpu"].samples.calls), 1)
        self.assertTrue(c._rearm_dcgm_watches(now=1061.0, reason="test"))
        self.assertEqual(c._rearm_count, 2)


if __name__ == "__main__":
    unittest.main()
