# -*- coding: utf-8 -*-

# This code is part of the TrinityX software suite
# Copyright (C) 2026  ClusterVision Solutions b.v.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>

"""
The three slurm files as the Slurm app saved them (the console's
tests/fixtures/slurm-editor/answers/presets.json, its `files`): what test_slurm_ops lays out.
"""

FILES = {
    'nodes': (
        'NodeName=DEFAULT Boards=1 SocketsPerBoard=1 CoresPerSocket=1 ThreadsPerCore=1 RealMemory=6992 State=UNKNOWN\n'
        '\n'
        '# Below line is an example/placeholder and can be altered\n'
        '# NodeName=dummynode\n'
        '\n'
        '\n'
        '# Defaults blocks are for trinityx-config-slurm to read and append generated config.\n'
        '# The settings and values can be altered and will not be overwritten.\n'
        '#### Defaults Managed block start ####\n'
        '# HWPresetName=fat Boards=1 CoresPerSocket=8 RealMemory=64000 SocketsPerBoard=2 ThreadsPerCore=2 # Nodes=node[001,003-004] Partitions=compute-group\n'
        '# HWPresetName=thin Boards=1 CoresPerSocket=4 RealMemory=8000 SocketsPerBoard=1 ThreadsPerCore=1 # Nodes=node002 \n'
        '# NodeName=node001 Boards=1 CoresPerSocket=8 RealMemory=64000 SocketsPerBoard=2 ThreadsPerCore=2 Gres=gpu:a100:2,mps:100  # HWPreset=fat GresPreset=gpu\n'
        '# NodeName=node002 Boards=1 CoresPerSocket=4 RealMemory=8000 SocketsPerBoard=1 ThreadsPerCore=1 Gres=mps:100  # HWPreset=thin\n'
        '# NodeName=node003 Boards=1 CoresPerSocket=8 RealMemory=64000 SocketsPerBoard=2 ThreadsPerCore=2 Gres=mps:100  # HWPreset=fat\n'
        '# NodeName=node004 Boards=1 CoresPerSocket=8 RealMemory=64000 SocketsPerBoard=2 ThreadsPerCore=2 Gres=mps:100 State=DOWN # HWPreset=fat\n'
        '# PartitionName=compute-group Boards=1 CoresPerSocket=8 RealMemory=64000 SocketsPerBoard=2 ThreadsPerCore=2  # HWPreset=fat\n'
        '#### Defaults Managed block end   ####\n'
        '\n'
        '\n'
        "# TrinityX will only manage inside 'TrinityX Managed block'.\n"
        "# If manual override is desired, the 'TrinityX Managed block' can be removed.\n"
        '#### TrinityX Managed block start ####\n'
        '\n'
        'NodeName=node001 Boards=1 CoresPerSocket=8 RealMemory=64000 SocketsPerBoard=2 ThreadsPerCore=2 Gres=gpu:a100:2,mps:100 # GroupName=compute-group\n'
        'NodeName=node002 Boards=1 CoresPerSocket=4 RealMemory=8000 SocketsPerBoard=1 ThreadsPerCore=1 Gres=mps:100 # GroupName=compute-group\n'
        'NodeName=node003 Boards=1 CoresPerSocket=8 RealMemory=64000 SocketsPerBoard=2 ThreadsPerCore=2 Gres=mps:100 # GroupName=compute-group\n'
        'NodeName=node004 Boards=1 CoresPerSocket=8 RealMemory=64000 SocketsPerBoard=2 ThreadsPerCore=2 Gres=mps:100 State=DOWN # GroupName=compute-group\n'
        '\n'
        '# Nodesets\n'
        'NodeSet=compute-group Nodes=node[001-004]\n'
        '#### TrinityX Managed block end   ####\n'
        '\n'
        '\n'
    ),
    'partitions': (
        'PartitionName=defq   Nodes=ALL Shared=YES MaxTime=INFINITE State=UP Default=YES AllowAccounts=ALL\n'
        '\n'
        '\n'
        '# Defaults blocks are for trinityx-config-slurm to read and append generated config.\n'
        '# The settings and values can be altered and will not be overwritten.\n'
        '#### Defaults Managed block start ####\n'
        '# PartitionName=compute-group Default=YES MaxTime=1-00:00:00 \n'
        '#### Defaults Managed block end   ####\n'
        '\n'
        '\n'
        "# TrinityX will only manage inside 'TrinityX Managed block'.\n"
        "# If manual override is desired, the 'TrinityX Managed block' can be removed.\n"
        '#### TrinityX Managed block start ####\n'
        '\n'
        'PartitionName=compute-group Nodes=compute-group Default=YES MaxTime=1-00:00:00\n'
        '#### TrinityX Managed block end   ####\n'
        '\n'
        '\n'
    ),
    'gres': (
        '# File to configure slurm gres for nodes\n'
        '# Defaults blocks are for trinityx-config-slurm to read and append generated config.\n'
        '# The settings and values can be altered and will not be overwritten.\n'
        '#### Defaults Managed block start ####\n'
        '# GRESPresetName=gpu Name=gpu Type=a100 Count=2 File=/dev/nvidia[0-1] # Nodes=node001\n'
        '# GRESPresetName=mps Name=mps Count=100 no_consume=true # Nodes=node[001-004] Partitions=compute-group\n'
        '#### Defaults Managed block end   ####\n'
        '\n'
        '\n'
        "# TrinityX will only manage inside 'TrinityX Managed block'.\n"
        "# If manual override is desired, the 'TrinityX Managed block' can be removed.\n"
        '#### TrinityX Managed block start ####\n'
        'NodeName=node001 Name=gpu Type=a100 Count=2 File=/dev/nvidia[0-1]\n'
        'NodeName=node[001-004] Name=mps Count=100 no_consume\n'
        '#### TrinityX Managed block end   ####\n'
        '\n'
        '\n'
    ),
}
