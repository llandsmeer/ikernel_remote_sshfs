#!/usr/bin/env python
"""

Run standard IPython/Jupyter kernels on remote machines using
job schedulers.

"""

import secrets
import random
import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid

import pexpect

from tornado.log import LogFormatter

from ikernel_remote import __version__

# ALl the ports that need to be forwarded
PORT_NAMES = [
    'hb_port', 'shell_port', 'iopub_port', 'stdin_port', 'control_port'
]

# Blend in with the notebook logging
_LOG_FMT = (
    "%(color)s[%(levelname)1.1s %(asctime)s.%(msecs).03d "
    "%(name)s]%(end_color)s %(message)s"
)
_LOG_DATEFMT = "%H:%M:%S"


def _setup_logging(verbose):
    """
    Create a logger using tornado coloured output to appear like
    notebook messages. Will clear any existing handlers too.
    """

    log = logging.getLogger('ikernel_remote')
    if verbose:
        log.setLevel(logging.DEBUG)
    else:
        log.setLevel(logging.INFO)
    # Logging on stderr
    console = logging.StreamHandler()
    console.setFormatter(LogFormatter(fmt=_LOG_FMT, datefmt=_LOG_DATEFMT))

    log.handlers = []
    log.addHandler(console)

    # So that we can attach these to pexpect for debugging purposes
    # we need to make them look like files
    def _write(*args, **_):
        """
        Method to attach to a logger to allow it to act like a file object.

        """
        message = args[0]

        for line in message.splitlines():
            if line.strip():
                log.debug(line)

    def _pass():
        """pass"""
        pass

    log.write = _write
    log.flush = _pass

    return log


def get_password(prompt):
    """
    Interact with the user and ask for a password.

    Parameters
    ----------
    prompt : str
        Text to show the user when asking for a password.

    Returns
    -------
    password : str
        The text input by the user.

    """

    if 'SSH_ASKPASS' in os.environ:
        password = subprocess.check_output([os.environ['SSH_ASKPASS'], prompt])
    else:
        raise RuntimeError("Unable to get password, try setting SSH_ASKPASS")

    return password


def check_password(connection):
    """
    Check to see if a newly spawned process requires a password and retrieve
    it from the user if it does. Send the password to the process and
    check repeatedly for more passwords.

    Parameters
    ----------
    connection : pexpect.spawn
        The connection to check. Requires an expect and sendline method.

    """
    # This will loop until no more passwords are encountered
    while True:
        try:
            # Return all output as soon as anything arrives.
            # Assume that immediate output includes the
            # request for a password, or goes straight to
            # a prompt.
            text = connection.read_nonblocking(99999)
        except pexpect.TIMEOUT:
            # Nothing more to read from the output
            return

        re_passphrase = re.search('Enter passphrase .*:', text)
        re_password = re.search('.*@.* password:', text)
        if re_passphrase:
            passphrase = get_password(re_passphrase.group())
            connection.sendline(passphrase)
        elif re_password:
            password = get_password(re_password.group())
            connection.sendline(password)
        else:
            # No more passwords or passphrases requested
            return


def get_uuid(filename):
    """
    Given a filename containing a kernel, extract the uuid in
    the kernel name. If unsucessful, return new UUID
    Parameters
    ----------
    filename : str
        The name of the kernel-...-json file with the connection info.
    Returns
    -------
    uuid : str or None
        The extracted uuid, or None if not found.
    """
    if filename is not None:
        extracted = re.match(
            ".*kernel-([0-9a-f]{8}-?[0-9a-f]{4}-?4[0-9a-f]{3}-"
            "?[89ab][0-9a-f]{3}-?[0-9a-f]{12}).json",
            filename,
        )
        if extracted is not None:
            return uuid.UUID(extracted.group(1))
    return uuid.uuid4()


def safe_eval(s):
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1]
    if s.startswith("'") and s.endswith("'"):
        return s[1:-1]
    if s.startswith('b"') and s.endswith('"'):
        return s[2:-1]
    if s.startswith("b'") and s.endswith("'"):
        return s[2:-1]
    return s


class RemoteIKernel(object):
    """
    Configurable remote IPython kernel than runs on a node on a cluster
    using the a job manager system.

    """

    def __init__(
        self,
        connection_file=None,
        connection_info=None,
        interface='sge',
        cpus=1,
        mem=None,
        time=None,
        pe='smp',
        kernel_cmd='ipython kernel',
        workdir=None,
        tunnel=True,
        host=None,
        precmd=None,
        launch_args=None,
        verbose=False,
        tunnel_hosts=None,
        runtimedir=None,
        uuid=None
    ):
        """
        Initialise a kernel on a remote machine and start tunnels.

        """

        self.sshfs_proc = None
        self.log = _setup_logging(verbose)
        self.log.info("Remote kernel version: {}.".format(__version__))
        self.log.info("File location: {}.".format(__file__))
        # The connection info is provided by the notebook
        self.connection_file = connection_file
        self._delcf = False
        self.interface = interface
        self.cpus = cpus
        self.mem = mem
        self.time = time
        self.pe = pe
        self.kernel_cmd = kernel_cmd
        self.host = host  # Name of node to be changed once connection is ready.
        self.tunnel_hosts = tunnel_hosts
        self.connection = None  # will usually be a spawned pexpect
        self.workdir = workdir
        self.tunnel = tunnel
        self.tunnels = {}  # Processes running the SSH tunnels
        self.precmd = precmd
        self.launch_args = launch_args
        self.cwd = os.getcwd()  # Launch directory may be needed if no workdir
        self.uuid = uuid
        # Directory where kernel files should be created on the host
        if runtimedir is None:
            runtimedir = '~/.local/share/jupyter/runtime'
        self.runtimedir = runtimedir

        if connection_file is not None:
            if os.path.exists(self.connection_file):
                try:
                    loaded_connection_info = json.load(open(connection_file))
                    assert isinstance(connection_info, dict)
                except Exception:
                    loaded_connection_info = {}
            else:
                loaded_connection_info = {}
                self._delcf = True
            self.connection_info = loaded_connection_info.copy()
            self.connection_info.update(connection_info)
            if self._delcf or self.connection_info != loaded_connection_info:
                try:
                    json.dump(
                        self.connection_info,
                        open(connection_file, 'w'),
                        indent=2
                    )
                except Exception:
                    pass
        else:
            self.connection_info = connection_info

        # Initiate an ssh tunnel through any tunnel hosts
        # this will start a pexpect, so we must check if
        # self.connection exists when launching the interface
        if self.tunnel_hosts is not None:
            self.launch_tunnel_hosts()

        if self.interface == 'local':
            self.launch_local()
        elif self.interface == 'pbs':
            self.launch_pbs()
        elif self.interface == 'sge':
            self.launch_sge()
        elif self.interface == 'ssh':
            self.launch_ssh()
        elif self.interface == 'slurm':
            self.launch_slurm()
        else:
            raise ValueError("Unknown interface {}".format(interface))

        # If we've established a connection, start the kernel!
        if self.connection is not None:
            self.start_kernel()
            if self.tunnel:
                self.tunnel_connection()

    def __del__(self):
        """
        Make final cleanups.
        """
        if self._delcf:
            try:
                os.remove(self.connection_file)
            except Exception:
                pass

    def launch_tunnel_hosts(self):
        """
        Build a chain of hosts to tunnel through and start an ssh
        chain with pexpect.
        """
        tunnel_hosts_cmd = self.tunnel_hosts_cmd
        self.log.debug("Tunnel proxy command: {}.".format(tunnel_hosts_cmd))
        self._spawn(tunnel_hosts_cmd)
        check_password(self.connection)

    def launch_local(self):
        """
        Initialise a shell on the local machine that can be interacted with.
        Stop tunneling if it is not needed.
        """
        self.log.info("Launching local kernel.")
        if self.launch_args:
            bash = '/bin/bash {}'.format(self.launch_args)
        else:
            bash = '/bin/bash'
        self._spawn(bash)
        # Don't try and start tunnels to the same machine. Causes issues.
        self.tunnel = False

    def launch_ssh(self):
        """
        Initialise a connection through ssh.

        Launch an ssh connection using pexpect so it can be interacted with.
        """
        pr = 34000 + random.randint(0, 999) # ha
        pl = 35000 + random.randint(0, 999)
        wd = f'/tmp/ikrsshfs-{secrets.token_urlsafe()}'
        self.sshfs_proc = subprocess.Popen([
            'ncat', '-l', '-p', str(pl), '-e',
            f'/usr/lib/openssh/sftp-server -d {self.cwd}'
            ])
        time.sleep(0.1)
        assert self.sshfs_proc.returncode is None
        self.log.info("Launching kernel over SSH.")
        if self.launch_args:
            launch_args = self.launch_args
        else:
            launch_args = ''
        login_cmd = 'ssh -R {port_remote}:localhost:{port_local} -o StrictHostKeyChecking=no {args} {host}'.format(
            args=launch_args, host=self.host,
            port_remote=pr,
            port_local=pl
        )
        self.log.debug("Login command: '{}'.".format(login_cmd))
        ssh = self._spawn(login_cmd)
        check_password(self.connection)
        ssh.sendline(f'mkdir -p {wd}')
        ssh.sendline(f'sshfs localhost: {wd} -o directport={pr}')
        ssh.sendline(f'cd {wd}')

    def __del__(self):
        if self.sshfs_proc is not None:
            self.sshfs_proc.kill()
            self.sshfs_proc.wait()
            self.sshfs_proc = None

    def launch_pbs(self):
        """
        Start a kernel through the torque 'qsub -I' command. The connection
        will use the object's connection_info and kernel_command.
        """
        self.log.info("Launching kernel through PBS/Torque.")
        job_name = 'ikernel_remote'
        res = []
        if self.cpus > 1:
            res.append('ncpus={cpus}'.format(cpus=self.cpus))
        if self.mem is not None:
            res.append('mem={mem}'.format(mem=self.mem))
        if self.time is not None:
            res.append('walltime={time}'.format(time=self.time))
        if res:
            res_string = '-l ' + ','.join(res)
        else:
            res_string = ''
        if self.launch_args:
            args_string = self.launch_args
        else:
            args_string = ''
        pbs_cmd = 'qsub -I {} -N {} {}'.format(
            res_string, job_name, args_string
        )
        self.log.debug("PBS command: '{}'.".format(pbs_cmd))
        # Will wait in the queue for up to 10 mins
        qsub_i = self._spawn(pbs_cmd)
        # Hopefully this text is universal? Job started...
        qsub_i.expect('qsub: job (.*) ready')
        # Now we have to ask for the hostname (any way for it to
        # say automatically?)
        qsub_i.sendline('echo Running on `hostname`')

        # hostnames would be alphanumeric with . and - permitted
        # This way we also ignore the echoed echo command
        qsub_i.expect('Running on ([\w.-]+)')
        node = qsub_i.match.groups()[0]

        self.log.info("Established session on node: {}.".format(node))
        self.host = node

    def launch_sge(self):
        """
        Start a kernel through the gridengine 'qlogin' command. The connection
        will use the object's connection_info and kernel_command.
        """
        self.log.info("Launching kernel through GridEngine.")
        job_name = 'ikernel_remote'
        if self.cpus > 1:
            pe_string = "-pe {pe} {cpus}".format(pe=self.pe, cpus=self.cpus)
        else:
            pe_string = ''
        res = []
        if self.mem is not None:
            res.append('h_vmem={mem}'.format(mem=self.mem))
        if self.time is not None:
            res.append('h_rt={time}'.format(time=self.time))
        if res:
            res_string = '-l ' + ','.join(res)
        else:
            res_string = ''
        if self.launch_args:
            args_string = self.launch_args
        else:
            args_string = ''
        sge_cmd = 'qlogin -now n {pe} {res} -N {name} {args}'.format(
            pe=pe_string, res=res_string, name=job_name, args=args_string
        )
        self.log.debug("Gridengine command: '{}'.".format(sge_cmd))
        # Will wait in the queue for up to 10 mins
        qlogin = self._spawn(sge_cmd)
        # Hopefully this text is universal?
        qlogin.expect('Establishing builtin session to host (.*) ...')

        node = qlogin.match.groups()[0]
        self.log.info("Established session on node: {}.".format(node))
        self.host = node

    def launch_slurm(self):
        """
        Start a kernel through the slurm 'srun' command. Bind the spawned
        pexpect to the class to interact with it.
        """
        self.log.info("Launching kernel through SLURM.")
        job_name = 'ikernel_remote'
        opts = ''
        if self.cpus > 1:
            opts += ' --cpus-per-task {cpus}'.format(cpus=self.cpus)
        if self.mem is not None:
            opts += ' --mem {mem}'.format(mem=self.mem)
        if self.time is not None:
            opts += ' --time {time}'.format(time=self.time)
        if self.launch_args:
            launch_args = self.launch_args
        else:
            launch_args = ''
        # -u disables buffering, -i is interactive, -v so we know the node
        # tasks must be before the bash!
        srun_cmd = 'srun{opts} -J {job_name} {args} -v -u bash -i'.format(
            opts=opts, job_name=job_name, args=launch_args
        )
        self.log.info("SLURM command: '{}'.".format(srun_cmd))
        srun = self._spawn(srun_cmd)
        # Hopefully this text is universal?
        srun.expect('srun: Node (.*), .* tasks started')

        node = srun.match.groups()[0]
        self.log.info("Established session on node: {}.".format(node))
        self.host = node

    def start_kernel(self):
        """
        Start the kernel on the remote machine.
        """
        conn = self.connection
        self.log.info("Established connection; starting kernel.")

        # Use the specified working directory or try to change to the same
        # directory on the remote machine.
        if self.workdir:
            self.log.info("Remote working directory {}.".format(self.workdir))
            conn.sendline('cd {}'.format(self.workdir))
        else:
            self.log.info("Current working directory {}.".format(self.cwd))
            conn.sendline('cd {}'.format(self.cwd))

        if '{host_connection_file}' in self.kernel_cmd:
            kernel_name = "kernel-remote-{}.json".format(self.uuid)
            host_connection_file = os.path.join(self.runtimedir, kernel_name)

        else:
            host_connection_file = None

        # Create a temporary file to store a copy of the connection information
        # Delete the file if it already exists
        if host_connection_file is not None:
            connection_file_dir = os.path.dirname(host_connection_file)
            conn.sendline('mkdir -p {}'.format(connection_file_dir))
            conn.sendline('rm -f {}'.format(host_connection_file))
            file_contents = json.dumps(self.connection_info)
            conn.sendline(
                'echo \'{}\' > {}'.format(file_contents, host_connection_file)
            )

        # Is this the best place for a pre-command? I guess people will just
        # have to deal with it. Pass it on as is.
        if self.precmd:
            conn.sendline(self.precmd)

        # Init as a background process so we can delete the tempfile after
        kernel_init = self.kernel_cmd.format(
            host_connection_file=host_connection_file, **self.connection_info
        )
        self.log.info("Running kernel command: '{}'.".format(kernel_init))
        conn.sendline(kernel_init)

        if host_connection_file is not None:
            # The kernel blocks further commands, so queue deletion of the
            # transient file for once the process stops. Trying to do this
            # whilst simultaneously starting the kernel ended up deleting
            # the file before it was read.
            conn.sendline('rm -f {}'.format(host_connection_file))

        conn.sendline('exit')
        # Could check this for errors?
        conn.expect('exit')

    def tunnel_connection(self):
        """
        Set up tunnels to the node using the connection information.
        """
        # Auto accept ssh keys so tunnels work on previously unknown hosts.
        # This might need to change, but the other option is to get user or
        # admin to turn StrictHostKeyChecking off in .ssh/ssh_config for this
        # to work seamlessly. (tunnels will have already done this)
        pre = self.tunnel_hosts_cmd or ''
        pexpect.spawn(
            '{pre} ssh -o StrictHostKeyChecking=no '
            '{host}'.format(pre=pre, host=self.host).strip(),
            encoding='utf-8'
        ).sendline('exit')

        # connection info should have the ports being used
        tunnel_command = self.tunnel_cmd.format(**self.connection_info)
        tunnel = pexpect.spawn(tunnel_command, encoding='utf-8')
        check_password(tunnel)

        self.log.info(
            "Setting up tunnels on ports: {}.".format(
                ", ".join([
                    str(self.connection_info[port_name])
                    for port_name in PORT_NAMES
                ])
            )
        )
        self.log.debug("Tunnel command: {}.".format(tunnel_command))

        # Store the tunnel
        self.tunnels['tunnel'] = tunnel

    def check_tunnels(self):
        """
        Check the PID of tunnels and restart any that have died.
        """
        if not self.tunnels['tunnel'].isalive():
            self.log.debug("Restarting ssh tunnels.")
            self.tunnel_connection()

    def keep_alive(self, timeout=5):
        """
        Keep the script alive forever. KeyboardInterrupt will get passed on
        to the kernel. The timeout determines how often the ssh tunnels are
        checked.
        """
        # The timeout determines how long each loop will be,
        # if an ssh tunnel dies, this is how long it will be
        # before it is revived.
        self.connection.timeout = timeout
        time.sleep(timeout)

        # There might be a more elegant way to do this, but since this
        # process doesn't do anything and is managed by the notebook
        # it really doesn't matter
        while True:
            # If the kernel dies, we should too, but try and
            # give some error info
            if not self.connection.isalive():
                self.log.error("Kernel died.")
                for line in self.connection.readlines():
                    if line.strip():
                        self.log.error(line)
                break
            # Kernel is still alive, ensure tunnels are too
            self.check_tunnels()
            try:
                # read anything from the kernel output, pexpect
                # logging will be set up to emit anything if
                # required.
                self.connection.readlines()
            except pexpect.TIMEOUT:
                # Raises timeout if there is no data, prevents blocking
                # Moves on to the next loop.
                pass
            except KeyboardInterrupt:
                self.log.info("Caught interrupt; sending to kernel.")
                self.connection.sendcontrol('c')

    def _spawn(self, command, timeout=600):
        """
        Helper to start a pexpect.spawn as self.connection. If the session
        has already been started, just pass the command to sendline. Return
        the current spawn instance. The logfile is implicitly set to
        self.log.

        Parameters
        ----------
        command : str
            Command to spawn or run in the current session.
        timeout : int
            Timeout for command to complete, passed to pexpect.

        Returns
        -------
        connection : pexpect.spawn
            The connection object. This is also attached to the class.
        """
        if self.connection is None:
            self.connection = pexpect.spawn(
                command, timeout=timeout, logfile=self.log, encoding='utf-8'
            )
        else:
            self.connection.sendline(command)

        return self.connection

    @property
    def tunnel_hosts_cmd(self):
        """Return the ssh command to tunnel through the middle hosts."""
        if not self.tunnel_hosts:
            return None

        jumps = ','.join(host for host in self.tunnel_hosts[:-1])
        if jumps:
            jumps = '-J ' + jumps

        host = self.tunnel_hosts[-1]

        if ':' in host:
            host, port = host.split(":")
            ssh = 'ssh -o StrictHostKeyChecking=no {} -p {} {}'.format(
                jumps, port, host
            )
        else:
            ssh = 'ssh -o StrictHostKeyChecking=no {} {}'.format(jumps, host)

        return ssh

    @property
    def tunnel_cmd(self):
        """Return a tunnelling command that just needs a port."""

        # One connection can tunnel all the ports
        ports_str = " ".join([
            "-L 127.0.0.1:{{{port}}}:127.0.0.1:{{{port}}}"
            "".format(port=port) for port in PORT_NAMES
        ])

        # Add all the gateway machines as an ssh chain
        if self.tunnel_hosts:
            jumps = '-J ' + ','.join(host for host in self.tunnel_hosts)
        else:
            jumps = ''

        if ':' in self.host:
            host, host_port = self.host.split(":")
            ssh = 'ssh -o StrictHostKeyChecking=no -p {host_port}'.format(
                host_port=host_port
            )
        else:
            ssh = 'ssh -o StrictHostKeyChecking=no'
            host = self.host

        # Timeout is specified here, this should be longer than the checking
        # interval
        tunnel_cmd = (
            "{ssh} {jumps} -S none {ports_str} {host} sleep 600".format(
                ssh=ssh, jumps=jumps, host=host, ports_str=ports_str
            )
        )

        self.log.debug("Tunnel command: {}".format(tunnel_cmd))
        return tunnel_cmd


def start_remote_kernel():
    """
    Read command line arguments and initialise a kernel.
    """

    # These will not face a user since they are interpreting the command from
    # kernel the kernel.json
    description = "This is the kernel launcher, did you mean '%prog manage'"
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('-f', '--f')
    parser.add_argument('--interface', default='local')
    parser.add_argument('--cpus', type=int, default=1)
    parser.add_argument('--mem')
    parser.add_argument('--time')
    parser.add_argument('--pe', default='smp')
    parser.add_argument(
        '--kernel_cmd',
        default=sys.executable +
        ' -m ipykernel_launcher -f {host_connection_file}'
    )
    parser.add_argument('--workdir')
    parser.add_argument('--host')
    parser.add_argument('--precmd')
    parser.add_argument('--launch-args')
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--tunnel-hosts', nargs='+')
    parser.add_argument('--runtimedir')

    for port in PORT_NAMES:
        parser.add_argument('--{}'.format(port[:-5]), type=int, default=0)
    parser.add_argument('--ip')
    parser.add_argument('--signature_scheme', '--Session.signature_scheme')
    parser.add_argument('--key', '--Session.key')
    parser.add_argument('--transport')

    args = parser.parse_args()

    connection_info = {}
    for port in PORT_NAMES:
        arg = getattr(args, port[:-5])
        if args.hb:
            connection_info[port] = arg
    if args.ip:
        connection_info['ip'] = args.ip
    if args.signature_scheme:
        connection_info['signature_scheme'] = safe_eval(args.signature_scheme)
    if args.key:
        connection_info['key'] = safe_eval(args.key)
    if args.transport:
        connection_info['transport'] = safe_eval(args.transport)

    kernel = RemoteIKernel(
        connection_file=args.f,
        connection_info=connection_info,
        interface=args.interface,
        cpus=args.cpus,
        mem=args.mem,
        time=args.time,
        pe=args.pe,
        kernel_cmd=args.kernel_cmd,
        workdir=args.workdir,
        host=args.host,
        precmd=args.precmd,
        launch_args=args.launch_args,
        verbose=args.verbose,
        tunnel_hosts=args.tunnel_hosts,
        runtimedir=args.runtimedir,
        uuid=get_uuid(args.f)
    )
    kernel.keep_alive()
