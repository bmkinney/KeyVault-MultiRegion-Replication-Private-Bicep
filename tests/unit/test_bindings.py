import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "replication"))
from function_app import app, ensure_enabled, selection, boolean_setting


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

    def test_empty_runtime_selection_fails_closed(self):
        with patch.dict(os.environ, {"SECRET_NAMES": "[]", "SECRET_PREFIX": "", "REPLICATE_ALL_SECRETS": "false"}):
            with self.assertRaises(ValueError):
                selection()

    def test_runtime_selection_configuration(self):
        with patch.dict(os.environ, {"SECRET_NAMES": '["selected"]', "SECRET_PREFIX": "app-", "REPLICATE_ALL_SECRETS": "false"}):
            chosen = selection()
            self.assertTrue(chosen.includes("SELECTED"))
            self.assertTrue(chosen.includes("app-password"))
            self.assertFalse(chosen.includes("other"))

    def test_invalid_boolean_not_silently_defaulted(self):
        with patch.dict(os.environ, {"ALLOW_UNMANAGED_OVERWRITE": "yes"}):
            with self.assertRaises(ValueError):
                boolean_setting("ALLOW_UNMANAGED_OVERWRITE")
