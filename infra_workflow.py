"""
Infra team's stuff - terraform provisioning orchestrated by Temporal.

Kept it all in one file cos there's no point splitting models/activities/workflow
into separate packages for something this size. The gist of it:

  terraform init -> plan -> apply -> check the VM is alive

If apply blows up halfway, we run terraform destroy to clean up.
That's the saga pattern - destroy is our "undo" button.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import Optional

from temporalio import activity, workflow
from temporalio.common import RetryPolicy


# --- models ---

class InfraStatus(str, Enum):
    PENDING = "PENDING"
    INITIALIZING = "INITIALIZING"
    PLANNING = "PLANNING"
    PROVISIONING = "PROVISIONING"
    VALIDATING = "VALIDATING"
    READY = "READY"
    FAILED = "FAILED"
    DESTROYING = "DESTROYING"


@dataclass
class InfraInput:
    """These map straight to the terraform variables in main.tf."""
    project_name: str
    region: str = "uksouth"
    environment: str = "dev"
    vm_size: str = "Standard_B2s"
    vnet_address_space: str = "10.0.0.0/16"
    subnet_prefix: str = "10.0.1.0/24"
    admin_username: str = "azureadmin"


@dataclass
class TerraformPlanResult:
    has_changes: bool
    resources_to_add: int
    plan_file: str


@dataclass
class InfraOutput:
    """This is what the app team gets from us. Basically just
    'here's your VM, here's the IP, go deploy your thing'."""
    resource_group_name: str
    vnet_name: str
    nsg_name: str
    vm_name: str
    vm_public_ip: str
    vm_private_ip: str
    admin_username: str


# --- activities ---
# these are mocked but the comments show what you'd actually run

@activity.defn
async def terraform_init(input: InfraInput) -> str:
    # real version: subprocess.run(["terraform", "init"], cwd="./terraform")
    activity.logger.info(f"terraform init in ./terraform")
    await asyncio.sleep(1)
    return "./terraform"


@activity.defn
async def terraform_plan(input: InfraInput) -> TerraformPlanResult:
    # real version: subprocess.run(["terraform", "plan",
    #     "-var", f"project_name={input.project_name}",
    #     "-var", f"environment={input.environment}",
    #     "-out=tfplan"], cwd="./terraform")
    activity.logger.info(f"terraform plan for {input.project_name} ({input.environment})")
    await asyncio.sleep(1)
    return TerraformPlanResult(
        has_changes=True, resources_to_add=7, plan_file="./terraform/tfplan"
    )


@activity.defn
async def terraform_apply(input: InfraInput, plan_file: str) -> InfraOutput:
    # real version: subprocess.run(["terraform", "apply", "-auto-approve", plan_file])
    # then grab outputs with: terraform output -json
    activity.logger.info(f"terraform apply {plan_file}")
    for res in ["resource_group", "vnet", "nsg", "subnet", "public_ip", "nic", "vm"]:
        activity.logger.info(f"  creating {res}...")
        await asyncio.sleep(0.5)

    rg = f"rg-{input.project_name}-{input.environment}"
    return InfraOutput(
        resource_group_name=rg,
        vnet_name=f"vnet-{input.project_name}-{input.environment}",
        nsg_name=f"nsg-{input.project_name}-{input.environment}",
        vm_name=f"vm-{input.project_name}-{input.environment}",
        vm_public_ip="20.185.72.14",
        vm_private_ip="10.0.1.4",
        admin_username=input.admin_username,
    )


@activity.defn
async def validate_infra(output: InfraOutput) -> bool:
    """Quick check - is the VM actually responding on port 22?"""
    activity.logger.info(f"checking {output.vm_name} at {output.vm_public_ip}")
    await asyncio.sleep(1)
    activity.logger.info("looks good, VM is up")
    return True


@activity.defn
async def terraform_destroy(input: InfraInput) -> None:
    """The cleanup activity. Runs when things go wrong.
    Nice thing about terraform destroy is it's idempotent -
    if only 3 out of 7 resources got created, it just cleans up those 3."""
    # real version: subprocess.run(["terraform", "destroy", "-auto-approve"])
    activity.logger.info(f"ROLLBACK: terraform destroy for {input.project_name}")
    await asyncio.sleep(2)
    activity.logger.info("done, everything cleaned up")


# --- workflow ---

# azure can be flaky sometimes (429s, random 503s) so we retry with backoff
RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=60),
    maximum_attempts=3,
)


@workflow.defn
class InfraProvisioningWorkflow:
    """Runs terraform and hands off the VM details to whoever needs them.

    The app team calls get_infra_output() to grab the IP.
    That query is read-only so they can't break anything."""

    def __init__(self):
        self._status = InfraStatus.PENDING
        self._output: Optional[InfraOutput] = None
        self._applied = False

    @workflow.query
    def get_status(self) -> str:
        return self._status.value

    @workflow.query
    def get_infra_output(self) -> dict:
        """App team uses this to find out where to deploy."""
        if not self._output:
            return {"status": self._status.value, "ready": False}
        return {
            "status": self._status.value,
            "ready": self._status == InfraStatus.READY,
            "vm_public_ip": self._output.vm_public_ip,
            "vm_name": self._output.vm_name,
            "admin_username": self._output.admin_username,
            "resource_group": self._output.resource_group_name,
        }

    @workflow.run
    async def run(self, input: InfraInput) -> InfraOutput:
        try:
            self._status = InfraStatus.INITIALIZING
            await workflow.execute_activity(
                terraform_init, input,
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=RETRY,
            )

            self._status = InfraStatus.PLANNING
            plan = await workflow.execute_activity(
                terraform_plan, input,
                start_to_close_timeout=timedelta(seconds=300),
                retry_policy=RETRY,
            )

            # this is the big one - actually creates stuff in azure
            self._status = InfraStatus.PROVISIONING
            self._output = await workflow.execute_activity(
                terraform_apply, args=[input, plan.plan_file],
                start_to_close_timeout=timedelta(seconds=600),
                retry_policy=RETRY,
            )
            self._applied = True

            # make sure the VM is actually alive before we tell anyone it's ready
            self._status = InfraStatus.VALIDATING
            healthy = await workflow.execute_activity(
                validate_infra, self._output,
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=RETRY,
            )
            if not healthy:
                raise RuntimeError(f"{self._output.vm_name} failed health check")

            self._status = InfraStatus.READY
            workflow.logger.info(f"done - VM at {self._output.vm_public_ip}")
            return self._output

        except Exception as e:
            # if we already created stuff, tear it down
            if self._applied:
                self._status = InfraStatus.DESTROYING
                workflow.logger.info(f"something broke: {e} - running destroy")
                try:
                    await workflow.execute_activity(
                        terraform_destroy, input,
                        start_to_close_timeout=timedelta(seconds=600),
                        retry_policy=RetryPolicy(maximum_attempts=3),
                    )
                except Exception as de:
                    # this is bad - gonna need someone to clean up manually
                    workflow.logger.error(f"destroy failed too: {de}")
            self._status = InfraStatus.FAILED
            raise
