"""
App team's CI/CD stuff. Temporal handles the whole pipeline directly -
no need for Azure DevOps or Jenkins or whatever. Each step (clone,
build, test, deploy) is just a Temporal activity.

After the first deploy, the workflow hangs around waiting for a
redeploy signal. So when someone pushes to main, we just poke
the workflow and it does another build+test+deploy cycle on the
same VM. No need to spin up a whole new pipeline run.
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

class DeployStatus(str, Enum):
    PENDING = "PENDING"
    CHECKING_OUT = "CHECKING_OUT"
    BUILDING = "BUILDING"
    TESTING = "TESTING"
    DEPLOYING = "DEPLOYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


@dataclass
class DeployInput:
    repo_url: str
    branch: str = "main"
    commit_sha: Optional[str] = None
    build_command: str = "make build"
    test_command: str = "make test"
    # these come from the infra team's workflow output
    target_host: str = ""
    admin_username: str = "azureadmin"


@dataclass
class BuildResult:
    artifact_name: str
    commit_sha: str
    tests_passed: int
    tests_total: int


@dataclass
class DeployResult:
    target_host: str
    artifact: str
    application_url: str
    healthy: bool


# --- activities ---

@activity.defn
async def checkout_and_build(input: DeployInput) -> BuildResult:
    # real version:
    #   subprocess.run(["git", "clone", input.repo_url, "/tmp/build"])
    #   subprocess.run(["git", "checkout", input.branch], cwd="/tmp/build")
    #   subprocess.run(input.build_command.split(), cwd="/tmp/build")
    sha = input.commit_sha or uuid.uuid4().hex[:8]
    activity.logger.info(f"cloning {input.repo_url} @ {input.branch}")
    await asyncio.sleep(1)
    activity.logger.info(f"running {input.build_command}...")
    await asyncio.sleep(2)
    return BuildResult(
        artifact_name=f"app-{sha}.tar.gz",
        commit_sha=sha,
        tests_passed=0, tests_total=0,
    )


@activity.defn
async def run_tests(input: DeployInput) -> bool:
    # real version: subprocess.run(input.test_command.split(), cwd="/tmp/build")
    activity.logger.info(f"running {input.test_command}...")
    await asyncio.sleep(2)
    activity.logger.info("154/156 passed, 2 skipped, 0 failed")
    return True


@activity.defn
async def deploy_artifact(
    build: BuildResult, target_host: str, admin_username: str
) -> DeployResult:
    # real version:
    #   subprocess.run(["scp", f"dist/{build.artifact_name}",
    #                   f"{admin_username}@{target_host}:/opt/app/"])
    #   subprocess.run(["ssh", f"{admin_username}@{target_host}",
    #                   "cd /opt/app && tar xzf *.tar.gz && systemctl restart myapp"])
    activity.logger.info(f"scp {build.artifact_name} -> {admin_username}@{target_host}")
    await asyncio.sleep(1)
    activity.logger.info("restarting service...")
    await asyncio.sleep(0.5)

    url = f"http://{target_host}:8080"
    activity.logger.info(f"health check ok, app is at {url}")
    return DeployResult(
        target_host=target_host,
        artifact=build.artifact_name,
        application_url=url,
        healthy=True,
    )


@activity.defn
async def rollback(target_host: str, admin_username: str) -> None:
    """Puts the old version back if the new one broke something."""
    activity.logger.info(f"rolling back on {target_host}...")
    await asyncio.sleep(1)


@activity.defn
async def notify(message: str) -> None:
    """Could be Teams, Slack, email - whatever your org uses."""
    activity.logger.info(f">> {message}")
    await asyncio.sleep(0.2)


# --- workflow ---

RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    maximum_attempts=3,
)


@workflow.defn
class CICDPipelineWorkflow:
    """The whole CI/CD pipeline. Temporal replaces ADO/Jenkins here.

    After first deploy it sits there waiting for redeploy signals.
    Hook this up to a git webhook and you've got continuous deployment."""

    def __init__(self):
        self._status = DeployStatus.PENDING
        self._result: Optional[DeployResult] = None
        self._redeploy_requested = False
        self._next_input: Optional[DeployInput] = None

    @workflow.query
    def get_status(self) -> str:
        return self._status.value

    @workflow.query
    def get_deploy_details(self) -> dict:
        d = {"status": self._status.value}
        if self._result:
            d["application_url"] = self._result.application_url
            d["artifact"] = self._result.artifact
            d["healthy"] = self._result.healthy
        return d

    @workflow.signal
    async def trigger_redeploy(self, input: DeployInput) -> None:
        """Poke this when someone pushes new code."""
        workflow.logger.info(f"got redeploy signal for {input.branch}")
        self._next_input = input
        self._redeploy_requested = True

    @workflow.run
    async def run(self, input: DeployInput) -> DeployResult:
        if not input.target_host:
            raise ValueError(
                "no target_host - has the infra team provisioned the VM yet?"
            )

        # do the first deploy
        self._result = await self._pipeline(input)

        # then just sit here waiting for redeploy signals
        while True:
            await workflow.wait_condition(lambda: self._redeploy_requested)
            self._redeploy_requested = False
            redeploy = self._next_input
            if not redeploy.target_host:
                redeploy.target_host = input.target_host
                redeploy.admin_username = input.admin_username
            self._result = await self._pipeline(redeploy)

    async def _pipeline(self, input: DeployInput) -> DeployResult:
        """The actual build/test/deploy steps. Used for both
        first deploy and redeploys."""
        try:
            self._status = DeployStatus.BUILDING
            build = await workflow.execute_activity(
                checkout_and_build, input,
                start_to_close_timeout=timedelta(seconds=300),
                retry_policy=RETRY,
            )

            self._status = DeployStatus.TESTING
            passed = await workflow.execute_activity(
                run_tests, input,
                start_to_close_timeout=timedelta(seconds=600),
                retry_policy=RETRY,
            )
            if not passed:
                raise RuntimeError("tests failed, not deploying broken code")

            self._status = DeployStatus.DEPLOYING
            result = await workflow.execute_activity(
                deploy_artifact,
                args=[build, input.target_host, input.admin_username],
                start_to_close_timeout=timedelta(seconds=300),
                retry_policy=RETRY,
            )

            self._result = result
            self._status = DeployStatus.COMPLETED
            await workflow.execute_activity(
                notify,
                f"deployed {build.artifact_name} to {input.target_host}",
                start_to_close_timeout=timedelta(seconds=30),
            )
            return result

        except Exception as e:
            self._status = DeployStatus.FAILED
            workflow.logger.error(f"pipeline blew up: {e}")

            try:
                await workflow.execute_activity(
                    rollback, args=[input.target_host, input.admin_username],
                    start_to_close_timeout=timedelta(seconds=120),
                )
                self._status = DeployStatus.ROLLED_BACK
            except Exception:
                pass  # rollback failed too, not much we can do

            await workflow.execute_activity(
                notify, f"deploy failed: {e}",
                start_to_close_timeout=timedelta(seconds=30),
            )
            raise
