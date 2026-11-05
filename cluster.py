import typer
import subprocess
import json
import requests
import paramiko
import re
import shlex

from datetime import datetime
from typing import Optional, Iterator, TextIO
from pathlib import Path
from dataclasses import dataclass
from loguru import logger


ROOT_DIR = Path(__file__).parent
CONFIG_DIR = ROOT_DIR.parent / 'cluster'
LOG_DIR = ROOT_DIR / 'logs'
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


@dataclass
class Cluster:

    master_dns: str

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

        return cls(master_dns)

    @property
    def web_ui_url(self) -> str:
        """Build the URL for the Spark web UI.
        """
        return f'http://{self.master_dns}:8080'

    def ssh_client(self) -> paramiko.SSHClient:
        """Create an SSH client to the master node.
        """
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(self.master_dns, username='ubuntu', timeout=60*10)
        return client

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
    """Destroy the cluster.
    """
    terraform('destroy')


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
            elif terraform('destroy', profile, auto_approve=True, log=log):
                logger.error(
                    'Destroy failed, the cluster may still be running! '
                    'Run `./cluster.sh destroy` to retry.'
                )
                exit_code = exit_code or 1

            logger.info(f'Logs saved to {log_path}')

    raise typer.Exit(exit_code)


@app.command()
def login():
    """SSH into the master node.
    """
    subprocess.run(['ssh', f'ubuntu@{read_master_dns()}'])


@app.command()
def admin():
    """Open the Spark web UI.
    """
    subprocess.run(['open', f'http://{read_master_dns()}:8080'])


@app.command()
def cat_cloudinit_log():
    """Print the cloud-init log from the master node.
    """
    cluster = Cluster.from_tfstate()
    print(cluster.cat_cloudinit_log())


if __name__ == '__main__':
    app()
