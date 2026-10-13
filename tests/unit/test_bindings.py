import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "replication"))
from function_app import app, ensure_enabled


class BindingTests(unittest.TestCase):
    def test_functions_and_triggers(self):
        functions = {item.get_function_name(): item for item in app.get_functions()}
        self.assertEqual(set(functions), {"Replicate", "Reconcile"})
        self.assertEqual(functions["Replicate"].get_trigger().type, "serviceBusTrigger")
        self.assertEqual(functions["Reconcile"].get_trigger().type, "timerTrigger")

    def test_fenced_worker_raises_instead_of_acknowledging(self):
        for value in ["false", "", "invalid"]:
            with self.subTest(value=value), patch.dict(os.environ, {"REPLICATION_ENABLED": value}):
                with self.assertRaises((ValueError, RuntimeError)):
                    ensure_enabled()

    def test_enabled_worker(self):
        with patch.dict(os.environ, {"REPLICATION_ENABLED": "true"}):
            ensure_enabled()
