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

"""Installs Mobly Bluetooth controller module."""

import setuptools

description = (
    'Mobly Bluetooth controller module for operating Bluetooth reference'
    ' devices and development boards in Mobly tests.'
)

install_requires = [
    'dacite',
    'immutabledict',
    'mobly>=1.12.2',
    'paramiko>=2.10.4',
    'python-dateutil',
    'typing-extensions',
]

setuptools.setup(
    name='mobly-bluetooth',
    version='0.1.0',
    author='Google LLC',
    author_email='mobly-github@googlegroups.com',
    description=description,
    license='Apache2.0',
    url='https://github.com/google/mobly-bluetooth',
    packages=setuptools.find_namespace_packages(
        include=['mobly.controllers.*']
    ),
    package_data={
        'mobly.controllers.bluetooth': ['tools/*'],
    },
    include_package_data=True,
    install_requires=install_requires,
    python_requires='>=3.11',
)
