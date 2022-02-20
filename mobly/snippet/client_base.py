# Copyright 2022 Google Inc.
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
"""Base class for clients that communicate with servers over a JSON RPC
interface.

The JSON protocol expected by this module is:

.. code-block:: json

  Request:
  {
    'id': <Required. Monotonically increasing integer containing the ID of this
          request.>,
    'method': <Required. String containing the name of the method to execute.>,
    'params': <Required. JSON array containing the arguments to the method,
              `null` if no positional arguments for the RPC method.>,
    'kwargs': <Optional. JSON dict containing the keyword arguments for the
              method, `null` if no positional arguments for the RPC method.>,
  }

  Response:
  {
    'error': <Required. String containing the error thrown by executing the
             method, `null` if no error occurred.>,
    'id': <Required. Int id of request that this response maps to.>,
    'result': <Required. Arbitrary JSON object containing the result of
              executing the method, `null` if the method could not be executed
              or returned void.>,
    'callback': <Required. String that represents a callback ID used to
                identify events associated with a particular CallbackHandler
                object, `null` if this is not a async RPC.>,
  }
"""

import abc
import json
import socket
import threading
import enum
import time
import contextlib

from mobly.controllers.android_device_lib import callback_handler
from mobly.controllers.android_device_lib import errors
from mobly.controllers.android_device_lib import jsonrpc_client_base

# Maximum logging length of Rpc response in DEBUG level when verbose logging is
# off.
_MAX_RPC_RESP_LOGGING_LENGTH = 1024

# The required filed names of Rpc response.
RPC_RESPONSE_REQUIRED_FIELDS = ['id', 'error', 'result', 'callback']

# Used for remind users to switch from deprecated interfaces to new interfaces.
DEPRECATED_MESSAGE_TEMPLATE = (
    '{old_interface} is deprecated and will be removed in a future version.'
    'Use {new_interface} instead.')


class StartServerStages:
  """The stages for the starting server process."""
  BEFORE_STARTING_SERVER = 'before_starting_server'
  DO_START_SERVER = 'do_start_server'
  BUILD_CONNECTION = 'build_connection'
  AFTER_STARTING_SERVER = 'after_starting_server'


class ClientBase(abc.ABC):
  """Base class for Json Rpc clients that connect to remote servers.

  Connects to a remote device running a jsonrpc-compatible server. Before
  sending rpcs, users need to call client's interface to start server. The
  server runs on the remote device. After finishing sending all rpcs, users
  need to call the interface to stop server.

  Attributes:
    package: (str) The user-visible name of the snippet library being
      communicated with.
    host_port: (int) The host port of this RPC client.
    device_port: (int) The device port of this RPC client.
    log: (Logger) The logger of the corresponding device controller.
    verbose_logging: (bool) If True, prints more detailed log
      information. Default is False.
  """

  def __init__(self, package, device):
    """
    Args:
      package: (str) The user-visible name of the snippet library being
        communicated with.
      device: (DeviceController) The device object associated with a client.
    """

    self.package = package
    self.host_port = None
    self.device_port = None
    self.log = device.log
    self.verbose_logging = True
    self._device = device
    self._counter = None
    self._lock = threading.Lock()
    self._event_client = None

  def __del__(self):
    self.disconnect()

  @contextlib.contextmanager
  def _start_server_run_one_stage(self, stage_name, stop_server_if_failed):
    """Context manager for running each stage of starting server.

    This context manager is responsible for the log of stage information
    and call stop server if needed.

    Args:
      stage_name: (str) The stage name showing in the log.
      stop_server_if_failed: (bool) Whether to stop server if this context
        manager catches an Exception.
    """
    flag = '[START SERVER]'
    start_stage_msg = flag + 'Running the stage %s.'
    finish_stage_msg = flag + 'Finished the stage %s.'
    error_msg = flag + 'Error in stage %s.'
    error_stop_server_msg = (flag +
                             'Failed to stop server after failure in stage %s.')

    self.log.info(start_stage_msg % stage_name)
    try:
      yield
    except Exception as e:
      # Log the stacktrace of `e` as re-raising doesn't preserve trace.
      self.log.error(error_msg, stage_name)
      if stop_server_if_failed:
        try:
          self.stop_server()
        except Exception:
          self.log.exception(error_stop_server_msg, stage_name)

      # Explicitly raise the original error from starting app.
      raise e

    self.log.info(finish_stage_msg, stage_name)

  def start_server(self):
    """Starts the server on the testing device and connects to it.

    This process contain four stages:
      - before_starting_server: prepares for starting the server, e.g.
      precheck, modify remote device's settings.
      - do_start_server: starts the server on the remote device.
      - build_connection: builds a connection with the server, typically this
      stage sends a handshake request to ensure the server is ready.
      - after_starting_server: does the things after the server is ready, e.g.
      get device information.

    After this, the self.host_port and self.device_port attributes must be
    set.

    Raises:
      snippet_client.ProtocolVersionError: When the server's protocol is
        unknown.
      snippet_client.AppStartPreCheckError: When the precheck fails.
      jsonrpc_client_base.ProtocolError: When there's some error in
        the sending the handshake.
    """
    self.log.info('Starting the server.')
    start_time = time.perf_counter()

    with self._start_server_run_one_stage(
        StartServerStages.BEFORE_STARTING_SERVER, False):
      self._before_starting_server()

    with self._start_server_run_one_stage(StartServerStages.DO_START_SERVER,
                                          True):
      self._do_start_server()

    with self._start_server_run_one_stage(StartServerStages.BUILD_CONNECTION,
                                          True):
      self.build_connection()

    with self._start_server_run_one_stage(
        StartServerStages.AFTER_STARTING_SERVER, True):
      self._after_starting_server()

    self.log.debug('Snippet %s started after %.1fs on host port %s.',
                   self.package,
                   time.perf_counter() - start_time, self.host_port)

  def _before_starting_server(self):
    """Prepares for starting the server.

    Must be implemented by subclasses.
    """

  def _do_start_server(self):
    """Starts the server on the remote device.

    The client has completed the preparations, so the client calls this
    function to start the server.

    Must be implemented by subclasses.
    """

  def build_connection(self, host_port=None):
    """Builds a connection with the server on the remote device.

    This is a proxy function to guarantee the base implementation of
    build_connection is called. This function resets the RPC id counter.

    Args:
      host_port: (int) If given, this is the host port from which to connect
        to remote device port. If not provided, find a new available port as
        host port.
    """
    self._counter = self._id_counter()
    self._build_connection(host_port)

  def _build_connection(self, host_port=None):
    """Builds a connection with the server on the remote device.

    The command to start the server has been already sent before calling this
    function. So the client builds a connection to it and sends a handshake
    to ensure the server is ready for upcoming rpcs.

    Must be implemented by subclasses.

    Args:
      host_port: (int) If given, this is the host port from which to connect
        to remote device port. If not provided, find a new available port as
        host port.
    """

  def _after_starting_server(self):
    """Does the things after the server is ready.

    Must be implemented by subclasses.
    """

  def __getattr__(self, name):
    """Wrapper for python magic to turn method calls into RPC calls."""

    def rpc_call(*args, **kwargs):
      return self._rpc(name, *args, **kwargs)

    return rpc_call

  def _id_counter(self):
    """Return a id generator."""
    i = 0
    while True:
      yield i
      i += 1

  def set_snippet_client_verbose_logging(self, verbose):
    """Switches verbose logging. True for logging full RPC response.

    By default it will only write max_rpc_return_value_length for Rpc return
    strings. If you need to see full message returned from Rpc, please turn
    on verbose logging.

    max_rpc_return_value_length will set to 1024 by default, the length
    contains full Rpc response in Json format, included 1st element "id".

    Args:
      verbose: (bool) If True, turns on verbose logging, if False turns off
    """
    self.log.info('Sets verbose logging to %s.', verbose)
    self.verbose_logging = verbose

  def restore_server_connection(self, port=None):
    """Reconnects to the server after device USB was disconnected.

    Instead of creating new instance of the client:
      - Uses the given port (or finds a new available host_port if none is
      given).
      - Tries to connect to remote server with selected port.

    Must be implemented by subclasses.

    Args:
      port: (int) If given, this is the host port from which to connect to
        remote device port. If not provided, find a new available port as host
        port.

    Raises:
      jsonrpc_client_base.AppRestoreConnectionError: When the app was not able
      to be reconnected.
    """

  def _rpc(self, method, *args, **kwargs):
    """Sends an rpc to the server.

    Args:
      method: (str) The name of the method to execute.
      args: (any) The positional arguments of the method.
      kwargs: (any) The keyword arguments of the method.

    Returns:
      The result of the rpc.

    Raises:
      jsonrpc_client_base.ProtocolError: Something went wrong with the
        protocol.
      jsonrpc_client_base.ApiError: The rpc went through, however executed with
        errors.
    """
    try:
      self._check_server_proc_running()
    except Exception:
      self.log.exception(
          'Server process running check failed, skip sending rpc method(%s).',
          method)
      raise

    with self._lock:
      apiid = next(self._counter)
      request = self._gen_rpc_request(apiid, method, *args, **kwargs)

      self.log.debug('Sending rpc %s.', request)
      response = self._send_rpc_request(request)
      self.log.debug('Rpc %s sent.', request)

      if self.verbose_logging:
        self.log.debug('Snippet received: %s', response)
      else:
        if _MAX_RPC_RESP_LOGGING_LENGTH >= len(response):
          self.log.debug('Snippet received: %s', response)
        else:
          self.log.debug('Snippet received: %s... %d chars are truncated',
                         response[:_MAX_RPC_RESP_LOGGING_LENGTH],
                         len(response) - _MAX_RPC_RESP_LOGGING_LENGTH)

    return self._parse_rpc_response(apiid, method, response)

  def _check_server_proc_running(self):
    """Checks whether the server is still running.

    If the server is not running, throws an error. As this function is called
    each time the client tries to send an rpc, this should be a quick check
    without affecting performance. Otherwise it is fine to not check anything.

    Must be implemented by subclasses.
    """

  def _gen_rpc_request(self, apiid, method, *args, **kwargs):
    """Genereates Json rpc request.

    Args:
      appid: (int) The id the this rpc.
      method: (str) The name of the method to execute.
      args: (any) The positional arguments of the method.
      kwargs: (any) The keyword arguments of the method.

    Returns:
      A string of the Json rpc request.
    """
    data = {'id': apiid, 'method': method, 'params': args}
    if kwargs:
      data['kwargs'] = kwargs
    request = json.dumps(data)
    return request

  def _send_rpc_request(self, request):
    """Sends Json rpc request to the server and gets response.

    The request and response are bot in string format. So if the connection
    with server provides interface with bytes, please transform it to string
    in the implementation of this funcion.

    Must be implemented by subclasses.

    Args:
      request: (str) A string of the rpc request.

    Returns:
      A string of the rpc response.
    """
    pass

  def _parse_rpc_response(self, apiid, method, response):
    """Parses the rpc response from the server.

    This function parses the response of the server and checks the response
    with the Mobly JSON RPC Protocol.

    Args:
      appid: (int) The actual id of this rpc. It should be the same with the id
        in response, otherwise throws an error.
      method: (str) The method name of this rpc.
      response: (str) A string of the Json rpc response.

    Returns:
      The result of the rpc. If sync rpc, returns the result field of
      the response. If async rpc, returns the callback handler object.
    """
    if not response:
      raise jsonrpc_client_base.ProtocolError(
          self._device,
          jsonrpc_client_base.ProtocolError.NO_RESPONSE_FROM_SERVER)
    result = json.loads(response)
    for field_name in RPC_RESPONSE_REQUIRED_FIELDS:
      if field_name not in result:
        raise jsonrpc_client_base.ProtocolError(
            self._device,
            jsonrpc_client_base.ProtocolError.RESPONSE_MISS_FIELD % field_name)

    if result['error']:
      raise jsonrpc_client_base.ApiError(self._device, result['error'])
    if result['id'] != apiid:
      raise jsonrpc_client_base.ProtocolError(
          self._device, jsonrpc_client_base.ProtocolError.MISMATCHED_API_ID)
    if result['callback'] is not None:
      return self._handle_callback(result['callback'], result['result'], method)
    return result['result']

  def _handle_callback(self, callback_id, ret_value, method_name):
    """Creates callback handler for an async rpc.

    Must be implemented by subclasses.

    Args:
      callback_id: (str) The callback ID for creating callback handler object.
      ret_value: (JSON Array) The result field of the rpc response.
      method_name: (str) The method name of this rpc.

    Returns:
      The callback handler object.
    """

  def stop_server(self):
    """Proxy function to guarantee the base implementation of
    _stop_server is called.
    """
    self.log.info('Stopping snippet %s.', self.package)
    self._stop_server()
    self.log.info('Snippet %s stopped.', self.package)

  def _stop_server(self):
    """Kills any running instance of the server.

    Must be implemented by subclasses.
    """

  def disconnect(self):
    """Closes the connection to the snippet server on the device.

    This is a unilateral disconnect from the client side, without tearing down
    the snippet server running on the device.

    The connection to the snippet server can be re-established by calling
    `restore_server_connection`.

    Must be implemented by subclasses.
    """

  # TODO(minghaoli): Clears these interfaces after siwtching to client v2.
  # Provides these interfaces as they are used by snippet_management_service.
  def stop_app(self):
    self.log.warning(
        DEPRECATED_MESSAGE_TEMPLATE.format(old_interface='stop_app',
                                           new_interface='stop_server'))
    self.stop_server()

  def restore_app_connection(self):
    self.log.warning(
        DEPRECATED_MESSAGE_TEMPLATE.format(
            old_interface='restore_app_connection',
            new_interface='restore_server_connection'))
    self.restore_server_connection()

  def start_app_and_connect(self):
    self.log.warning(
        DEPRECATED_MESSAGE_TEMPLATE.format(
            old_interface='start_app_and_connect',
            new_interface='start_server'))
    self.start_server()
