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

"""Mobly controller module for a BES Bluetooth device (BES dev board)."""

from __future__ import annotations

from collections.abc import Sequence
import dataclasses
import datetime
import enum
import logging
import os
import pathlib
import re
import shutil
import subprocess
import time
from typing import Any, Protocol

import dacite
import immutabledict
from mobly import logger as mobly_logger
from mobly import runtime_test_info
from mobly import signals
from mobly import utils as mobly_utils
from mobly_bluetooth.controllers import bes_device_config
from mobly_bluetooth.controllers import bluetooth_reference_device_base
from mobly_bluetooth.lib import audio_recorder
from mobly_bluetooth.lib import bes_constants as constants
from mobly_bluetooth.lib import bes_hid_tool
from mobly_bluetooth.lib import bes_log_pubsub
from mobly_bluetooth.lib import file_clipper
from mobly_bluetooth.lib import ssh as ssh_lib
from mobly_bluetooth.lib import utils
from typing_extensions import override

# This is used in the config file located in the test lab's home directory.
MOBLY_CONTROLLER_CONFIG_NAME = 'BesDevice'

# Error messages used in this module.
_DEVICE_CONFIG_ERROR_MSG = (
    'Failed to parse device configs when creating BES devices: '
)
_SSH_NOT_REMOTE_MODE_ERROR_MSG = (
    'Unable to start SSH connection because `remote_mode` property is False'
    ' in the device config'
)
_PUBLISHER_NOT_STARTED_MESSAGE = 'BES log publisher not started.'
_LOG_CLIPPER_NOT_STARTED_MESSAGE = 'BES log clipper not started.'
_NO_LOG_OUTPUT_MESSAGE = (
    'No log output from BES board. Please power on the board by pressing the'
    ' `PWR` and `RST` button.'
)
_NO_OUTPUT_FILENAME_MESSAGE = 'No output filename. Run start() first.'
_AUDIO_RECORDER_NOT_CONFIGURED_MESSAGE = (
    'Audio recorder is not configured. Please set `audio_configs` in the device'
    ' config.'
)
# Bound on the housekeeping `pkill` run before a remote process is started, so
# a network that degrades right then cannot hang the caller.
_REMOTE_COMMAND_TIMEOUT_SECONDS = 30.0
_RECORDING_FILE_NOT_FOUND_MESSAGE = (
    'Recording file not found, there might be something wrong with the audio'
    ' recorder. Please check the audio recorder log.'
)

# Default filename of the log file on the host machine.
_DEFAULT_LOG_FILENAME = 'bes_log_{timestamp}.txt'
# Default baud rate of the serial connection
_DEFAULT_BAUD_RATE = 1152000

# The commands to communicate with BES devboard via serial connection
_SET_TTY_PERMISSIONS_COMMAND = 'sudo chmod a+rw {serial_port}'
_CONFIG_SERIAL_PORT_COMMAND = 'stty -echo -F {serial_port} speed {speed} raw'
_READ_SERIAL_COMMAND = 'cat {serial_port}'
# Kills any serial reader of ours still holding the port, see
# `_claim_serial_port()`. Anchored so that it matches the reader itself and
# not the shell this command runs in.
_CLAIM_SERIAL_PORT_COMMAND = (
    "pkill -u {username} -f '^cat {serial_port}$' || true"
)
# Same for the audio capture device, see `_claim_capture_device()`.
# Only these PCM name prefixes open a card exclusively; see
# `_claim_capture_device()` for why the others are left alone.
_EXCLUSIVE_CAPTURE_DEVICE_PREFIXES = ('hw:', 'plughw:')
_CLAIM_CAPTURE_DEVICE_COMMAND = (
    "pkill -u {username} -f '^arecord .* -D {capture_device} ' || true"
)
_WRITE_SERIAL_COMMAND = 'echo -e -n "{command}" > {serial_port}'
_WRITE_SERIAL_COMMAND_ON_HOST = 'echo {command} > {serial_port}'

# The wait time for a command to process
_SEND_BES_COMMAND_INTERVAL = datetime.timedelta(seconds=1)
_BES_EXECUTION_TIMEOUT = datetime.timedelta(seconds=10)
_REBOOT_TIMEOUT = datetime.timedelta(seconds=30)
_REBOOT_WAIT_TIME = datetime.timedelta(seconds=3)

# Regexs for parsing BES response
_REBOOT_DONE_PATTERN = re.compile(r'.*bt_stack_init_done.*')
_COMMAND_NOT_SUPPORTED_PATTERN = re.compile(r'.*command not supported!.*')
_FIRMWARE_BUILD_DATE_PATTERN = re.compile(r'.*BUILD_DATE=(?P<build_date>.*)')
_FIRMWARE_VERSION_PATTERN = re.compile(r'.*REV_INFO=(?P<version>.*)')
_DEVICE_INFO_PATTERN = re.compile(r'(?P<key>.*): (?P<value>.*)')
_GET_VOLUME_PATTERN = re.compile(r'volume=(?P<level>\d+)')
_GET_BATTERY_LEVEL_PATTERN = re.compile(r'.*battery_level: (?P<level>\d+).*')
_GET_BATTERY_LEVEL_TWS_PATTERN = re.compile(
    r'Main ear battery_level: (?P<left_level>\d+)\nRemote ear battery_level:'
    r' (?P<right_level>\d+)(\nCase battery_level: (?P<case_level>\d+))?.*'
)
_PAIRED_DEVICE_INFO_PATTERN = re.compile(
    r'addr: (?P<addr>.*)\n.*name: (?P<name>.*)'
)
_LE_PAIRED_DEVICE_INFO_PATTERN = re.compile(r'BLE addr: (?P<addr>.*)')
_BOX_STATE_PATTERN = re.compile(r'box_state=(?P<state>.*)')
_ACCESS_MODE_PATTERN = r'.*Access mode changed to {access_mode}.*'

# NV custom parameter flash address suffixes logged on boot for Fast Pair
# parameters (e.g. 0x307fb00e on v1 and 0x307eb00e on v2).
_FP_MODEL_ID_BOOT_ADDRESS = 'b00e'
_FP_PRIVATE_KEY_BOOT_ADDRESS = 'b059'
_SET_FP_PARAMS_MAX_ATTEMPTS = 3

# The latest build date of BES v1 board. The firmware builds after this date are
# v2 builds.
_V1_LATEST_BUILD_DATE = '2024-09-21'

# The key name mapping from BES response to `BluetoothInfo` dataclass
_DEVICE_INFO_KEY_MAP = immutabledict.immutabledict({
    'bt_addr': 'bluetooth_address',
    'ble_addr': 'ble_address',
    'bt_name': 'bluetooth_name',
    'ble_name': 'ble_name',
})


class BesDeviceError(signals.ControllerError):
  """Raised for errors related to the BesDevice controller module."""


class BesRuntimeError(Exception):
  """Raised when BES board encounters error during runtime."""


class CommandTimeoutError(BesRuntimeError):
  """Raised when BES command execution times out."""


class ErrorType(enum.Enum):
  """The type of the BES runtime error."""

  NO_ERROR = 0
  RESOURCE_BUSY_ERROR = 1
  COMMAND_PARAM_ERROR = 2
  COMMAND_NOT_SUPPORT_ERROR = 3
  TIMEOUT_ERROR = 4
  BT_STACK_ERROR = 5


@enum.unique
class AudioChannelMode(enum.IntEnum):
  """Audio channel modes for telecom tests.

  Attributes:
    MONO: Represents mono audio channel mode.
    STEREO: Represents stereo audio channel mode.
  """

  MONO = 1
  STEREO = 2


class BesCommandError(BesRuntimeError):
  """Raised when BES command execution failed on the board."""

  def __init__(self, command: str, error_code: int):
    super().__init__(
        'BES command execution failed on the board. '
        f'Command: {command}. Error type: {ErrorType(error_code)}'
    )


class AccessMode(enum.IntEnum):
  """The type of the BES runtime error."""

  INIT_PAIRING = 0
  DISABLE_PAIRING = 2
  ENABLE_PAIRING = 3


def create(configs: Sequence[dict[str, Any]]) -> list[BesDevice]:
  """Creates BesDevice controller objects.

  Mobly uses this to instantiate BesDevice controller objects from configs.
  The configs come from Mobly configs that look like:

    ```config.yaml
    TestBeds:
    - Name: SampleTestBed
      Controllers:
        BesDevice:
        - serial_port: '/dev/ttyUSB0'
          bluetooth_address: '11:22:33:44:55:66'
    ```

  Each config should have required key-value pair 'serial_port' and
  'bluetooth_address'.

  Args:
    configs: a list of dicts, each representing a configuration for a BES
      device.

  Returns:
    A list of BesDevice objects.

  Raises:
    BesDeviceError: Invalid controller configs are given.
  """
  try:
    device_configs = bes_device_config.from_dicts(configs)
  except Exception as e:
    raise BesDeviceError(_DEVICE_CONFIG_ERROR_MSG, e) from e

  devices = []
  for config in device_configs:
    logging.debug(
        'Creating BES device %s on serial port %s',
        config.bluetooth_address,
        config.serial_port,
    )
    try:
      devices.append(BesDevice(config))
    except Exception:
      mobly_utils.concurrent_exec(
          lambda d: d.destroy(),
          param_list=[[d] for d in devices],
          raise_on_exception=True,
      )
      raise

  return devices


def destroy(devices: Sequence[BesDevice]) -> None:
  """Destroys BesDevice objects.

  Mobly uses this to destroy BesDevice objects created by `create`.

  Args:
    devices: list of BesDevice.
  """
  for device in devices:
    try:
      device.destroy()
    except Exception:
      logging.exception('Failed to clean up device properly: %s', repr(device))


class SshHost(Protocol):
  """A controller for the host the board is attached to, lending its session.

  `BesDevice` normally opens an SSH session of its own to the host. A caller
  that already holds a session to the same host, such as the Raspberry Pi
  controller behind `bes_manager`, can hand itself in instead, so that there
  is one session and one source of truth about whether it is alive. The host
  keeps owning the session: `BesDevice` never closes it.
  """

  @property
  def ssh(self) -> ssh_lib.SSHProxy:
    """The host's live SSH session, reconnected by the host if needed."""

  @property
  def is_ssh_connected(self) -> bool:
    """True if the host holds a live SSH session, without opening one."""


class BesDevice(bluetooth_reference_device_base.BluetoothReferenceDeviceBase):
  """Mobly controller for a BES device.

  The BES device is a Bluetooth development board. Each object of this class
  represents one BES device in Mobly.

  The BES board can be attached to a local Linux workstation that runs as Mobly
  host, or a remote Linux-based devive such as Raspberry Pi that can be SSH from
  the Mobly host.
  If the BES board is not directly plugged to the Mobly host, users should set
  `remote_mode: True` and specify the `hostname` field in the device config.
  Then a serial connection will be built over SSH to control the board. If the
  BES board is directly plugged to the Mobly host, users should set
  `remote_mode: False`. Then the serial connetion will be built locally.

  Attributes:
    bluetooth_address: The unique BRE/DR address (classic Bluetooth address) of
      the device.
    version: The version of the firmware on the BES board.
    config: The configurations for the device.
    debug_tag: A string that represents this device in the debug info.
    log_path: The local path on the host to save logs in the test.
    log: A logger adapted from root logger with an added prefix
      '[BesDevice|<bluetooth_address>] 'specific to a test device.
  """

  bluetooth_address: str
  _version: str | None = None
  _build_date: datetime.datetime | None = None
  _fp_model_id: str | None = None
  _fp_private_key: str | None = None

  _debug_tag: str
  # The session this device opened for itself. None in local mode and when
  # the session is borrowed from `_ssh_host`.
  _ssh: ssh_lib.SSHProxy | None = None
  _ssh_host: SshHost | None = None

  _log_collecting_process: (
      subprocess.Popen[Any] | ssh_lib.RemotePopen | None
  ) = None
  _log_clipper: file_clipper.FileClipper | None = None
  _publisher: bes_log_pubsub.BesLogPublisher | None = None
  _output_filename: str | None = None

  _audio_recorder: audio_recorder.AudioRecorder | None = None
  # The settings the running recording was actually started with, which can
  # differ from `config.audio_configs` when the caller overrode them.
  _audio_channels: int | None = None
  _audio_sample_rate: int | None = None
  # Whether a recording was running when the session was last detached, so
  # `reconnect()` can bring it back.
  _audio_recording_was_active: bool = False
  # True between `detach()` and a successful `reconnect()`.
  _detached: bool = False

  def __init__(
      self,
      config: bes_device_config.DeviceConfig,
      ssh_host: SshHost | None = None,
  ) -> None:
    """Initializes the device and brings the board up.

    Args:
      config: The device config.
      ssh_host: In remote mode, a controller of the host the board is attached
        to whose SSH session this device should use instead of opening its own.
        See `SshHost`. Ignored in local mode.
    """
    self.config = config
    self.bluetooth_address = self.config.bluetooth_address
    self._ssh_host = ssh_host if self.config.remote_mode else None

    # logging.log_path only exists when this is used in a Mobly test run.
    log_path_base = getattr(logging, 'log_path', '/tmp/logs')
    device_log_directory_name = mobly_logger.sanitize_filename(
        f'BesDevice_{config.bluetooth_address}'
    )
    self._debug_tag = config.bluetooth_address
    self.log_path = os.path.join(log_path_base, device_log_directory_name)
    mobly_utils.create_dir(self.log_path)
    self.log = mobly_logger.PrefixLoggerAdapter(
        logging.getLogger(),
        {
            mobly_logger.PrefixLoggerAdapter.EXTRA_KEY_LOG_PREFIX: (
                f'[BesDevice|{self.debug_tag}]'
            )
        },
    )

    try:
      self.start_bes_connection()
      self._init_audio_recorder()
      return
    except Exception:
      self.destroy()
      self.log.warning(
          'Failed to initialize BES device. Start to try hard power on the'
          ' board. If the board still fails to initialize, please contact lab'
          ' team to manually recover the board.'
      )

    try:
      if self.config.remote_mode:
        bes_hid_tool.power_on_remote(
            self.ssh, self.config.username, self.config.password
        )
      else:
        bes_hid_tool.power_on_local()
      self.start_bes_connection()
      self._init_audio_recorder()
    except Exception:
      self.destroy()
      raise

  def start_bes_connection(self) -> None:
    """Starts the BES board connection."""
    # Builds serial connection with the board and starts streaming board
    # output.
    self._start_serial_connection()
    self._generate_output_filename()
    # Initializes Bluetooth address of the board.
    try:
      self._set_bt_address_to_configured_address()
    except (BesCommandError, CommandTimeoutError):
      self.log.exception(
          'Failed to set Bluetooth address to configured address. Retrying.'
      )
      # Retry once if the command fails. If the USB connection is not stable,
      # or the board is not in good state, there might be some junk data in
      # UART input buffer of the board, causing the board to fail to recognize
      # the target command. A retry helps in this case.
      time.sleep(_BES_EXECUTION_TIMEOUT.total_seconds())
      self._set_bt_address_to_configured_address()

  def _init_audio_recorder(self) -> None:
    """Initializes the audio recorder."""
    if self.config.audio_configs is None:
      return
    if self.config.remote_mode:
      self._audio_recorder = audio_recorder.AudioRecorder(self.ssh)
    else:
      self._audio_recorder = audio_recorder.AudioRecorder()

  @property
  def debug_tag(self) -> str:
    """A string that represents this device in the debug info.

    This will be used as part of the prefix of debugging messages emitted by
    this device object, like log lines and the message of DeviceError. Default
    value is the Bluetooth address of the board.
    """
    return self._debug_tag

  @debug_tag.setter
  def debug_tag(self, tag: str) -> None:
    """Sets the debug tag."""
    self.log.set_log_prefix(f'[BesDevice|{tag}]')
    self.log.debug('Logging debug tag set to "%s"', tag)
    self._debug_tag = tag

  def __repr__(self) -> str:
    return f'<BesDevice|{self.debug_tag}>'

  def __del__(self) -> None:
    self.destroy()

  @override
  def destroy(self) -> None:
    """Tears BesDevice object down.

    Converges on a torn-down state whatever the remote side does. Mobly calls
    this from teardown and `__del__` calls it again, so raising here either
    aborts the rest of the teardown or surfaces as an ignored exception during
    garbage collection.
    """
    # Pulling the recording is the only step that needs the remote side, so it
    # is the only one skipped when the session is gone. Everything below it
    # releases local resources and has to run either way.
    session_is_gone = (
        self.config.remote_mode
        and (self._ssh is not None or self._ssh_host is not None)
        and not self.is_session_connected
    )
    try:
      if session_is_gone:
        self.log.debug(
            'Skipping the BES audio recording pull; the SSH session is gone.'
        )
      elif self._audio_recorder is not None and self._audio_recorder.is_alive:
        self.stop_audio_recording(pathlib.Path(self.log_path))
    except ssh_lib.SSH_ERRORS:
      self.log.warning('Failed to stop the BES audio recording.', exc_info=True)

    try:
      # Runs even on a dead session: this is what releases the log clipper, the
      # log publisher and the handle `is_alive` reports on.
      self.stop_bes_connection()
    except ssh_lib.SSH_ERRORS:
      self.log.warning('Failed to tear down the BES board.', exc_info=True)
    finally:
      self._close_own_ssh()

  def _close_own_ssh(self) -> None:
    """Closes the session this device opened for itself, if any.

    A session borrowed from `_ssh_host` is the host's to close. The local
    forward servers of an own session outlive a dead transport, so this runs
    even when the session is already gone.
    """
    if self._ssh is None:
      return
    try:
      self._ssh.disconnect()
    except ssh_lib.SSH_ERRORS:
      self.log.debug('Failed to close the SSH session.', exc_info=True)
    finally:
      self._ssh = None

  @property
  def is_alive(self) -> bool:
    """True if the BES board is alive; False otherwise."""
    return self._log_collecting_process is not None

  @property
  def is_session_connected(self) -> bool:
    """True if this device holds a live SSH session to the remote host.

    Unlike reading `ssh`, this never opens a session, which is what makes it
    safe to call on a device that is detached or whose session just dropped.
    Always True in local mode, where there is no session to lose.
    """
    if not self.config.remote_mode:
      return True
    if self._ssh_host is not None:
      return self._ssh_host.is_ssh_connected
    return self._ssh is not None and self._ssh.is_connected

  def detach(self) -> None:
    """Lets go of the SSH session and everything that only lives on it.

    The board is an independent device: its state changes only through BES
    commands and through power, not through the SSH session that carries the
    serial stream. So when that session is lost, only the log reader, the
    publisher and the audio recorder bound to it need to go; the board, and
    what this object remembers about it (firmware version, build date, the
    recording settings in use), stay as they are. `reconnect()` brings the
    session back without re-initializing the board.

    Sends nothing over the session being let go of: it is presumed dead, and
    a session whose remote FIN is stuck behind a network blackhole still
    reports itself connected, so opening a channel on it would hang. The
    remote processes left behind are not killed here either; the serial port
    and the capture device are claimed again, whatever still holds them, right
    before `reconnect()` reopens them. An own session is closed locally; a
    borrowed one is left to its host.
    """
    if not self.config.remote_mode:
      return
    self._detached = True
    recorder = self._audio_recorder
    self._audio_recording_was_active = (
        recorder is not None and recorder.is_alive
    )
    try:
      self._audio_recorder = None
      self._close_own_ssh()
    finally:
      # Dropped without a kill, see above.
      self._log_collecting_process = None
      self.stop_bes_connection()

  def reconnect(self) -> None:
    """Reopens the SSH session and the log stream after `detach()`.

    Only the session-bound pieces are rebuilt: a new serial log reader, a new
    publisher and clipper, a new audio recorder, and the recording itself if
    one was running, at the settings it ran with. No BES command is sent and
    the board's power is not touched, so the board comes back in exactly the
    state it was in when the session dropped.

    Raises:
      Whatever opening the session raised, if the host is still unreachable.
      The device stays detached, so a later call retries.
    """
    if not self.config.remote_mode:
      return
    if self.is_alive:
      return
    try:
      self._start_serial_connection()
      self._generate_output_filename()
      self._init_audio_recorder()
      if self._audio_recording_was_active:
        self.start_audio_recording()
    except Exception:
      # Back to detached, so that `is_alive` is False and a later call retries
      # from the start instead of returning early on a half-built device. An
      # own session opened on the way is closed too, rather than being handed
      # to the retry possibly dead.
      self._audio_recorder = None
      self._log_collecting_process = None
      self.stop_bes_connection()
      self._close_own_ssh()
      raise
    self._audio_recording_was_active = False
    self._detached = False

  @property
  def version(self) -> str:
    """The version of the firmware on the BES board."""
    if self._version is None or not self._version:
      return 'unknown'
    return self._version

  @property
  def is_v2(self) -> bool:
    """Checks if the BES board is a v2 board."""
    if self._build_date is None:
      return False
    return self._build_date > datetime.datetime.fromisoformat(
        _V1_LATEST_BUILD_DATE
    )

  def get_info(self) -> dict[str, Any]:
    """Gets the build information of the BES board."""
    info = dataclasses.asdict(self.get_device_info())
    info['firmware_version'] = self.version
    return info

  @property
  def ssh(self) -> ssh_lib.SSHProxy:
    """The ssh connection to the device that BES board is connected to."""
    if not self.config.remote_mode:
      raise BesDeviceError(_SSH_NOT_REMOTE_MODE_ERROR_MSG)

    # TODO: Replace the SSH dep with the SSH module for posix
    if self._ssh_host is not None:
      # Never cached here: the host replaces its session when it reconnects,
      # and a copy taken earlier would outlive that.
      return self._ssh_host.ssh
    if self._ssh is None:
      ssh_connection = ssh_lib.SSHProxy(
          self.config.hostname,  # pyrefly: ignore[bad-argument-type]
          self.config.ssh_port,
          self.config.username,
          self.config.password,
          keyfile=self.config.keyfile,
          proxy_command=self.config.proxy_command,
      )
      ssh_connection.connect()
      self._ssh = ssh_connection
    return self._ssh

  def _execute_command(self, command: str) -> None:
    """Executes a command to control the serial connection."""
    if self.config.remote_mode:
      self.ssh.execute_command(command)
    else:
      mobly_utils.run_command(command, shell=True)

  def _configure_serial_connection(self) -> None:
    """Configures the serial connection with BES board."""
    if not self.config.remote_mode:
      # Set the TTY permissions to avoid permission denied error if the board
      # is connected locally to Mobly host.
      self._execute_command(
          _SET_TTY_PERMISSIONS_COMMAND.format(
              serial_port=self.config.serial_port
          )
      )
    self._execute_command(
        _CONFIG_SERIAL_PORT_COMMAND.format(
            serial_port=self.config.serial_port,
            speed=_DEFAULT_BAUD_RATE,
        )
    )

  def _claim_serial_port(self) -> None:
    """Makes sure no earlier reader of ours still holds the serial port.

    Two readers on one serial port corrupt each other's stream, and a reader
    can outlive the session that started it: when the network blackholes, this
    side gives up the session and reconnects while the remote sshd, unaware,
    keeps the old reader alive until its own TCP times out. Rather than
    tracking that reader's PID across sessions and reboots, whoever is about
    to open the port kills whatever reader of ours is on it. Idempotent, and
    a no-op in the common case where nothing is there.
    """
    self.ssh.execute_command(
        _CLAIM_SERIAL_PORT_COMMAND.format(
            username=self.config.username,
            serial_port=re.escape(self.config.serial_port),
        ),
        timeout=_REMOTE_COMMAND_TIMEOUT_SECONDS,
    )

  def _claim_capture_device(self, capture_device: str) -> None:
    """Makes sure no earlier recorder of ours still holds the capture device.

    Same reasoning as `_claim_serial_port()`, but only for `hw:`/`plughw:`
    devices. Those open the card directly and exclusively, so a surviving
    `arecord` keeps it busy and the new one fails to start. Other PCMs, such as
    `default` or a `dsnoop` device, can be opened by several recorders at once
    on purpose; a survivor there does not block the new recorder, and killing
    every `arecord` on that name could stop a recording someone else runs.
    Several recorders on one host, each on its own sound card, are normal and
    unaffected either way, since the match is on the device name.

    Args:
      capture_device: The ALSA capture device the recorder is about to open.
    """
    if not capture_device.startswith(_EXCLUSIVE_CAPTURE_DEVICE_PREFIXES):
      return
    self.ssh.execute_command(
        _CLAIM_CAPTURE_DEVICE_COMMAND.format(
            username=self.config.username,
            capture_device=re.escape(capture_device),
        ),
        timeout=_REMOTE_COMMAND_TIMEOUT_SECONDS,
    )

  def _start_log_streaming(self, local_log_path: pathlib.Path) -> None:
    """Starts streaming the log output from the BES board."""
    command = _READ_SERIAL_COMMAND.format(serial_port=self.config.serial_port)
    if self.config.remote_mode:
      self._claim_serial_port()
      self._log_collecting_process = self.ssh.start_remote_process(
          command, get_pty=True, output_file_path=str(local_log_path)
      )
    else:
      self._log_collecting_process = mobly_utils.start_standing_subprocess(
          f'{command} > {local_log_path}', shell=True
      )

    self._log_clipper = file_clipper.FileClipper(local_log_path)
    self._publisher = bes_log_pubsub.BesLogPublisher(local_log_path)
    self._publisher.start()

  def _wait_and_log_board_time(self, filename: str) -> None:
    """Logs the current time of the BES board for log alignment."""
    if self._publisher is None:
      raise BesDeviceError(_PUBLISHER_NOT_STARTED_MESSAGE)

    with self._publisher.event() as event:
      if event.wait(timeout=_REBOOT_TIMEOUT) and event.trigger is not None:
        self._log_board_time(event.trigger.time, filename)
        return
    raise BesDeviceError(_NO_LOG_OUTPUT_MESSAGE)

  def _log_board_time(self, device_time: str, filename: str) -> None:
    """Logs the current time of the BES board for log alignment."""
    self.log.info(
        f'Log alignment: current BES device time is {device_time}, output'
        f' filename {filename}'
    )

  def _start_serial_connection(self) -> None:
    """Starts the serial connection with BES board."""
    timestamp = mobly_logger.get_log_file_timestamp()
    host_log_path = pathlib.Path(
        self.log_path, _DEFAULT_LOG_FILENAME.format(timestamp=timestamp)
    )
    self._configure_serial_connection()
    self._start_log_streaming(host_log_path)

  def _set_bt_address_to_configured_address(self) -> None:
    """Sets the Bluetooth and BLE address to the configured address."""
    device_info = self.get_device_info()
    if (
        device_info.bluetooth_address != self.bluetooth_address.upper()
        or device_info.ble_address != self.bluetooth_address.upper()
    ):
      self.set_address(self.bluetooth_address)

  def stop_bes_connection(self) -> None:
    """Stops the serial connection with BES board.

    Converges on a stopped state whatever the remote side does. Killing the log
    collecting process is the only step that needs a live SSH session; the log
    clipper and the log publisher are local resources, and `is_alive` reports
    on a local handle, so all three are released even when the remote kill is
    skipped or fails.
    """
    try:
      if self._log_collecting_process is not None:
        if not self.config.remote_mode:
          mobly_utils.stop_standing_subprocess(self._log_collecting_process)
        elif self.is_session_connected:
          self._log_collecting_process.kill()
        else:
          self.log.debug(
              'Skipping the remote log process kill; the SSH session is gone.'
          )
    finally:
      self._log_collecting_process = None

      if self._log_clipper is not None:
        self._log_clipper.close()
        self._log_clipper = None

      if self._publisher is not None:
        self._publisher.stop()
        self._publisher = None

  def start_audio_recording(
      self,
      channels: AudioChannelMode | None = None,
      sample_rate: int | None = None,
  ) -> None:
    """Starts the audio recorder to record audio output of the BES board.

    Args:
      channels: The number of channels to record. If None, the value the last
        recording ran with is reused, falling back to the device config.
      sample_rate: The sample rate to record at. If None, the value the last
        recording ran with is reused, falling back to the device config.

    Raises:
      BesDeviceError: If the audio recorder is not configured.
    """
    if self._audio_recorder is None or self.config.audio_configs is None:
      raise BesDeviceError(_AUDIO_RECORDER_NOT_CONFIGURED_MESSAGE)
    audio_config = self.config.audio_configs
    prefix = f'bes_audio,{self.bluetooth_address}'
    # A recording restarted after a rotation or a recovery has to come back at
    # the settings the test asked for, not at the config defaults, or the
    # captured audio stops matching what the test is measuring.
    use_channels = channels
    if use_channels is None:
      use_channels = self._audio_channels
    if use_channels is None:
      use_channels = audio_config.channels

    use_sample_rate = sample_rate
    if use_sample_rate is None:
      use_sample_rate = self._audio_sample_rate
    if use_sample_rate is None:
      use_sample_rate = audio_config.sample_rate
    raw_name = audio_config.pcm_name
    if raw_name.startswith('hw:'):
      real_capture_device = raw_name.replace('hw:', 'plughw:', 1)
    else:
      real_capture_device = raw_name
    if self.config.remote_mode:
      self._claim_capture_device(real_capture_device)
    self._audio_recorder.start(
        prefix=prefix,
        sample_rate=use_sample_rate,
        sample_format=audio_config.sample_format,
        capture_device_name=real_capture_device,
        channels=use_channels,
        log_dir=self.log_path,
    )
    self._audio_channels = use_channels
    self._audio_sample_rate = use_sample_rate

  def stop_audio_recording(
      self, output_directory: pathlib.Path
  ) -> pathlib.Path:
    """Stops the audio recorder and moves the recording file to the output directory."""
    if self._audio_recorder is None:
      raise BesDeviceError(_AUDIO_RECORDER_NOT_CONFIGURED_MESSAGE)
    self._audio_recorder.stop()
    recording_file = self._audio_recorder.recording_filename
    if recording_file is None:
      raise RuntimeError(_RECORDING_FILE_NOT_FOUND_MESSAGE)
    if self.config.remote_mode:
      self.ssh.pull_to_directory(str(recording_file), str(output_directory))
    else:
      shutil.move(recording_file, output_directory)
    return pathlib.Path(output_directory, recording_file.name)

  @override
  def create_output_excerpts(
      self, test_info: runtime_test_info.RuntimeTestInfo
  ) -> list[pathlib.Path]:
    """Creates excerpts for specified logs and returns the excerpt paths.

    Every step below needs the remote side: rotating the output filename waits
    for the next log line coming over the serial stream, and pulling the audio
    recording copies a file off the board. So this returns no excerpt at all
    when the SSH session is gone, rather than blocking for the reboot timeout
    and then failing the teardown it was called from.

    Args:
      test_info: `self.current_test_info` in a Mobly test.

    Returns:
      The list of absolute paths to excerpt files.

    Raises:
      BesDeviceError: If the log clipper is not started or no output filename.
    """
    if not self.is_session_connected or self._detached:
      self.log.warning(
          'Skipping the BES output excerpts; the SSH session is gone or the'
          ' device is detached from it.'
      )
      return []

    if self._log_clipper is None:
      raise BesDeviceError(_LOG_CLIPPER_NOT_STARTED_MESSAGE)

    if self._output_filename is None:
      raise BesDeviceError(_NO_OUTPUT_FILENAME_MESSAGE)

    mobly_utils.create_dir(test_info.output_path)
    excerpts_path = pathlib.Path(test_info.output_path, self._output_filename)
    self.log.debug('Creating output excerpts as file: %s', excerpts_path)
    self._log_clipper.clip_new_content(excerpts_path)
    self._generate_output_filename()

    excerpts = [excerpts_path]

    # Stop the audio recording if it is running and add the recording file to
    # the excerpts. Then start a new audio recording.
    if self._audio_recorder is not None and self._audio_recorder.is_alive:
      audio_recording_path = self.stop_audio_recording(test_info.output_path)
      self.start_audio_recording()
      excerpts.append(audio_recording_path)

    return excerpts

  def _generate_output_filename(self) -> None:
    timestamp = mobly_logger.get_log_file_timestamp()
    self._output_filename = f'bes_log,{self.bluetooth_address},{timestamp}.txt'
    self._wait_and_log_board_time(self._output_filename)

  def _send_bes_command(
      self, command: str | enum.StrEnum, wait_response: bool = True
  ) -> str:
    """Sends the serial command to the BES devboard."""
    if self._publisher is None:
      raise BesDeviceError(_PUBLISHER_NOT_STARTED_MESSAGE)

    with (
        self._publisher.bes_response() as response,
        self._publisher.event(pattern=_COMMAND_NOT_SUPPORTED_PATTERN) as event,
    ):
      time.sleep(_SEND_BES_COMMAND_INTERVAL.total_seconds())
      if self.config.remote_mode:
        self._execute_command(
            _WRITE_SERIAL_COMMAND.format(
                command=str(command) + '\\r\\n',
                serial_port=self.config.serial_port,
            )
        )
      else:
        self._execute_command(
            _WRITE_SERIAL_COMMAND_ON_HOST.format(
                command=str(command),
                serial_port=self.config.serial_port,
            )
        )
      if not wait_response:
        return ''
      if not response.wait(timeout=_BES_EXECUTION_TIMEOUT):
        if event.is_set():
          raise BesCommandError(
              error_code=ErrorType.COMMAND_NOT_SUPPORT_ERROR.value,
              command=command,
          )
        raise CommandTimeoutError(
            f'Failed to wait for response of the command: {command}'
        )
      data = response.trigger
      # `response.wait()` ensures data is not None.
      assert data is not None
      self.log.info(
          'Response: %s (error code %s) %s',
          data.status,
          data.error_code,
          data.message,
      )
      if data.error_code != 0:
        raise BesCommandError(error_code=data.error_code, command=command)
      return data.message

  def _get_firmware_version(
      self, build_date_str: str, version_str: str
  ) -> None:
    """Gets the firmware version of the BES board."""
    info_list = []
    if matched := _FIRMWARE_VERSION_PATTERN.match(version_str):
      info_list.append(matched['version'].strip())
    if matched := _FIRMWARE_BUILD_DATE_PATTERN.match(build_date_str):
      build_date = '_'.join(matched['build_date'].strip().split())
      self._build_date = datetime.datetime.strptime(
          build_date, '%b_%d_%Y_%H:%M:%S'
      )
      info_list.append(build_date)
    self._version = ':'.join(info_list)
    self.log.info('BES firmware version: %s', self._version)

  def _reboot_and_wait_for_completion(
      self,
      reboot_command: str,
      fail_message: str,
      access_mode_after_reboot: AccessMode = AccessMode.INIT_PAIRING,
  ) -> None:
    """Executes the reboot command and waits for the reboot completion."""
    if self._publisher is None:
      raise BesDeviceError(_PUBLISHER_NOT_STARTED_MESSAGE)

    with (
        self._publisher.event(pattern=_REBOOT_DONE_PATTERN) as reboot_done,
        self._publisher.event(
            pattern=_FIRMWARE_BUILD_DATE_PATTERN
        ) as build_date,
        self._publisher.event(pattern=_FIRMWARE_VERSION_PATTERN) as version,
        self._publisher.event(
            pattern=_ACCESS_MODE_PATTERN.format(
                access_mode=access_mode_after_reboot
            )
        ) as access_mode,
        self._publisher.boot_param(_FP_MODEL_ID_BOOT_ADDRESS) as fp_model_id,
        self._publisher.boot_param(
            _FP_PRIVATE_KEY_BOOT_ADDRESS
        ) as fp_private_key,
    ):
      # Reset the cached Fast Pair parameters so they only ever reflect what
      # was actually read back from flash on the most recent boot.
      self._fp_model_id = None
      self._fp_private_key = None
      self._send_bes_command(reboot_command, wait_response=False)
      if version.wait(timeout=_REBOOT_TIMEOUT) and version.trigger is not None:
        self._get_firmware_version(
            build_date.trigger.message, version.trigger.message
        )
      if not reboot_done.wait(timeout=_REBOOT_TIMEOUT):
        raise BesRuntimeError(fail_message)
      if not access_mode.wait(timeout=_REBOOT_TIMEOUT):
        raise BesRuntimeError(
            f'Failed to wait for access mode {access_mode_after_reboot} after'
            ' reboot.'
        )
      if fp_model_id.is_set() and fp_model_id.trigger is not None:
        self._fp_model_id = (
            fp_model_id.trigger.message.strip().replace(' ', ':').lower()
        )
      if fp_private_key.is_set() and fp_private_key.trigger is not None:
        self._fp_private_key = (
            fp_private_key.trigger.message.strip().replace(' ', '').lower()
        )

    # Wait for a short time to reduce flakiness.
    time.sleep(_REBOOT_WAIT_TIME.total_seconds())

  @override
  def reboot(self) -> None:
    self._reboot_and_wait_for_completion(
        reboot_command=constants.BESCommand.REBOOT,
        fail_message='Failed to wait for device reboot.',
    )

  @override
  def factory_reset(self, wait_for_access: bool = True) -> None:
    self._reboot_and_wait_for_completion(
        reboot_command=constants.BESCommand.FACTORY_RESET,
        fail_message='Failed to wait for device factory reset.',
        access_mode_after_reboot=(
            AccessMode.ENABLE_PAIRING
            if wait_for_access
            else AccessMode.INIT_PAIRING
        ),
    )

  @override
  def power_on(self, ignore_error: bool = False) -> None:
    """Soft powers on the BES board.

    Args:
      ignore_error: If True, log a warning and return instead of raising when
        the board is already powered on.

    Raises:
      BesRuntimeError: If the board is already powered on and `ignore_error` is
        False.
    """
    if self.get_box_open_state():
      message = 'The board is already in working status, cannot power on again.'
      if ignore_error:
        self.log.warning(message)
        return
      raise BesRuntimeError(message)
    self.open_box()

  @override
  def power_off(self, ignore_error: bool = False) -> None:
    """Soft powers off the BES board.

    Args:
      ignore_error: If True, log a warning and return instead of raising when
        the board is already powered off.

    Raises:
      BesRuntimeError: If the board is already powered off and `ignore_error` is
        False.
    """
    if not self.get_box_open_state():
      message = 'The board is already in rest status, cannot power off again.'
      if ignore_error:
        self.log.warning(message)
        return
      raise BesRuntimeError(message)
    # Put the buds in the case before closing the case.
    self.set_in_box_state(True)
    self.close_box()

  def get_serial_number(self) -> str:
    if not self.is_v2:
      raise NotImplementedError('`get_serial_number` is not implemented in v1.')
    return self._send_bes_command(constants.BESCommand.GET_SERIAL_NUMBER)

  @override
  def get_device_info(self) -> bluetooth_reference_device_base.BluetoothInfo:
    device_info = self._send_bes_command(constants.BESCommand.GET_DEVICE_INFO)
    info_dict = dict(_DEVICE_INFO_PATTERN.findall(device_info))
    map_key_name = (
        lambda x: _DEVICE_INFO_KEY_MAP[x] if x in _DEVICE_INFO_KEY_MAP else x
    )
    try:
      return dacite.from_dict(
          data_class=bluetooth_reference_device_base.BluetoothInfo,
          data={map_key_name(key): value for key, value in info_dict.items()},
      )
    except dacite.MissingValueError as e:
      raise BesRuntimeError(
          f'Failed to parse device info: {device_info}'
      ) from e

  @override
  def set_address(self, address: str) -> None:
    """Sets the Bluetooth address of the device.

    This command reboots the device for the new address to take effect.

    Args:
      address: The new Bluetooth address to be set on the BES board.

    Raises:
      ValueError: If the given Bluetooth address is not valid.
    """
    if not utils.is_valid_address(address):
      raise ValueError(f'Invalid Bluetooth address {address}.')
    self._send_bes_command(
        f'{constants.BESCommand.SET_ADDRESS} {address}',
        wait_response=False,
    )
    self.reboot()

  @override
  def set_name(self, bluetooth_name: str, ble_name: str) -> None:
    """Sets the classic Bluetooth name and BLE name of the device.

    This command reboots the device for the new name to take effect.
    If you want to set both address and Fast Pair parameters, call
    `set_name_and_fp_params` instead of calling `set_name` and `set_fp_params`
    sequentially.

    Args:
      bluetooth_name: The new classic Bluetooth name of the device.
      ble_name: The new BLE name of the device.
    """
    self._send_bes_command(
        rf'{constants.BESCommand.SET_NAME} \"{bluetooth_name}\" \"{ble_name}\"'
    )
    self.reboot()

  @override
  def get_fast_pair_support(self) -> bool:
    # TODO: Add command to check Fast Pair when it's ready.
    return True

  @override
  def enable_fast_pair(self) -> None:
    raise NotImplementedError('Not implemented yet.')

  @override
  def disable_fast_pair(self) -> None:
    raise NotImplementedError('Not implemented yet.')

  @override
  def get_fp_params(self) -> tuple[str, str]:
    raise NotImplementedError('Not implemented yet.')

  @override
  def set_fp_params(self, model_id: str, private_key: str) -> None:
    """Sets the Fast Pair parameters of the device.

    This command reboots the device for the new parameters to take effect, and
    verifies the boot-time flash readback of the model ID and private key,
    retrying if flash readback mismatches. If the device has already cached
    matching Fast Pair parameters from a previous boot, the flash write and
    reboot are skipped.
    If you want to set both address and Fast Pair parameters, call
    `set_name_and_fp_params` instead of calling `set_name` and `set_fp_params`
    sequentially.

    Args:
      model_id: Google Fast Pair model ID (format XXXXXX or 0xXXXXXX).
      private_key: Google Fast Pair anti-spoofing key (base64, uncompressed).

    Raises:
      ValueError: If the model ID or private key format is invalid.
      BesRuntimeError: If the Fast Pair parameters read back from flash after
        reboot do not match the expected values after all retry attempts.
    """
    self._set_fp_params(model_id, private_key, force_reboot_if_cached=False)

  def _set_fp_params(
      self,
      model_id: str,
      private_key: str,
      force_reboot_if_cached: bool,
  ) -> None:
    """Sets Fast Pair parameters with optional skipping and readback retry."""
    reversed_model_id = utils.reverse_fp_model_id(model_id)
    decoded_private_key = utils.decode_fp_private_key(private_key)

    if (
        self._fp_model_id == reversed_model_id
        and self._fp_private_key == decoded_private_key
    ):
      self.log.info(
          'Fast Pair parameters already match cached values, skipping flash'
          ' write.'
      )
      if force_reboot_if_cached:
        self.reboot()
      return

    for attempt in range(1, _SET_FP_PARAMS_MAX_ATTEMPTS + 1):
      self._send_bes_command(
          rf'{constants.BESCommand.SET_FP_MODEL_ID} {reversed_model_id}'
      )
      time.sleep(_SEND_BES_COMMAND_INTERVAL.total_seconds())
      self._send_bes_command(
          rf'{constants.BESCommand.SET_FP_PRIVATE_KEY} {decoded_private_key}'
      )
      # `reboot` clears the cache and refills it from the boot readback.
      self.reboot()

      if self._fp_model_id is None or self._fp_private_key is None:
        # Nothing to verify against. Leave the cache unset so the next call
        # writes flash again instead of trusting an unverified write.
        self.log.warning(
            'Fast Pair parameter boot readback not captured (model_id=%s,'
            ' private_key=%s); skipping flash readback verification.',
            self._fp_model_id,
            self._fp_private_key,
        )
        return

      if (
          self._fp_model_id == reversed_model_id
          and self._fp_private_key == decoded_private_key
      ):
        return

      self.log.warning(
          'Fast Pair parameter flash readback mismatch after reboot (attempt'
          ' %d/%d): expected model_id=%s, got %s; expected private_key=%s, got'
          ' %s.',
          attempt,
          _SET_FP_PARAMS_MAX_ATTEMPTS,
          reversed_model_id,
          self._fp_model_id,
          decoded_private_key,
          self._fp_private_key,
      )

    raise BesRuntimeError(
        'Failed to set Fast Pair parameters after'
        f' {_SET_FP_PARAMS_MAX_ATTEMPTS} attempts due to flash readback'
        f' mismatch: expected model_id={reversed_model_id}, got'
        f' {self._fp_model_id}; expected private_key={decoded_private_key},'
        f' got {self._fp_private_key}.'
    )

  def set_name_and_fp_params(
      self,
      bluetooth_name: str,
      ble_name: str,
      model_id: str,
      private_key: str,
  ) -> None:
    """Sets the Bluetooth name and Fast Pair parameters of the device.

    This command reboots the device for the new settings to take effect.

    Args:
      bluetooth_name: The new classic Bluetooth name of the device.
      ble_name: The new BLE name of the device.
      model_id: Google Fast Pair model ID (format XXXXXX or 0xXXXXXX).
      private_key: Google Fast Pair anti-spoofing key (base64, uncompressed).
    """
    # Validate Fast Pair parameters before sending SET_NAME command.
    _ = utils.reverse_fp_model_id(model_id)
    _ = utils.decode_fp_private_key(private_key)
    self._send_bes_command(
        rf'{constants.BESCommand.SET_NAME} \"{bluetooth_name}\" \"{ble_name}\"'
    )
    time.sleep(_SEND_BES_COMMAND_INTERVAL.total_seconds())
    self._set_fp_params(model_id, private_key, force_reboot_if_cached=True)

  @override
  def get_sass_support(self) -> bool:
    # TODO: Add command to check SASS when it's ready.
    return True

  @override
  def enable_sass(self) -> None:
    raise NotImplementedError('Not implemented yet.')

  @override
  def disable_sass(self) -> None:
    raise NotImplementedError('Not implemented yet.')

  @override
  def get_lea_support(self) -> bool:
    # TODO: Add command to check LE Audio when it's ready.
    return True

  @override
  def enable_lea(self) -> None:
    raise NotImplementedError('Not implemented yet.')

  @override
  def disable_lea(self) -> None:
    raise NotImplementedError('Not implemented yet.')

  @override
  def set_single_point(self) -> None:
    if not self.is_v2:
      raise NotImplementedError('`set_single_point` is not implemented in v1.')
    self._send_bes_command(f'{constants.BESCommand.SET_MULTIPOINT} 0')

  @override
  def set_multi_point(self) -> None:
    if not self.is_v2:
      raise NotImplementedError('`set_multi_point` is not implemented in v1.')
    self._send_bes_command(f'{constants.BESCommand.SET_MULTIPOINT} 1')

  @override
  def start_pairing_mode(
      self, timeout: datetime.timedelta | None = None
  ) -> None:
    del timeout  # Unused, waiting for firmware update to support timeout
    self._send_bes_command(constants.BESCommand.START_PAIRING_MODE)

  @override
  def stop_pairing_mode(self) -> None:
    self._send_bes_command(constants.BESCommand.STOP_PAIRING_MODE)

  @override
  def connect(self, address: str) -> None:
    if not utils.is_valid_address(address):
      raise ValueError(f'Invalid Bluetooth address {address}.')

    address = address.replace(':', '').upper()
    self._send_bes_command(f'{constants.BESCommand.CONNECT} {address}')

  @override
  def disconnect(self, address: str) -> None:
    if not utils.is_valid_address(address):
      raise ValueError(f'Invalid Bluetooth address {address}.')

    address = address.replace(':', '').upper()
    self._send_bes_command(f'{constants.BESCommand.DISCONNECT} {address}')

  @override
  def clear_paired_devices(self) -> None:
    self._send_bes_command(constants.BESCommand.CLEAR_PAIRED_DEVICES)

  @override
  def enable_tws(self) -> None:
    self._send_bes_command(f'{constants.BESCommand.SET_TWS_ENABLE} 1')

  @override
  def disable_tws(self) -> None:
    self._send_bes_command(f'{constants.BESCommand.SET_TWS_ENABLE} 0')

  @override
  def get_component_number(self) -> int:
    raise NotImplementedError('Not implemented yet.')

  @override
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
    if number not in (1, 2):
      raise ValueError(f'Invalid component number {number}')
    self._send_bes_command(
        f'{constants.BESCommand.SET_COMPONENT_NUMBER} {number}'
    )

  @override
  def pair_tws(self) -> None:
    self._send_bes_command(
        constants.BESCommand.TWS_PAIRING,
        wait_response=(True if self.is_v2 else False),
    )

  @enum.unique
  class BoxState(enum.StrEnum):
    """The state of the TWS earpods and box."""

    IN_BOX_CLOSED = 'IN_BOX_CLOSED'
    IN_BOX_OPEN = 'IN_BOX_OPEN'
    OUT_BOX = 'OUT_BOX'
    OUT_BOX_WEARED = 'OUT_BOX_WEARED'

    @property
    def is_box_open(self) -> bool:
      return self in (self.IN_BOX_OPEN, self.OUT_BOX, self.OUT_BOX_WEARED)

    @property
    def is_in_box(self) -> bool:
      return self in (self.IN_BOX_CLOSED, self.IN_BOX_OPEN)

    @property
    def is_on_head(self) -> bool:
      return self == self.OUT_BOX_WEARED

  def _get_box_state(self) -> BoxState:
    """Gets the box state of the board."""
    result = self._send_bes_command(constants.BESCommand.GET_BOX_STATE)
    if matched := _BOX_STATE_PATTERN.match(result):
      return self.BoxState(matched['state'])
    raise BesRuntimeError(
        f'Failed to get box state from command result: {result}'
    )

  @override
  def get_box_open_state(self) -> bool:
    """Gets if the box is open."""
    return self._get_box_state().is_box_open

  @override
  def get_in_box_state(self) -> bool:
    """Gets if the TWS earpod is in the box."""
    return self._get_box_state().is_in_box

  @override
  def set_in_box_state(self, in_box: bool) -> None:
    """Sets if the TWS earpod is in the box."""
    current_state = self._get_box_state()
    # Current state matches the target state, no need to change.
    if current_state.is_in_box == in_box:
      return
    # Current state does not match the target state, change the state.
    if current_state.is_in_box:
      if not current_state.is_box_open:
        self._send_bes_command(constants.BESCommand.OPEN_BOX)
      self._send_bes_command(constants.BESCommand.FETCH_OUT)
    else:
      if current_state.is_on_head:
        self._send_bes_command(constants.BESCommand.WEAR_DOWN)
      self._send_bes_command(constants.BESCommand.PUT_IN)

  @override
  def get_on_head_state(self) -> bool:
    """Gets if the TWS earpod is on head."""
    return self._get_box_state().is_on_head

  @override
  def set_on_head_state(self, on_head: bool) -> None:
    """Sets if the TWS earpod is on head."""
    current_state = self._get_box_state()
    # Current state matches the target state, no need to change.
    if current_state.is_on_head == on_head:
      return
    # Current state does not match the target state, change the state.
    if current_state.is_on_head:
      self.wear_down()
    else:
      if not current_state.is_box_open:
        self._send_bes_command(constants.BESCommand.OPEN_BOX)
      if current_state.is_in_box:
        self._send_bes_command(constants.BESCommand.FETCH_OUT)
      self._send_bes_command(constants.BESCommand.WEAR_UP)

  @override
  def open_box(self) -> None:
    """Opens the case of the TWS earpod.

    This will also reconnects the board with the last 2 paired phones.

    Raises:
      BesRuntimeError: If the case is already open.
    """
    if self.get_box_open_state():
      raise BesRuntimeError('The box is already open, cannot re-open.')
    self._send_bes_command(constants.BESCommand.OPEN_BOX)

  @override
  def fetch_out(self) -> None:
    """Fetches the TWS earpod out of the case."""
    if not self.get_in_box_state():
      raise BesRuntimeError('The BES device is not in box, cannot fetch out.')
    self._send_bes_command(constants.BESCommand.FETCH_OUT)

  @override
  def wear_up(self) -> None:
    """Wears the TWS earpod on head."""
    current_state = self._get_box_state()
    if current_state.is_on_head:
      raise BesRuntimeError(
          'The BES device is already on head, cannot wear up.'
      )
    if current_state.is_in_box:
      raise BesRuntimeError('The BES device is in box, cannot wear up.')
    self._send_bes_command(constants.BESCommand.WEAR_UP)

  @override
  def wear_down(self) -> None:
    """Wears the TWS earpod off head."""
    if not self.get_on_head_state():
      raise BesRuntimeError('The BES device is not on head, cannot wear down.')
    self._send_bes_command(constants.BESCommand.WEAR_DOWN)

  @override
  def put_in(self) -> None:
    """Puts the TWS earpod in the case."""
    current_state = self._get_box_state()
    if current_state.is_in_box:
      raise BesRuntimeError('The BES device is already in box, cannot put in.')
    if current_state.is_on_head:
      raise BesRuntimeError('The BES device is on head, cannot put in.')
    self._send_bes_command(constants.BESCommand.PUT_IN)

  @override
  def close_box(self) -> None:
    """Closes the case of the TWS earpod.

    This will disconnect the board from any connected phones and reject any
    incoming connections until the case is re-opened.

    Raises:
      BesRuntimeError: If the case is already closed.
    """
    if not self.get_box_open_state():
      raise BesRuntimeError('The box is already closed, cannot re-close.')
    self._send_bes_command(constants.BESCommand.CLOSE_BOX)

  @override
  def set_battery_level(self, level: int) -> None:  # pyrefly: ignore[bad-override]
    """Sets the fake battery level of the device.

    Args:
      level: The fake battery level of the device, in the range of 0-100.
        `level=80` represents that the battery is 80% full.

    Raises:
      ValueError: If the given battery level is not in the valid range.
    """
    if not 0 <= level <= 100:
      raise ValueError(
          f'Invalid battery level {level}, should be in the range of 0-100.'
      )
    self._send_bes_command(
        f'{constants.BESCommand.SET_BATTERY_LEVEL} {level} {level}'
    )

  def set_battery_level_tws(
      self,
      left_level: int,
      right_level: int,
      case_level: int | None = None,
  ) -> None:
    """Sets the fake battery level of the earbuds in a TWS pair.

    Args:
      left_level: The fake battery level of the left earbud in a pair of TWS
        earbuds, in the range of 0-100. `level=80` represents that the battery
        is 80% full.
      right_level: The fake battery level of the right earbud in a pair of TWS
        earbuds, in the range of 0-100
      case_level: The fake battery level of the case in a pair of TWS earbuds,
        in the range of 0-100. If None, there will be no case level set.

    Raises:
      ValueError: If the given battery level is not in the valid range.
    """
    if not 0 <= left_level <= 100:
      raise ValueError(
          f'Invalid battery level {left_level}, should be in the range of'
          ' 0-100.'
      )
    if not 0 <= right_level <= 100:
      raise ValueError(
          f'Invalid battery level {right_level}, should be in the range of'
          ' 0-100.'
      )
    if case_level is not None and not 0 <= case_level <= 100:
      raise ValueError(
          f'Invalid battery level {case_level}, should be in the range of'
          ' 0-100.'
      )
    if self.is_v2 and case_level is not None:
      self._send_bes_command(
          f'{constants.BESCommand.SET_BATTERY_LEVEL} {left_level} {right_level}'
          f' {case_level}'
      )
    else:
      self._send_bes_command(
          f'{constants.BESCommand.SET_BATTERY_LEVEL} {left_level} {right_level}'
      )

  @override
  def get_battery_level(self) -> int:
    """Gets the fake battery level of the board.

    Returns:
      The fake battery level of the board, in the range of 0-100.
      `level=80` represents that the battery is 80% full.

    Raises:
      BesRuntimeError: If failed to get valid battery level from the BES
        response.
    """
    result = self._send_bes_command(constants.BESCommand.GET_BATTERY_LEVEL)
    if matched := _GET_BATTERY_LEVEL_PATTERN.match(result):
      return int(matched['level'])
    raise BesRuntimeError(
        f'Failed to get battery level from command result: {result}'
    )

  def get_battery_level_tws(self) -> tuple[int, int, int | None]:
    """Gets the fake battery level of the board and its remote peer in TWS mode.

    Returns:
      A tuple of 3 integers, representing the fake battery level of the left
      earbud, right earbud, and case in a TWS device, in the range of 0-100.
      `level=80` represents that the battery is 80% full. The case level will
      be None if the BES is not v2.

    Raises:
      BesRuntimeError: If failed to get valid battery level from the BES
        response.
    """
    result = self._send_bes_command(constants.BESCommand.GET_BATTERY_LEVEL)
    if matched := _GET_BATTERY_LEVEL_TWS_PATTERN.match(result):
      left_level = int(matched['left_level'])
      right_level = int(matched['right_level'])
      case_level = int(matched['case_level']) if matched['case_level'] else None
      if (
          left_level < 0
          or left_level > 100
          or right_level < 0
          or right_level > 100
      ):
        raise BesRuntimeError(
            f'Failed to get valid battery level from command result: {result}'
        )
      return left_level, right_level, case_level
    raise BesRuntimeError(
        'Failed to get battery level of TWS earbuds from command result:'
        f' {result}'
    )

  @override
  def get_paired_devices(self) -> list[dict[str, str]]:
    raw_result = self._send_bes_command(constants.BESCommand.GET_PAIRED_DEVICES)
    return [
        {
            'Name': matched['name'],
            'Address': utils.lsb_addr_to_bd_addr(matched['addr']),
        }
        for matched in _PAIRED_DEVICE_INFO_PATTERN.finditer(raw_result)
    ] + [
        {
            'Name': '',
            'Address': utils.lsb_addr_to_bd_addr(matched['addr']),
        }
        for matched in _LE_PAIRED_DEVICE_INFO_PATTERN.finditer(raw_result)
    ]

  @override
  def media_play(self) -> None:
    self._send_bes_command(constants.BESCommand.MEDIA_PLAY, wait_response=False)

  @override
  def media_pause(self) -> None:
    self._send_bes_command(
        constants.BESCommand.MEDIA_PAUSE, wait_response=False
    )

  @override
  def media_next(self) -> None:
    self._send_bes_command(constants.BESCommand.MEDIA_NEXT, wait_response=False)

  @override
  def media_prev(self) -> None:
    self._send_bes_command(constants.BESCommand.MEDIA_PREV, wait_response=False)

  @override
  def volume_up(self, level: int = 1) -> None:
    for _ in range(level):
      self._send_bes_command(constants.BESCommand.VOLUME_UP)

  @override
  def volume_down(self, level: int = 1) -> None:
    for _ in range(level):
      self._send_bes_command(constants.BESCommand.VOLUME_DOWN)

  @override
  def set_volume(self, level: int) -> None:
    """Sets the volume of the device to a given level.

    Args:
      level: The target volume level, in the range of [0, 127].

    Raises:
      ValueError: If the given volume level is not in the valid range.
    """
    if level < 0 or level > 127:
      raise ValueError(
          f'Invalid volume level {level}, should be in the range of [0, 127].'
      )

    self._send_bes_command(f'{constants.BESCommand.SET_VOLUME} {level}')

  @override
  def get_volume(self) -> int:
    """Gets the volume of the device.

    Returns:
      The volume level of the device.

    Raises:
      BesRuntimeError: If failed to get valid volume level from the BES
        response.
    """
    result = self._send_bes_command(constants.BESCommand.GET_VOLUME)
    if matched := _GET_VOLUME_PATTERN.match(result):
      return int(matched['level'])
    raise BesRuntimeError(
        f'Failed to get volume level from command result: {result}'
    )

  @override
  def call_accept(self) -> None:
    self._send_bes_command(
        constants.BESCommand.CALL_ACCEPT, wait_response=False
    )

  @override
  def call_decline(self) -> None:
    self._send_bes_command(
        constants.BESCommand.CALL_DECLINE, wait_response=False
    )

  @override
  def call_hold(self) -> None:
    self._send_bes_command(constants.BESCommand.CALL_HOLD, wait_response=False)

  @override
  def call_redial(self) -> None:
    self._send_bes_command(
        constants.BESCommand.CALL_REDIAL, wait_response=False
    )

  @override
  def get_anc_support(self) -> bool:
    # TODO: Add command to check ANC when it's ready.
    return True

  @override
  def enable_anc(self) -> None:
    raise NotImplementedError('Not implemented yet.')

  @override
  def disable_anc(self) -> None:
    raise NotImplementedError('Not implemented yet.')

  @override
  def get_anc_mode(self) -> bluetooth_reference_device_base.AncMode:
    raise NotImplementedError('Not implemented yet.')

  @override
  def set_anc_mode(
      self, mode: str | bluetooth_reference_device_base.AncMode
  ) -> None:
    if not self.is_v2:
      raise NotImplementedError('`set_anc_mode` is not implemented in v1.')
    if isinstance(mode, str):
      mode = bluetooth_reference_device_base.AncMode.from_string(mode)
    self._send_bes_command(f'{constants.BESCommand.SET_ANC_MODE} {mode.value}')

  @override
  def get_spatial_audio_support(self) -> bool:
    # TODO: Add command to check Spatial Audio when it's ready.
    return True

  @override
  def enable_spatial_audio(self) -> None:
    if not self.is_v2:
      raise NotImplementedError('Not implemented yet.')
    self._send_bes_command(f'{constants.BESCommand.SET_SPATIAL_AUDIO_ENABLE} 1')

  @override
  def disable_spatial_audio(self) -> None:
    if not self.is_v2:
      raise NotImplementedError('Not implemented yet.')
    self._send_bes_command(f'{constants.BESCommand.SET_SPATIAL_AUDIO_ENABLE} 0')
