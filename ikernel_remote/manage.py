"""
manage.py

Manage the kernels available to ikernel_remote.
Run ``ikernel_remote manage`` to see a list of commands.

"""

from __future__ import print_function

import argparse
import getpass
import json
import os
import re
import sys
from os import path
from subprocess import list2cmdline

# How we identify kernels that ikernel_remote will manage
# These go through a compatibility layer to work with IPython and Jupyter
from ikernel_remote.compat import kernelspec as ks
from ikernel_remote.compat import tempdir

from ikernel_remote import __version__


def delete_kernel(kernel_name):
    """
    Delete the kernel by removing the kernel.json and directory.

    Parameters
    ----------
    kernel_name : str
        The name of the kernel to delete

    Raises
    ------
    KeyError
        If the kernel is not found.
    """
    spec = ks.get_kernel_spec(kernel_name)
    os.remove(path.join(spec.resource_dir, 'kernel.json'))
    try:
        os.rmdir(spec.resource_dir)
    except OSError:
        # Non empty directory, just leave it
        pass


def show_kernel(kernel_name):
    """
    Print the contents of the kernel.json to the terminal, plus some extra
    information.

    Parameters
    ----------
    kernel_name : str
        The name of the kernel to show the information for.
    """
    # Load the raw json, since we store some unexpected data in there too
    spec = ks.get_kernel_spec(kernel_name)
    with open(path.join(spec.resource_dir, 'kernel.json')) as kernel_file:
        kernel_json = json.load(kernel_file)

    # Manually format the json to put each key: value on a single line
    print("Kernel found in: {}".format(spec.resource_dir))
    print("Name: {}".format(spec.display_name))
    print("Kernel command: {}".format(list2cmdline(spec.argv)))
    print(
        "Create command: {}".format(
            list2cmdline(kernel_json['ikernel_remote_argv'])
        )
    )
    print("Raw json: {}".format(json.dumps(kernel_json, indent=2)))


def add_kernel(
    interface,
    name,
    kernel_cmd,
    cpus=1,
    mem=None,
    time=None,
    pe=None,
    language=None,
    system=False,
    workdir=None,
    host=None,
    precmd=None,
    launch_args=None,
    tunnel_hosts=None,
    verbose=False,
    runtimedir=None
):
    """
    Add a kernel. Generates a kernel.json and installs it for the system or
    user.
    """
    kernel_name = []
    display_name = []
    argv = [sys.executable, '-m', 'ikernel_remote']

    # How to connect to kernel
    if interface == 'local':
        argv.extend(['--interface', 'local'])
        kernel_name.append('local')
        display_name.append("Local")
    elif interface == 'pbs':
        argv.extend(['--interface', 'pbs'])
        display_name.append('PBS')
    elif interface == 'sge':
        argv.extend(['--interface', 'sge'])
        kernel_name.append('sge')
        display_name.append("GridEngine")
    elif interface == 'ssh':
        if host is None:
            raise KeyError('A host is required for ssh.')
        argv.extend(['--interface', 'ssh'])
        argv.extend(['--host', host])
        kernel_name.append('ssh')
        kernel_name.append(host)
        display_name.append("SSH")
        display_name.append(host)
    elif interface == 'slurm':
        argv.extend(['--interface', 'slurm'])
        kernel_name.append('slurm')
        display_name.append("SLURM")
    else:
        raise ValueError("Unknown interface {}".format(interface))

    display_name.append(name)
    kernel_name.append(re.sub(r'\W', '', name).lower())

    if pe is not None:
        argv.extend(['--pe', pe])
        kernel_name.append(pe)
        display_name.append(pe)

    if cpus and cpus > 1:
        argv.extend(['--cpus', str(cpus)])
        kernel_name.append('{}cpus'.format(cpus))
        display_name.append('{} CPUs'.format(cpus))

    if mem is not None:
        argv.extend(['--mem', mem])
        kernel_name.append(mem.lower())
        display_name.append(mem)

    if time is not None:
        argv.extend(['--time', time])
        kernel_name.append('t{}'.format(re.sub(r'\W', '', time).lower()))
        display_name.append(time)

    if workdir is not None:
        argv.extend(['--workdir', workdir])

    if runtimedir is not None:
        argv.extend(['--runtimedir', runtimedir])

    if precmd is not None:
        argv.extend(['--precmd', precmd])

    if launch_args is not None:
        argv.extend(['--launch-args', launch_args])

    if verbose:
        argv.extend(['--verbose'])

    # protect the {connection_file} part of the kernel command
    if kernel_cmd is not None:
        kernel_cmd = kernel_cmd.replace(
            '{connection_file}', '{host_connection_file}'
        )
        argv.extend(['--kernel_cmd', kernel_cmd])

    if tunnel_hosts:
        # This will be a list of hosts
        kernel_name.append('via_{}'.format("_".join(tunnel_hosts)))
        display_name.append("(via {})".format(" ".join(tunnel_hosts)))
        argv.extend(['--tunnel-hosts'] + tunnel_hosts)

    # ikernel_remote needs the connection file too
    argv.extend(['-f', '{connection_file}'])

    # Prefix all kernels with 'rik_' for management.
    kernel_name = 'remote_' + '_'.join(kernel_name)
    # Having an @ in the string messes up the javascript;
    # so get rid of evrything just in case.
    kernel_name = re.sub(r'\W', '_', kernel_name)
    kernel_json = {
        'display_name': " ".join(display_name),
        'argv': argv,
    }

    if language is not None:
        kernel_json['language'] = language

    # Put the commandline in so that '--show' will show how to recreate
    # the kernel
    kernel_json['ikernel_remote_argv'] = sys.argv

    # False attempts a system install, otherwise install as the current user
    if system:
        username = False
    else:
        username = getpass.getuser()

    # kernel.json file installation
    with tempdir.TemporaryDirectory() as temp_dir:
        os.chmod(temp_dir, 0o755)  # Starts off as 700, not user readable

        with open(path.join(temp_dir, 'kernel.json'), 'w') as kernel_file:
            json.dump(kernel_json, kernel_file, sort_keys=True, indent=2)

        ks.install_kernel_spec(
            temp_dir, kernel_name, user=username, replace=True
        )

    return kernel_name


def manage():
    """
    Manage the available ikernel_remotes.

    All the options are pulled from arguments so we take no
    arguments here.
    """

    kernels_display = [
        "Currently installed remote kernels:"
    ]
    kernels = {}

    # Sort so they are always in the same order
    for kernel_name in sorted(ks.find_kernel_specs()):
        spec = ks.get_kernel_spec(kernel_name)
        if 'ikernel_remote' in spec.argv:
            display = "  {name} : {desc}".format(
                name=kernel_name, desc=spec.display_name
            )
            kernels[kernel_name] = spec
            kernels_display.append(display)

    # The raw formatter stops lines wrapping
    parser = argparse.ArgumentParser(
        description="Remote IKernel management utility. "
                    "Run without arguments to list installed remote kernels.",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        '-V',
        '--version',
        action='version',
        version="%(prog)s: ikernel_remote ver. " + __version__
    )
    parser.add_argument(
        '--show', '-s', help="Print the contents of the "
        "kernel."
    )
    parser.add_argument(
        '--add',
        '-a',
        action="store_true",
        help="Add a new "
        "kernel according to other commandline options."
    )
    parser.add_argument(
        '--delete',
        '-d',
        help="Remove the kernel and delete "
        "the associated kernel.json."
    )
    parser.add_argument(
        '--kernel_cmd', '-k', help="Kernel command "
        "to install."
    )
    parser.add_argument(
        '--name',
        '-n',
        help="Name to identify the kernel,"
        "e.g. 'Python 2.7'."
    )
    parser.add_argument(
        '--language',
        '-l',
        help="Explicitly specify the "
        "language of the kernel."
    )
    parser.add_argument(
        '--cpus',
        '-c',
        type=int,
        help="Launch the kernel "
        "as a multi-core job with this many cores if > 1."
    )
    parser.add_argument(
        '--mem',
        '-m',
        help="Allocate specified "
        "amount of memory for your kernel job."
    )
    parser.add_argument(
        '--time',
        '-t',
        help="Allocate specified "
        "runtime for your kernel job."
    )
    parser.add_argument(
        '--pe',
        help="Parallel environment to use on when "
        "running on gridengine."
    )
    parser.add_argument(
        '--host',
        '-x',
        help="The hostname or ip address "
        "running through an SSH connection. For non standard "
        "ports use host:port."
    )
    parser.add_argument(
        '--interface',
        '-i',
        choices=['local', 'ssh', 'pbs', 'sge', 'slurm'],
        help="Specify how the remote kernel is launched."
    )
    parser.add_argument(
        '--system',
        help="Install the kernel into the system "
        "directory so that it is available for all users. "
        "Might need admin privileges.",
        action='store_true'
    )
    parser.add_argument(
        '--workdir',
        help="Directory in which to start the "
        "kernel. If not specified it will use the current "
        "directory. This is important if the local and remote "
        "filesystems differ."
    )
    parser.add_argument(
        '--remote-precmd',
        help="Command to execute on the "
        "remote host before launching the kernel, but after "
        "changing to the working directory."
    )
    parser.add_argument(
        '--remote-launch-args',
        help="Arguments to add to the "
        "command that launches the remote session, i.e. the "
        "ssh or qlogin command, such as '-l h_rt=24:00:00' to "
        "limit job time on GridEngine jobs."
    )
    parser.add_argument(
        '--tunnel-hosts',
        '-u',
        nargs='+',
        help="Tunnel the "
        "connection through the given ssh hosts before "
        "starting the endpoint interface. Works with any "
        "interface. For non standard ports use host:port."
    )
    parser.add_argument(
        '--runtimedir',
        help="Directory in which to create "
        "kernel.json files on the host. Please provide an "
        "absolute path. By default it is "
        "'~/.local/share/jupyter/runtimedir'."
    )
    parser.add_argument(
        '--verbose',
        '-v',
        action='store_true',
        help="Running "
        "kernel will produce verbose debugging on the console."
    )

    # Temporarily remove 'manage' from the arguments
    raw_args = sys.argv[:]
    if len(sys.argv) > 1 and sys.argv[1] == 'manage':
        del sys.argv[1]
    args = parser.parse_args()
    sys.argv = raw_args

    if args.add:
        kernel_name = add_kernel(
            args.interface, args.name, args.kernel_cmd, args.cpus, args.mem,
            args.time, args.pe, args.language, args.system, args.workdir,
            args.host, args.remote_precmd, args.remote_launch_args,
            args.tunnel_hosts, args.verbose, args.runtimedir
        )
        print("Installed kernel {}.".format(kernel_name))
    elif args.delete:
        if args.delete in kernels:
            delete_kernel(args.delete)
        else:
            print("Can't delete {}".format(args.delete))
            print("\n" + "\n".join(kernels_display))
    elif args.show:
        if args.show in kernels:
            show_kernel(args.show)
        else:
            print("Kernel {} doesn't exist".format(args.show))
            print("\n" + "\n".join(kernels_display))
    else:
        print("\n".join(kernels_display))
