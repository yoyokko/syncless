#! /usr/local/bin/stackless2.6

"""WSGI server library for Syncless.

This Python module implements a HTTP and HTTPS server which can server WSGI
web applications. Example use:

  from syncless import wsgi
  def WsgiApp(env, start_response):
    start_response('200 OK', [('Content-Type', 'text/html')])
    return ['Hello, World!']
  wsgi.RunHttpServer(WsgiApp)

Example use with yield:

  from syncless import wsgi
  def WsgiApp(env, start_response):
    start_response('200 OK', [('Content-Type', 'text/html')])
    yield 'Hello, '
    yield 'World!'
  wsgi.RunHttpServer(WsgiApp)

Minimal WebSocket server (see examples/demo_websocket_server.py for a useful
example with a web browser client):

  from syncless import wsgi
  def WsgiApp(env, start_response):
    if env.get('HTTP_UPGRADE') == 'WebSocket':
      web_socket = start_response('WebSocket', ())
      web_socket.write_msg('Hello!')
      for msg in iter(web_socket.read_msg, None):
        web_socket.write_msg('re(%s)' % msg)
      return ()
    start_response('404 Not Found', [('Content-Type', 'text/plain')])
    return 'not found: %s' % env['PATH_INFO']
  wsgi.RunHttpServer(WsgiApp)

See the following examples uses:

* examples/demo.py (WSGI web app for both HTTP and HTTPS)
* examples/demo_syncless_basehttp.py (BaseHTTPRequestHandler web app)
* examples/demo_syncless_cherrypy.py (CherryPy web app)
* examples/demo_syncless_web_py.py (web.py web app)
* examples/demo_syncless_webapp.py (Google AppEngine ``webapp'' web app)
* examples/demo_websocket_server.py for a useful WebSocket server

See http://www.python.org/dev/peps/pep-0333/ for more information about WSGI.

The most important entry point in this module is the WsgiListener method,
which accepts connections and serves HTTP requests on a socket (can be SSL).
There is also CherryPyWsgiListener, which uses the CherryPy's WSGI server
implementation in a Syncless-compatible, non-blocking way to achieve the
same goal as WsgiListener.

The convenience function RunHttpServer can be used in __main__ to run a HTTP
server forever, serving WSGI, BaseHTTPRequestHandler, CherrPy, web.py or
webapp applications.

WsgiListener and WsgiWorker take care of error detection and recovery. The
details:

* WsgiListener won't crash: it catches, reports and recovers from all I/O
  errors, HTTP request parse errors and also the exceptions raised by the
  WSGI application.
* WsgiListener won't emit an obviously invalid HTTP response (e.g. with
  binary junk in the response status code or in the response headers). It
  will emit a 400 (Bad Request) or an 500 (Internal Server Error) error page
  instead.
* WsgiListener counts the number of bytes sent in a response with
  Content-Length, and it won't ever send more than Content-Length. It also
  closes the TCP connection if too few bytes were sent.
* WsgiListener always calls the close() method of the response body iterable
  returned by the WSGI application, so the application can detect in the
  close method whether all data has been sent.
* WsgiListener prints unbloated exception stack traces when
  logging.root.setLevel(logging.DEBUG) is active.

WsgiWorker supports HTTP keep-alive and HTTP/1.1 request pipelining. Please
note that the total size of the pipelineable requests must not exceed a
small limit (about 8192 bytes). If that's exceeded, then later requests will
blocked until the response is returned for earlier requests.

FYI flush-after-first-body-byte is defined in the WSGI specification. An
excerpt: The start_response callable must not actually transmit the response
headers.  Instead, it must store them for the server or gateway to transmit
only after the first iteration of the application return value that yields a
non-empty string, or upon the application's first invocation of the write()
callable.  In other words, response headers must not be sent until there is
actual body data available, or until the application's returned iterable is
exhausted.  (The only possible exception to this rule is if the response
headers explicitly include a Content-Length of zero.)

Please note that the WebSocket WSGI bindings implemented in syncless.wsgi
are Syncless-specific, since WebSocket bindings are not standardized in the
WSGI specifications. gevent-websocket exposes a different binding API.

WsgiListener always listens on a single TCP port. It handles incoming
requests as HTTP by default (upgrade_ssl_callback=None), but it can be
configured to handle HTTPS requests or both HTTP and HTTPS (on the same
port). To use HTTPS, you need a private SSL key and a certificate here is
how you can generate one (with a self-signed certificate):

  $ sudo apt-get install openssl
  $ openssl genrsa -out my_key.pem 2048
  $ yes '' | openssl req -new -key my_key.pem -out my_cert.csr
  $ yes '' | openssl req -new -x509 -key my_key.pem -out my_cert.pem
  $ rm -f my_cert.csr
  $ ls -l my_cert.{csr,pem}

Once you have the cert and the key files, run WsgiListener like this for HTTPS:

  wsgi.WsgiListener(serverr_socket, wsgi_application, wsgi.SslUpgrader(
      {'certfile': 'my_cert.pem', 'keyfile': 'my_key.pem'}, use_http=False))

If you want WsgiListener to handle both HTTP and HTTPS on the same port
(deciding on-the-fly for each request, based on the 1st byte the client
sends), specify use_http=True instead above.

For HTTPS requests, WsgiWorker sets env['wgsi.url_scheme'] to 'https' and
env['HTTPS'] to 'on'.

Doc: WSGI server in stackless: http://stacklessexamples.googlecode.com/svn/trunk/examples/networking/wsgi/stacklesswsgi.py
Doc: WSGI specification: http://www.python.org/dev/peps/pep-0333/
TODO(pts): Validate this implementation with wsgiref.validate.
TODO(pts): Write access.log like BaseHTTPServer and CherryPy
"""

__author__ = 'pts@fazekas.hu (Peter Szabo)'

import errno
import logging
import re
import sys
import socket
import struct
import time
import traceback
import types

from syncless.best_stackless import stackless
from syncless import coio
from syncless import ssl_util

# TODO(pts): Add tests.

# It would be nice to ignore errno.EBADF, errno.EINVAL and errno.EFAULT, but
# that's a performance overhead.

HTTP_REQUEST_METHODS_WITH_BODY = ['POST', 'PUT', 'OPTIONS', 'TRACE']
"""HTTP request methods which can have a body (Content-Length)."""

COMMA_SEPARATED_REQHEAD = set(['ACCEPT', 'ACCEPT_CHARSET', 'ACCEPT_ENCODING',
    'ACCEPT_LANGUAGE', 'ACCEPT_RANGES', 'ALLOW', 'CACHE_CONTROL',
    'CONNECTION', 'CONTENT_ENCODING', 'CONTENT_LANGUAGE', 'EXPECT',
    'IF_MATCH', 'IF_NONE_MATCH', 'PRAGMA', 'PROXY_AUTHENTICATE', 'TE',
    'TRAILER', 'TRANSFER_ENCODING', 'UPGRADE', 'VARY', 'VIA', 'WARNING',
    'WWW_AUTHENTICATE'])
"""HTTP request headers which will be joined by comma + space.

The list was taken from cherrypy.wsgiserver.comma_separated_headers.
"""

REQHEAD_CONTINUATION_RE = re.compile(r'\n[ \t]+')
"""Matches HTTP request header line continuation."""

INFO = logging.INFO
DEBUG = logging.DEBUG

HEADER_WORD_LOWER_LETTER_RE = re.compile(r'(?:\A|-)[a-z]')

HEADER_KEY_RE = re.compile(r'[A-Za-z][A-Za-z-]*\Z')

HEADER_VALUE_RE = re.compile(r'[ -~]+\Z')

HTTP_RESPONSE_STATUS_RE = re.compile(r'[2-5]\d\d [A-Z][ -~]*\Z')

SUB_URL_RE = re.compile(r'\A/[-A-Za-z0-9_./,~!@$*()\[\]\';:?&%+=]*\Z')
"""Matches a HTTP sub-URL, as appearing in line 1 of a HTTP request."""

NON_DIGITS_RE = re.compile(r'\D+')

MAX_WEBSOCKET_MESSAGE_SIZE = 10 << 20
"""Maximum number of bytes in an incoming WebSocket message."""

HTTP_1_1_METHODS = ('GET', 'HEAD', 'POST', 'PUT', 'DELETE',
                    'OPTIONS', 'TRACE', 'CONNECT')

HTTP_VERSIONS = ('HTTP/1.0', 'HTTP/1.1')

KEEP_ALIVE_RESPONSES = (
    'Connection: close\r\n',
    'Connection: Keep-Alive\r\n')

WDAY = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
MON = ('', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep',
       'Oct', 'Nov', 'Dec')

HTTP_STATUS_STRINGS = {
    400: 'Bad Request',
    500: 'Internal Server Error',
}

if issubclass(socket.error, IOError):
  # Python2.6
  IOError_all = IOError
else:
  # Python2.5
  IOError_all = (IOError, socket.error)
if getattr(socket, '_ssl', None) and getattr(socket._ssl, 'SSLError', None):
  assert issubclass(socket._ssl.SSLError, IOError)

# ---

class WsgiErrorsStream(object):
  @classmethod
  def flush(cls):
    pass

  @classmethod
  def write(cls, msg):
    # TODO(pts): Buffer on newline.
    if logging.root.level <= DEBUG:
      if msg[-1:] == '\n':
        logging.debug(msg[:-1])
      else:
        logging.debug(msg)

  @classmethod
  def writelines(cls, msgs):
    for msg in msgs:
      cls.write(msg)


class WsgiReadError(IOError):
  """Raised when reading the HTTP request."""


class WebSocketMessageTruncatedError(WsgiReadError):
  """Raised when a WebSocket message has been truncated."""


class WebSocketInvalidFrameTypeError(WsgiReadError):
  """Raised when a message with invalid frame type is read from a WebSocket."""


class WebSocketMessageTooLargeError(WsgiReadError):
  """Raised when a message about to be read would be too large."""


class WsgiResponseSyntaxError(IOError):
  """Raised when parsing the HTTP request."""


class WsgiResponseBodyTooLongError(IOError):
  """Raised when the HTTP response body is logner than the Content-Length."""


class WsgiWriteError(IOError):
  """Raised when writing the HTTP response."""


def GetHttpDate(at):
  now = time.gmtime(at)
  return '%s, %02d %s %4d %02d:%02d:%02d GMT' % (
      WDAY[now[6]], now[2], MON[now[1]], now[0], now[3], now[4], now[5])


def RespondWithBad(status, date, server_software, sockfile, reason):
  status_str = HTTP_STATUS_STRINGS[status]
  if reason:
    msg = '%s: %s' % (status_str, reason)
  else:
    msg = status_str
  # TODO(pts): Add Server: and Date:
  sockfile.write('HTTP/1.0 %s %s\r\n'
                 'Server: %s\r\n'
                 'Date: %s\r\n'
                 'Connection: close\r\n'
                 'Content-Type: text/plain\r\n'
                 'Content-Length: %d\r\n\r\n%s\n' %
                 (status, status_str, server_software, date, len(msg) + 1, msg))
  sockfile.flush()


def ReportAppException(exc_info, which='app'):
  exc = 'error calling WSGI %s: %s.%s: %s' % (
      which, exc_info[1].__class__.__module__, exc_info[1].__class__.__name__,
      exc_info[1])
  if logging.root.level <= DEBUG:
    exc_line1 = exc
    exc = traceback.format_exception(
        exc_info[0], exc_info[1], exc_info[2].tb_next)
    exc[:1] = [exc_line1,
               '\nTraceback of WSGI %s call (most recent call last):\n'
               % which]
    exc = ''.join(exc).rstrip('\n')
  # TODO(pts): Include the connection id in the log message.
  logging.error(exc)


def ConsumerWorker(items, is_debug):
  """Stackless tasklet to consume the rest of a wsgi_application output.

  Args:
    items: Iterable returned by the call to a wsgi_application.
    is_debug: Bool specifying whether debugging is enabled.
  """
  try:
    for data in items:  # This calls the WSGI application.
      pass
  except WsgiWriteError, e:
    if is_debug:
      logging.debug('error writing HTTP body response: %s' % e)
  except Exception, e:
    ReportAppException(sys.exc_info(), which='consume')
  finally:
    if hasattr(items, 'close'):  # According to the WSGI spec.
      try:
        items.close()
      except WsgiWriteError, e:
        if is_debug:
          logging.debug('error writing HTTP body response close: %s' % e)
      except Exception, e:
        ReportAppException(sys.exc_info(), which='consume-close')


def PrependIterator(value, iterator):
  """Iterator which yields value, then all by iterator."""
  yield value
  for item in iterator:
    yield item


def SendWildcardPolicyFile(env, start_response):
  """Helper function for WSGI applications to send the flash policy-file."""
  # The Content-Type doesn't matter, it won't be sent.
  start_response('200 OK', [('Content-Type', 'text/plain')])
  return ('<?xml version="1.0"?>\n'
          '<!DOCTYPE cross-domain-policy SYSTEM '
          '"http://www.macromedia.com/xml/dtds/cross-domain-policy.dtd">\n'
          '<cross-domain-policy>\n'
          '<allow-access-from domain="*" to-ports="%s"/>\n'
          '</cross-domain-policy>\n' % env['SERVER_PORT'],)


def IsWebSocketResponse(response_headers):
  """Return a bool indicating if HTTP response headers are for WebSocket."""
  retval = False
  for key, value in response_headers:
    if key.lower() == 'upgrade':
      retval = value == 'WebSocket'
  return retval


def GetWebSocketKey(value):
  """Return an 4-byte digest key from a Sec-WebSocket-Key{1,2} value.

  Raises:
   ValueError
  """
  number = int(NON_DIGITS_RE.sub('', value))
  spaces = value.count(' ')
  if number % spaces:
    raise ValueError('invalid number of spaces in web socket key: %r' % value)
  return struct.pack('>I', number / spaces)


class WebSocket(object):
  """A message-based bidirectional TCP connection with the WebSocket protocol.

  It is assumed that the WebSocket handshake is done before a WebSocket object
  gets created.

  TOOD(pts): Implement this in C (Pyrex) for speed.
  TODO(pts): Implement the close() method.
  """
  __slots__ = ['_rwfile']

  def __init__(self, rwfile):
    """rwfile must be a syncless.coio.nbfile."""
    if not isinstance(rwfile, coio.nbfile):
      raise TypeError
    self._rwfile = rwfile

  def read_msg(self):
    """Read one message (as a str) from a WebSocket file object.

    A byte string is always returned. It contains UTF-8 or binary data
    according to the WebSocket specification -- but that's not checked.
    """
    # TODO(pts): Create a unicode object for UTF-8 data if requested.
    f = self._rwfile
    frame_type = f.read(1)
    if not frame_type:
      return  # None
    if frame_type == '\xff':
      size = 0
      while True:
        i = f.read(1)
        if not i:
          raise WebSocketMessageTruncatedError
        i = ord(i)
        size = size * 128 + (i & 127)
        if not (i & 128):  # Stop if the highest bit is unset.
          break
      if size:  # TODO(pts): Remember EOF condition (e.g. shutdown).
        if size > MAX_WEBSOCKET_MESSAGE_SIZE:
          # Protect against out-of-memory.
          raise WebSocketMessageTooLargeError
        msg = f.read(size)
        if len(msg) < size:
          raise WebSocketMessageTruncatedError
        return msg
    elif frame_type == '\x00':
      while True:
        i = f.find('\xff')
        if i >= 0:
          msg = f.read(i)
          f.discard(1)  # '\xff'
          return msg
        if f.read_buffer_len >= MAX_WEBSOCKET_MESSAGE_SIZE:
          # Protect against out-of-memory.
          raise WebSocketMessageTooLargeError
        if not f.read_more(1):
          raise WebSocketMessageTruncatedError
    else:
      raise WebSocketInvalidFrameTypeError('%02X' % ord(frame_type))
    # return None

  def write_msg(self, msg):
    """Write a UTF-8 message (str or unicode) to a WebSocket.

    The order of the arguments is deliberate, so the defaults can be overridden.

    The message to be written must be valid UTF-8 (only it is checked that it
    doesn't contain \\xFF).
    """
    # TODO(pts): Add support for writing binary messages, once there is
    # consensus.
    if isinstance(msg, str):
      if '\xff' in msg:
        raise ValueError('byte \\xFF in UTF-8 WebSocket message')
      self._rwfile.write('\x00%s\xff' % msg)
    elif isinstance(msg, unicode):
      self._rwfile.write('\x00%s\xff' % msg.encode('UTF-8'))
    else:
      raise TypeError('expected WebSocket message, got %s' % type(msg))


def WsgiWorker(sock, peer_name, wsgi_application, default_env, date,
               do_multirequest, upgrade_ssl_callback):
  # TODO(pts): Implement the full WSGI spec
  # http://www.python.org/dev/peps/pep-0333/
  #
  # The implementation of this function is intentionally long: splitting it
  # up to smaller functions would hurt performance.

  if not isinstance(date, str):
    raise TypeError
  if not hasattr(sock, 'makefile_samefd'):  # isinstance(sock, coio.nbsocket)
    raise TypeError
  if not (upgrade_ssl_callback is None or callable(upgrade_ssl_callback)):
    raise TypeError

  EISDIR = errno.EISDIR
  loglevel = logging.root.level
  is_debug = loglevel <= DEBUG
  do_keep_alive_ary = [True]
  headers_sent_ary = [False]
  server_software = default_env['SERVER_SOFTWARE']

  if upgrade_ssl_callback is not None:
    # Make shallow copy because upgrade_ssl_callback may modify it in place.
    default_env = dict(default_env)
    sock = upgrade_ssl_callback(sock, default_env, is_debug)
    if sock is None:
      return

  sockfile = sock.makefile_samefd()
  sockfile.read_exc_class = WsgiReadError
  sockfile.write_exc_class = WsgiWriteError

  reqhead_continuation_re = REQHEAD_CONTINUATION_RE
  try:
    while do_keep_alive_ary[0]:
      do_keep_alive_ary[0] = False
      special_request_type = None

      # This enables the infinite write buffer so we can buffer the HTTP
      # response headers (without a size limit) until the first body byte.
      # Please note that the use of sockfile.write_buffer_len in this
      # function prevents us from using unbuffered output.  But unbuffered
      # output would be silly anyway since we send the HTTP response headers
      # line-by-line.
      sockfile.write_buffer_limit = 2
      # Ensure there is no leftover from the previous request.
      assert not sockfile.write_buffer_len, sockfile.write_buffer_len

      env = dict(default_env)
      env['REMOTE_HOST'] = env['REMOTE_ADDR'] = peer_name[0]
      env['REMOTE_PORT'] = str(peer_name[1])
      env['wsgi.errors'] = WsgiErrorsStream
      if date is None:  # Reusing a keep-alive socket.
        items = data = input = None
        # For efficiency reasons, we don't check now whether the child has
        # already closed the connection. If so, we'll be notified next time.

        # Let other tasklets make some progress before we serve our next
        # request.
        stackless.schedule(None)

      # Read HTTP/1.0 or HTTP/1.1 request. (HTTP/0.9 is not supported.)
      if is_debug:
        logging.debug('reading HTTP request on sock=%x' % id(sock))
      try:
        method, suburl, http_version, special_request_type, req_lines = (
            sockfile.read_http_reqhead(32768))
      except IndexError:
        if is_debug:
          logging.debug('HTTP request headers too long')
        return
      except EOFError:
        if is_debug:
          logging.debug('EOF in HTTP request headers')
        return
      except ValueError:
        if is_debug:
          logging.debug('syntax error parsing HTTP request headers')
        return
      except IOError_all, e:  # Raised in sockfile.read_at_most above.
        if is_debug and e[0] != errno.ECONNRESET:
          logging.debug('error reading HTTP request headers: %s' % e)
        return
      if special_request_type == 'ssl':
        if is_debug:
          logging.debug('found an uexpected SSL (http) request')
        return  # TODO(pts): Process the SSL request. Needs buffering changes.
      if date is None:
        date = GetHttpDate(time.time())
      if http_version not in HTTP_VERSIONS:
        RespondWithBad(400, date,
            server_software, sockfile, 'bad HTTP version: %r' % http_version)
        return  # Don't reuse the connection.
      # TODO(pts): Support more methods for WebDAV.
      if method not in HTTP_1_1_METHODS:
        RespondWithBad(400, date, server_software, sockfile, 'bad method')
        return  # Don't reuse the connection.
      if not SUB_URL_RE.match(suburl):
        if special_request_type != 'policy-file':
          # This also fails for HTTP proxy URLS http://...
          RespondWithBad(400, date, server_software, sockfile, 'bad suburl')
          return  # Don't reuse the connection.
      env['REQUEST_METHOD'] = method
      env['SERVER_PROTOCOL'] = http_version
      if is_debug:
        logging.debug(
            'on sock=%x %s %s' %
            (id(sock), method, re.sub(r'(?s)[?].*\Z', '?...', suburl)))
      # TODO(pts): What does appengine set here? wsgiref.validate recommends
      # the empty string (not starting with '.').
      env['SCRIPT_NAME'] = ''
      i = suburl.find('?')
      if i >= 0:
        env['PATH_INFO'] = suburl[:i]
        env['QUERY_STRING'] = suburl[i + 1:]
      else:
        env['PATH_INFO'] = suburl
        env['QUERY_STRING'] = ''

      content_length = None
      do_req_keep_alive = http_version == 'HTTP/1.1'  # False for HTTP/1.0
      for name_upper, value in req_lines:
        if name_upper == 'CONNECTION':
          do_req_keep_alive = value.lower() == 'keep-alive'
        elif name_upper == 'KEEP_ALIVE':
          pass  # TODO(pts): Implement keep-alive timeout.
        elif name_upper == 'CONTENT_LENGTH':
          try:
            content_length = int(value)
          except ValueError:
            RespondWithBad(400,
                date, server_software, sockfile, 'bad content-length')
            return
          env['CONTENT_LENGTH'] = value
        elif name_upper == 'CONTENT_TYPE':
          env['CONTENT_TYPE'] = value
        elif not name_upper.startswith('PROXY_'):
          key = 'HTTP_' + name_upper
          if key in env and name_upper in COMMA_SEPARATED_REQHEAD:
            # Fast (linear) version of the quadratic env[key] += ', ' + value.
            s = env[key]
            env[key] = ''
            s += ', '
            s += value
            env[key] = s
          else:
            env[key] = value
          # TODO(pts): Maybe override SERVER_NAME and SERVER_PORT from HTTP_HOST?
          # Does Apache do this?

      if content_length is None:
        if method in ('POST', 'PUT'):
          RespondWithBad(400,
              date, server_software, sockfile, 'missing content')
          return
        env['wsgi.input'] = input = coio.nblimitreader(None, 0)
        if ('HTTP_SEC_WEBSOCKET_KEY1' in env and
            'HTTP_SEC_WEBSOCKET_KEY2' in env):
          content_length = 8
      elif method not in ('POST', 'PUT'):
        if content_length:
          RespondWithBad(400,
              date, server_software, sockfile, 'unexpected content')
          return
        content_length = None
        del env['CONTENT_LENGTH']

      if content_length:  # TODO(pts): Test this branch.
        # TODO(pts): Avoid the memcpy() in unread.
        env['wsgi.input'] = input = coio.nblimitreader(sockfile, content_length)
      else:
        env['wsgi.input'] = input = coio.nblimitreader(None, 0)

      is_not_head = method != 'HEAD'
      res_content_length_ary = []
      headers_sent_ary[0] = False

      def WriteHead(data):
        """HEAD write() callback returned by StartResponse to the app."""
        data = str(data)
        if not data:
          return
        data = None  # Save memory.
        if not headers_sent_ary[0]:
          do_keep_alive_ary[0] = do_req_keep_alive
          sockfile.write(KEEP_ALIVE_RESPONSES[do_keep_alive_ary[0]])
          sockfile.write('\r\n')
          if input.discard_to_read_limit():
            raise WsgiReadError(EISDIR, 'could not discard HTTP request body')
          sockfile.flush()
          if not do_keep_alive_ary[0]:
            try:
              sock.close()
            except IOError_all, e:
              raise WsgiWriteError(*e.args)
          headers_sent_ary[0] = True

      def WriteNotHead(data):
        """Non-HEAD write() callback returned by StartResponse, to the app."""
        data = str(data)
        if not data:
          return
        if headers_sent_ary[0]:
          if res_content_length_ary:
            res_content_length_ary[1] -= len(data)
            if res_content_length_ary[1] < 0:
              sockfile.write(data[:res_content_length_ary[1]])
              raise WsgiResponseBodyTooLongError
          # Autoflush because we've set up sockfile.write_buffer_limit = 0
          # previously.
          sockfile.write(data)
        else:
          do_keep_alive_ary[0] = bool(
              do_req_keep_alive and res_content_length_ary)
          sockfile.write(KEEP_ALIVE_RESPONSES[do_keep_alive_ary[0]])
          sockfile.write('\r\n')
          if res_content_length_ary:
            res_content_length_ary[1] -= len(data)
            if res_content_length_ary[1] < 0:
              sockfile.flush()
              sockfile.write_buffer_limit = 0
              sockfile.write(data[:res_content_length_ary[1]])
              raise WsgiResponseBodyTooLongError
          if 0 < len(data) <= 65536:  # TODO(pts): Wy do we need this?
            sockfile.write(data)
            sockfile.flush()
            sockfile.write_buffer_limit = 0  # Unbuffered (autoflush).
          else:
            sockfile.flush()
            sockfile.write_buffer_limit = 0  # Unbuffered (autoflush).
            sockfile.write(data)
          headers_sent_ary[0] = True
          if input.discard_to_read_limit():
            raise WsgiReadError(EISDIR, 'could not discard HTTP request body')

      def StartResponse(status, response_headers, exc_info=None):
        """Callback called by wsgi_application."""
        # Just ignore exc_info, because we don't have to re-raise it since
        # we haven't sent any headers yet.

        # StartResponse called again by an error handler: ignore the headers
        # previously buffered, so we can start generating the error header.
        if sockfile.write_buffer_len:
          sockfile.discard_write_buffer()
          del res_content_length_ary[:]

        if HTTP_RESPONSE_STATUS_RE.match(status) and status[-1].strip():
          pass
        elif status == 'WebSocket':
          # This is non-standard behavior, because handling WebSocket
          # connections is not specified in the WSGI specification.
          ws_http_version = max(http_version, 'HTTP/1.1')
          origin = env.get('HTTP_ORIGIN') or 'http://%s' % env['HTTP_HOST']
          if env.get('wsgi.url_scheme') == 'https':
            ws_protocol = 'wss'
          else:
            ws_protocol = 'ws'
          location = '%s://%s%s' % (
              ws_protocol, env['HTTP_HOST'], env['PATH_INFO'])
          key1 = env.get('HTTP_SEC_WEBSOCKET_KEY1')  # '2\\;!0=9"  8915i  6Fr\\2 S0 p'
          key2 = env.get('HTTP_SEC_WEBSOCKET_KEY2')  # "2'_a 7  729  =$ec,0P  1I 13*  R0"
          if key1 and key2:
            from hashlib import md5  # Present in both Python 2.5 and 2.6.
            key3 = sockfile.read(8)
            ws_head = ['Sec-WebSocket-Origin: %s\r\n' % origin,
                       'Sec-WebSocket-Location: %s\r\n' % location]
            ws_digest = md5(GetWebSocketKey(key1) + GetWebSocketKey(key2) +
                            key3).digest()   # 16 bytes
          else:  # Old, WebSocket draft 75.
            # TODO(pts): Flag to disable old (draft 75) connections. Are they
            # insecure?
            ws_head = ['WebSocket-Origin: %s\r\n' % origin,
                       'WebSocket-Location: %s\r\n' % location]
            ws_digest = ''
          for key, value in response_headers:
            key = key.lower()
            if (key not in ('status', 'server', 'date', 'connection',
                            'charset', 'upgrade', 'set-cookie') and
                not key.startswith('proxy-') and
                not key.startswith('content-')):
              key_capitalized = HEADER_WORD_LOWER_LETTER_RE.sub(
                  lambda match: match.group(0).upper(), key)
              value = str(value).strip()
              if not HEADER_VALUE_RE.match(value):
                raise WsgiResponseSyntaxError('invalid value for key %r: %r' %
                                              (key_capitalized, value))
              # TODO(pts): Eliminate duplicate keys (except for set-cookie).
              ws_head.append('%s: %s\r\n' % (key_capitalized, value))

          # TODO(pts): Add more response headers from response_headers.
          sockfile.write(
              '%s 101 Web Socket Protocol Handshake\r\n'
              'Upgrade: WebSocket\r\nConnection: Upgrade\r\n%s\r\n%s' %
              (ws_http_version, ''.join(ws_head), ws_digest))
          # TODO(pts): Add methods read_msg and write_msg instead.
          # A callback for closing the connection is currently not
          # implemented, neither by sending '\xff\x00', nor by closing the
          # TCP connection. TODO(pts): Implement this once there is consensus.
          sockfile.flush()
          sockfile.write_buffer_limit = 0  # Unbuffered.
          headers_sent_ary[0] = True
          do_keep_alive_ary[0] = False
          return WebSocket(sockfile)
        else:
          raise WsgiResponseSyntaxError('bad HTTP response status: %r' % status)
        if special_request_type:  # e.g. 'policy-file'
          headers_sent_ary[0] = True
        else:
          sockfile.write('%s %s\r\n' % (http_version, status))  # HTTP/1.0
          sockfile.write('Server: %s\r\n' % server_software)
          sockfile.write('Date: %s\r\n' % date)
          for key, value in response_headers:
            key = key.lower()
            if (key not in ('status', 'server', 'date', 'connection') and
                not key.startswith('proxy-') and
                # Apache responds with content-type for HEAD requests.
                (is_not_head or key not in ('content-length',
                                            'content-transfer-encoding'))):
              if key == 'content-length':
                del res_content_length_ary[:]
                try:
                  res_content_length_ary.append(int(str(value)))
                  # Number of bytes remaining. Checked and updated only for
                  # non-HEAD respones.
                  res_content_length_ary.append(res_content_length_ary[-1])
                except ValueError:
                  raise WsgiResponseSyntaxError('bad content-length: %r' % value)
              elif not HEADER_KEY_RE.match(key):
                raise WsgiResponseSyntaxError('invalid key: %r' % key)
              key_capitalized = HEADER_WORD_LOWER_LETTER_RE.sub(
                  lambda match: match.group(0).upper(), key)
              value = str(value).strip()
              if not HEADER_VALUE_RE.match(value):
                raise WsgiResponseSyntaxError('invalid value for key %r: %r' %
                                              (key_capitalized, value))
              # TODO(pts): Eliminate duplicate keys (except for set-cookie).
              sockfile.write('%s: %s\r\n' % (key_capitalized, value))

        # Don't flush yet.
        if is_not_head:
          return WriteNotHead
        else:
          return WriteHead

      # TODO(pts): Handle application-level exceptions here.
      try:
        items = wsgi_application(env, StartResponse) or ''
        if isinstance(items, types.GeneratorType) and not (
            sockfile.write_buffer_len or headers_sent_ary[0]):
          # Make sure StartResponse gets called now, by forcing the first
          # iteration (yield).
          try:
            item = items.next()  # Only this might raise StopIteration.
            if item:
              items = PrependIterator(item, items)
              item = None
          except StopIteration:
            item = None
      except WsgiReadError, e:
        if is_debug:
          logging.debug('error reading HTTP request body at call: %s' % e)
        return
      except WsgiWriteError, e:
        if is_debug:
          logging.debug('error writing HTTP response at call: %s' % e)
        return
      except Exception, e:
        ReportAppException(sys.exc_info(), which='start')
        if not headers_sent_ary[0]:
          # TODO(pts): Report exc on HTTP in development mode.
          sockfile.discard_write_buffer()
          try:
            RespondWithBad(500, date, server_software, sockfile, '')
          except WsgiWriteError, e:
            if is_debug:
              logging.debug('error writing HTTP response at start-500: %s' % e)
            return
          do_keep_alive_ary[0] = do_req_keep_alive
          continue
        if (do_req_keep_alive and res_content_length_ary and
            not (is_not_head and res_content_length_ary[1])):
          # The whole HTTP response body has been sent.
          do_keep_alive_ary[0] = True
          continue
        return

      try:
        if not (sockfile.write_buffer_len or headers_sent_ary[0]):
          logging.error('app has not called start_response')
          RespondWithBad(500, date, server_software, sockfile, '')
          return
        date = None
        if (isinstance(items, list) or isinstance(items, tuple) or
            isinstance(items, str)):
          if is_not_head:
            if isinstance(items, str):
              data = items
            else:
              data = ''.join(map(str, items))
          else:
            data = ''
          items = None
          if headers_sent_ary[0]:
            if (res_content_length_ary and
                len(data) != res_content_length_ary[1]):
              if len(data) > res_content_length_ary[1]:
                # SUXX: wget(1) will keep retrying here.
                logging.error(
                    'truncated content: header=%d remaining=%d body=%d'
                    % (res_content_length_ary[0], res_content_length_ary[1],
                       len(data)))
                data = data[:res_content_length_ary[1] - len(data)]
              else:
                logging.error(
                    'content length too large: header=%d remaining=%d body=%d'
                    % (res_content_length_ary[0], res_content_length_ary[1],
                       len(data)))
                do_keep_alive_ary[0] = False
          else:
            if input.discard_to_read_limit():
              raise WsgiReadError(EISDIR, 'could not discard HTTP request body')
            do_keep_alive_ary[0] = do_req_keep_alive
            if res_content_length_ary:
              if len(data) != res_content_length_ary[1]:
                logging.error(
                    'invalid content length: header=%d remaining=%d body=%d' %
                    (res_content_length_ary[0], res_content_length_ary[1],
                     len(data)))
                sockfile.discard_write_buffer()
                RespondWithBad(500, date, server_software, sockfile, '')
                continue
            else:
              if is_not_head:
                sockfile.write('Content-Length: %d\r\n' % len(data))
            sockfile.write(KEEP_ALIVE_RESPONSES[do_keep_alive_ary[0]])
            sockfile.write('\r\n')
          sockfile.write(data)
          sockfile.flush()
        elif is_not_head:
          if not headers_sent_ary[0]:
            do_keep_alive_ary[0] = bool(
                do_req_keep_alive and res_content_length_ary)
            sockfile.write(KEEP_ALIVE_RESPONSES[do_keep_alive_ary[0]])
            sockfile.write('\r\n')
          # TODO(pts): Speed: iterate over `items' below in another tasklet
          # as soon as Content-Length has been reached.
          #
          # This loop just waits for the first nonempty data item in the
          # HTTP response body.
          data = ''
          if res_content_length_ary:
            if res_content_length_ary[1]:
              for data in items:
                if data:
                  break
              res_content_length_ary[1] -= len(data)
              if res_content_length_ary[1] < 0:
                logging.error('truncated first yielded content')
                sockfile.flush()
                sockfile.write_buffer_limit = 0
                sockfile.write(data[:res_content_length_ary[1]])
                continue
          else:
            for data in items:
              if data:  # Implement flush-after-first-body-byte.
                break
          # TODO(pts): Catch and ignore TypeError in all these conversions.
          data = str(data)
          if 0 < len(data) <= 65536:
            sockfile.write(data)  # Still buffering it.
            sockfile.flush()
            sockfile.write_buffer_limit = 0  # Unbuffered.
          else:
            sockfile.flush()
            sockfile.write_buffer_limit = 0  # Unbuffered.
            sockfile.write(data)
          if input.discard_to_read_limit():
            raise WsgiReadError(
                EISDIR, 'could not discard HTTP request body')
          try:  # Call the WSGI application by iterating over `items'.
            if res_content_length_ary:
              for data in items:
                data = str(data)
                sockfile.write(data)
                res_content_length_ary[1] -= len(data)
                if res_content_length_ary[1] < 0:
                  logging.error('truncated yielded content')
                  sockfile.flush()
                  sockfile.write_buffer_limit = 0
                  sockfile.write(data[:res_content_length_ary[1]])
                  break
              if res_content_length_ary[1] > 0:
                logging.error('content length too large for yeald')
                # Ignore the rest in another tasklet.
                stackless.tasklet(ConsumerWorker)(items, is_debug)
                return
            else:
              for data in items:
                sockfile.write(str(data))
          except (WsgiReadError, WsgiWriteError):
            raise
          except Exception, e:
            ReportAppException(sys.exc_info(), which='yield')
            return
        else:  # HTTP HEAD response.
          if not headers_sent_ary[0]:
            do_keep_alive_ary[0] = do_req_keep_alive
            sockfile.write(KEEP_ALIVE_RESPONSES[do_keep_alive_ary[0]])
            sockfile.write('\r\n')
            if input.discard_to_read_limit():
              raise WsgiReadError(
                  EISDIR, 'could not discard HTTP request body')
            sockfile.flush()
            if not do_keep_alive_ary[0]:
              try:
                sock.close()
              except IOError_all, e:
                raise WsgiWriteError(*e.args)

          # Iterate over `items' below in another tasklet, so we can read
          # the next request asynchronously from the HTTP client while the
          # other tasklet is working.
          # TODO(pts): Is this optimization safe? Limit the number of tasklets
          # to 1 to prevent DoS attacks.
          stackless.tasklet(ConsumerWorker)(items, is_debug)  # Don't run it yet.
          items = None  # Prevent double items.close(), see below.

      except WsgiReadError, e:
        # This should not happen, iteration should not try to read.
        sockfile.discard_write_buffer()
        if is_debug:
          logging.debug('error reading HTTP request at iter: %s' % e)
        return
      except WsgiWriteError, e:
        sockfile.discard_write_buffer()
        if is_debug:
          logging.debug('error writing HTTP response at iter: %s' % e)
        return
      finally:
        if hasattr(items, 'close'):  # According to the WSGI spec.
          try:
            # The close() method defined in the app will be able to detect if
            # `for data in items' has iterated all the way through. For
            # example, when StartResponse was called with a too small
            # Content-Length, some of the items will not be reached, but
            # we call close() here nevertheless.
            items.close()
          except WsgiReadError, e:
            sockfile.discard_write_buffer()
            if is_debug:
              logging.debug('error reading HTTP request at close: %s' % e)
            return
          except WsgiWriteError, e:
            sockfile.discard_write_buffer()
            if is_debug:
              logging.debug('error writing HTTP response at close: %s' % e)
            return
          except Exception, e:
            sockfile.discard_write_buffer()
            ReportAppException(sys.exc_info(), which='close')
            return
      # TODO(pts): Implement wsgi_test.py without do_multirequest.
      if not do_multirequest:  # do_multirequest=False for testing.
        break
  finally:
    # Without this, when the function returns, sockfile.__del__ calls
    # sockfile.close calls sockfile.flush, which raises EBADF, because
    # sock.close() below has already been called.
    sockfile.discard_write_buffer()
    if do_multirequest:
      try:
        sock.close()
      except IOError_all:
        pass
    if is_debug:
      logging.debug('connection closed sock=%x' % id(sock))

  # Don't add code here, since we have many ``return'' calls above.


def PopulateDefaultWsgiEnv(env, server_socket):
  """Populate the default, initial WSGI environment dict."""
  env['wsgi.version']      = (1, 0)
  env['wsgi.multithread']  = True
  env['wsgi.multiprocess'] = False
  env['wsgi.run_once']     = False
  env['wsgi.url_scheme']   = 'http'
  env['HTTPS']             = 'off'
  if isinstance(server_socket, tuple):
    server_ipaddr, server_port = server_socket
  else:
    server_ipaddr, server_port = server_socket.getsockname()
  env['SERVER_PORT'] = str(server_port)
  env['SERVER_SOFTWARE'] = 'pts-syncless-wsgi'
  if server_ipaddr and server_ipaddr != '0.0.0.0':
    # TODO(pts): Do a canonical name lookup.
    env['SERVER_ADDR'] = env['SERVER_NAME'] = server_ipaddr
  else:  # Listens on all interfaces.
    # TODO(pts): Do a canonical name lookup.
    env['SERVER_ADDR'] = env['SERVER_NAME'] = socket.gethostname()


def WsgiListener(server_socket, wsgi_application, upgrade_ssl_callback=None):
  """HTTP or HTTPS server serving WSGI, listing on server_socket.

  WsgiListener should be run in is own tasklet.

  WsgiListener supports HTTP/1.0 and HTTP/1.1 requests (but not HTTP/0.9).

  WsgiListener is robust: it detects, reports and reports errors (I/O
  errors, request parse errors, invalid HTTP responses, and exceptions
  raised in the WSGI application code).

  WsgiListener supports HTTP Keep-Alive, and it will keep TCP connections
  alive indefinitely. TODO(pts): Specify a timeout.

  Args:
    server_socket: An acceptable coio.nbsocket or coio.nbsslsocket.
    upgrade_ssl_callback: A callable which takes (sock, env, is_debug), where
      sock is a coio.nbsocket
      and returns another coio.nbsocket (usually a coio.nbsslsocket, usually
      doing the server side of an SSL handshake), or None. The callback may
      modify the WSGI environment env in place. Must not raise an exception,
      but may return None. Please note that it
      is possible to make the WSGI server listen as both HTTP and HTTPS on the
      same port using an appropriate upgrade_ssl_callback calling
      accepted_socket.recv(..., socket.MSG_PEEK).
  """
  if not hasattr(server_socket, 'getsockname'):
    raise TypeError
  if not callable(wsgi_application):
    raise TypeError
  env = {}
  PopulateDefaultWsgiEnv(env, server_socket)
  try:
    while True:
      accepted_socket, peer_name = server_socket.accept()
      date = GetHttpDate(time.time())
      if logging.root.level <= DEBUG:
        logging.debug('connection accepted from=%r' % (peer_name,))
      stackless.tasklet(WsgiWorker)(
          accepted_socket, peer_name, wsgi_application, env, date, True,
          upgrade_ssl_callback)
      accepted_socket = peer_name = None  # Help the garbage collector.
  finally:
    server_socket.close()


class SslUpgrader(object):
  """A simple upgrade_ssl_callback implementation."""

  __slots__ = ['sslsock_kwargs', 'use_http']

  def __init__(self, sslsock_kwargs, use_http):
    """Constructor.

    Args:
      sslsock_kwargs: dict containing keyword arguments for ssl.SSLSocket.
        Example: {'certfile': ..., 'keyfile': ...}.
      use_http: bool indicating if HTTP connections are also allowed (in
        addition to HTTPS connections).
    """
    self.use_http = bool(use_http)
    self.sslsock_kwargs = dict(sslsock_kwargs)
    self.sslsock_kwargs['do_handshake_on_connect'] = False
    self.sslsock_kwargs['server_side'] = True
    ssl_util.validate_new_sslsock(**sslsock_kwargs)

  def __call__(self, sock, env, is_debug):
    if self.use_http:
      try:
        first_byte = sock.recv(1, socket.MSG_PEEK)
      except IOError_all, e:
        if is_debug:
          logging.debug('peeking first byte for SSL failed: %s' % e)
        return
      if first_byte not in ('\x80', '\x16'):  # Not SSL.
        return sock
    try:
      sock = coio.nbsslsocket(sock, **self.sslsock_kwargs)
      sock.do_handshake()
    except IOError_all, e:
      if is_debug:
        logging.debug('https SSL handshake failed: %s' % e)
      return
    env['wsgi.url_scheme'] = 'https'
    env['HTTPS'] = 'on'     # Apache sets this.
    return sock


# --- CherryPy WSGI

class FakeServerSocket(object):
  """A fake TCP server socket, used as CherryPyWSGIServer.socket."""

  __attrs__ = ['accepted_sock', 'accepted_addr']

  def __init__(self):
    self.accepted_sock = None
    self.accepted_addr = None

  def accept(self):
    """Return and clear self.accepted_sock.

    This method is called by CherryPyWSGIServer.tick().
    """
    accepted_sock = self.accepted_sock
    assert accepted_sock
    accepted_addr = self.accepted_addr
    self.accepted_sock = None
    self.accepted_addr = None
    return accepted_sock, accepted_addr

  def ProcessAccept(self, accepted_sock, accepted_addr):
    assert accepted_sock
    assert self.accepted_sock is None
    self.accepted_sock = accepted_sock
    self.accepted_addr = accepted_addr


class FakeRequests(object):
  """A list of HTTPConnection objects, for CherryPyWSGIServer.requests."""

  __slots__ = 'requests'

  def __init__(self):
    self.requests = []

  def put(self, request):
    # Called by CherryPyWSGIServer.tick().
    self.requests.append(request)


def CherryPyWsgiListener(server_socket, wsgi_application,
                         upgrade_ssl_callback=None):
  """HTTP or HTTPS server serving WSGI, using CherryPy's implementation.

  This function should be run in is own tasklet.

  Args:
    server_socket: An acceptable coio.nbsocket or coio.nbsslsocket.
    upgrade_ssl_callback: A callable which takes (sock, env, is_debug), where
      sock is a coio.nbsocket
      and returns another coio.nbsocket (usually a coio.nbsslsocket, usually
      doing the server side of an SSL handshake), or None. The callback may
      modify the WSGI environment env in place. Must not raise an exception,
      but may return None.
  """
  # !! TODO(pts): Speed: Why is CherryPy's /infinite twice as fast as ours?
  # Only sometimes.
  if not (isinstance(server_socket, coio.nbsocket) or
          isinstance(server_socket, coio.nbsslsocket)):
    raise TypeError
  if not callable(wsgi_application):
    raise TypeError
  try:
    # CherryPy-3.1.2 is known to work. Maybe it won't work with newer versions.
    from cherrypy import wsgiserver
  except ImportError:
    from web import wsgiserver  # Another implementation in (web.py).
  wsgi_server = wsgiserver.CherryPyWSGIServer(
      server_socket.getsockname(), wsgi_application)
  wsgi_server.ready = True
  wsgi_server.socket = FakeServerSocket()
  wsgi_server.requests = FakeRequests()
  wsgi_server.timeout = None  # TODO(pts): Fix once implemented.

  def UpgradeAndCommunicate(sock, http_connection):
    assert http_connection.socket is sock
    is_debug = logging.root.level <= DEBUG
    sock = upgrade_ssl_callback(sock, http_connection.environ, is_debug)
    if sock is not None:
      http_connection.socket = sock
      http_connection.rfile._sock = sock
      http_connection.wfile._sock = sock
    del sock
    http_connection.communicate()

  try:
    while True:
      sock, peer_name = server_socket.accept()
      if logging.root.level <= DEBUG:
        logging.debug('cpw connection accepted from=%r sock=%x' %
                      (peer_name, id(sock)))
      wsgi_server.socket.ProcessAccept(sock, peer_name)
      assert not wsgi_server.requests.requests
      wsgi_server.tick()
      assert len(wsgi_server.requests.requests) == 1
      http_connection = wsgi_server.requests.requests.pop()
      if upgrade_ssl_callback:
        stackless.tasklet(UpgradeAndCommunicate)(sock, http_connection)
      else:
        stackless.tasklet(http_connection.communicate)()
      # Help the garbage collector free memory early.
      http_connection = sock = peer_name = None
  finally:
    sock.close()


class FakeBaseHttpWFile(object):
  def __init__(self, env, start_response):
    self.env = env
    self.start_response = start_response
    self.wsgi_write_callback = None
    self.write_buf = []
    self.closed = False

  def write(self, data):
    data = str(data)
    if not data:
      return
    write_buf = self.write_buf
    if self.wsgi_write_callback is not None:
      write_buf.append(data)
    else:
      assert data.endswith('\r\n'), [write_buf, data]
      data = data.rstrip('\n\r')
      if data:
        write_buf.append(data)  # Buffer status and headers.
      else:
        assert len(write_buf) > 2  # HTTP/..., Server:, Date:
        assert write_buf[0].startswith('HTTP/')
        status = write_buf[0][write_buf[0].find(' ') + 1:]
        write_buf.pop(0)
        response_headers = [
            tuple(header_line.split(': ', 1)) for header_line in write_buf]
        # Set to `false' in case self.start_response raises an error.
        self.wsgi_write_callback = False
        self.wsgi_write_callback = self.start_response(
            status, response_headers)
        assert callable(self.wsgi_write_callback)
        del self.write_buf[:]

  def close(self):
    if not self.closed:
      self.flush()
    self.closed = True

  def flush(self):
    if self.wsgi_write_callback:
      if self.write_buf:
        data = ''.join(self.write_buf)
        del self.write_buf[:]
        if data:
          self.wsgi_write_callback(data)


class ConstantReadLineInputStream(object):
  """Used as self.rfile in the BaseHTTPRequestHandler subclass."""

  def __init__(self, lines, body_rfile):
    self.lines_rev = list(lines)
    self.lines_rev.reverse()
    self.closed = False
    self.body_rfile = body_rfile

  def readline(self):
    if self.lines_rev:
      return self.lines_rev.pop()
    else:
      return self.body_rfile.readline()

  def read(self, size):
    assert not self.lines_rev
    return self.body_rfile.read(size)

  def close(self):
    # We don't clear self.lines_rev[:], the hacked
    # WsgiInputStream doesn't do that eiter.
    # Don't ever close the self.body_rfile.
    self.closed = True


class FakeBaseHttpConnection(object):
  def __init__(self, env, start_response, request_lines):
    self.env = env
    self.start_response = start_response
    self.request_lines = request_lines

  def makefile(self, mode, bufsize):
    if mode.startswith('r'):
      rfile = self.env['wsgi.input']
      assert len(self.request_lines) > 1
      assert self.request_lines[-1] == '\r\n'
      assert self.request_lines[-2].endswith('\r\n')
      rfile = ConstantReadLineInputStream(self.request_lines, rfile)
      self.request_lines = None  # Save memory.
      return rfile
    elif mode.startswith('w'):
      return FakeBaseHttpWFile(self.env, self.start_response)


class FakeBaseHttpServer(object):
  pass


def HttpRequestFromEnv(env, connection=None):
  """Convert a CGI or WSGI environment to a HTTP request header.

  Returns:
    A list of lines, all ending with '\r\n', the last being '\r\n' for
    HTTP/1.x.
  """
  # TODO(pts): Add unit test.
  if not isinstance(env, dict):
    raise TypeError
  output = []
  path = (env['SCRIPT_NAME'] + env['PATH_INFO']) or '/'
  if env['QUERY_STRING']:
    path += '?'
    path += env['QUERY_STRING']
  if env['SERVER_PROTOCOL'] == 'HTTP/0.9':
    output.append('%s %s\r\n' % (env['REQUEST_METHOD'], path))
  else:
    output.append(
        '%s %s %s\r\n' %
        (env['REQUEST_METHOD'], path, env['SERVER_PROTOCOL']))
    for key in sorted(env):
      if key.startswith('HTTP_') and key not in (
          'HTTP_CONTENT_TYPE', 'HTTP_CONTENT_LENGTH', 'HTTP_CONNECTION'):
        name = re.sub(
            r'[a-z0-9]+', lambda match: match.group(0).capitalize(),
            key[5:].lower().replace('_', '-'))
        output.append('%s: %s\r\n' % (name, env[key]))
    if env['REQUEST_METHOD'] in HTTP_REQUEST_METHODS_WITH_BODY:
      # It should be CONTENT_LENGTH, not HTTP_CONTENT_LENGTH.
      content_length = env.get(
          'CONTENT_LENGTH', env.get('HTTP_CONTENT_LENGTH'))
      if content_length is not None:
        output.append('Content-Length: %s\r\n' % content_length)
      # It should be CONTENT_TYPE, not HTTP_CONTENT_TYPE.
      content_type = env.get('CONTENT_TYPE', env.get('HTTP_CONTENT_TYPE'))
      if content_type:
        output.append('Content-Type: %s\r\n' % content_type)
    if connection is not None:
      output.append('Connection: %s\r\n' % connection)
    output.append('\r\n')
  return output


def BaseHttpWsgiWrapper(bhrh_class):
  """Return a WSGI application running a BaseHTTPRequestHandler."""
  BaseHTTPServer = sys.modules['BaseHTTPServer']
  if not ((isinstance(bhrh_class, type) or
           isinstance(bhrh_class, types.ClassType)) and
          issubclass(bhrh_class, BaseHTTPServer.BaseHTTPRequestHandler)):
    raise TypeError

  def WsgiApplication(env, start_response):
    request_lines = HttpRequestFromEnv(env, connection='close')
    connection = FakeBaseHttpConnection(env, start_response, request_lines)
    server = FakeBaseHttpServer()
    client_address = (env['REMOTE_ADDR'], int(env['REMOTE_PORT']))
    # So we'll get a nice HTTP/1.0 answer even for a bad request, and
    # FakeBaseHttpWFile won't complain about the missing '\r\n'. We have
    # to set it early, because th bhrh constructor handles the request.
    bhrh_class.default_request_version = 'HTTP/1.0'
    # The constructor calls bhrh.handle_one_request() automatically.
    bhrh = bhrh_class(connection, client_address, server)
    # If there is an exception in the bhrh_class creation above, then these
    # assertions are not reached, and bhrh.wfile and bhrh.rfile remain
    # unclosed, but that's OK.
    assert bhrh.wfile.wsgi_write_callback
    assert not bhrh.wfile.write_buf
    return ''

  return WsgiApplication


def CanBeCherryPyApp(app):
  """Return True if app is a CherryPy app class or object."""
  # Since CherryPy applications can be of any type, the only way for us to
  # detect such an application is to look for an exposed method (or class?).
  if isinstance(app, type) or isinstance(app, types.ClassType):
    pass
  elif isinstance(app, object) or isinstance(app, types.InstanceType):
    app = type(app)
  else:
    return False
  for name in dir(app):
    value = getattr(app, name)
    if callable(value) and getattr(value, 'exposed', False):
      return True
  return False


def RunHttpServer(app, server_address=None, listen_queue_size=100):
  """Listen as a HTTP server, and run the specified application forever.

  Args:
    app: A WSGI application function, or a (web.py) web.application object.
    server_address: TCP address to bind to, e.g. ('', 8080), or None to use
      the default.
  """
  # TODO(pts): Support HTTPS in this function. See examples/demo.py for
  # HTTPS support.
  try:
    import psyco
    psyco.full()  # TODO(pts): Measure the speed in Stackless Python.
  except ImportError:
    pass
  webapp = (sys.modules.get('google.appengine.ext.webapp') or
            sys.modules.get('webapp'))
  # Use if already loaded.
  BaseHTTPServer = sys.modules.get('BaseHTTPServer')
  if webapp and isinstance(app, type) and issubclass(
      app, webapp.RequestHandler):
    logging.info('running webapp RequestHandler')
    wsgi_application = webapp.WSGIApplication(
        [('/', app)], debug=bool(coio.VERBOSE))
    assert callable(wsgi_application)
    if server_address is None:
      server_address = ('127.0.0.1', 6666)
  elif (not callable(app) and
      hasattr(app, 'handle') and hasattr(app, 'request') and
      hasattr(app, 'run') and hasattr(app, 'wsgifunc') and
      hasattr(app, 'cgirun') and hasattr(app, 'handle')):
    logging.info('running (web.py) web.application')
    wsgi_application = app.wsgifunc()
    if server_address is None:
      server_address = ('0.0.0.0', 8080)  # (web.py) default
  elif CanBeCherryPyApp(app):
    logging.info('running CherryPy application')
    if isinstance(app, type) or isinstance(app, types.ClassType):
      app = app()
    import cherrypy
    # See http://www.cherrypy.org/wiki/WSGI
    wsgi_application = cherrypy.tree.mount(app, '/')
    if server_address is None:
      server_address = ('127.0.0.1', 8080)  # CherryPy default
    # TODO(pts): Use CherryPy config files.
  elif (BaseHTTPServer and
        (isinstance(app, type) or isinstance(app, types.ClassType)) and
        issubclass(app, BaseHTTPServer.BaseHTTPRequestHandler)):
    logging.info('running BaseHTTPRequestHandler application')
    wsgi_application = BaseHttpWsgiWrapper(app)
    if server_address is None:
      server_address = ('127.0.0.1', 6666)
  elif callable(app):
    if webapp and isinstance(app, webapp.WSGIApplication):
      logging.info('running webapp WSGI application')
    else:
      logging.info('running WSGI application')

    # Check that app accepts the proper number of arguments.
    has_self = False
    if isinstance(app, type) or isinstance(app, types.ClassType):
      func = getattr(app, '__init__', None)
      assert isinstance(func, types.UnboundMethodType)
      func = func.im_func
      has_self = True
    elif isinstance(app, types.FunctionType):
      func = app
    elif isinstance(app, object) or isinstance(app, types.InstanceType):
      func = getattr(app, '__call__', None)
      assert isinstance(func, types.MethodType)
      func = func.im_func
      has_self = True
    else:
      func = app
    expected_argcount = int(has_self) + 2  # self, env, start_response
    assert func.func_code.co_argcount == expected_argcount, (
        'invalid argument count -- maybe not a WSGI application: %r' % app)
    func = None

    wsgi_application = app
    if server_address is None:
      server_address = ('127.0.0.1', 6666)
  else:
    assert 0, 'unsupported application type for %r' % (app,)

  server_socket = coio.nbsocket(socket.AF_INET, socket.SOCK_STREAM)
  server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
  server_socket.bind(server_address)
  # Reducing this has a strong negative effect on ApacheBench worst-case
  # connection times, as measured with:
  # ab -n 100000 -c 50 http://127.0.0.1:6666/ >ab.stackless3.txt
  # It increases the maximum Connect time from 8 to 9200 milliseconds.
  server_socket.listen(listen_queue_size)
  logging.info('listening on %r' % (server_socket.getsockname(),))
  # From http://webpy.org/install (using with mod_wsgi).
  WsgiListener(server_socket, wsgi_application)


def simple(server_port=8080, function=None, server_host='0.0.0.0'):
  """A simple (non-WSGI) HTTP server for demonstration purposes."""
  default_start_response_args = ('200 OK', [('Content-Type', 'text/html')])

  def WsgiApplication(env, start_response):
    is_called_ary = [False]
    def StartResponseWrapper(*args, **kwargs):
      is_called_ary[0] = True
      start_response(*args, **kwargs)
    items = function(env, StartResponseWrapper)
    if is_called_ary[0]:
      return items
    elif (isinstance(items, str) or isinstance(items, list) or
        isinstance(items, tuple)):
      start_response(*default_start_response_args)
      return items
    else:
      items = iter(items)
      for item in items:
        start_response(*default_start_response_args)
        return items

  stackless.tasklet(RunHttpServer)(
      WsgiApplication, (server_host, server_port))
  #return lambda: coio.sleep(100)
  return stackless.schedule_remove
  #RunHttpServer(WsgiApplication, (server_host, server_port))
