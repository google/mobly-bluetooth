# Mobly Bluetooth Controller

Mobly Bluetooth controller module for using Python code to operate Bluetooth
reference devices in Mobly tests.

It supports two kinds of reference devices:

-   A Raspberry Pi used as a Bluetooth reference peer (`RpiDevice`).
-   BES Bluetooth development boards, as a single board (`BesDevice`) or as a
    pair of true wireless earbuds (`TwsDevice`).

## Requirements

-   Python 3.11+
-   Mobly 1.12.2+

## Installation

```shell
pip install mobly-bluetooth
```

## Start to Use

Mobly Bluetooth controller is an add-on module for
[Mobly](https://github.com/google/mobly). To learn more about Mobly, visit
[Getting started with Mobly](https://github.com/google/mobly/blob/master/docs/tutorial.md).

### Ensure the reference device is reachable via SSH

Mobly tests run on a single host computer.

To use a Raspberry Pi reference device in Mobly tests, connect it to your local
network and confirm SSH access from the host machine.

### Write Mobly Device Configs

Write a testbed config to specify the information of the reference device. For
example:

**sample_config.yaml**

```yaml
TestBeds:
- Name: SampleBluetoothTestbed
  Controllers:
    RpiDevice:
    - hostname: 'IP_ADDRESS'
      username: 'USERNAME'
      password: 'PASSWORD'
```

NOTE: Replace `IP_ADDRESS`, `USERNAME`, and `PASSWORD` with the SSH login
information of the Raspberry Pi. You can use `keyfile` instead of `password` to
log in with an SSH key.

### Write a Hello World Mobly Test

This example test connects to the Raspberry Pi and prints its kernel version.

**hello_world_test.py**

```python
"""A basic Mobly test with a Raspberry Pi Bluetooth reference device."""

from mobly import base_test
from mobly import test_runner
from mobly.controllers.bluetooth import rpi_device


class HelloWorldTest(base_test.BaseTestClass):

  def setup_class(self):
    # Register controller for the Raspberry Pi reference device.
    self.ref = self.register_controller(rpi_device)[0]

  def test_print_kernel_version(self):
    output = self.ref.ssh.execute_command('uname -a')
    self.ref.log.info('Kernel version: %s', output)


if __name__ == '__main__':
  test_runner.main()
```

### Execute the Test

```bash
python hello_world_test.py -c sample_config.yaml
```

### Use BES development boards

BES boards are connected to the host computer over USB. Use `BesDevice` for a
single board, or `TwsDevice` for a pair of boards that act as true wireless
earbuds. Both are registered through the `bluetooth_reference_device` module:

```python
from mobly.controllers.bluetooth import bluetooth_reference_device

ref = self.register_controller(bluetooth_reference_device)[0]
```

## Security

Eligibility for the [Google Open Source Software Vulnerability Rewards
Program](https://bughunters.google.com/open-source-security) is determined by
the [Google Open Source Software Vulnerability Reward Program
Rules](https://bughunters.google.com/about/rules/open-source/google-open-source-software-vulnerability-reward-program-rules).

## License

This project is licensed under the Apache License, Version 2.0. See
[LICENSE](./LICENSE) for details.
