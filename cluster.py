import typer
import subprocess
import json
import requests
import paramiko
import re

from typing import Optional, Iterator
from pathlib import Path
from dataclasses import dataclass
from loguru import logger


ROOT_DIR = Path(__file__).parent
CONFIG_DIR = ROOT_DIR.parent / 'cluster'
CLOUDINIT_LOG_PATH = '/var/log/cloud-init-output.log'


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
        client.connect(self.master_dns, username='ubuntu', timeout=60*3)
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


def read_master_dns():
    """Read the master IP out of the TF state.
    """
    with open(ROOT_DIR / 'terraform.tfstate') as fh:
        return json.load(fh)['outputs']['master_dns']['value']


app = typer.Typer()


@app.command()
def create(profile: Optional[str] = typer.Argument(None)):
    """Create a cluster using default variables defined in:

    `../cluster/default.tfvars`

    If a "profile" is passed, also inject variables defined at:

    `../cluster/profiles/<profile>.tfvars`
    """

    cmd = [
        'terraform', 'apply',
        '-var-file', CONFIG_DIR / 'default.tfvars',
    ]

    if profile is not None:
        cmd += [
            '-var-file', CONFIG_DIR / 'profiles' / f'{profile}.tfvars'
        ]

    subprocess.run(cmd)

    cluster = Cluster.from_tfstate()

    logger.info('Tailing `cloud-init-output.log` on the master node...')

    for line in cluster.tail_cloutinit_log_until_finished():
        print(line)

    logger.info('Cluster up 🚀🚀')


@app.command()
def destroy():
    """Destroy the cluster.
    """
    subprocess.run([
        'terraform', 'destroy',
        '-var-file', CONFIG_DIR / 'default.tfvars',
    ])


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
