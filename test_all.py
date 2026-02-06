"""
Tests. Uses Temporal's built-in test server so you don't
need to have anything else running.

    python test_all.py
"""

import asyncio
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from infra_workflow import (
    InfraProvisioningWorkflow, InfraInput, InfraStatus,
    terraform_init, terraform_plan, terraform_apply,
    validate_infra, terraform_destroy,
)
from cicd_workflow import (
    CICDPipelineWorkflow, DeployInput, DeployStatus,
    checkout_and_build, run_tests, deploy_artifact,
    rollback, notify,
)

INFRA_ACTIVITIES = [terraform_init, terraform_plan, terraform_apply,
                    validate_infra, terraform_destroy]
CICD_ACTIVITIES = [checkout_and_build, run_tests, deploy_artifact,
                   rollback, notify]


async def test_infra():
    """Does the whole provisioning flow work end to end?"""
    print("testing infra provisioning...")
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue="t", workflows=[InfraProvisioningWorkflow],
                          activities=INFRA_ACTIVITIES):
            h = await env.client.start_workflow(
                InfraProvisioningWorkflow.run,
                InfraInput(project_name="test", environment="test"),
                id="t-infra", task_queue="t",
            )
            result = await h.result()

            assert result.vm_name == "vm-test-test"
            assert result.vm_public_ip == "20.185.72.14"
            assert result.admin_username == "azureadmin"

            status = await h.query(InfraProvisioningWorkflow.get_status)
            assert status == "READY"

            output = await h.query(InfraProvisioningWorkflow.get_infra_output)
            assert output["ready"] is True
    print("  passed")


async def test_cicd():
    """Does the build/test/deploy pipeline work?"""
    print("testing cicd pipeline...")
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue="t", workflows=[CICDPipelineWorkflow],
                          activities=CICD_ACTIVITIES):
            h = await env.client.start_workflow(
                CICDPipelineWorkflow.run,
                DeployInput(repo_url="https://github.com/x/y.git", target_host="1.2.3.4"),
                id="t-cicd", task_queue="t",
            )

            # workflow stays alive for signals so we have to poll
            for _ in range(60):
                await asyncio.sleep(0.5)
                d = await h.query(CICDPipelineWorkflow.get_deploy_details)
                if d.get("application_url"):
                    break

            assert d["application_url"] == "http://1.2.3.4:8080"
            assert d["healthy"] is True
            assert d["status"] == "COMPLETED"
    print("  passed")


async def test_infra_query():
    """Can we query the infra workflow for VM details?"""
    print("testing infra query...")
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue="t", workflows=[InfraProvisioningWorkflow],
                          activities=INFRA_ACTIVITIES):
            h = await env.client.start_workflow(
                InfraProvisioningWorkflow.run,
                InfraInput(project_name="q"),
                id="t-query", task_queue="t",
            )
            result = await h.result()
            assert result.vm_public_ip == "20.185.72.14"
    print("  passed")


async def main():
    print("\n" + "=" * 40)
    print("running tests")
    print("=" * 40 + "\n")

    await test_infra()
    await test_cicd()
    await test_infra_query()

    print("\n" + "=" * 40)
    print("all good!")
    print("=" * 40)


if __name__ == "__main__":
    asyncio.run(main())
