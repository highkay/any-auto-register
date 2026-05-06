import subprocess
import sys
import unittest
from pathlib import Path


class TurnstileSolverStartTests(unittest.TestCase):
    def test_start_script_help_resolves_repo_imports(self):
        script = Path("services/turnstile_solver/start.py").resolve()
        proc = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=script.parents[2],
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)
        combined = f"{proc.stdout}\n{proc.stderr}".lower()
        self.assertIn("usage", combined)


if __name__ == "__main__":
    unittest.main()
