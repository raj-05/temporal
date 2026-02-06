"""
App team's script. Only deploy stuff here - no infra operations.

Lives in the app team's repo, runs on their machines. They can
deploy and redeploy but can't touch any infrastructure.

    python deploy.py worker                   start the worker
    python deploy.py run                      deploy (auto-finds the VM)
    python deploy.py run --host 20.185.72.14  deploy to a specific host
    python deploy.py redeploy                 push new code to same VM
    python deploy.py status                   check deployment status
"""

import argparse
import asyncio
from temporalio.client import Client
from temporalio.worker import Worker

from infra_workflow import InfraProvisioningWorkflow
from cicd_workflow import (
    CICDPipelineWorkflow, DeployInput,
    checkout_and_build, run_tests, deploy_artifact,
    rollback, notify,
)

TASK_QUEUE = "app-deployments"


async def worker(client):
    w = Worker(
        client, task_queue=TASK_QUEUE,
        workflows=[CICDPipelineWorkflow],
        activities=[checkout_and_build, run_tests, deploy_artifact,
                    rollback, notify],
    )
    print(f"app worker up, listening on '{TASK_QUEUE}'")
    await w.run()


async def run_deploy(client, host=None):
    # if no host given, ask the infra workflow for it
    if not host:
        try:
            infra_handle = client.get_workflow_handle("infra-myapp-dev")
            infra = await infra_handle.query(InfraProvisioningWorkflow.get_infra_output)
        except Exception:
            print("can't find infra workflow - has the infra team run 'python infra.py provision' yet?")
            return

        if not infra.get("ready"):
            print(f"infra's not ready yet (status: {infra['status']})")
            return
        host = infra["vm_public_ip"]
        print(f"found VM at {host}")

    input = DeployInput(
        repo_url="https://github.com/example/myapp.git",
        branch="main",
        target_host=host,
    )
    wf_id = "cicd-myapp-dev"

    print(f"starting pipeline: {wf_id}")
    handle = await client.start_workflow(
        CICDPipelineWorkflow.run, input,
        id=wf_id, task_queue=TASK_QUEUE,
    )

    # wait for it to finish
    for _ in range(60):
        await asyncio.sleep(1)
        details = await handle.query(CICDPipelineWorkflow.get_deploy_details)
        if details.get("application_url"):
            print(f"\ndeployed!")
            print(f"  url: {details['application_url']}")
            print(f"  artifact: {details['artifact']}")
            return
    print("timed out waiting for deploy to finish")


async def redeploy(client):
    input = DeployInput(
        repo_url="https://github.com/example/myapp.git",
        branch="main",
    )
    handle = client.get_workflow_handle("cicd-myapp-dev")
    await handle.signal(CICDPipelineWorkflow.trigger_redeploy, input)
    print("redeploy signal sent - new build+test+deploy cycle starting")


async def status(client):
    try:
        handle = client.get_workflow_handle("cicd-myapp-dev")
        d = await handle.query(CICDPipelineWorkflow.get_deploy_details)
        print(f"status: {d['status']}")
        if d.get("application_url"):
            print(f"  url: {d['application_url']}")
            print(f"  artifact: {d['artifact']}")
    except Exception as e:
        print(f"not running ({e})")


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["worker", "run", "redeploy", "status"])
    p.add_argument("--host", help="VM IP (skip auto-discovery)")
    args = p.parse_args()

    client = await Client.connect("localhost:7233")

    match args.command:
        case "worker":   await worker(client)
        case "run":      await run_deploy(client, args.host)
        case "redeploy": await redeploy(client)
        case "status":   await status(client)


if __name__ == "__main__":
    asyncio.run(main())
