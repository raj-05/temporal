# Azure Infra Provisioning + CI/CD with Temporal

Two separate teams, one Temporal cluster:

1. **Infra team** spins up Azure resources with Terraform
2. **App team** builds and deploys their code to the VM

The interesting bit: Temporal IS the CI/CD pipeline here. No Azure DevOps, no Jenkins. Each step is just a Temporal activity with retries and rollback baked in.

## How it fits together

```
Infra Team                              App Team
==========                              ========

infra.py provision                      deploy.py run
    |                                       |
    v                                       v
InfraProvisioningWorkflow               CICDPipelineWorkflow
    |                                       |
    ├─ terraform init                       ├─ git clone + build
    ├─ terraform plan (7 resources)         ├─ run tests
    ├─ terraform apply ──────────────┐      ├─ scp artifact to VM
    └─ validate VM                   │      └─ restart service
                                     │
                                     └─── app team queries
                                          get_infra_output()
                                          to find the VM IP
```

Two task queues, two workers, two CLI scripts. Only thing connecting them is a read-only query.

## Why Temporal instead of a normal CI/CD tool?

Honestly it comes down to: you get a bunch of stuff for free that you'd otherwise have to build yourself.

- Terraform apply fails halfway through → saga compensation runs `terraform destroy` automatically
- Worker crashes mid-deploy → Temporal replays from the last completed step, nothing gets re-created
- App team needs the VM IP → they query the infra workflow, no shared config files or wiki pages
- Someone pushes new code → signal the running workflow, it redeploys without a whole new pipeline run
- Azure returns a 503 → retry policy handles it, exponential backoff, no custom retry loops

You could do all of this with ADO pipelines + a webhook + a database + some custom retry logic. But why would you.

## What's in here

```
├── infra_workflow.py       # infra team's models + activities + workflow
├── cicd_workflow.py        # app team's models + activities + workflow
├── infra.py                # infra team's CLI (worker, provision, status)
├── deploy.py               # app team's CLI (worker, run, redeploy, status)
├── test_all.py             # tests (uses Temporal's built-in test server)
├── terraform/
│   └── main.tf             # the actual Azure resources (RG, VNet, NSG, VM etc)
└── diagrams/               # mermaid diagrams
```

Kept each team's stuff in one file. No point having a `models/` and `activities/` and `workflows/` package hierarchy for something this size.

## Running it

You need Python 3.10+ and the [Temporal CLI](https://docs.temporal.io/cli).

```bash
# install deps
pip install -r requirements.txt

# run the tests first - no server needed, uses Temporal's test env
python test_all.py
```

If that passes, fire up the full thing:

```bash
# terminal 1 - temporal server
temporal server start-dev

# terminal 2 - infra worker
python infra.py worker

# terminal 3 - app worker
python deploy.py worker

# terminal 4 - provision the infra
python infra.py provision

# terminal 4 - deploy the app (auto-discovers the VM)
python deploy.py run

# later, after a code push
python deploy.py redeploy

# check on things
python infra.py status
python deploy.py status
```

## Security

Each team gets their own script so they can only do their own stuff. But the real security comes from deeper layers:

- **Separate repos** - infra team has their repo with terraform creds, app team has theirs with SSH keys. Neither can see the other's secrets.
- **Separate machines** - infra worker runs where terraform is installed. App worker runs where SSH keys live. Different boxes, different access.
- **Task queue isolation** - even if someone starts the wrong workflow, there's no worker on the other queue to pick it up. It just sits there doing nothing.
- **Temporal RBAC** - in Temporal Cloud you lock down who can start/signal/query which workflows. Infra team gets access to `infra-*`, app team gets `cicd-*`.
- **Read-only handoff** - the app team uses a query to get the VM IP. Queries are read-only in Temporal by design. They can't modify or destroy infra through it.

## Temporal concepts used

**Separate task queues** - `infra-platform` and `app-deployments`. Different teams, different workers, different machines.

**Queries** - `get_infra_output()` is how the app team finds the VM. The workflow is the source of truth, no config files needed.

**Signals** - `trigger_redeploy()` kicks off a new build+test+deploy cycle. Wire it to a git webhook and you've got CD.

**Saga compensation** - terraform destroy is the undo for terraform apply. It's idempotent so partial failures are fine.

**Retry policies** - exponential backoff on everything. Azure is flaky sometimes, Temporal just handles it.

**Activity timeouts** - terraform apply gets 10 minutes, builds get 5 minutes, notifications get 30 seconds. Nothing hangs.

**Durable execution** - worker dies during terraform apply? Temporal replays from where it left off. No orphaned resources.

## Why Terraform + Temporal?

They do different things:

- Terraform is declarative - it says "these 7 resources should exist"
- Temporal is the orchestrator - it runs init/plan/apply in order, retries failures, tears down on errors

The activities just shell out to terraform CLI. In prod that's `subprocess.run(["terraform", "apply", ...])`. Comments in the code show exactly what those calls look like.

## Why are the activities mocked?

The exercise asked to focus on the Temporal code. The Terraform HCL in `/terraform/main.tf` is real and would work against Azure. The Python activities simulate the CLI calls but the comments show what the real implementation would be.
