import typer
import subprocess
import json
import requests
import paramiko
import re
import shlex
import time
import webbrowser

from datetime import datetime
from typing import Optional, Iterator, TextIO
from pathlib import Path
from dataclasses import dataclass, field
from loguru import logger


ROOT_DIR = Path(__file__).parent
CONFIG_DIR = ROOT_DIR.parent / 'cluster'
LOG_DIR = ROOT_DIR / 'logs'
STATE_PATH = ROOT_DIR / 'terraform.tfstate'
DEFAULT_REGION = 'us-east-1'
CLOUDINIT_LOG_PATH = '/var/log/cloud-init-output.log'


def emit(line: str, log: Optional[TextIO] = None, end: str = '\n'):
    """Print a line, and also write it to `log` if given. Flushes the log so
    that it is complete even if the process dies mid-job.
    """
    print(line, end=end)

    if log is not None:
        log.write(line + end)
        log.flush()


def read_terraform_output(src: str, key: str):
    """Read a given output key from the Terraform state.
    """
    with open(src) as fh:
        return json.load(fh)['outputs'][key]['value']


def tail_log_file(
    hostname: str,
    username: str,
    log_file_path: str,
) -> Iterator[str]:

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname, username=username)

    command = f'tail -f {log_file_path}'
    stdin, stdout, stderr = client.exec_command(command)

    for line in stdout:
        yield line.strip()

    client.close()


def read_worker_dns(src: str) -> list[str]:
    """Read the public DNS of the worker nodes from the Terraform state.
    Workers that are stopped (eg, interrupted spot instances) have none and
    are skipped.
    """
    with open(src) as fh:
        resources = json.load(fh).get('resources', [])

    return [
        instance['attributes']['public_dns']
        for r in resources
        if r['type'] == 'aws_instance'
        and r['name'] in ('workers', 'spot_workers')
        for instance in r['instances']
        if instance['attributes'].get('public_dns')
    ]


@dataclass
class Cluster:

    master_dns: str
    worker_dns: list[str] = field(default_factory=list)

    @classmethod
    def from_tfstate(
        cls,
        *,
        src: str = ROOT_DIR / 'terraform.tfstate',
        master_dns_key: str = 'master_dns',
    ):
        """Create an instance from the master DNS output in a tfstate file.
        """
        try:
            master_dns = read_terraform_output(src, master_dns_key)
        except Exception:
            raise RuntimeError(
                'Unable to read master node DNS. Is the cluster up?'
            )

        return cls(master_dns, read_worker_dns(src))

    @property
    def web_ui_url(self) -> str:
        """Build the URL for the Spark web UI.
        """
        return f'http://{self.master_dns}:8080'

    def ssh_client(
        self,
        retry_timeout: float = 60*10,
        retry_interval: float = 5,
        host: Optional[str] = None,
    ) -> paramiko.SSHClient:
        """Create an SSH client to the master node (or `host`, if given),
        retrying until `sshd` is up. Right after `terraform apply`, the
        instance is running but port 22 often refuses connections for a while.
        """
        deadline = time.monotonic() + retry_timeout

        while True:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                client.connect(
                    host or self.master_dns, username='ubuntu', timeout=60,
                )
                return client
            except (
                paramiko.AuthenticationException,
                paramiko.BadHostKeyException,
            ):
                # Permanent failures, not "sshd is still starting".
                client.close()
                raise
            except (OSError, paramiko.SSHException) as e:
                client.close()
                if time.monotonic() >= deadline:
                    raise
                logger.info(f'SSH not ready ({e}), retrying...')
                time.sleep(retry_interval)

    def update_image(self, host: str) -> int:
        """Re-pull the Docker image on a node and restart the Spark container.
        The cached image is removed so that `docker run` in the per-boot script
        pulls the current one. Returns the exit status.
        """
        command = (
            "img=$(sudo docker inspect --format '{{.Config.Image}}' spark)"
            ' && sudo docker stop spark'
            ' && sudo docker rmi "$img"'
            ' && sudo sh /var/lib/cloud/scripts/per-boot/start-spark.sh'
        )

        with self.ssh_client(host=host) as client:
            _, stdout, _ = client.exec_command(command, get_pty=True)

            for line in stdout:
                emit(f'[{host}] {line}', end='')

            return stdout.channel.recv_exit_status()

    def cat_cloudinit_log(self) -> str:
        """Pull `cloud-init-output.log` from the master node using Fabric.
        """
        with self.ssh_client() as client:
            _, stdout, _ = client.exec_command(f'cat {CLOUDINIT_LOG_PATH}')
            return stdout.read().decode()

    def tail_cloudinit_log(self) -> Iterator[str]:
        """Tail `cloud-init-output.log` in real-time.
        """
        with self.ssh_client() as client:

            cmd = f'tail -f {CLOUDINIT_LOG_PATH}'
            stdin, stdout, stderr = client.exec_command(cmd)

            for line in stdout:
                yield line.strip()

    def tail_cloutinit_log_until_finished(self) -> Iterator[str]:
        """Tail `cloud-init-output.log` until the cluster is up.
        """
        for line in self.tail_cloudinit_log():
            yield line
            if re.search(r'Cloud-init .* finished', line):
                break

    def exec_in_container(
        self,
        command: str,
        log: Optional[TextIO] = None,
    ) -> int:
        """Run a command in the Spark container on the master node, streaming
        output (and writing it to `log`, if given) until it exits. Returns the
        exit status.
        """
        with self.ssh_client() as client:

            # Keep the connection alive through long-running jobs.
            client.get_transport().set_keepalive(30)

            # Use a pty so stderr is merged into the streamed output.
            _, stdout, _ = client.exec_command(
                f'sudo docker exec spark bash -lc {shlex.quote(command)}',
                get_pty=True,
            )

            for line in stdout:
                emit(line, log, end='')

            return stdout.channel.recv_exit_status()


def read_master_dns():
    """Read the master IP out of the TF state.
    """
    with open(ROOT_DIR / 'terraform.tfstate') as fh:
        return json.load(fh)['outputs']['master_dns']['value']


app = typer.Typer()


def terraform(
    action: str,
    profile: Optional[str] = None,
    auto_approve: bool = False,
    log: Optional[TextIO] = None,
) -> int:
    """Run `terraform apply` or `terraform destroy` with the default and
    (optionally) profile variable files. If `log` is given, the output is also
    written to it. Returns the exit code.
    """
    cmd = [
        'terraform', action,
        '-var-file', CONFIG_DIR / 'default.tfvars',
    ]

    if profile is not None:
        cmd += [
            '-var-file', CONFIG_DIR / 'profiles' / f'{profile}.tfvars'
        ]

    if auto_approve:
        cmd.append('-auto-approve')

    if log is None:
        return subprocess.run(cmd).returncode

    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    ) as proc:
        for line in proc.stdout:
            emit(line, log, end='')

    return proc.returncode


def aws(region: str, *args: str):
    """Run an `aws ec2` command and return its parsed JSON output, if any.
    """
    out = subprocess.run(
        ['aws', 'ec2', *args, '--region', region, '--output', 'json'],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    return json.loads(out) if out.strip() else None


def aws_idempotent(region: str, *args: str):
    """Like `aws`, but treat "already gone" errors as success so that a retry
    after a partial failure doesn't trip over completed operations.
    """
    try:
        return aws(region, *args)
    except subprocess.CalledProcessError as e:
        if 'NotFound' in (e.stderr or ''):
            logger.info(f'Already gone, skipping: {args[0]}')
            return None
        raise


def fast_destroy(log: Optional[TextIO] = None) -> bool:
    """Tear down the cluster without waiting for instances to terminate.

    Terraform's destroy blocks until every instance is fully `terminated`,
    which is slow. Instead, cancel the spot requests (so that AWS doesn't
    relaunch the workers), ask AWS to terminate the instances, delete the key
    pair, and forget the state. The security group and master network
    interface can't be deleted until the instances are gone, so they are left
    for `./cluster.sh clean`. Returns True on success.
    """
    if not STATE_PATH.exists():
        emit('No Terraform state, nothing to destroy.', log)
        return True

    with open(STATE_PATH) as fh:
        resources = json.load(fh)['resources']

    attrs = [
        (r['type'], i['attributes'])
        for r in resources if r['mode'] == 'managed'
        for i in r['instances']
    ]

    instances = [a['id'] for t, a in attrs if t == 'aws_instance']
    spot_requests = [
        a['spot_instance_request_id'] for t, a in attrs
        if t == 'aws_instance' and a.get('spot_instance_request_id')
    ]
    keys = [a['key_name'] for t, a in attrs if t == 'aws_key_pair']
    arns = [a['arn'] for t, a in attrs if a.get('arn')]
    region = arns[0].split(':')[3] if arns else DEFAULT_REGION

    try:
        # One call per ID, so that a single already-gone resource (from a
        # previous partial attempt) doesn't fail the whole batch.
        for request in spot_requests:
            emit(f'Cancelling spot request: {request}', log)
            aws_idempotent(region, 'cancel-spot-instance-requests',
                           '--spot-instance-request-ids', request)

        for instance in instances:
            emit(f'Terminating instance: {instance}', log)
            aws_idempotent(region, 'terminate-instances',
                           '--instance-ids', instance)

        for key in keys:
            emit(f'Deleting key pair: {key}', log)
            aws_idempotent(region, 'delete-key-pair', '--key-name', key)

    except subprocess.CalledProcessError as e:
        emit(f'AWS call failed: {e.stderr}', log)
        return False

    # Keep the old state around for debugging, but out of Terraform's way.
    STATE_PATH.replace(STATE_PATH.with_name('terraform.tfstate.destroyed'))
    emit('Cluster is shutting down. Run `./cluster.sh clean` later to remove '
         'the leftover security group and network interface.', log)

    return True


def wait_for_cluster(log: Optional[TextIO] = None) -> Cluster:
    """Tail the cloud-init log on the master node until the cluster is up.
    """
    cluster = Cluster.from_tfstate()

    logger.info('Tailing `cloud-init-output.log` on the master node...')

    for line in cluster.tail_cloutinit_log_until_finished():
        emit(line, log)

    logger.info('Cluster up 🚀🚀')

    return cluster


@app.command()
def create(profile: Optional[str] = typer.Argument(None)):
    """Create a cluster using default variables defined in:

    `../cluster/default.tfvars`

    If a "profile" is passed, also inject variables defined at:

    `../cluster/profiles/<profile>.tfvars`
    """
    terraform('apply', profile)
    wait_for_cluster()


@app.command()
def destroy():
    """Destroy the cluster. Doesn't wait for the instances to finish
    terminating; run `clean` later to remove the leftovers.
    """
    if not fast_destroy():
        raise typer.Exit(1)


@app.command()
def clean(
    region: str = typer.Option(DEFAULT_REGION, help='AWS region.'),
):
    """Delete security groups and network interfaces left behind by `destroy`
    once their instances have terminated. Anything still in use is skipped.
    """
    tag = 'Name=tag:Project,Values=pyspark-deploy'

    enis = aws(region, 'describe-network-interfaces',
               '--filters', tag, 'Name=status,Values=available')
    for eni in enis['NetworkInterfaces']:
        eni_id = eni['NetworkInterfaceId']
        try:
            aws(region, 'delete-network-interface',
                '--network-interface-id', eni_id)
            logger.info(f'Deleted network interface {eni_id}')
        except subprocess.CalledProcessError:
            logger.info(f'Network interface {eni_id} still in use, skipping.')

    sgs = aws(region, 'describe-security-groups', '--filters', tag)
    for sg in sgs['SecurityGroups']:
        sg_id = sg['GroupId']
        try:
            aws(region, 'delete-security-group', '--group-id', sg_id)
            logger.info(f'Deleted security group {sg_id}')
        except subprocess.CalledProcessError:
            logger.info(f'Security group {sg_id} still in use, skipping.')


@app.command()
def run(
    command: str = typer.Argument(
        ...,
        help='Shell command to run in the Spark container on the master, '
             'eg: "spark-submit s3a://bucket/job.py".',
    ),
    profile: Optional[str] = typer.Option(
        None,
        '--profile', '-p',
        help='Cluster profile, as in `create`.',
    ),
    keep: bool = typer.Option(
        False,
        '--keep',
        help='Leave the cluster up after the job finishes.',
    ),
):
    """Create a cluster, run a command on it, and destroy the cluster when the
    command finishes, whether it succeeds, fails, or is interrupted. Exits
    with the command's exit code.
    """
    exit_code = 1

    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    log_path = LOG_DIR / f'{stamp}-{profile or "default"}.log'

    logger.info(f'Saving logs to {log_path}')

    with open(log_path, 'w') as log:
        try:
            emit(f'$ {command}', log)

            if terraform('apply', profile, auto_approve=True, log=log) != 0:
                raise RuntimeError('`terraform apply` failed.')

            cluster = wait_for_cluster(log)

            logger.info(f'Running: {command}')
            exit_code = cluster.exec_in_container(command, log)
            logger.info(f'Command exited with code {exit_code}.')

        finally:
            if keep:
                logger.warning('Leaving the cluster up (--keep).')

            # Destroy even after a failed or partial apply, which can leave
            # resources behind.
            elif not fast_destroy(log):
                logger.error(
                    'Destroy failed, the cluster may still be running! '
                    'Run `./cluster.sh destroy` to retry.'
                )
                exit_code = exit_code or 1

            logger.info(f'Logs saved to {log_path}')

    raise typer.Exit(exit_code)


@app.command()
def update():
    """Pull the latest Docker image on every node of a running cluster and
    restart the Spark containers, without recreating the cluster. Restarts the
    master first, then the workers. Running jobs are killed.
    """
    cluster = Cluster.from_tfstate()
    failed = []

    for host in [cluster.master_dns, *cluster.worker_dns]:
        logger.info(f'Updating {host}...')
        if cluster.update_image(host):
            logger.error(f'Update failed on {host}.')
            failed.append(host)

    if failed:
        raise typer.Exit(1)

    logger.info('Cluster updated 🚀')


@app.command()
def login():
    """SSH into the master node.
    """
    subprocess.run(['ssh', f'ubuntu@{read_master_dns()}'])


@app.command()
def admin():
    """Open the Spark web UI.
    """
    webbrowser.open(f'http://{read_master_dns()}:8080')


@app.command()
def cat_cloudinit_log():
    """Print the cloud-init log from the master node.
    """
    cluster = Cluster.from_tfstate()
    print(cluster.cat_cloudinit_log())


if __name__ == '__main__':
    app()
