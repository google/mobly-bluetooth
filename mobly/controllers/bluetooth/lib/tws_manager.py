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

"""The module to manage two BES devboards as a pair of TWS earbuds."""

import dataclasses
import enum
from typing import Any

from mobly import runtime_test_info
from mobly import utils
from mobly.controllers.android_device_lib.services import base_service

from mobly.controllers.bluetooth.lib import bes_manager


# Avoid directly importing rpi_device, which causes circular
# dependencies
RpiDevice = Any

# Error messages
_ALREADY_STARTED_MESSAGE = 'TWS manager has already started.'
_INVALID_BLUETOOTH_ADDRESS_MESSAGE = (
    'Invalid primary Bluetooth address. The last byte of primary Bluetooth'
    ' should be odd number.'
)


@enum.unique
class AncMode(enum.StrEnum):
  """The supported ANC modes."""

  OFF = 'off'
  ON = 'on'
  TRANSPARENT = 'transparent'


class Error(Exception):
  """Raised for exceptions that occurred in TwsManager."""


@dataclasses.dataclass(frozen=True)
class Config:
  """A configuration object for configuring TWS manager.

  Attributes:
    bluetooth_address: The unique BRE/DR address (classic Bluetooth address) of
      of the primiary BES devboard.
    serial_port: The serial port location on the Raspberry Pi to communicate
      with BES devboard.
    secondary_serial_port: The serial port for the secondary BES devboard.
    baud_rate: The baud rate of the serial connection.
  """

  bluetooth_address: str
  serial_port: str = '/dev/ttyUSB0'
  secondary_serial_port: str = '/dev/ttyUSB1'
  baud_rate: int = 1152000


class TwsManager(base_service.BaseService):
  """Manager for controlling two BES boards as a pair of TWS earbuds.

  Attributes:
    is_alive: True if the BES manager is alive; False otherwise.
    bluetooth_address_primary: The unique BRE/DR address (classic Bluetooth
      address) of the primary BES board.
    bluetooth_address_secondary: The unique BRE/DR address (classic Bluetooth
      address) of the secondary BES board.
  """

  _primary_device: bes_manager.BesManager
  _secondary_device: bes_manager.BesManager

  bluetooth_address_primary: str
  bluetooth_address_secondary: str

  def __init__(self,
               device: 'RpiDevice',
               configs: Config) -> None:
    """Initializes the instance."""
    self._device = device
    self._configs = configs

    if int(self._configs.bluetooth_address[-1], 16) % 2 == 0:
      raise ValueError(_INVALID_BLUETOOTH_ADDRESS_MESSAGE)

    self.bluetooth_address_primary = self._configs.bluetooth_address
    self.bluetooth_address_secondary = (
        f'{self._configs.bluetooth_address[:-1]}'
        f'{(int(self._configs.bluetooth_address[-1], 16) - 1):x}'
    )

    self._primary_device = bes_manager.BesManager(
        self._device,
        bes_manager.Config(
            serial_port=configs.serial_port,
            bluetooth_address=self.bluetooth_address_primary,
        ),
    )
    self._secondary_device = bes_manager.BesManager(
        self._device,
        bes_manager.Config(
            serial_port=configs.secondary_serial_port,
            bluetooth_address=self.bluetooth_address_secondary,
        ),
    )

  @property
  def is_alive(self) -> bool:
    """True if the remote BES connection is alive; False otherwise."""
    # The connection is alive if we are able to read from the serial port.
    return self._primary_device.is_alive or self._secondary_device.is_alive

  @property
  def remote_pids(self) -> list[int]:
    """The PIDs this service left running on the Raspberry Pi."""
    return self._primary_device.remote_pids + self._secondary_device.remote_pids

  @property
  def version(self) -> str:
    """The version of the BES devboards."""
    if self.is_alive:
      return (
          f'L: {self._secondary_device.version}, '
          f'R: {self._primary_device.version}'
      )
    else:
      return 'unknown'

  @property
  def is_v2(self) -> bool:
    """True if the BES devboards are v2; False otherwise."""
    if self.is_alive:
      return self._primary_device.is_v2 and self._secondary_device.is_v2
    return False

  def start(self) -> None:
    """Starts the BES manager.

    This method starts the remote process to stream the logs from BES devboard.
    And gets the information of BES devboard.

    Raises:
      Error: If the BES manager has already started.
    """
    if self.is_alive:
      raise Error(_ALREADY_STARTED_MESSAGE)

    try:
      self._primary_device.start()
      self._secondary_device.start()
    except Exception:
      self.stop()
      raise

  def stop(self) -> None:
    """Stops the running BES manager.

    Both boards are stopped even if the first teardown raises. Leaving the
    secondary running would keep `is_alive` True with no way back, and
    `start()` refuses to run in that state.
    """
    try:
      if self._primary_device.is_alive:
        self._primary_device.stop()
    finally:
      if self._secondary_device.is_alive:
        self._secondary_device.stop()

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
    """
    if not self.is_alive:
      return []

    return self._primary_device.create_output_excerpts(
        test_info
    ) + self._secondary_device.create_output_excerpts(test_info)

  def _pair_tws_after_reboot(self) -> None:
    """Pairs the two BES devboards as a pair of TWS earbuds after reboot."""
    if not self.is_v2:
      return
    utils.concurrent_exec(
        lambda d: d.enable_tws(),
        param_list=[[self._primary_device], [self._secondary_device]],
        raise_on_exception=True,
    )
    utils.concurrent_exec(
        lambda d: d.pair_tws(),
        param_list=[[self._primary_device], [self._secondary_device]],
        raise_on_exception=True,
    )

  def reboot(self) -> None:
    """Soft reboots the device."""
    utils.concurrent_exec(
        lambda d: d.reboot(),
        param_list=[[self._primary_device], [self._secondary_device]],
        raise_on_exception=True,
    )
    self._pair_tws_after_reboot()

  def factory_reset(self) -> None:
    """Factory resets the BES devboard."""
    utils.concurrent_exec(
        lambda d, wait_for_access: d.factory_reset(wait_for_access),
        param_list=[
            [self._primary_device, True],
            [self._secondary_device, False],
        ],
        raise_on_exception=True,
    )
    self._pair_tws_after_reboot()

  def get_device_info(
      self, target: str = 'primary'
  ) -> bes_manager.BesDeviceInfo:
    """Gets the general information of the device.

    Args:
      target: The target board to get the device information of, should be
        'primary' or 'secondary'.

    Returns:
      A BesDeviceInfo object that contains the Bluetooth address and name
      information of the BES board.

    Raises:
      ValueError: If the value of `target` is invalid.
    """
    if target == 'primary':
      return self._primary_device.get_device_info()
    elif target == 'secondary':
      return self._secondary_device.get_device_info()
    else:
      raise ValueError(f'Unknown target: {target}')

  def set_address(self, address: str) -> None:
    """Sets the Bluetooth address of the device.

    This command will reboot the device for the new address to take effect.

    Args:
      address: The new primary Bluetooth address to be set on the BES board.

    Raises:
      ValueError: If the given primary address is invalid.
    """
    if int(address[-1], 16) % 2 == 0:
      raise ValueError(_INVALID_BLUETOOTH_ADDRESS_MESSAGE)

    self._primary_device.set_address(address)
    self.bluetooth_address_primary = address

    secondary_address = f'{address[:-1]}{(int(address[-1], 16) - 1):x}'
    self._secondary_device.set_address(secondary_address)
    self.bluetooth_address_secondary = secondary_address

  def set_name(self, bluetooth_name: str, ble_name: str) -> None:
    """Sets the classic Bluetooth name and BLE name of the device.

    This command will reboot the device for the new names to take effect.

    Args:
      bluetooth_name: The new classic Bluetooth name of the device.
      ble_name: The new BLE name of the device.
    """
    self._primary_device.set_name(bluetooth_name, ble_name)

  def set_fp_params(self, model_id: str, private_key: str) -> None:
    """Sets the Fast Pair parameters of the device.

    This command will reboot the device for the new parameters to take effect.

    Args:
      model_id: Google Fast Pair model ID (format XXXXXX or 0xXXXXXX).
      private_key: Google Fast Pair anti-spoofing key (base64, uncompressed).
    """
    self._primary_device.set_fp_params(model_id, private_key)

  def set_name_and_fp_params(
      self,
      bluetooth_name: str,
      ble_name: str,
      model_id: str,
      private_key: str,
  ) -> None:
    """Sets the Bluetooth name and Fast Pair parameters of the device.

    This command will reboot the device for the settings to take effect.

    Args:
      bluetooth_name: The new classic Bluetooth name of the device.
      ble_name: The new BLE name of the device.
      model_id: Google Fast Pair model ID (format XXXXXX or 0xXXXXXX).
      private_key: Google Fast Pair anti-spoofing key (base64, uncompressed).
    """
    self._primary_device.set_name_and_fp_params(
        bluetooth_name, ble_name, model_id, private_key
    )

  def start_pairing_mode(self, timeout: int | None = None) -> None:
    """Puts the devboard into pairing mode.

    Args:
      timeout: The optional timeout to stop pairing mode. The device will exit
        the pairing mode after this timeout has elapsed. If None, the device
        will keep staying in pairing mode.
    """
    self._primary_device.start_pairing_mode(timeout)

  def stop_pairing_mode(self) -> None:
    """Exits the pairing mode."""
    self._primary_device.stop_pairing_mode()

  def pair_tws(self) -> None:
    """Pairs the two BES devboards as a pair of TWS earbuds."""
    utils.concurrent_exec(
        lambda d: d.pair_tws(),
        param_list=[[self._primary_device], [self._secondary_device]],
        raise_on_exception=True,
    )

  def connect(self, address: str) -> None:
    """Connects the BES devboard to the given address.

    Args:
      address: The target classic Bluetooth address to connect to.
    """
    self._primary_device.connect(address)

  def disconnect(self, address: str) -> None:
    """Disconnects from a given address.

    Args:
      address: The target classic Bluetooth address to disconnect from.
    """
    self._primary_device.disconnect(address)

  def clear_paired_devices(self) -> None:
    """Clears all of the paired devices.

    This method disconnects and unpairs all the paired devices.
    """
    self._primary_device.clear_paired_devices()

  def set_component_number(self, number: int) -> None:
    """Sets the number of components of the device.

    Component number refers to the device number in the CSIP
    (https://www.bluetooth.com/specifications/specs/csip-1-0-1/) Coordinated
    Set. If 1, the device has a single component. It shows 1 address on Android
    after pairing. If 2, the device has two components, one CSIP Set Coordinator
    (primary earbud), one CSIP Set Member (secondary earbud). And shows 2
    addresses on Android after pairing.

    Args:
      number: The target number of components.
    """
    self._primary_device.set_component_number(number)

  def get_in_box_state(self) -> bool:
    """Gets if the TWS earpods are in the box."""
    return self._primary_device.get_in_box_state()

  def set_in_box_state(self, in_box: bool) -> None:
    """Sets the box state of the TWS earpods."""
    utils.concurrent_exec(
        lambda d: d.set_in_box_state(in_box),
        param_list=[[self._primary_device], [self._secondary_device]],
        raise_on_exception=True,
    )

  def get_on_head_state(self) -> bool:
    """Gets if the TWS earpods are on head."""
    return self._primary_device.get_on_head_state()

  def set_on_head_state(self, on_head: bool) -> None:
    """Sets the on head status of the TWS earpods."""
    utils.concurrent_exec(
        lambda d: d.set_on_head_state(on_head),
        param_list=[[self._primary_device], [self._secondary_device]],
        raise_on_exception=True,
    )

  def open_box(self) -> None:
    """Opens the charging box."""
    utils.concurrent_exec(
        lambda d: d.open_box(),
        param_list=[[self._primary_device], [self._secondary_device]],
        raise_on_exception=True,
    )

  def fetch_out(self) -> None:
    """Fetches the TWS earpods out of the charging box."""
    utils.concurrent_exec(
        lambda d: d.fetch_out(),
        param_list=[[self._primary_device], [self._secondary_device]],
        raise_on_exception=True,
    )

  def wear_up(self) -> None:
    """Puts the TWS earpods on head."""
    utils.concurrent_exec(
        lambda d: d.wear_up(),
        param_list=[[self._primary_device], [self._secondary_device]],
        raise_on_exception=True,
    )

  def wear_down(self) -> None:
    """Takes the TWS earpods off head."""
    utils.concurrent_exec(
        lambda d: d.wear_down(),
        param_list=[[self._primary_device], [self._secondary_device]],
        raise_on_exception=True,
    )

  def power_on(self) -> None:
    """Soft powers on the TWS earpods."""
    utils.concurrent_exec(
        lambda d: d.power_on(),
        param_list=[[self._primary_device], [self._secondary_device]],
        raise_on_exception=True,
    )

  def power_off(self) -> None:
    """Soft powers off the TWS earpods."""
    utils.concurrent_exec(
        lambda d: d.power_off(),
        param_list=[[self._primary_device], [self._secondary_device]],
        raise_on_exception=True,
    )

  def put_in(self) -> None:
    """Puts the TWS earpods into the charging box."""
    utils.concurrent_exec(
        lambda d: d.put_in(),
        param_list=[[self._primary_device], [self._secondary_device]],
        raise_on_exception=True,
    )

  def close_box(self) -> None:
    """Closes the charging box."""
    utils.concurrent_exec(
        lambda d: d.close_box(),
        param_list=[[self._primary_device], [self._secondary_device]],
        raise_on_exception=True,
    )

  def set_battery_level(
      self,
      left_level: int,
      right_level: int | None = None,
      case_level: int | None = None,
  ) -> None:
    """Sets the fake battery level of the device.

    Args:
      left_level: The fake battery level of the left earbud, in the range of
        0-100. `level=80` represents that the battery is 80% full.
      right_level: The fake battery level of the right earbud, in the range of
        0-100. If None, the left level will be set for both earbuds.
      case_level: The fake battery level of the case, in the range of 0-100.
        If None, there will be no case level set.
    """
    right_level = right_level if right_level is not None else left_level
    self._primary_device.set_battery_level_tws(
        left_level, right_level, case_level
    )

  def get_battery_level(self, target: str = 'primary') -> int | None:
    """Gets the fake battery level of the device.

    Args:
      target: The target board to get the battery level of, should be 'primary',
        'secondary', or 'case'. The case level could be None if the device is
        not v2.

    Returns:
      The fake battery level of the device, in the range of 0-100.
      `level=80` represents that the battery is 80% full.
    """
    if target not in ['primary', 'secondary', 'case']:
      raise ValueError(f'Unknown target: {target}')

    try:
      # Supported only on BES v2 and above.
      left_level, right_level, case_level = (
          self._primary_device.get_battery_level_tws()
      )
      if target == 'primary':
        return right_level
      elif target == 'secondary':
        return left_level
      return case_level
    except (bes_manager.Error, bes_manager.BesRuntimeError):
      pass

    if target == 'primary':
      return self._primary_device.get_battery_level()
    elif target == 'secondary':
      return self._secondary_device.get_battery_level()
    return None

  def get_paired_devices(self) -> list[dict[str, str]]:
    """Gets the list of paired devices information.

    Returns:
      A list of dicts containing the paired device name and address. The keys in
      the result dict are aligned with the Android `btGetPairedDevices` result.
      Example:
      [
        {'Name': 'Phone A', 'Address': '00:11:22:33:44:55'},
        {'Name': 'Phone B', 'Address': '66:77:88:99:AA:BB'},
      ]
    """
    return self._primary_device.get_paired_devices()

  def media_play(self) -> None:
    """Plays the media stream."""
    self._primary_device.media_play()

  def media_pause(self) -> None:
    """Pauses the media stream."""
    self._primary_device.media_pause()

  def media_next(self) -> None:
    """Jumps to the next media track."""
    self._primary_device.media_next()

  def media_prev(self) -> None:
    """Jumps to the previous media track."""
    self._primary_device.media_prev()

  def volume_up(self, level: int = 1) -> None:
    """Increases the device volume.

    This method simulates a press on the `Vol+` button. Each simulated press on
    the button will increase the volume by `level` unit.

    Args:
      level: The number of volume levels to increase.
    """
    self._primary_device.volume_up(level)

  def volume_down(self, level: int = 1) -> None:
    """Increases the device volume.

    This method simulates a press on the `Vol-` button. Each simulated press on
    the button will decrease the volume by `level` unit.

    Args:
      level: The number of volume levels to decrease.
    """
    self._primary_device.volume_down(level)

  def set_volume(self, level: int) -> None:
    """Sets the volume of the device to a given level.

    Args:
      level: The target volume level, in the range of [0, 127].
    """
    self._primary_device.set_volume(level)

  def get_volume(self) -> int:
    """Gets the volume of the device.

    Returns:
      The volume level of the device.
    """
    return self._primary_device.get_volume()

  def call_accept(self) -> None:
    """Accepts a phone call."""
    self._primary_device.call_accept()

  def call_decline(self) -> None:
    """Declines a phone call or hangs up on a current phone call."""
    self._primary_device.call_decline()

  def call_hold(self) -> None:
    """Holds the current call."""
    self._primary_device.call_hold()

  def call_redial(self) -> None:
    """Redials the last phone call."""
    self._primary_device.call_redial()

  def set_anc_mode(self, mode: AncMode) -> None:
    """Sets the ANC mode of the device.

    Args:
      mode: The target ANC mode.
    """
    self._primary_device.set_anc_mode(mode)
