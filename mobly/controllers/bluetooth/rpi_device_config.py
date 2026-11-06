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

"""Controller configurations for the Bluetooth reference device module."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import dataclasses
import logging
from typing import Any

import dacite

from mobly.controllers.bluetooth.lib import errors

# Error messages used in this module.
_DEVICE_EMPTY_CONFIG_MSG = 'Configuration is empty, abort!'
_DEVICE_CONFIGS_NOT_LIST_MSG = (
    'Configurations should be a list of dicts, abort!'
)
_DEVICE_CONFIG_NOT_DICT_MSG = 'Each configuration should be a dict, abort!'
_CONFIG_MISSING_REQUIRED_KEY_MSG = 'Missing required key in config'
_CONFIG_INVALID_VALUE_MSG = 'Invalid value in config'


def from_dicts(configs: Sequence[dict[str, Any]]) -> list[DeviceConfig]:
  """Create DeviceConfig objects from a list of dict configs.

  Args:
    configs: A list of dicts each representing the configuration of one
      Bluetooth reference device.

  Returns:
    A list of DeviceConfig.

  Raises:
    errors.ConfigError: Invalid controller config is given.
  """
  device_configs = []
  if not configs:
    raise errors.ConfigError(_DEVICE_EMPTY_CONFIG_MSG)
  elif not isinstance(configs, list):
    raise errors.ConfigError(_DEVICE_CONFIGS_NOT_LIST_MSG)

  for config in configs:
    if not isinstance(config, dict):
      raise errors.ConfigError(f'{_DEVICE_CONFIG_NOT_DICT_MSG}: {config}')

    logging.debug('Parsing Bluetooth reference device config: %s', config)
    device_configs.append(DeviceConfig.from_dict(config))

  return device_configs


@dataclasses.dataclass
class DeviceConfig:
  """Provides configs and default values for RpiDevice.

  Attributes:
    hostname: The IP address or hostname which SSH connects to.
    ssh_port: The SSH port of the test machine.
    username: Username to log the device in.
    password: Password to log the device in.
    keyfile: The lacal file path to the SSH key file on the Mobly test host
      machine.
    proxy_command: The SSH proxy command to use. For example, to access a device
      via a jump host, set "proxy_command='ssh -W <IP>:<port> jump_host'". Note
      that placeholders within the SSH command, such as '%h' and '%p', will not
      be substituted with the hostname and port when a proxy command is used.
    audio_capture_device_name: The name of the audio capture hardware device of
      the Raspberry Pi.
    bes_board_serial_port: The serial port of the BES dev board connected to the
      Raspberry Pi.
    bes_board_bt_address: The Bluetooth MAC address of the BES dev board
      connected to the Raspberry Pi.
    dimensions: The field for user to pass testbed dimensions or custom configs
      of the Bluetooth reference device.
  """

  # SSH configs
  hostname: str
  ssh_port: int = 22
  username: str = ''
  password: str = ''
  keyfile: str | None = None
  proxy_command: str | None = None

  # This device can be used as audio input in audio recorder.
  # To query the audio capture hardware device, run command `arecord -l` on the
  # test machine.
  # Example command output to device name conversion:
  # Command output "card 0: PCH [HDA Intel PCH], device 0: ALC662 rev3 Analog"
  # The device name of the above capture device is `hw:0,0`
  audio_capture_device_name: str | None = None

  # BES configs
  bes_board_serial_port: str | None = None
  bes_board_bt_address: str | None = None

  # Testbed dimensions of this testbed. The dimensions can then be used to
  # filter devices in the test. Dimensions can also be set in local testbeds.
  #
  # Example testbed:
  #   ```config.yaml
  #   RpiDevice:
  #   - hostname: '127.0.0.1'
  #     dimensions:
  #       mode: 'headset'
  #       use_bes: True
  #   ```
  dimensions: dict[str, Any] = dataclasses.field(default_factory=dict)

  def __post_init__(self):
    """Validates required fields and initializes directory paths if not set."""
    if not self.username:
      raise errors.ConfigError(
          f'{_CONFIG_MISSING_REQUIRED_KEY_MSG}: username is required.'
      )

  def get(self, key: str, default_value: Any = None) -> Any:
    """Gets the value of the key in device config or its dimensions.

    This method first tries to get the value of the key from DeviceConfig
    attributes. If the key is not in the attributes, it will try to get the
    value from `dimensions` dict.

    Args:
      key: The key to find.
      default_value: The value to return if cannot find target key.

    Returns:
      The value of the key if the key is in DeviceConfig attributes or
      dimensions. Otherwise, returns `default_value`.
    """
    if hasattr(self, key):
      return getattr(self, key)
    if key in self.dimensions:
      return self.dimensions[key]
    return default_value

  @classmethod
  def from_dict(cls, config: dict[str, Any]) -> DeviceConfig:
    """Parses controller configs from Mobly runner to DeviceConfig.

    Args:
      config: A dictionary of string parameters.

    Returns:
      DeviceConfig data class.

    Raises:
      errors.ConfigError: Invalid controller config is given.
    """
    type_converters: dict[type[Any], Callable[[Any], Any]] = {
        # Integer converter: any integer value in string
        # simply cast it to integer.
        int: int,
    }
    try:
      return dacite.from_dict(
          data_class=DeviceConfig,
          data=config,
          config=dacite.Config(type_hooks=type_converters),
      )
    except dacite.exceptions.MissingValueError as err:
      raise errors.ConfigError(
          f'{_CONFIG_MISSING_REQUIRED_KEY_MSG}: {config}'
      ) from err
    except ValueError as err:
      raise errors.ConfigError(
          f'{_CONFIG_INVALID_VALUE_MSG}: {config}'
      ) from err
