"""
Infra team's script. Only infra stuff lives here.

App team doesn't have access to this - it lives in the infra
team's repo, runs on their machines, uses their Azure creds.

    python infra.py worker      start the worker
    python infra.py provision   create the azure resources
    python infra.py status      check how things are going
"""

import argparse
import asyncio
from temporalio.client import Client
from temporalio.worker import Worker

from infra_workflow import (
    InfraProvisioningWorkflow, InfraInput,
    terraform_init, terraform_plan, terraform_apply,
    validate_infra, terraform_destroy,
)

TASK_QUEUE = "infra-platform"


async def worker(client):
    w = Worker(
        client, task_queue=TASK_QUEUE,
        workflows=[InfraProvisioningWorkflow],
        activities=[terraform_init, terraform_plan, terraform_apply,
                    validate_infra, terraform_destroy],
    )
    print(f"infra worker up, listening on '{TASK_QUEUE}'")
    await w.run()


async def provision(client):
    input = InfraInput(project_name="myapp", environment="dev")
    wf_id = f"infra-{input.project_name}-{input.environment}"

    print(f"kicking off provisioning: {wf_id}")
    handle = await client.start_workflow(
        InfraProvisioningWorkflow.run, input,
        id=wf_id, task_queue=TASK_QUEUE,
    )

    result = await handle.result()
    print(f"\nall good!")
    print(f"  VM: {result.vm_name} @ {result.vm_public_ip}")
    print(f"  RG: {result.resource_group_name}")
    print(f"\napp team can deploy now: python deploy.py run --host {result.vm_public_ip}")


async def status(client):
    try:
        handle = client.get_workflow_handle("infra-myapp-dev")
        s = await handle.query(InfraProvisioningWorkflow.get_status)
        o = await handle.query(InfraProvisioningWorkflow.get_infra_output)
        print(f"status: {s}")
        if o.get("ready"):
            print(f"  VM: {o['vm_name']} @ {o['vm_public_ip']}")
    except Exception as e:
        print(f"not running or doesn't exist yet ({e})")


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["worker", "provision", "status"])
    args = p.parse_args()

    client = await Client.connect("localhost:7233")

    match args.command:
        case "worker":    await worker(client)
        case "provision": await provision(client)
        case "status":    await status(client)


if __name__ == "__main__":
    asyncio.run(main())
