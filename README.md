
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

### `./cluster.sh run [--profile <profile>] [--drain] <command>`

Create a cluster and run a command on it. The cluster is left up by default; pass `--drain` to terminate the workers when the command finishes (the master stays up). The command runs in the Spark container on the master node (with the cluster's Spark config), and its output is streamed to the terminal. For example -

```sh
./cluster.sh run --profile cpu "spark-submit s3a://bucket/job.py"
```

`run` exits with the command's exit code. The job runs detached on the master, so a dropped connection doesn't affect it; reconnect with `./cluster.sh attach [job]`. With `--drain`, the master terminates the workers when the job finishes, whether it succeeds or fails. Change your mind while it runs with `./cluster.sh policy keep|drain`. The master stays up until you run `./cluster.sh destroy` (see below) or it has been idle for `idle_shutdown_minutes` (default 120), after which the whole cluster is terminated. If the job never starts (eg, `terraform apply` fails), `--drain` destroys everything.

All output (Terraform, the cloud-init log, and the command) is also saved to `logs/<timestamp>-<profile>.log` in this directory, so it's available for debugging after the cluster is gone. Executor logs, which live on the workers, are not captured.

This runs from your machine, so it needs to stay connected until the job finishes.

### `./cluster.sh update`

Pull the latest Docker image on every node of a running cluster and restart the Spark containers, without recreating the cluster. The master is restarted first, then the workers. Running jobs are killed.

On each node, the cached image is removed and the Spark container is restarted via the per-boot script, so `docker run` pulls the current image. The exit code is non-zero if the update fails on any node. Nodes that are no longer running (eg, workers terminated by `run --drain`) are skipped with a warning.

### `./cluster.sh attach [job]`

Reattach to a job started by `run`, streaming its output from the beginning until it finishes. Defaults to the latest job. Exits with the job's exit code. Ctrl-C detaches without affecting the job.

### `./cluster.sh policy [keep|drain]`

Show or change what happens to the workers when the running job finishes: `keep` leaves them up, `drain` terminates them (the master stays up). It can be changed any number of times while the job runs, but has no effect once the job is done.

### `./cluster.sh destroy`
 
Destroy the cluster. This cancels the spot requests and tells AWS to terminate the instances, but doesn't wait for them to finish terminating (which `terraform destroy` would), so a new cluster can be created right away. The security group and master network interface can't be deleted until the instances are gone, so they're left behind (they're free, and tagged `Project=pyspark-deploy`) and the Terraform state is moved to `terraform.tfstate.destroyed`.

### `./cluster.sh clean [--region <region>]`

Delete the leftover security groups and network interfaces from destroyed clusters. Anything still in use is skipped, so it's safe to run any time.

### `./cluster.sh login`
 
SSH into the master node.

### `./cluster.sh admin`
 
Open the Spark web UI in a browser.

### `./cluster.sh cat-cloudinit-log`
 
Download the `cloud-init-output.log` file from the master node. Useful for debugging cluster startup problems.

## Idle shutdown

The master terminates the whole cluster, itself included, once it has been continuously idle for `idle_shutdown_minutes` (default 120; 0 disables it). The cluster counts as idle only if no job is running, no Spark application is active, and nobody is logged in to the master. Anything unexpected, such as the Spark master not answering, counts as active, and any activity restarts the clock. Set it in a profile or in `default.tfvars`:

```hcl
idle_shutdown_minutes = 240
```

This is set up when the master is created, so it only applies to new clusters.
