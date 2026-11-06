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

"""The module to record audio on the Bluetooth reference device."""

from collections.abc import Sequence
import dataclasses
import logging
import os
import pathlib
import re
import shlex
import time
from typing import Any

from mobly import logger as mobly_logger
from mobly import runtime_test_info
from mobly import utils
from mobly.controllers.android_device_lib.services import base_service

from mobly.controllers.bluetooth.lib import ssh as ssh_lib
from mobly.controllers.bluetooth.lib import errors

# Avoid directly importing rpi_device, which causes circular
# dependencies.
_RpiDevice = Any

# Error messages used in this module.
_START_RECORDER_IS_RUNNIN_MSG = (
    'Cannot start audio recording because recorder is running'
)
_RECORDER_STARTUP_TIMEOUT_MSG = (
    'Timeout expired when waiting on the output of'
    ' the audio recorder startup process.'
)
_RECORDER_DIES_DURING_STARTUP_MSG = (
    'The audio recorder crashed during startup. Check the log of the recorder.'
)

# The command and regex used to filter the audio interface information
_LIST_AUDIO_DEVICES_CMD = 'arecord -l'
_CAPTURE_HARDWARE_DEVICE_PATTERN = re.compile(
    'card (?P<card_num>[0-9]+): (?P<card_name>.+), '
    'device (?P<device_num>[0-9]+): (?P<device_name>.+)'
)

# The file path pattern of the audio recording in test device
_RECORDING_FILENAME_PATTERN = '{prefix},{hostname}:{port},{timestamp}.wav'
_RECORDING_REMOTE_PATH_PATTERN = '/tmp/{filename}'

# The pattern used to filter the log indicating that the recorder has started
_WAIT_FOR_RECORDING_PATTERN = 'Recording WAVE'

# Maximum time to wait for the audio recorder to be ready
_RECORDER_STARTUP_TIMEOUT_SEC = 60


@dataclasses.dataclass
class AudioRecorderConfig:
  """The configuration of the audio recorder."""

  prefix: str
  sample_rate: int
  sample_format: str
  use_capture_device: bool


_DEFAULT_RECORDER_CONFIG = AudioRecorderConfig(
    prefix='audio',
    sample_rate=8000,
    sample_format='S16_LE',
    use_capture_device=False,
)


@dataclasses.dataclass
class CaptureDevice:
  """A container to collect capture hardware device information.

  Attributes:
    card_num: The card number of the PCM device.
    card_name: The card name of the PCM device.
    device_num: The device number of the PCM device.
    device_name: The device name of the PCM device.
  """

  card_num: int
  card_name: str
  device_num: int
  device_name: str

  def is_valid_pcm_name(self, pcm_name: str) -> bool:
    """Returns if the PCM name is a valid name of the capture device."""
    return pcm_name == self.hw_name or pcm_name == self.plughw_name

  @property
  def hw_name(self) -> str:
    """Returns the hw name of the capture device.

    The ALSA hw device description uses the hw plugin, which is a raw
    communication without any conversions.
    https://www.alsa-project.org/alsa-doc/alsa-lib/pcm.html

    Returns:
      The hw name of the capture device.
    """
    return f'hw:{self.card_num},{self.device_num}'

  @property
  def plughw_name(self) -> str:
    """Returns the plughw name of the capture device.

    The plughw device description uses the plug plugin and hw plugin as the
    underlying hardware device.
    Plug plugin is an automatic conversion plugin, which converts channels, rate
    and format on request.
    https://www.alsa-project.org/alsa-doc/alsa-lib/pcm.html
    https://www.alsa-project.org/alsa-doc/alsa-lib/pcm_plugins.html

    Returns:
      The plughw name of the capture device.
    """
    return f'plughw:{self.card_num},{self.device_num}'

  @property
  def detail(self) -> str:
    """Returns the detailed information of the capture device."""
    return (
        f'card {self.card_num}: {self.card_name}, '
        f'device {self.device_num}: {self.device_name}'
    )


def _list_capture_devices(ssh: ssh_lib.SSHProxy) -> Sequence[CaptureDevice]:
  """Lists the capture hardware devices on the Raspberry Pi.

  There can be multiple audio inputs on the Raspberry Pi, such as built-in
  audio server and external soundcards. https://linux.die.net/man/1/arecord
  To query the audio capture hardware device list, run command `arecord -l`
  on the Raspberry Pi.

  Audio recorder supports recording from different sound input by setting
  `use_capture_device` param in `start()`.
  This method lists all *hardware* devices that can be captured on the
  Raspberry Pi.

  Args:
    ssh: The ssh connection to the Raspberry Pi device.

  Returns:
    A sequence of CaptureDevice instances of available capture hardware devices.
  """
  capture_devices = []
  try:
    raw_output = ssh.execute_command(_LIST_AUDIO_DEVICES_CMD)
    for matched in _CAPTURE_HARDWARE_DEVICE_PATTERN.finditer(raw_output):
      capture_devices.append(
          CaptureDevice(
              int(matched['card_num']),
              matched['card_name'],
              int(matched['device_num']),
              matched['device_name'],
          )
      )
  except ssh_lib.ExecuteCommandError:
    logging.exception('Failed to list capture hardware devices.')
  return capture_devices


def _is_capture_device_available(
    ssh: ssh_lib.SSHProxy, capture_device: str
) -> bool:
  """Returns if the given capture device is available on the Raspberry Pi."""
  return capture_device == 'default' or any(
      device.is_valid_pcm_name(capture_device)
      for device in _list_capture_devices(ssh)
  )


class AudioRecorder(base_service.BaseService):
  """The module to record the audio output of the device.

  This module records the audio output from the Raspberry Pi in a WAV file in
  the background, and then pulls the WAV file to the host for debug use.

  This module uses `arecord`, a command-line sound recorder for Advanced Linux
  Sound Architecture(ALSA) soundcard driver like PulseAudio and Pipewire.
  The default soundcard on Raspberry Pi is snd_bcm2835, which is also provided
  by ALSA.

  To install ALSA shared library and `arecord`, run on the Raspberry Pi:
    sudo apt install alsa-utils libasound2-dev


  Attributes:
    is_alive: True if the audio recorder is running; False otherwise.
  """

  _remote_recorder_process: ssh_lib.RemotePopen | None = None
  _recording_filename: str | None = None
  _config: AudioRecorderConfig | None = None

  def __init__(self, device: _RpiDevice, configs: Any = None) -> None:
    """Initializes the AudioRecorder instance.

    Args:
      device: The Bluetooth reference device controller object.
      configs: This config argument is required by the interface of
        `BaseService` and will never be used by the audio recorder.
    """
    del configs  # Unused
    self._device = device

    self._host_log_dir = self._device.log_path
    self._log = mobly_logger.PrefixLoggerAdapter(
        self._device.log,
        {
            mobly_logger.PrefixLoggerAdapter.EXTRA_KEY_LOG_PREFIX: (
                '[AudioRecorder]'
            )
        },
    )

  @property
  def is_alive(self) -> bool:
    """True if the audio recorder is running; False otherwise.

    A recorder whose SSH session was severed still counts as alive. Paramiko
    closes every channel the moment the transport goes inactive, so `poll()`
    starts reporting an exit code for a process nobody stopped. Taking that at
    face value would make `ServiceManager` skip this service in `stop_all()`
    and omit it from `list_live_services()`, so nothing would restart it once
    the session is replaced.
    """
    if self._remote_recorder_process is None:
      return False
    if not self._device.is_ssh_connected:
      return True
    return self._remote_recorder_process.poll() is None

  @property
  def remote_pid(self) -> int | None:
    """The remote PID of the audio recorder process, or None if not running."""
    if self._remote_recorder_process is None:
      return None
    return self._remote_recorder_process.pid

  def start(
      self,
      prefix: str | None = None,
      sample_rate: int | None = None,
      sample_format: str | None = None,
      use_capture_device: bool | None = None,
  ) -> None:
    """Starts audio recording on the test device.

    Every argument left unset falls back to the value this recorder was last
    started with, and to the module defaults on the first start. A restart
    therefore records the same way as the run it replaces, which matters when
    something other than the test restarts the service, such as the device
    recovering from a severed SSH session.

    Args:
      prefix: Prefix of the file name of the recording. Defaults to 'audio'.
      sample_rate: The sample rate, in Hertz. Defaults to 8000.
      sample_format: The sample format of the recording. Defaults to 'S16_LE'.
      use_capture_device: Whether to use hardware capture device as audio input.
        If False, the builtin sound server will be used as audio input. If True,
        the capture hardware device defined in the device config will be used.
        Defaults to False.

    Raises:
      errors.AudioRecorderError: Raised when the recorder has been started.
      errors.AudioDeviceNotAvailableError: Raised when the specified audio
        capture device is not available.
    """
    if self.is_alive:
      raise errors.AudioRecorderError(_START_RECORDER_IS_RUNNIN_MSG)

    previous_config = self._config or _DEFAULT_RECORDER_CONFIG
    config = AudioRecorderConfig(
        prefix=previous_config.prefix if prefix is None else prefix,
        sample_rate=(
            previous_config.sample_rate if sample_rate is None else sample_rate
        ),
        sample_format=(
            previous_config.sample_format
            if sample_format is None
            else sample_format
        ),
        use_capture_device=(
            previous_config.use_capture_device
            if use_capture_device is None
            else use_capture_device
        ),
    )
    self._config = config

    timestamp = mobly_logger.get_log_file_timestamp()

    self._recording_filename = _RECORDING_FILENAME_PATTERN.format(
        prefix=config.prefix,
        hostname=self._device.config.hostname,
        port=self._device.config.ssh_port,
        timestamp=timestamp,
    )
    device_recording_filename = _RECORDING_REMOTE_PATH_PATTERN.format(
        username=self._device.config.username,
        filename=self._recording_filename,
    )
    host_log_path = os.path.join(
        self._host_log_dir, f'audio_log_{timestamp}.txt'
    )

    capture_device = (
        self._device.config.audio_capture_device_name
        if config.use_capture_device
        else 'default'
    )
    if capture_device is None or not _is_capture_device_available(
        self._device.ssh, capture_device
    ):
      raise errors.AudioDeviceNotAvailableError(
          'Failed to record because audio capture hardware device'
          f' is not available: {capture_device}'
      )

    cmd = (
        f'arecord -f {shlex.quote(config.sample_format)} -r'
        f' {config.sample_rate} -D {shlex.quote(capture_device)}'
        f' {shlex.quote(device_recording_filename)}'
    )

    self._log.debug('Running audio recorder on the test device')
    self._remote_recorder_process = self._device.ssh.start_remote_process(
        cmd, get_pty=True, output_file_path=host_log_path
    )

    self._wait_for_recorder_start(host_log_path)

  def _wait_for_recorder_start(self, host_log_path: str) -> None:
    """Waits for the audio recorder log.

    Args:
      host_log_path: The path of the recorder log file on the host device.

    Raises:
      errors.AudioRecorderError: Raised if failed to start the audio recorder on
        the test device.
    """
    deadline_time = time.perf_counter() + _RECORDER_STARTUP_TIMEOUT_SEC
    while deadline_time > time.perf_counter():
      # Checked directly rather than through `is_alive`, which reports a
      # process whose SSH session was severed as still alive. During startup
      # that is a failure either way, and waiting out the timeout to find out
      # helps nobody.
      if (
          self._remote_recorder_process is None
          or self._remote_recorder_process.poll() is not None
      ):
        self.stop()
        raise errors.AudioRecorderError(_RECORDER_DIES_DURING_STARTUP_MSG)
      try:
        match = re.search(
            _WAIT_FOR_RECORDING_PATTERN, open(host_log_path, 'r').read()
        )
        if match is not None:
          return
      except OSError:
        self._log.error('Failed to open the recorder log file. Trying again.')

      self._log.debug('Wait 0.5s for recorder startup log file.')
      time.sleep(0.5)

    self.stop()
    raise errors.AudioRecorderError(_RECORDER_STARTUP_TIMEOUT_MSG)

  def stop(self, log_path: str | None = None) -> None:
    """Stops the audio recorder on the test device.

    Converges on a stopped state whatever the remote side does. Mobly calls
    this from teardown, where raising aborts the rest of the teardown, and a
    half-cleared state blocks the next `start()` on the `is_alive` check.

    Args:
      log_path: Where to place the pulled recording. Defaults to the device log
        directory.
    """
    try:
      if not self._device.is_ssh_connected:
        # The recording only exists on the device, and the process lost its
        # channel along with the session. Touching `self._device.ssh` here
        # would open a whole new connection just to chase it.
        self._log.debug(
            'Skipping remote audio recorder teardown; the SSH session is gone.'
        )
        return

      if self.is_alive:
        assert self._remote_recorder_process  # Checked by is_alive.
        self._remote_recorder_process.kill()
        if self._recording_filename is not None:
          # Pull the audio file from the device
          remote_recording_filename = _RECORDING_REMOTE_PATH_PATTERN.format(
              username=self._device.config.username,
              filename=self._recording_filename,
          )
          self._device.ssh.pull(
              remote_recording_filename,
              os.path.join(
                  log_path or self._host_log_dir, self._recording_filename
              ),
          )
          self._device.ssh.rm_file(remote_recording_filename)
    except ssh_lib.SSH_ERRORS:
      self._log.warning(
          'Failed to tear down the audio recorder on the device.',
          exc_info=True,
      )
    finally:
      self._remote_recorder_process = None
      self._recording_filename = None

  def create_output_excerpts(
      self, test_info: runtime_test_info.RuntimeTestInfo
  ) -> list[Any]:
    """Creates excerpts for the audio recorder and returns the excerpt paths.

    Args:
      test_info: `self.current_test_info` in a Mobly test.

    Returns:
      The list of absolute paths to excerpt files. Empty if the recorder is not
      running, or if the SSH session is gone.
    """
    if not self.is_alive:
      return []

    # Collecting the excerpt pulls the recording off the device and restarts
    # the recorder, so both halves need a working session. Without one,
    # `start()` would block for the full reconnect budget before failing.
    if not self._device.is_ssh_connected:
      self._log.warning(
          'Skipping audio recorder excerpts because the SSH session is gone.'
      )
      return []

    if self._recording_filename is None:
      raise ValueError('Recording file name is undefined. Run start() first.')

    utils.create_dir(test_info.output_path)
    excerpt_path = pathlib.Path(test_info.output_path, self._recording_filename)

    # Restart recorder to pull the recording from Raspberry Pi to the host.
    # `start()` resumes with the settings the recorder is already using.
    self.stop(log_path=test_info.output_path)
    self.start()

    return [excerpt_path]
