"""Evaluate explicit bootstrap observations without treating partial installation as ready."""

from scripts.customer_migration import require

INSTALLATION_CHECKS = (
    "private_repository", "protected_default_branch", "protected_environments",
    "workflow_federations", "scoped_deployment_permissions", "graph_permissions",
    "database_admin_identity", "kubernetes_roles", "private_runner_toolchain",
    "runner_registration_authority", "private_dns_routes", "dns_certificate_permissions",
)


def installation_readiness(observations):
    require(isinstance(observations, dict) and not set(observations) - set(INSTALLATION_CHECKS), "Unknown installation observation")
    checks = {}
    for name in INSTALLATION_CHECKS:
        evidence = observations.get(name)
        if evidence is None:
            checks[name] = {"status": "not-verified"}
        else:
            require(isinstance(evidence, dict) and set(evidence) == {"passed", "source", "scope"}, "Installation observations need a result, actual source and scope")
            require(type(evidence["passed"]) is bool and isinstance(evidence["source"], str) and evidence["source"] and isinstance(evidence["scope"], str) and evidence["scope"], "Invalid installation evidence")
            checks[name] = {"status": "passed" if evidence["passed"] else "failed", "source": evidence["source"], "scope": evidence["scope"]}
    return {"checks": checks, "readyForDeployment": all(check["status"] == "passed" for check in checks.values()), "stageAccepted": False}