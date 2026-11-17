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

from mobly_bluetooth.controllers import bes_device
from mobly_bluetooth.controllers import bes_device_config
from mobly_bluetooth.controllers import bluetooth_reference_device_base
from mobly_bluetooth.lib import ssh as ssh_lib

# Avoid directly importing rpi_device, which causes circular
# dependencies
_RpiDevice: TypeAlias = Any

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
  # True between `pause()` and `resume()`: the instance is kept, but it holds
  # no session to the board's host.
  _is_paused: bool = False
  # `RpiDevice.ssh_generation` at the time the board's serial log
  # reader was last started. A different value means the controller has
  # replaced its session since, and the reader died with the old one.
  _bound_ssh_generation: int = -1

  bluetooth_address: str

  def __init__(
      self,
      device: _RpiDevice,
      configs: Config,
  ) -> None:
    """Initializes the instance."""
    self._device = device
    self._configs = configs
    self._bes_device_instance = None
    self._is_paused = False
    self.bluetooth_address = self._configs.bluetooth_address

  @property
  def is_alive(self) -> bool:
    """True if the BES manager is running and attached to the board's host."""
    return self._bes_device_instance is not None and not self._is_paused

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

    This method starts the underlaying BesDevice instance, which initializes
    the board. A paused manager is started afresh rather than resumed: a
    caller that asks for a start rather than a resume, such as the device
    coming back from a reboot that power-cycled the board, needs the board
    initialized again.

    Raises:
      Error: If the BES manager has already started.
    """
    if self._bes_device_instance is not None:
      if not self._is_paused:
        raise Error(_ALREADY_STARTED_MESSAGE)
      self.stop()

    # The board shares the controller's SSH session rather than opening one of
    # its own, so there is a single session to the Raspberry Pi and a single
    # answer to whether it is alive; see `bes_device.SshHost`.
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
        ssh_host=self._device,
    )
    self._bound_ssh_generation = self._device.ssh_generation

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
      self._is_paused = False

  def pause(self) -> None:
    """Drops the SSH session to the board's host, leaving the board as it is.

    The board only changes state through BES commands and through power, so
    losing the session that carries its serial stream is not a reason to
    re-initialize it. Pausing keeps the `BesDevice` and everything it knows
    about the board, and `resume()` only reopens the session.
    """
    if self._bes_device_instance is None or self._is_paused:
      return
    try:
      self._bes_device_instance.detach()
    except ssh_lib.SSH_ERRORS:
      self._device.log.warning(
          'Failed to detach from the BES devboard.', exc_info=True
      )
    finally:
      self._is_paused = True

  def resume(self) -> None:
    """Reopens the session to the board's host after `pause()`.

    Raises:
      Whatever reconnecting raised, if the host is still unreachable. The
      manager stays paused, so a later call retries.
    """
    if self._bes_device_instance is None:
      self.start()
      return
    if not self._is_paused:
      return
    self._bes_device_instance.reconnect()
    self._bound_ssh_generation = self._device.ssh_generation
    self._is_paused = False

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
    if self._bes_device_instance is None or self._is_paused:
      # A paused board has let go of its log reader and clipper; there is
      # nothing to excerpt until it is resumed.
      return []

    return self._bes_device_instance.create_output_excerpts(test_info)

  def _needs_recovery(self) -> bool:
    """Whether the board's serial stream is no longer carried by a live session.

    Returns:
      True if the manager is paused, the controller's session is dead, or the
      controller has replaced its session since this board's log reader was
      started (another caller's recovery does that), which left the reader
      behind on the old one. A manager that is not running only because an
      earlier recovery could not reconnect reports True as well.
    """
    if self._bes_device_instance is None:
      return self._device.has_pending_recovery
    return (
        self._is_paused
        or not self._device.is_ssh_connected
        or self._device.ssh_generation != self._bound_ssh_generation
    )

  def _recover_if_session_lost(self) -> None:
    """Recovers the device if the session carrying the board's stream is gone.

    The board talks over the controller's SSH session. When that session
    drops, nothing would notice until a BES command failed on it, and every
    later command would fail the same way, so the check happens before each
    command instead. Recovery goes through the controller, because only it
    can reopen the session, and it also reaps the serial port reader the old
    session left running, which would otherwise corrupt the new reader's
    stream.

    The board itself is left alone throughout: only the session is replaced,
    so the board comes back in the state it was in before the drop.
    """
    # The check runs under the lock: a recovery another caller is in the
    # middle of, e.g. for the other board of a TWS pair, is then waited out
    # and seen in full, rather than racing a check made moments before it
    # replaced the session. Uncontended, the lock costs nothing.
    with self._device.recovery_lock:
      if not self._needs_recovery():
        return
      self._device.log.warning(
          'The SSH session to BES board %s is gone, recovering the device'
          ' before sending the next command.',
          self.bluetooth_address,
      )
      self._device.recover_services()
      if not self._needs_recovery():
        return
      # Recovery brings back only the services registered on the device. A
      # manager used on its own has to do it itself, by the same rule the
      # device applies: resume on the board state that was there, unless the
      # host rebooted meanwhile, which power-cycled the board too.
      if self._device.rebooted_since_last_session:
        self.stop()
        self.start()
      else:
        self.pause()
        self.resume()

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
