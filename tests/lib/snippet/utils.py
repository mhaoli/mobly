# Copyright 2017 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Util functions for snippet client test modules."""

import string
import random


MOCK_RESP_TEMPLATE = (
    '{"id": 0, "result": "%s", "error": null, "status": 0, "callback": null}')


def generate_fix_length_rpc_response(response_length):
  length = response_length - len(MOCK_RESP_TEMPLATE) + 2
  chars = string.ascii_letters + string.digits
  random_msg = ''.join(random.choice(chars) for _ in range(length))
  return MOCK_RESP_TEMPLATE % random_msg


def mock_android_device_for_client_test(
    package_name=None, snippet_runner=None, adb_proxy=None, mock_user_id=0):
  """Mock Android Device Controller used for testing snippet client."""
  adb_proxy = adb_proxy or mock_android_device.MockAdbProxy(
      instrumented_packages=[(package_name,
                              snippet_runner,
                              package_name)])
  ad = mock.Mock()
  ad.adb = adb_proxy
  ad.adb.current_user_id = mock_user_id
  ad.build_info = {
      'build_version_codename': ad.adb.getprop('ro.build.version.codename'),
      'build_version_sdk': ad.adb.getprop('ro.build.version.sdk'),
  }
  return ad
