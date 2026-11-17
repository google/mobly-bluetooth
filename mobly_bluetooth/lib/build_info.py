# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Build info collector for a Raspberry Pi device."""

from __future__ import annotations

import dataclasses
import re

from mobly_bluetooth.lib import ssh as ssh_lib


@dataclasses.dataclass
class BuildInfo:
  """Build information from the Raspberry Pi device.

  Attributes:
    serial: Serial number of the device.
    model: Product model of the device.
    hardware: The hardware processor name of the device.
    revision_code: The revision code of the Raspberry Pi model revision.
    number_of_cpus: The number of CPUs.
    cpu_model_name: The model name of the CPU.
    max_cpu_frequency_ghz: Maximum frequency of the cpu, in GHz.
    os_name: Name of the operating system.
    kernel_version: Version of the Linux kernel.
    total_memory_gb: Total physical memory, in GBytes.
  """

  # Device information
  serial: str
  model: str
  hardware: str
  revision_code: str

  # CPU information
  number_of_cpus: int
  cpu_model_name: str
  max_cpu_frequency_ghz: float

  # OS information
  os_name: str
  kernel_version: str

  # Memory information
  total_memory_gb: float

  @classmethod
  def collect(cls, ssh: ssh_lib.SSHProxy) -> BuildInfo:
    """Collects build information from a remote Raspberry Pi device."""
    device_info = ssh.get_remote_file_contents('/proc/cpuinfo')
    cpu_info = ssh.execute_command('lscpu')
    os_info = ssh.execute_command('hostnamectl')
    memory_info = ssh.get_remote_file_contents('/proc/meminfo')

    return BuildInfo(
        # Parses device information
        serial=_get_property(device_info, 'Serial'),
        model=_get_property(device_info, 'Model'),
        hardware=_get_property(device_info, 'Hardware'),
        revision_code=_get_property(device_info, 'Revision'),
        # Parses CPU information
        number_of_cpus=int(_get_property(cpu_info, r'CPU\(s\)')),
        cpu_model_name=_get_property(cpu_info, 'Model name'),
        max_cpu_frequency_ghz=_frequency_to_ghz(
            _get_property(cpu_info, 'CPU max MHz')
        ),
        # Parses OS information
        os_name=_get_property(os_info, 'Operating System'),
        kernel_version=_get_property(os_info, 'Kernel'),
        # Parses memory information
        total_memory_gb=_memory_to_gb(_get_property(memory_info, 'MemTotal')),
    )


def _get_property(info: str, key: str) -> str:
  """Parses the property of the key from an information string."""
  # The key should be a full match from the start of the line to a colon.
  # There may be spaces and tabs before or after the key.
  # The key and value may have colons inside.
  if match := re.search(
      rf'^(?P<key>[ \t]*{key}[ \t]*):(?P<value>.*)', info, flags=re.MULTILINE
  ):
    return match['value'].strip()
  return ''


def _frequency_to_ghz(frequency: str) -> float:
  """Converts frequency to GHz with 1 digit accuracy after the decimal point."""
  return round(float(frequency) / 1000, 1)


def _memory_to_gb(memory: str) -> float:
  """Converts memory to GBytes with 1 digit accuracy after the decimal point."""
  memory_size = int(memory.replace('kB', ''))
  return round(memory_size / pow(1024, 2), 1)
