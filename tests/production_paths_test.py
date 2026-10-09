"""Installed services take mutable paths from the installer, not app/.local."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

class ProductionPathsTests(unittest.TestCase):
    def test_runtime_and_diagnostics_use_explicit_data_paths(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, DSH_KNOWLEDGE_STATE_DIR=str(Path(temp) / 'state'),
                       DSH_KNOWLEDGE_DIAGNOSTICS_DIR=str(Path(temp) / 'diagnostics'),
                       PYTHONPATH=str(root / 'engine'), PYTHONDONTWRITEBYTECODE='1')
            code = ('import os; from pathlib import Path; import service, diagnostics; '
                    'assert service.PROFILE_STATE_FILE == Path(os.environ["DSH_KNOWLEDGE_STATE_DIR"]) / "isolation-profile.json"; '
                    'assert diagnostics.DEFAULT_DIR == Path(os.environ["DSH_KNOWLEDGE_DIAGNOSTICS_DIR"]); '
                    'sink = diagnostics.open_for_job("synthetic-job"); sink.stage("request_received", "synthetic-job"); '
                    'assert list(diagnostics.DEFAULT_DIR.glob("*.jsonl"))')
            run = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, timeout=20)
            self.assertEqual(run.returncode, 0, run.stderr.decode('utf-8', errors='replace'))