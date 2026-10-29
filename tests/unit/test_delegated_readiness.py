"""Pure helpers behind the delegated readiness report."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from delegated_readiness import ASSIGN_ACTION, BASE_NAMESPACES, permits, provider_namespaces


class ReadinessHelperTests(unittest.TestCase):
    def test_permissions_honor_wildcards_and_not_actions(self):
        self.assertTrue(permits([{"actions": ["*"]}], ASSIGN_ACTION))
        self.assertTrue(permits([{"actions": ["Microsoft.ManagedIdentity/*"]}], ASSIGN_ACTION))
        self.assertTrue(permits([{"actions": [ASSIGN_ACTION.upper()]}], ASSIGN_ACTION))
        self.assertFalse(permits([{"actions": ["*"], "notActions": ["Microsoft.ManagedIdentity/*"]}], ASSIGN_ACTION))
        self.assertFalse(permits([{"actions": ["Microsoft.ManagedIdentity/userAssignedIdentities/read"]}], ASSIGN_ACTION))
        self.assertFalse(permits([], ASSIGN_ACTION))

    def test_namespaces_come_from_resource_types_including_nested_templates(self):
        template = {
            "parameters": {"name": {"type": "string"}},
            "resources": [{
                "type": "Microsoft.KeyVault/vaults",
                "properties": {"template": {"resources": [{"type": "Microsoft.EventGrid/systemTopics"}]}},
            }, {"properties": {"delegations": [{"properties": {"serviceName": "Microsoft.App/environments"}}]}}],
        }
        found = provider_namespaces(template)
        self.assertEqual(set(found), {"Microsoft.KeyVault", "Microsoft.EventGrid", "Microsoft.App", *BASE_NAMESPACES})


if __name__ == "__main__":
    unittest.main()
