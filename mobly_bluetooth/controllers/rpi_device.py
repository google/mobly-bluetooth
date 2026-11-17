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

"""Mobly controller module for a Raspberry Pi Bluetooth reference device."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
import contextlib
import dataclasses
import datetime
import enum
import logging
import os
import threading
import time
from typing import Any

from mobly import logger as mobly_logger
from mobly.controllers.android_device_lib import service_manager
from mobly.controllers.android_device_lib.services import base_service
from mobly_bluetooth.controllers import rpi_device_config as device_config
from mobly_bluetooth.lib import bes_manager as bes_manager_lib
from mobly_bluetooth.lib import build_info as build_info_lib
from mobly_bluetooth.lib import errors
from mobly_bluetooth.lib import rpi_audio_recorder as audio_recorder_lib
from mobly_bluetooth.lib import ssh as cros_ssh_lib
from mobly_bluetooth.lib import tws_manager as tws_manager_lib

# This is used in the config file located in the test lab's home directory.
MOBLY_CONTROLLER_CONFIG_NAME = 'RpiDevice'

# Error messages used in this module.
_DEVICE_CONFIG_ERROR_MSG = (
    'Failed to parse device configs when creating Bluetooth reference devices: '
)
_BES_INFO_NOT_SPECIFIED_ERROR_MSG = (
    'Unable to start BES manager because `bes_board_serial_port` or'
    ' `bes_board_bt_address` property is not specified in the device config'
)
_REBOOT_TIMEOUT_ERROR_MSG = (
    'Failed to reboot Bluetooth reference device due to timeout'
)

# Timeouts
_DEFAULT_TIMEOUT_REBOOT_COMPLETION = datetime.timedelta(minutes=5)
_DEFAULT_WAIT_TIME_REBOOT = datetime.timedelta(seconds=5)
# Prints the random UUID the kernel generated at boot. It changes on every boot
# and on nothing else, which makes it the one reliable sign that the device
# rebooted between two SSH sessions.
_READ_BOOT_ID_COMMAND = 'cat /proc/sys/kernel/random/boot_id'
# Bound on the short housekeeping commands a recovery runs while it holds the
# recovery lock, so a network that degrades right after the session was opened
# cannot hang every other user of the device behind that lock.
_RECOVERY_COMMAND_TIMEOUT_SECONDS = 30.0

# Errors that operations on `RpiDevice.ssh` can raise, re-exported here
# so that callers can react to a broken SSH session or a missing remote path
# without depending on Paramiko or on the SSH module this controller happens to
# use.
#
# `cros_ssh_lib.Error` covers the failures the SSH wrapper reports itself, such
# as a command that exits non-zero or a local port that is not forwarded, with
# `RemotePathDoesNotExistError` distinguishing a missing remote path during
# file transfers. The rest are what the layers underneath raise once the
# session is gone: Paramiko wraps transport faults in `SSHException`, surfaces
# a peer that disappeared mid-exchange as `EOFError`, and lets socket errors
# such as `BrokenPipeError` through as `OSError`.
SSH_ERRORS = cros_ssh_lib.SSH_ERRORS
RemotePathDoesNotExistError = cros_ssh_lib.RemotePathDoesNotExistError


def create(configs: Sequence[dict[str, Any]]) -> list[RpiDevice]:
  """Creates RpiDevice controller objects.

  Mobly uses this to instantiate RpiDevice controller objects from
  configs. The configs come from Mobly configs that look like:

    ```config.yaml
    TestBeds:
    - Name: SampleTestBed
      Controllers:
        RpiDevice:
        - hostname: '127.0.0.1'
          username: 'pi'
    ```

  Each config should have required key-value pair 'hostname' and 'username'.

  Args:
    configs: a list of dicts, each representing a configuration for a Bluetooth
      reference device.

  Returns:
    A list of RpiDevice objects.

  Raises:
    errors.BtReferenceError: Invalid controller configs are given.
  """
  try:
    device_configs = device_config.from_dicts(configs)
  except Exception as e:
    raise errors.BtReferenceError(_DEVICE_CONFIG_ERROR_MSG, e) from e

  devices = []
  for config in device_configs:
    logging.debug(
        'Creating Bluetooth reference device %s:%d',
        config.hostname,
        config.ssh_port,
    )
    devices.append(RpiDevice(config))

  return devices


def destroy(devices: Sequence[RpiDevice]) -> None:
  """Destroys RpiDevice objects.

  Mobly uses this to destroy RpiDevice objects created by `create`.

  Args:
    devices: list of RpiDevice.
  """
  for device in devices:
    try:
      device.destroy()
    except Exception:
      logging.exception('Failed to clean up device properly: %s', repr(device))


def get_info(devices: Sequence[RpiDevice]) -> Sequence[dict[str, Any]]:
  """Gets info from the RpiDevice objects used in a test run.

  Args:
    devices: A list of RpiDevice objects.

  Returns:
    list of dict, each representing info for input devices.
  """
  return [d.get_info() for d in devices]


class RpiDevice:
  """Mobly controller for a Raspberry Pi Bluetooth reference device.

  The Bluetooth reference device refers to a Raspberry Pi configured as a
  Bluetooth peer device for automated testing.

  Each object of this class represents one Bluetooth reference device in Mobly.
  This class provides SSH command execution and built-in services to control the
  Bluetooth reference device.

  Users can also register custom services to the device's service manager.
  The service mechanism for Bluetooth reference device is the same as for
  Android device. See `mobly.controllers.android_device_lib.service_manager`
  and `mobly.controllers.android_device_lib.services.base_service` for details.

  Attributes:
    ssh: The underlying SSH client object.
    services: The manager of long-running services on the device.
    audio_recorder: The recorder to record audio output on the Raspberry Pi.
    bes_manager: The manager for controlling BES dev board on the Raspberry Pi.
    tws_manager: The manager for controlling two BES dev boards as a TWS pair on
      the Raspberry Pi.
    config: The configurations for the Bluetooth reference device.
    debug_tag: A string that represents this Bluetooth reference device in the
      debug info.
    log_path: The local path on the host to save logs in the test.
    log: A logger adapted from root logger with an added prefix
      '[RpiDevice|<hostname>:<ssh_port>] 'specific to a test device.
  """

  # Timeout to wait for ssh connection.
  #
  # Paramiko falls back to the OS TCP default when this is unset, which can
  # block for minutes. An explicit value is what makes the retry below bounded.
  SSH_CONNECT_TIMEOUT_SECONDS = 30
  # Timeout to wait for banner to be presented.
  SSH_BANNER_TIMEOUT_SECONDS = None
  # Number of times opening a new SSH connection is attempted.
  #
  # Connecting fails for reasons that clear on their own: the device is still
  # finishing a boot, sshd has not bound its port yet, a transient network
  # fault. Set to 1 to attempt once and propagate the first failure.
  SSH_CONNECT_MAX_ATTEMPTS = 3
  # Delay before the first connection retry, doubled after each further
  # failure. With the values above the worst case is 30+2+30+4+30 = 96 seconds.
  SSH_CONNECT_RETRY_DELAY_SECONDS = 2
  # Interval between the keepalive messages sent on an otherwise idle SSH
  # session.
  #
  # A test class holds one device object across all of its test methods, and
  # routinely leaves the session untouched for minutes at a time between them.
  # That gives the server's idle timeout, a NAT table or a firewall an
  # opportunity to drop it. Keepalive traffic both discourages that and makes a
  # session that did get dropped observable, which is what `is_connected`
  # relies on to trigger a reconnect.
  SSH_KEEPALIVE_INTERVAL_SECONDS = 30
  _CONTROLLER_LOG_NAME = 'RpiDevice'

  @enum.unique
  class _RecoveryPolicy(enum.Enum):
    """What `ssh` does when it finds the cached session dead.

    Exactly one policy is in force at a time, and every path that must not
    trigger a recovery of its own sets it through `_recovery_policy()` for its
    duration, so `ssh` and `recover_services()` have a single place to look.
    """

    # Reconnect and bring the running services back. The default.
    RECOVER = 'recover'
    # Only reopen the transport. In force while a recovery or a reboot is
    # already replacing the session and will deal with the services itself.
    RECONNECT_ONLY = 'reconnect_only'
    # Raise `ConnectionError` instead of reconnecting. In force while services
    # are being stopped or paused: a service touching a dead session in its
    # teardown should fail fast, not spend the reconnect budget on a session
    # nobody is going to use.
    FAIL_FAST = 'fail_fast'

  _ssh: cros_ssh_lib.SSHProxy | None = None
  _services: service_manager.ServiceManager | None = None
  _build_info: build_info_lib.BuildInfo | None = None

  def __init__(self, config: device_config.DeviceConfig) -> None:
    self.config = config
    self._pending_recovery_services: list[str] = []
    self._orphan_remote_pids: list[int] = []
    # The device's boot ID as seen over the most recent SSH session, and
    # whether the session before that saw a different one. Remote PIDs and
    # live services are always recorded under the session that is being
    # replaced, so comparing boot IDs across the replacement tells whether the
    # device rebooted in between; see `_establish_ssh()`.
    self._boot_id: str | None = None
    self._rebooted_since_last_session = False
    # Bumped every time a new session is established; see `ssh_generation`.
    self._ssh_generation = 0
    # Reentrant because `_reconnect_and_recover()` takes it both when a caller
    # asks for recovery through `recover_services()` and when the `ssh`
    # property notices a dead session on its own.
    self._recovery_lock = threading.RLock()
    self._policy = self._RecoveryPolicy.RECOVER

    # logging.log_path only exists when this is used in a Mobly test run.
    log_path_base = getattr(logging, 'log_path', '/tmp/logs')
    device_log_directory_name = mobly_logger.sanitize_filename(
        f'{self._CONTROLLER_LOG_NAME}_{config.hostname}:{config.ssh_port}'
    )
    self.log_path = os.path.join(log_path_base, device_log_directory_name)
    self._debug_tag = f'{config.hostname}:{config.ssh_port}'
    self.log = mobly_logger.PrefixLoggerAdapter(
        logging.getLogger(),
        {
            mobly_logger.PrefixLoggerAdapter.EXTRA_KEY_LOG_PREFIX: (
                f'[{self._CONTROLLER_LOG_NAME}|{self.debug_tag}]'
            )
        },
    )

  @property
  def debug_tag(self) -> str:
    """A string that represents this device in the debug info.

    This will be used as part of the prefix of debugging messages emitted by
    this device object, like log lines and the message of DeviceError. Default
    value is 'hostname:ssh_port'.
    """
    return self._debug_tag

  @debug_tag.setter
  def debug_tag(self, tag: str) -> None:
    """Setter for the debug tag."""
    self.log.set_log_prefix(f'[{self._CONTROLLER_LOG_NAME}|{tag}]')
    self.log.debug('Logging debug tag set to "%s"', tag)
    self._debug_tag = tag

  def __repr__(self) -> str:
    return f'<{self._CONTROLLER_LOG_NAME}|{self.debug_tag}>'

  def destroy(self) -> None:
    """Tears RpiDevice object down.

    This function releases following resources:
      * SSH session
      * Long-running services:
        * Audio recorder
        * BES manager
        * TWS manager

    Runs under the recovery lock, so a recovery another thread is in the
    middle of finishes before the services it is bringing back are torn down.
    """
    with self._recovery_lock:
      if not self.is_ssh_connected:
        self._close_ssh()

      if self._services is not None:
        try:
          with self._recovery_policy(self._RecoveryPolicy.FAIL_FAST):
            # A paused service reports itself not alive, so `unregister_all()`
            # would skip its `stop()` and leak what it still holds. The
            # services still pending from a recovery are exactly the paused
            # ones.
            for service_name in self._pending_recovery_services:
              service = getattr(self._services, service_name, None)
              if service is None:
                continue
              try:
                service.stop()
              except Exception:
                # Same leniency as `unregister_all()`: one service's failing
                # teardown must not keep the others from being torn down.
                self.log.exception(
                    'Failed to stop the paused service %s.', service_name
                )
            self._services.unregister_all()
        finally:
          self._services = None

      self._pending_recovery_services.clear()
      self._orphan_remote_pids.clear()

      # Closes the SSH session.
      self._close_ssh()

  @property
  def build_info(self) -> build_info_lib.BuildInfo:
    """Gets the build information of this Raspberry Pi device."""
    if self._build_info is None:
      self._build_info = build_info_lib.BuildInfo.collect(self.ssh)
    return self._build_info

  def get_info(self) -> dict[str, Any]:
    """Gets the build information of this Raspberry Pi device."""
    info = dataclasses.asdict(self.build_info)
    if not isinstance(
        getattr(self.services, self._BuiltinServices.TWS_MANAGER.value),
        self._PlaceholderService,
    ):
      info['tws_version'] = self.services.tws_manager.version
      info['tws_is_v2'] = self.services.tws_manager.is_v2
    if not isinstance(
        getattr(self.services, self._BuiltinServices.BES_MANAGER.value),
        self._PlaceholderService,
    ):
      info['bes_version'] = self.services.bes_manager.version
      info['bes_is_v2'] = self.services.bes_manager.is_v2
    return info

  @property
  def is_ssh_connected(self) -> bool:
    """True if there is a live SSH session to the device.

    Reading this never establishes a connection, which is what makes it usable
    from teardown paths and from the `is_alive` of services that would
    otherwise trigger a reconnect just to answer the question.
    """
    session = self._ssh
    return session is not None and session.is_connected

  @property
  def rebooted_since_last_session(self) -> bool:
    """Whether the most recent session found a different boot than the one before.

    Only meaningful right after a recovery or a reboot: a service that brings
    itself back afterwards uses it to decide between resuming on the same
    board state and starting afresh on a power-cycled board.
    """
    return self._rebooted_since_last_session

  @property
  def ssh_generation(self) -> int:
    """A counter that changes whenever `ssh` starts handing out a new session.

    Services that keep remote processes alive on the session, such as the BES
    boards' serial log readers, remember the generation they started them on.
    A different value later means the session was replaced in the meantime,
    possibly by another caller's recovery, and whatever they held on the old
    one is gone even though `is_ssh_connected` is True again.
    """
    return self._ssh_generation

  @property
  def ssh(self) -> cros_ssh_lib.SSHProxy:
    """The ssh connection to the RpiDevice device.

    If the cached connection is no longer usable, e.g. the session was severed
    while the device sat idle, it is discarded here and a new one is
    established along with recovering any running device services. Without this
    the device object would keep handing out a dead connection for the rest of
    the test class, since Mobly creates the controller once in `setup_class`
    and shares it across every test method.

    Raises:
      ConnectionError: If accessed while tearing down services when no active
        SSH session exists.
    """
    # TODO: Replace the SSH dep with the SSH module for posix
    # Validate and replace the session under the recovery lock, so that a
    # caller arriving while another thread is already recovering, e.g. the two
    # boards of a TWS pair driven concurrently, waits for that recovery and
    # then receives its session instead of closing it or opening a second one.
    # The lock is reentrant, so the recovering thread itself can still read
    # this property while it restarts the services. The policy is read inside
    # the lock for the same reason: a recovery pauses services under
    # `FAIL_FAST`, and a concurrent caller has to wait that out rather than
    # fail.
    with self._recovery_lock:
      policy = self._policy
      if policy is self._RecoveryPolicy.FAIL_FAST and not self.is_ssh_connected:
        raise ConnectionError(
            'The SSH connection to the device is no longer active.'
        )

      if self._ssh is not None and not self._ssh.is_connected:
        if policy is self._RecoveryPolicy.RECOVER:
          self._reconnect_and_recover()
        else:
          self.log.warning(
              'The SSH connection to the device dropped during recovery or'
              ' reboot, reconnecting.'
          )
          self._close_ssh()
      elif (
          self._ssh is None
          and self._pending_recovery_services
          and policy is self._RecoveryPolicy.RECOVER
      ):
        self._reconnect_and_recover()

      if self._ssh is None:
        return self._establish_ssh()
      return self._ssh

  @contextlib.contextmanager
  def _recovery_policy(self, policy: _RecoveryPolicy) -> Iterator[None]:
    """Puts `policy` in force for the block and restores the previous one.

    Nests: `reboot()` runs under `RECONNECT_ONLY` and stops its services under
    `FAIL_FAST` inside that, after which `RECONNECT_ONLY` is back in force.
    Callers hold `_recovery_lock`, which is what keeps the policy from being
    observed half-way by a concurrent reader of `ssh`.
    """
    previous = self._policy
    self._policy = policy
    try:
      yield
    finally:
      self._policy = previous

  def _connect_ssh(self) -> cros_ssh_lib.SSHProxy:
    """Opens a new SSH connection, retrying a failed attempt a few times.

    Each attempt is bounded by `SSH_CONNECT_TIMEOUT_SECONDS`, and up to
    `SSH_CONNECT_MAX_ATTEMPTS` are made with an exponential backoff between
    them. A fresh `SSHProxy` is built per attempt, because Paramiko leaves a
    client whose connect failed in an unspecified state.

    Callers that drive their own retry loop, such as
    `_wait_for_boot_completion`, still see the underlying Paramiko exception
    when every attempt fails, so they can keep selecting on its type.

    Returns:
      The established SSH connection.

    Raises:
      Whatever the last attempt raised, unchanged.
    """
    delay = self.SSH_CONNECT_RETRY_DELAY_SECONDS
    for attempt in range(1, self.SSH_CONNECT_MAX_ATTEMPTS):
      try:
        return self._open_ssh_connection()
      except SSH_ERRORS as e:
        # Logged at DEBUG, without a traceback, because a failed attempt is
        # expected while the device is coming back up:
        # `_wait_for_boot_completion` drives its own polling loop over this
        # property, so anything louder repeats for every poll.
        self.log.debug(
            'SSH connection attempt %d of %d failed (%s), retrying in %gs.',
            attempt,
            self.SSH_CONNECT_MAX_ATTEMPTS,
            e,
            delay,
        )
        time.sleep(delay)
        delay *= 2

    return self._open_ssh_connection()

  def _establish_ssh(self) -> cros_ssh_lib.SSHProxy:
    """Opens a new SSH session and notes whether the device rebooted meanwhile.

    The boot ID (`/proc/sys/kernel/random/boot_id`) is read once per session
    and compared with the one the previous session saw. A different ID means
    the device rebooted while no session was held: the remote processes the
    previous session left behind are gone and their PIDs may belong to
    unrelated processes by now, and anything cached about the boot, such as
    `build_info`, is stale.

    The orphan PIDs on record are settled right here, on the new session: they
    were all recorded under the previous session, so this is the one moment
    their fate is known for certain. After this returns the list is empty and
    everything added to it later belongs to the boot this session sees.

    Returns:
      The new session, which is also stored in `_ssh`.

    Raises:
      Whatever connecting raised, or reading the boot ID over the new session
      raised; in the latter case the session is closed again first.
    """
    session = self._connect_ssh()
    try:
      boot_id = session.execute_command(
          _READ_BOOT_ID_COMMAND, timeout=_RECOVERY_COMMAND_TIMEOUT_SECONDS
      ).strip()
    except SSH_ERRORS:
      try:
        session.disconnect()
      except SSH_ERRORS:
        pass
      raise
    rebooted = self._boot_id is not None and boot_id != self._boot_id
    if rebooted:
      self.log.info(
          'The device rebooted since the previous SSH session (boot ID %s ->'
          ' %s).',
          self._boot_id,
          boot_id,
      )
      self._build_info = None
    self._boot_id = boot_id
    self._rebooted_since_last_session = rebooted
    self._ssh = session
    self._ssh_generation += 1
    self._reap_orphan_remote_pids(session, rebooted=rebooted)
    return session

  def _open_ssh_connection(self) -> cros_ssh_lib.SSHProxy:
    """Opens a single SSH connection to the device."""
    ssh_connection = cros_ssh_lib.SSHProxy(
        self.config.hostname,
        self.config.ssh_port,
        self.config.username,
        self.config.password,
        keyfile=self.config.keyfile,
        proxy_command=self.config.proxy_command,
    )
    ssh_connection.connect(
        self.SSH_CONNECT_TIMEOUT_SECONDS,
        self.SSH_BANNER_TIMEOUT_SECONDS,
        keepalive_interval=self.SSH_KEEPALIVE_INTERVAL_SECONDS,
    )
    return ssh_connection

  def _close_ssh(self) -> None:
    """Closes the cached SSH connection and clears it.

    Errors raised while tearing the connection down are logged and ignored,
    because this is also used on paths where the connection is already known to
    be unusable, e.g. after the device has been told to reboot. Closing the SFTP
    session and the transport both write to the socket, so they fail once the
    peer is gone.
    """
    if self._ssh is None:
      return
    try:
      self._ssh.disconnect()
    except SSH_ERRORS:
      self.log.debug(
          'Ignoring error while closing the SSH connection.', exc_info=True
      )
    finally:
      self._ssh = None

  @property
  def recovery_lock(self) -> threading.RLock:
    """The lock held while this device recovers from a lost SSH session.

    A service that decides on its own that recovery is needed takes this lock,
    checks again that recovery is still needed, and only then calls
    `recover_services()`. Without the second check, two callers that noticed
    the same lost session, such as both boards of a TWS pair driven
    concurrently, would recover twice, and the second recovery would tear down
    the services the first one had just restarted.
    """
    return self._recovery_lock

  @property
  def has_pending_recovery(self) -> bool:
    """True if services are still waiting to be brought back after a recovery.

    This is the case after a recovery that could not reconnect, e.g. because
    the network to the device was still down. Services that were paused by
    that attempt use it to tell "never started" apart from "waiting to come
    back", and to retry the recovery.
    """
    return bool(self._pending_recovery_services)

  def recover_services(self) -> None:
    """Re-establishes SSH and restarts the running services.

    Some services, like the BES boards behind `bes_manager` and `tws_manager`,
    talk to the device over an SSH session of their own. They lose that session
    together with this device's, but reading `ssh` would not notice when this
    device's own session was never opened or already recovered, and only this
    device knows how to reap the remote processes every service left behind and
    bring all of them back. Such services call this when they find their own
    session gone.

    If this device's own session is already live, another caller has recovered
    the device in the meantime, or only the caller's session was lost. Then the
    session is kept: orphan PIDs an earlier recovery failed to reap are reaped
    over it, and any service still waiting to be restarted from an earlier
    recovery is started again, without disturbing the services that are
    already running.

    Raises:
      ConnectionError: If called while the device is tearing down its services
        or rebooting. Those paths bypass recovery on purpose, the same way the
        `ssh` property does, so a service that notices its session gone in the
        middle of them does not start a reconnect the caller is about to undo.
      Whatever reconnecting raised, if the device is still unreachable. The
      services stay recorded as pending, so a later call retries.
    """
    with self._recovery_lock:
      if self._policy is not self._RecoveryPolicy.RECOVER:
        raise ConnectionError(
            'The SSH connection to the device is no longer active while the'
            ' device is rebooting, recovering or stopping its services.'
        )
      if self.is_ssh_connected:
        # Anything still on the list survived a failed reap on this session,
        # which `_establish_ssh()` would have dropped had the device rebooted.
        assert self._ssh is not None
        self._reap_orphan_remote_pids(self._ssh, rebooted=False)
        self._resume_pending_services()
        return
      self._reconnect_and_recover()

  def _reconnect_and_recover(self) -> None:
    """Re-establishes a severed SSH session and brings live services back.

    Services are paused rather than stopped while the session is replaced.
    For the BES boards this is the difference between keeping the board in
    the state the test left it in and re-initializing it; the board does not
    care that the session carrying its serial stream went away. The one case
    where the services are started afresh instead is a device that rebooted
    while it was unreachable, because that power-cycled the boards as well.
    """
    with (
        self._recovery_lock,
        self._recovery_policy(self._RecoveryPolicy.RECONNECT_ONLY),
    ):
      self.log.warning(
          'The SSH connection to the device is no longer active, reconnecting'
          ' and recovering running services.'
      )
      self._record_pending_recovery_state()
      self._close_ssh()
      self._pause_all_services()
      self._establish_ssh()
      self._resume_pending_services()

  def _record_pending_recovery_state(self) -> None:
    """Records live services and orphan PIDs before pausing services."""
    self._record_orphan_remote_pids(self._collect_orphan_remote_pids())
    for service_name in self.services.list_live_services():
      if service_name not in self._pending_recovery_services:
        self._pending_recovery_services.append(service_name)

  def _record_orphan_remote_pids(self, pids: Iterable[int]) -> None:
    """Remembers remote PIDs to reap once the device is reachable again.

    Args:
      pids: The remote PIDs to remember. Ones already recorded are skipped.
    """
    for pid in pids:
      if pid not in self._orphan_remote_pids:
        self._orphan_remote_pids.append(pid)

  def _collect_orphan_remote_pids(self) -> list[int]:
    """Collects remote PIDs of active services before local handles are cleared.

    A service reports either a single `remote_pid` or, when it fans out over
    several remote processes like the BES and TWS managers do, a `remote_pids`
    list. Both are read here so no orphan is left behind. Every registered
    service is asked, not only the builtin ones, so a service a test registers
    itself is reaped the same way.

    Returns:
      The PIDs the live services still hold on the remote host.
    """
    if self._services is None:
      return []
    pids: list[int] = []

    def collect(service: base_service.BaseService) -> None:
      if isinstance(service, self._PlaceholderService):
        return
      pid = getattr(service, 'remote_pid', None)
      if isinstance(pid, int):
        pids.append(pid)
      service_pids = getattr(service, 'remote_pids', None)
      if isinstance(service_pids, list):
        pids.extend(pid for pid in service_pids if isinstance(pid, int))

    self._services.for_each(collect)
    return pids

  def _stop_all_services(self) -> None:
    """Stops all registered services with reconnection suppressed."""
    with self._recovery_policy(self._RecoveryPolicy.FAIL_FAST):
      self.services.stop_all()

  def _pause_all_services(self) -> None:
    """Pauses all live services with reconnection suppressed.

    Pausing lets a service drop what it holds on the dead session without
    giving up the state behind it; a service that does not tell the two apart
    simply stops, which is what Mobly's `BaseService.pause()` does by default.
    """

    def pause_if_alive(service: base_service.BaseService) -> None:
      if service.is_alive:
        service.pause()

    with self._recovery_policy(self._RecoveryPolicy.FAIL_FAST):
      self.services.for_each(pause_if_alive)

  def _reap_orphan_remote_pids(
      self, session: cros_ssh_lib.SSHProxy, rebooted: bool
  ) -> None:
    """Settles the orphan remote processes left behind by a severed session.

    Runs on the session handed in rather than on the `ssh` property, so it can
    never trigger a reconnect of its own while it works through the list.

    Args:
      session: The live session to kill the processes over.
      rebooted: Whether the device rebooted after the PIDs were recorded. If it
        did, the reboot has already reaped the processes and the kernel may have
        handed the same PIDs to unrelated ones since, so the list is dropped
        without killing anything. The SSH user is not root, so even a reused PID
        could only ever belong to another process of that user.
    """
    if not self._orphan_remote_pids:
      return
    pids_str = ' '.join(str(pid) for pid in self._orphan_remote_pids)
    if rebooted:
      self.log.debug(
          'The device rebooted after these remote processes were recorded,'
          ' not killing them: %s',
          pids_str,
      )
      self._orphan_remote_pids.clear()
      return
    try:
      self.log.debug('Killing orphan remote processes: %s', pids_str)
      session.execute_command(
          f'kill -9 {pids_str}',
          timeout=_RECOVERY_COMMAND_TIMEOUT_SECONDS,
          ignore_error=True,
      )
      self._orphan_remote_pids.clear()
    except SSH_ERRORS:
      self.log.warning(
          'Failed to kill orphan remote processes: %s', pids_str, exc_info=True
      )

  def _restart_pending_services(self) -> None:
    """Restarts services recorded before the SSH disconnect or reboot."""
    if not self._pending_recovery_services:
      return
    services_to_start = list(self._pending_recovery_services)
    try:
      self.services.start_services(services_to_start)
    finally:
      self._forget_recovered_services()

  def _resume_pending_services(self) -> None:
    """Resumes the services paused for a recovery on the new SSH session.

    A resumed service reattaches to the new session and leaves whatever runs
    behind it, such as a BES board, exactly as it was. That only holds if the
    device stayed up: a device that rebooted while it was unreachable, as
    `_establish_ssh()` found out from its boot ID, has power-cycled the boards
    too, so the paused services are started afresh instead, which
    re-initializes them.
    """
    if not self._pending_recovery_services:
      return
    services_to_resume = list(self._pending_recovery_services)
    try:
      if self._rebooted_since_last_session:
        self.log.info(
            'The device rebooted while it was unreachable, starting the'
            ' paused services afresh: %s',
            services_to_resume,
        )
        self.services.start_services(services_to_resume)
        return
      for service_name in services_to_resume:
        service = getattr(self.services, service_name)
        if not service.is_alive:
          service.resume()
    finally:
      self._forget_recovered_services()

  def _forget_recovered_services(self) -> None:
    """Drops services that came back from the pending list."""
    if not self.is_ssh_connected:
      return
    still_alive = set(self.services.list_live_services())
    self._pending_recovery_services = [
        s for s in self._pending_recovery_services if s not in still_alive
    ]

  class _PlaceholderService(base_service.BaseService):
    """A service class that serves as a placeholder."""

  @enum.unique
  class _BuiltinServices(enum.Enum):
    """Names of builtin services of the Bluetooth reference device."""

    AUDIO_RECORDER = 'audio_recorder'
    BES_MANAGER = 'bes_manager'
    TWS_MANAGER = 'tws_manager'

  _BUILTIN_SERVICE_NAMES: tuple[str, ...] = tuple(
      service.value for service in _BuiltinServices
  )

  @property
  def services(self) -> service_manager.ServiceManager:
    """The manager of long-running services on the device.

    Upon creating the service manager, placeholders for builtin services listed
    in `_BUILTIN_SERVICE_NAMES` are registered to avoid name collision.
    """
    if self._services is None:
      self._services = service_manager.ServiceManager(device=self)
      # Register placeholders for builtin services to avoid name collision.
      for service_name in self._BUILTIN_SERVICE_NAMES:
        self._services.register(service_name, self._PlaceholderService)

    return self._services

  @property
  def audio_recorder(self) -> audio_recorder_lib.AudioRecorder:
    """The module to record the audio output of the device.

    Accessing this property will register a service called `audio_recorder`,
    which records the audio output of the Bluetooth reference device.

    Usage example:
      ref_device = self.register_controller(rpi_device)[0]
      ref_device.audio_recorder.start()
    """
    return self._get_builtin_service(
        self._BuiltinServices.AUDIO_RECORDER,
        audio_recorder_lib.AudioRecorder,
        start_service=False,
    )

  @property
  def bes_manager(self) -> bes_manager_lib.BesManager:
    """The manager to control the BES board connected to the device.

    Accessing this property will register a service called `bes_manager`,
    which controls the BES board on the Raspberry Pi.

    Users need to specify `bes_board_serial_port` and `bes_board_bt_address` in
    the device config to use the BES manager.

    Usage example:
      ref_device = self.register_controller(rpi_device)[0]
      bes = ref_device.bes_manager
      bes.reboot()
    """
    if (
        self.config.bes_board_serial_port is None
        or self.config.bes_board_bt_address is None
    ):
      raise errors.BtReferenceError(_BES_INFO_NOT_SPECIFIED_ERROR_MSG)
    return self._get_builtin_service(
        self._BuiltinServices.BES_MANAGER,
        bes_manager_lib.BesManager,
        configs=bes_manager_lib.Config(
            serial_port=self.config.bes_board_serial_port,
            bluetooth_address=self.config.bes_board_bt_address,
        ),
        start_service=True,
    )

  @property
  def tws_manager(self) -> tws_manager_lib.TwsManager:
    """The manager to control two BES boards as a TWS pair on the device.

    Accessing this property will register a service called `tws_manager`,
    which controls two BES boards on the Raspberry Pi.

    Users need to specify `bes_board_serial_port` and `bes_board_bt_address` in
    the device config to use the TWS manager.

    Usage example:
      ref_device = self.register_controller(rpi_device)[0]
      tws = ref_device.tws_manager
      tws.reboot()
    """
    if (
        self.config.bes_board_serial_port is None
        or self.config.bes_board_bt_address is None
    ):
      raise errors.BtReferenceError(_BES_INFO_NOT_SPECIFIED_ERROR_MSG)
    return self._get_builtin_service(
        self._BuiltinServices.TWS_MANAGER,
        tws_manager_lib.TwsManager,
        configs=tws_manager_lib.Config(
            serial_port=self.config.bes_board_serial_port,
            bluetooth_address=self.config.bes_board_bt_address,
        ),
        start_service=True,
    )

  def _get_builtin_service(
      self,
      service_name: enum.Enum | str,
      service_class: type[base_service.BaseService],
      configs: Any = None,
      start_service: bool = True,
  ) -> Any:
    """Gets the service instance of builtin service.

    This method gets builtin services instances of Bluetooth reference device
    from the service manager. If the service is a placeholder, create a new
    service instance of the given service class.

    Args:
      service_name: The name or enum entry of the builtin service.
      service_class: The service class to instantiate.
      configs: Config object to pass to the service class's constructor.
      start_service: True for starting the service. False otherwise.

    Returns:
      The service instance registered with given alias.
    """
    alias = (
        service_name.value
        if isinstance(service_name, enum.Enum)
        else service_name
    )
    if isinstance(getattr(self.services, alias), self._PlaceholderService):
      self.services.unregister(alias)
      try:
        self.services.register(
            alias, service_class, configs=configs, start_service=start_service
        )
      except Exception:
        # Restore the placeholder for builtin services if service registration
        # encounters any error, and re-raise the exception.
        self.services.register(alias, self._PlaceholderService)
        raise

    return getattr(self.services, alias)

  def _wait_for_boot_completion(
      self, timeout: datetime.timedelta = _DEFAULT_TIMEOUT_REBOOT_COMPLETION
  ) -> None:
    """Waits for a SSH connection to be re-established.

    Args:
      timeout: The time to wait before timing out.

    Raises:
      errors.BtReferenceError: Raised if booting process timed out.
    """
    deadline = time.perf_counter() + timeout.total_seconds()
    while time.perf_counter() < deadline:
      try:
        self.ssh.execute_command('echo')
        return
      except SSH_ERRORS:
        # ssh connect or command execution may fail during certain period of
        # booting process, which is normal. Close any half-open session before
        # polling again.
        self._close_ssh()
      time.sleep(_DEFAULT_WAIT_TIME_REBOOT.total_seconds())

    raise errors.BtReferenceError(f'{repr(self)} {_REBOOT_TIMEOUT_ERROR_MSG}')

  def reboot(self) -> None:
    """Reboots the device.

    Runs under the recovery lock: a reboot records, stops and restarts the
    services itself, so a recovery that another thread notices the need for
    in the meantime waits for the reboot to finish rather than interleaving
    with it.
    """
    self.log.info('Rebooting Bluetooth reference device ...')
    with (
        self._recovery_lock,
        self._recovery_policy(self._RecoveryPolicy.RECONNECT_ONLY),
    ):
      self._record_pending_recovery_state()
      if not self.is_ssh_connected:
        self._close_ssh()
      self._stop_all_services()

      # Execute reboot command on the device.
      # Use execute_command_async here to avoid getting stuck in dangling
      # SSH connection during rebooting.
      try:
        self.ssh.execute_command_async('sudo reboot')
        time.sleep(_DEFAULT_WAIT_TIME_REBOOT.total_seconds())
      except SSH_ERRORS:
        self.log.warning(
            'Failed to send reboot command over SSH; the device may already'
            ' be rebooting or disconnected. Proceeding to wait for boot'
            ' completion.',
            exc_info=True,
        )

      # Close the old SSH connection.
      self._close_ssh()

      # Setup new SSH connection.
      self._wait_for_boot_completion()

      # On boot completion, invalidate the `build_info` cache since any
      # value it had from before boot completion is potentially invalid.
      self._build_info = None
      # The processes recorded above were settled by the session that boot
      # completion opened: dropped if the device really rebooted, killed if
      # it never received the reboot command and is still running them.
      # Restart services.
      self._restart_pending_services()
