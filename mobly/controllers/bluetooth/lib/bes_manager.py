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

"""The module to manage the BES devboard connected to the Raspberry Pi."""

import dataclasses
from typing import Any, TypeAlias

from mobly import runtime_test_info
from mobly.controllers.android_device_lib.services import base_service

from mobly.controllers.bluetooth import bes_device
from mobly.controllers.bluetooth import bes_device_config
from mobly.controllers.bluetooth import bluetooth_reference_device_base
from mobly.controllers.bluetooth.lib import ssh as ssh_lib

# Avoid directly importing rpi_device, which causes circular
# dependencies
rpi_device = Any

# Error messages
_ALREADY_STARTED_MESSAGE = 'BES manager has already started.'
_NOT_STARTED_MESSAGE = (
    'BES manager not started yet. Please start the BES manager first.'
)

# Aliases for BES device errors and information dataclasses
Error: TypeAlias = bes_device.BesDeviceError
CommandTimeoutError: TypeAlias = bes_device.CommandTimeoutError
BesRuntimeError: TypeAlias = bes_device.BesRuntimeError
BesCommandError: TypeAlias = bes_device.BesCommandError
BesDeviceInfo: TypeAlias = bluetooth_reference_device_base.BluetoothInfo


@dataclasses.dataclass(frozen=True)
class Config:
  """A configuration object for configuring BES manager.

  Attributes:
    bluetooth_address: The unique BRE/DR address (classic Bluetooth address) of
      of the BES devboard.
      NOTE: This field will be deprecated in the next version firmware.
    serial_port: The serial port location on the Raspberry Pi to communicate
      with BES devboard.
    baud_rate: The baud rate of the serial connection.
  """

  bluetooth_address: str
  serial_port: str = '/dev/ttyUSB0'
  baud_rate: int = 1152000


class BesManager(base_service.BaseService):
  """Manager for BES devboard Bluetooth reference device.

  IMPORTANT: This class is DEPRECATED. Please use `bes_device.BesDevice`
  controller directly.

  This is a thin wrapper of `bes_device.BesDevice` to provide a Mobly service
  interface.

  Attributes:
    is_alive: True if the BES manager is alive; False otherwise.
    bluetooth_address: The unique BRE/DR address (classic Bluetooth address)
      configuered in the Bluetooth dongle on the device.
  """

  _bes_device_instance: bes_device.BesDevice | None = None

  bluetooth_address: str

  def __init__(
      self,
      device: 'rpi_device.RpiDevice',  # pyrefly: ignore[missing-attribute]
      configs: Config,
  ) -> None:
    """Initializes the instance."""
    self._device = device
    self._configs = configs
    self._bes_device_instance = None
    self.bluetooth_address = self._configs.bluetooth_address

  @property
  def is_alive(self) -> bool:
    """True if the remote BES connection is alive; False otherwise."""
    return self._bes_device_instance is not None

  @property
  def remote_pids(self) -> list[int]:
    """The PIDs this service left running on the Raspberry Pi."""
    if self._bes_device_instance is None:
      return []
    return self._bes_device_instance.remote_pids

  @property
  def version(self) -> str:
    """The version of the BES devboard."""
    if self._bes_device_instance is None:
      raise Error(_NOT_STARTED_MESSAGE)
    return self._bes_device_instance.version

  @property
  def is_v2(self) -> bool:
    """True if the BES devboard is v2; False otherwise."""
    if self._bes_device_instance is None:
      raise Error(_NOT_STARTED_MESSAGE)
    return self._bes_device_instance.is_v2

  def start(self) -> None:
    """Starts the BES manager.

    This method starts the underlaying BesDevice instance.

    Raises:
      Error: If the BES manager has already started.
    """
    if self._bes_device_instance is not None:
      raise Error(_ALREADY_STARTED_MESSAGE)

    self._bes_device_instance = bes_device.BesDevice(
        bes_device_config.DeviceConfig(
            bluetooth_address=self._configs.bluetooth_address,  # pyrefly: ignore[missing-attribute]
            serial_port=self._configs.serial_port,  # pyrefly: ignore[missing-attribute]
            remote_mode=True,
            hostname=self._device.config.hostname,
            ssh_port=self._device.config.ssh_port,
            username=self._device.config.username,
            password=self._device.config.password,
            keyfile=self._device.config.keyfile,
            proxy_command=self._device.config.proxy_command,
        ),
    )

  def stop(self) -> None:
    """Stops the running BES manager.

    Converges on a stopped state whatever `BesDevice.destroy()` does. Holding
    on to the instance after a failed teardown would leave `is_alive` True with
    no way back, and `start()` refuses to run in that state.
    """
    if self._bes_device_instance is None:
      return

    try:
      self._bes_device_instance.destroy()
    except ssh_lib.SSH_ERRORS:
      self._device.log.warning(
          'Failed to tear down the BES devboard.', exc_info=True
      )
    finally:
      self._bes_device_instance = None

  def __del__(self) -> None:
    """Cleans up the instance."""
    self.stop()

  def create_output_excerpts(
      self, test_info: runtime_test_info.RuntimeTestInfo
  ) -> list[Any]:
    """Creates excerpts for specified logs and returns the excerpt paths.

    Args:
      test_info: `self.current_test_info` in a Mobly test.

    Returns:
      The list of absolute paths to excerpt files.

    Raises:
      Error: If the log clipper is uninitialized.
    """
    if self._bes_device_instance is None:
      return []

    return self._bes_device_instance.create_output_excerpts(test_info)

  def _needs_recovery(self, instance: bes_device.BesDevice | None) -> bool:
    """Whether `instance` can no longer reach the board and must be replaced.

    Args:
      instance: The board instance a caller was about to use, or None if the
        manager is not running.

    Returns:
      True if the board's own SSH session is gone, or if the manager is not
      running only because an earlier recovery could not reconnect.
    """
    if instance is None:
      return self._device.has_pending_recovery
    return not instance.ssh.is_connected

  def _recover_if_session_lost(self) -> None:
    """Recovers the device if the board's own SSH session is gone.

    `BesDevice` talks to the board over its own SSH session, separate from
    the Raspberry Pi controller's, and never checks that session before using
    it. When the network to the Raspberry Pi drops, that session dies with the
    controller's, but nothing would notice until a BES command failed on it,
    and every later command would fail the same way. Recovery goes through
    the controller rather than reopening only this board's session, because
    the controller also reaps the serial port reader the old session left
    running, which would otherwise corrupt the new reader's stream.
    """
    instance = self._bes_device_instance
    if not self._needs_recovery(instance):
      return
    with self._device.recovery_lock:
      # Both boards of a TWS pair notice the same lost session. Whoever got
      # the lock first already replaced this board's instance.
      if self._bes_device_instance is not instance:
        return
      self._device.log.warning(
          'The SSH session to BES board %s is gone, recovering the device'
          ' before sending the next command.',
          self.bluetooth_address,
      )
      # Hand over this board's remote processes: a manager used on its own is
      # not registered on the device, so the recovery would not find them, and
      # `stop()` on a dead session cannot reach them either.
      self._device.recover_services(orphan_remote_pids=self.remote_pids)
      if self._bes_device_instance is instance and instance is not None:
        # Recovery restarts only the services registered on the device. A
        # manager used on its own has to restart itself.
        self.stop()
        self.start()

  def __getattr__(self, name):
    """Wrapper to turn method calls into BES API calls."""

    def api_call(*args, **kwargs):
      self._recover_if_session_lost()
      if self._bes_device_instance is None:
        raise Error(_NOT_STARTED_MESSAGE)
      if not hasattr(self._bes_device_instance, name):
        raise AttributeError(f'BES device has no attribute "{name}"')
      func = getattr(self._bes_device_instance, name)
      return func(*args, **kwargs)

    return api_call
