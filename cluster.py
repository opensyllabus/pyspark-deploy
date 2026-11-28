import typer
import codecs
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
JOBS_DIR = '/home/ubuntu/jobs'
JOB_ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')


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


def read_state_region(resources: list[dict]) -> str:
    """The AWS region the cluster was created in, from the ARN of any
    resource in the Terraform state (which follows `var.aws_region`).
    """
    for r in resources:
        for i in r.get('instances', []):
            arn = i['attributes'].get('arn')
            if arn:
                return arn.split(':')[3]

    return DEFAULT_REGION


def validate_job_id(job_id: str) -> str:
    """Job IDs end up in remote shell commands and paths, so only allow a
    safe basename.
    """
    if not JOB_ID_RE.fullmatch(job_id):
        raise typer.BadParameter(
            f'Invalid job ID {job_id!r}: use only letters, digits, `.`, `_` '
            'and `-`, starting with a letter or digit.'
        )

    return job_id


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

        with self.ssh_client(host=host, retry_timeout=120) as client:
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

    def run_remote(self, command: str) -> tuple[int, str]:
        """Run a short command on the master and return (exit status, stdout).
        """
        with self.ssh_client() as client:
            _, stdout, _ = client.exec_command(command)
            out = stdout.read().decode()
            return stdout.channel.recv_exit_status(), out

    def set_policy(self, policy: str):
        """Set what the master does when the current (or next) job finishes:
        `keep` the workers, or `drain` them. Read by `cluster-job` at the
        end of the job, so it can be changed at any time while it runs.
        """
        code, _ = self.run_remote(
            f'mkdir -p {JOBS_DIR} && echo {policy} > {JOBS_DIR}/policy'
        )
        if code:
            raise RuntimeError('Unable to set the policy on the master.')

    def get_policy(self) -> str:
        _, out = self.run_remote(f'cat {JOBS_DIR}/policy 2>/dev/null')
        return out.strip() or 'keep'

    def latest_job(self) -> Optional[str]:
        _, out = self.run_remote(
            f'ls -1t {JOBS_DIR} | grep -v "^policy$" | head -n 1'
        )
        return out.strip() or None

    def start_job(self, job_id: str, command: str):
        """Start `command` in the Spark container, detached from this SSH
        session (see `cluster-job`), and wait for it to be running.
        """
        job_dir = shlex.quote(f'{JOBS_DIR}/{validate_job_id(job_id)}')
        code, _ = self.run_remote(
            f'mkdir -p {job_dir} && (setsid nohup /usr/local/bin/cluster-job '
            f'{job_dir} {shlex.quote(command)} >/dev/null 2>&1 </dev/null &);'
            f' for i in $(seq 50); do [ -s {job_dir}/pid ] && exit 0;'
            ' sleep 0.2; done; exit 1'
        )
        if code:
            raise RuntimeError('The job did not start on the master.')

    def job_status(self, job_id: str) -> Optional[int]:
        """Return the job's exit code, or None if it's still running. A job
        that died without recording one (eg, the master rebooted) is 255.
        """
        job_dir = shlex.quote(f'{JOBS_DIR}/{validate_job_id(job_id)}')
        _, out = self.run_remote(
            f'if [ -e {job_dir}/exit ]; then cat {job_dir}/exit;'
            f' elif kill -0 $(cat {job_dir}/pid) 2>/dev/null; then echo running;'
            ' else echo 255; fi'
        )
        out = out.strip()
        return None if out == 'running' else int(out)

    def follow_job(self, job_id: str, log: Optional[TextIO] = None) -> int:
        """Stream a job's output until it finishes and return its exit code.
        The job runs on the master regardless of this connection, so if the
        connection drops (or goes quiet), reconnect and resume where the
        output left off, for as long as the job is running.
        """
        job_dir = shlex.quote(f'{JOBS_DIR}/{validate_job_id(job_id)}')
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        offset = 0

        while True:
            try:
                with self.ssh_client() as client:
                    client.get_transport().set_keepalive(30)

                    _, stdout, _ = client.exec_command(
                        f'tail -c +{offset + 1} -f {job_dir}/out '
                        f'--pid=$(cat {job_dir}/pid)'
                    )
                    channel = stdout.channel
                    channel.settimeout(300)

                    while True:
                        data = channel.recv(65536)
                        if not data:
                            break
                        offset += len(data)
                        emit(decoder.decode(data), log, end='')

                status = self.job_status(job_id)
                if status is not None:
                    return status

            except (
                paramiko.AuthenticationException,
                paramiko.BadHostKeyException,
            ):
                raise
            except (OSError, EOFError, paramiko.SSHException) as e:
                # Includes timeouts.
                logger.warning(f'Connection lost ({e!r}), reconnecting...')


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


def fast_destroy() -> bool:
    """Tear down the cluster without waiting for instances to terminate.

    Terraform's destroy blocks until every instance is fully `terminated`,
    which is slow. Instead, cancel the spot requests (so that AWS doesn't
    relaunch the workers), ask AWS to terminate the instances, delete the key
    pair, and forget the state. The security group and master network
    interface can't be deleted until the instances are gone, so they are left
    for `./cluster.sh clean`. Returns True on success.
    """
    if not STATE_PATH.exists():
        logger.info('No Terraform state, nothing to destroy.')
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
    region = read_state_region(resources)

    try:
        # One call per ID, so that a single already-gone resource (from a
        # previous partial attempt) doesn't fail the whole batch.
        for request in spot_requests:
            logger.info(f'Cancelling spot request: {request}')
            aws_idempotent(region, 'cancel-spot-instance-requests',
                           '--spot-instance-request-ids', request)

        for instance in instances:
            logger.info(f'Terminating instance: {instance}')
            aws_idempotent(region, 'terminate-instances',
                           '--instance-ids', instance)

        for key in keys:
            logger.info(f'Deleting key pair: {key}')
            aws_idempotent(region, 'delete-key-pair', '--key-name', key)

    except subprocess.CalledProcessError as e:
        logger.info(f'AWS call failed: {e.stderr}')
        return False

    # Keep the old state around for debugging, but out of Terraform's way.
    STATE_PATH.replace(STATE_PATH.with_name('terraform.tfstate.destroyed'))
    logger.info(
        'Cluster is shutting down. Run `./cluster.sh clean` later to remove '
        'the leftover security group and network interface.'
    )

    return True


def wait_for_cluster() -> Cluster:
    """Tail the cloud-init log on the master node until the cluster is up.
    """
    cluster = Cluster.from_tfstate()

    logger.info('Tailing `cloud-init-output.log` on the master node...')

    for line in cluster.tail_cloutinit_log_until_finished():
        logger.info(line)

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
    drain: bool = typer.Option(
        False,
        '--drain',
        help='Terminate the workers when the command finishes (default: keep '
             'them). Can be changed while it runs with `policy`.',
    ),
):
    """Create a cluster and run a command on it. The command runs detached on
    the master, so a dropped connection doesn't affect it; `attach` to
    reconnect. When it finishes, the master terminates the workers if the
    policy is `drain`, and otherwise leaves everything up. The master itself
    is only ever terminated by `destroy` or the idle shutdown. Exits with the
    command's exit code.
    """
    exit_code = 1

    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    job_id = validate_job_id(f'{stamp}-{profile or "default"}')
    log_path = LOG_DIR / f'{job_id}.log'

    logger.info(f'Saving logs to {log_path}')

    with open(log_path, 'w') as log:
        # Mirror log messages into the file, next to the raw command output.
        sink = logger.add(log, format='{time:HH:mm:ss} {level} {message}')
        started = False

        try:
            logger.info(f'$ {command}')

            if terraform('apply', profile, auto_approve=True, log=log) != 0:
                raise RuntimeError('`terraform apply` failed.')

            cluster = wait_for_cluster()
            cluster.set_policy('drain' if drain else 'keep')

            logger.info(f'Running: {command}')
            cluster.start_job(job_id, command)
            started = True

            logger.info(
                'Job is running on the master, so it is safe to disconnect. '
                f'Reattach with `./cluster.sh attach {job_id}`.'
            )
            exit_code = cluster.follow_job(job_id, log)
            logger.info(f'Command exited with code {exit_code}.')

        except KeyboardInterrupt:
            exit_code = 130

        finally:
            if started:
                # The job is the master's now. Don't tear anything down
                # here, since we may not know that it has finished.
                logger.info(
                    'Cluster left to the master: workers are terminated after '
                    'the job if the policy is `drain` (see `policy`), and '
                    'everything is terminated after being idle for a while.'
                )

            elif drain:
                # The job never started, so there is nothing to drain: destroy
                # everything, even after a failed or
                # partial apply, which can leave resources behind.
                if not fast_destroy():
                    logger.error(
                        'Destroy failed, the cluster may still be running! '
                        'Run `./cluster.sh destroy` to retry.'
                    )
                    exit_code = exit_code or 1

            logger.info(f'Logs saved to {log_path}')
            logger.remove(sink)

    raise typer.Exit(exit_code)


@app.command()
def attach(
    job_id: Optional[str] = typer.Argument(
        None,
        help='Job to attach to, as printed by `run` (default: the latest).',
    ),
):
    """Reattach to a job started by `run`, streaming its output from the
    beginning until it finishes. Exits with the job's exit code.
    """
    cluster = Cluster.from_tfstate()
    job_id = job_id or cluster.latest_job()

    if job_id is None:
        logger.error('No jobs found on the master.')
        raise typer.Exit(1)

    logger.info(f'Attached to {job_id} (policy: {cluster.get_policy()}).')

    try:
        raise typer.Exit(cluster.follow_job(job_id))
    except KeyboardInterrupt:
        logger.info('Detached, the job is still running.')
        raise typer.Exit(130)


@app.command()
def policy(
    value: Optional[str] = typer.Argument(
        None,
        help='`keep` or `drain`. Omit to show the current policy.',
    ),
):
    """Show or change what happens to the workers when the running job
    finishes. Can be changed any number of times while it runs, but has no
    effect once the job is done.
    """
    cluster = Cluster.from_tfstate()

    if value is None:
        print(cluster.get_policy())
    elif value in ('keep', 'drain'):
        cluster.set_policy(value)
        logger.info(f'Policy set to {value}.')
    else:
        logger.error('Policy must be `keep` or `drain`.')
        raise typer.Exit(1)


@app.command()
def update():
    """Pull the latest Docker image on every node of a running cluster and
    restart the Spark containers, without recreating the cluster. Restarts the
    master first, then the workers. Running jobs are killed.
    """
    cluster = Cluster.from_tfstate()
    failed = []

    # The state still lists nodes that were terminated since (eg, workers
    # after a `--drain` job, or spot workers stopped by an interruption), so
    # ask AWS which are actually running instead of waiting on SSH to time out.
    hosts = [cluster.master_dns, *cluster.worker_dns]
    with open(STATE_PATH) as fh:
        region = read_state_region(json.load(fh).get('resources', []))
    running = {
        i['PublicDnsName']
        for r in aws(
            region, 'describe-instances',
            '--filters',
            f'Name=dns-name,Values={",".join(hosts)}',
            'Name=instance-state-name,Values=running',
        )['Reservations']
        for i in r['Instances']
    }

    if cluster.master_dns not in running:
        logger.error('The master is not running. Is the cluster up?')
        raise typer.Exit(1)

    for host in hosts:
        if host not in running:
            logger.warning(f'Skipping {host}: not running.')
            continue

        logger.info(f'Updating {host}...')
        try:
            code = cluster.update_image(host)
        except (OSError, paramiko.SSHException) as e:
            logger.error(f'Unable to reach {host}: {e!r}')
            code = 1

        if code:
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
