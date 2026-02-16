
# Spark deploy

This directory contains a Terraform project for deploying Spark clusters.

## Setup

1. Install [Terraform](https://www.terraform.io/) and [Poetry](https://python-poetry.org/).
1. Run `./setup.sh` to initialize the Terraform and Poetry projects.

Create a filed in this directory called `secrets.auto.tfvars` with the three required credentials:

```hlcl
aws_access_key_id     = "XXX"
aws_secret_access_key = "XXX"
wandb_api_key         = "XXX"
```

In the parent directory, create a directory called `cluster/` and add a file called `default.tfvars`. This file contains default variables that will be applied to all clusters. Generally it makes sense to define at least these four variables, which are required:

```hcl
ecr_server    = "XXX.dkr.ecr.us-east-1.amazonaws.com"
ecr_repo      = "XXX:latest"
aws_vpc_id    = "vpc-XXX"
aws_subnet_id = "subnet-XXX"
```

Then, custom cluster "profiles" can be defined as `.tfvars` files under `../cluster/profiles/`. For example, if a project needs a CPU cluster for ETL jobs and a GPU cluster for model inference, these could be defined with two profiles like -

`../cluster/profiles/cpu.tfvars`

```hcl
spot_worker_count    = 30
worker_instance_type = "m5d.metal"
executor_memory      = "360g"
```

`../cluster/profiles/gpu.tfvars`

```hcl
on_demand_worker_count = 10
gpu_workers            = true
worker_instance_type   = "g4dn.2xlarge"
executor_memory        = "28g"
```

## Usage

Then, use `./cluster.sh <command>` to control the cluster.

### `./cluster.sh create <profile>`
 
Create a cluster. If a profile name is passed, the variables in the corresponding `.tfvars` file under `../cluster/profiles/` will be added as overrides to the Terraform configuration.

The `create` command will apply the Terraform configuration and then tail the outputs of the `/var/log/cloud-init-output.log` file on the master node, to give some visibility onto the state of the startup process. When cloud-init finishes, the command will exit.

### `./cluster.sh destroy`
 
Destroy the cluster

### `./cluster.sh login`
 
SSH into the master node.

### `./cluster.sh admin`
 
Open the Spark web UI in a browser.

### `./cluster.sh cat-cloudinit-log`
 
Download the `cloud-init-output.log` file from the master node. Useful for debugging cluster startup problems.
