import subprocess
import unittest
from unittest.mock import patch

from cfd_twin.parallelism import default_solver_mpi_ranks


class PerformanceCoreTests(unittest.TestCase):
    @patch("cfd_twin.parallelism.platform.system", return_value="Darwin")
    @patch("cfd_twin.parallelism.subprocess.run")
    def test_named_performance_tier_on_m5_pro(self, run, system):
        run.return_value.stdout = (
            "hw.perflevel0.physicalcpu: 6\nhw.perflevel0.name: Super\n"
            "hw.perflevel1.physicalcpu: 12\nhw.perflevel1.name: Performance\n"
        )
        self.assertEqual(default_solver_mpi_ranks(), 18)

    @patch("cfd_twin.parallelism.platform.system", return_value="Darwin")
    @patch("cfd_twin.parallelism.subprocess.run")
    def test_older_apple_silicon(self, run, system):
        run.return_value.stdout = (
            "hw.perflevel0.physicalcpu: 8\nhw.perflevel0.name: Performance\n"
            "hw.perflevel1.physicalcpu: 4\nhw.perflevel1.name: Efficiency\n"
        )
        self.assertEqual(default_solver_mpi_ranks(), 8)

    @patch("cfd_twin.parallelism.platform.system", return_value="Darwin")
    @patch("cfd_twin.parallelism.subprocess.run", side_effect=subprocess.TimeoutExpired("sysctl", 5))
    def test_unavailable_probe_falls_back(self, run, system):
        self.assertEqual(default_solver_mpi_ranks(), 4)

    @patch("cfd_twin.parallelism.platform.system", return_value="Linux")
    def test_other_hosts_keep_existing_default(self, system):
        self.assertEqual(default_solver_mpi_ranks(), 4)
