import unittest

from scripts.installation_readiness import INSTALLATION_CHECKS, installation_readiness


class InstallationReadinessTests(unittest.TestCase):
    def test_partial_or_claimed_approval_does_not_make_installation_ready(self):
        self.assertFalse(installation_readiness({})["readyForDeployment"])
        observation = {"passed": True, "source": "synthetic test fixture only", "scope": "synthetic target"}
        observations = {name: observation for name in INSTALLATION_CHECKS}
        self.assertTrue(installation_readiness(observations)["readyForDeployment"])
        for name in INSTALLATION_CHECKS:
            incomplete = dict(observations)
            incomplete.pop(name)
            self.assertFalse(installation_readiness(incomplete)["readyForDeployment"])
            incomplete[name] = {**observation, "passed": False}
            self.assertFalse(installation_readiness(incomplete)["readyForDeployment"])
        self.assertFalse(installation_readiness(observations)["stageAccepted"])