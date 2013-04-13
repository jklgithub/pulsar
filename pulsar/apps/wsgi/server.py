'''

.. autoclass:: HttpServerResponse
   :members:
   :member-order: bysource
   
'''
import sys
import time
import os
import socket
from wsgiref.handlers import format_date_time
from io import BytesIO

import pulsar
from pulsar import lib, HttpException, ProtocolError
from pulsar.utils.pep import is_string, to_bytes, native_str
from pulsar.utils.httpurl import Headers, unquote, has_empty_content,\
                                 host_and_port_default, Headers, REDIRECT_CODES
from pulsar.utils import events

from .utils import handle_wsgi_error, LOGGER, HOP_HEADERS
from .wrappers import WsgiResponse


__all__ = ['HttpServerResponse', 'MAX_CHUNK_SIZE']

MAX_CHUNK_SIZE = 65536


def chunk_encoding(chunk):
    '''Write a chunk::

    chunk-size(hex) CRLF
    chunk-data CRLF
    
If the size is 0, this is the last chunk, and an extra CRLF is appended.
'''
    head = ("%X\r\n" % len(chunk)).encode('utf-8')
    return head + chunk + b'\r\n'

def keep_alive(headers, version):
        """ return True if the connection should be kept alive"""
        conn = set((v.lower() for v in headers.get_all('connection', ())))
        if "close" in conn:
            return False
        elif 'upgrade' in conn:
            headers['connection'] = 'Upgrade'
            return True
        elif "keep-alive" in conn:
            return True
        elif version == (1, 1):
            headers['connection'] = 'keep-alive'
            return True
        else:
            return False

class HttpServerResponse(pulsar.ProtocolConsumer):
    '''Server side HTTP :class:`pulsar.ProtocolConsumer`.'''
    _status = None
    _headers_sent = None
    _request_headers = None
    SERVER_SOFTWARE = pulsar.SERVER_SOFTWARE
    
    def __init__(self, wsgi_callable, cfg, connection):
        super(HttpServerResponse, self).__init__(connection)
        self.wsgi_callable = wsgi_callable
        self.cfg = cfg
        self.parser = lib.Http_Parser(kind=0)
        self.headers = Headers()
        self.keep_alive = False
        
    def data_received(self, data):
        '''Implements :class:`pulsar.Protocol.data_received`. Once we have a
full HTTP message, build the wsgi ``environ`` and write the response
using the :meth:`pulsar.Transport.writelines` method.'''
        p = self.parser
        request_headers = self._request_headers
        if p.execute(bytes(data), len(data)) == len(data):
            done = p.is_message_complete()
            if request_headers is None and p.is_headers_complete():
                self._request_headers = Headers(p.get_headers(), kind='client')
                if not done:
                    self.expect_continue()
            if done: # message is done
                environ = self.wsgi_environ()
                self.transport.writelines(self.generate(environ))
        else:
            # This is a parsing error, the client must have sent
            # bogus data
            raise ProtocolError
    
    def expect_continue(self):
        '''Handle the expect=100-continue header if available, according to
the following algorithm:

* Send the 100 Continue response before waiting for the body.
* Omit the 100 (Continue) response if it has already received some or all of
  the request body for the corresponding request.
    '''
        if self._request_headers.has('expect', '100-continue'):
            self.transport.write(b'HTTP/1.1 100 Continue\r\n\r\n')
    
    @property
    def status(self):
        return self._status

    @property
    def upgrade(self):
        return self.headers.get('Upgrade')

    @property
    def chunked(self):
        return self.headers.get('Transfer-Encoding') == 'chunked'

    @property
    def content_length(self):
        c = self.headers.get('Content-Length')
        if c:
            return int(c)

    @property
    def version(self):
        return self.parser.get_version()

    def start_response(self, status, response_headers, exc_info=None):
        '''WSGI compliant ``start_response`` callable, see pep3333_.
The application may call start_response more than once, if and only
if the exc_info argument is provided.
More precisely, it is a fatal error to call start_response without the exc_info
argument if start_response has already been called within the current
invocation of the application.

:parameter status: an HTTP "status" string like "200 OK" or "404 Not Found".
:parameter response_headers: a list of ``(header_name, header_value)`` tuples.
    It must be a Python list. Each header_name must be a valid HTTP header
    field-name (as defined by RFC 2616_, Section 4.2), without a trailing
    colon or other punctuation.
:parameter exc_info: optional python ``sys.exc_info()`` tuple. This argument
    should be supplied by the application only if start_response is being
    called by an error handler.

:rtype: The :meth:`HttpResponse.write` callable.

.. _pep3333: http://www.python.org/dev/peps/pep-3333/
.. _2616: http://www.faqs.org/rfcs/rfc2616.html
'''
        if exc_info:
            try:
                if self._headers_sent:
                    # if exc_info is provided, and the HTTP headers have
                    # already been sent, start_response must raise an error,
                    # and should re-raise using the exc_info tuple
                    raise (exc_info[0], exc_info[1], exc_info[2])
            finally:
                # Avoid circular reference
                exc_info = None
        elif self._status:
            # Headers already set. Raise error
            raise HttpException("Response headers already set!")
        self._status = status
        if type(response_headers) is not list:
            raise TypeError("Headers must be a list of name/value tuples")
        for header, value in response_headers:
            if header.lower() in HOP_HEADERS:
                # These features are the exclusive province of this class,
                # this should be considered a fatal error for an application
                # to attempt sending them, but we don't raise an error,
                # just log a warning
                LOGGER.warning('Application handler passing hop header "%s"',
                               header)
                continue
            self.headers.add_header(header, value)
        return self.write

    def write(self, data):
        '''The write function required by WSGI specification.'''
        head = self.send_headers(force=data)
        if head:
            self.transport.write(head)
        if data:
            self.transport.write(data)

    def generate(self, environ):
        '''Generator of response bytestrings conforming with the
:ref:`wsgi asynchronous implementation <wsgi-async>`.'''
        exc_info = None
        wsgi_iter = None
        while True:
            try:
                if exc_info is None:
                    wsgi_iter = self.wsgi_callable(environ, self.start_response)
                    iterable = wsgi_iter
                for b in iterable:
                    head = self.send_headers(force=b)
                    if head is not None:
                        yield head
                    if b:
                        if self.chunked:
                            while len(b) >= MAX_CHUNK_SIZE:
                                chunk, b = b[:MAX_CHUNK_SIZE], b[MAX_CHUNK_SIZE:]
                                yield chunk_encoding(chunk)
                            if b:
                                yield chunk_encoding(b)
                        else:
                            yield b
                    else:
                        yield b''
            except Exception as e:
                if exc_info or self._headers_sent:
                    self.keep_alive = False
                    LOGGER.critical('Could not send valid response',
                                    exc_info=True)
                    break
                else:
                    exc_info = sys.exc_info()
                    response = handle_wsgi_error(environ, exc_info)
                    if response.status_code not in REDIRECT_CODES:
                        self.keep_alive = False
                    iterable = response(environ, self.start_response, exc_info)
            else:
                head = self.send_headers(force=True)
                if head is not None:
                    yield head
                if self.chunked:
                    # Last chunk
                    yield chunk_encoding(b'')
                break
        # close transport if required
        # If the iterable returned by the application has a close() method,
        # the server or gateway must call that method upon completion of the
        # current request, whether the request was completed normally, or
        # terminated early due to an application error during iteration or
        # an early disconnect of the browser. (The close() method requirement
        # is to support resource release by the application. This protocol is
        # intended to complement PEP 342's generator support, and other common
        # iterables with close() methods.)
        if hasattr(wsgi_iter, 'close'):
            wsgi_iter.close()
        if not self.keep_alive:
            self.connection.close()
        self.finished()

    def is_chunked(self):
        '''Only use chunked responses when the client is
speaking HTTP/1.1 or newer and there was no Content-Length header set.'''
        if self.version <= (1, 0):
            return False
        elif has_empty_content(int(self.status[:3])):
            # Do not use chunked responses when the response
            # is guaranteed to not have a response body.
            return False
        elif self.headers.get('Transfer-Encoding') == 'chunked':
            return True
        else:
            return self.content_length is None

    def get_headers(self, force=False):
        '''Get the headers to send only if *force* is ``True`` or this
is an HTTP upgrade (websockets)'''
        if self.upgrade or force:
            if not self._status:
                # we are sending headers but the start_response was not called
                raise HttpException('Headers not set.')
            headers = self.headers
            # Set chunked header if needed
            if self.is_chunked():
                headers['Transfer-Encoding'] = 'chunked'
                headers.pop('content-length', None)
            else:
                headers.pop('Transfer-Encoding', None)
            if not self.keep_alive:
                headers['connection'] = 'close'
            return headers

    def send_headers(self, force=False):
        if not self._headers_sent:
            tosend = self.get_headers(force)
            if tosend:
                events.fire('http-headers', self, headers=tosend)
                self._headers_sent = tosend.flat(self.version, self.status)
                return self._headers_sent

    def wsgi_environ(self):
        #return a the WSGI environ dictionary
        p = self.parser
        input = BytesIO(p.recv_body())
        protocol = "HTTP/%s" % ".".join(('%s' % v for v in p.get_version()))
        environ = {
            "wsgi.input": input,
            "wsgi.errors": sys.stderr,
            "wsgi.version": (1, 0),
            "wsgi.run_once": False,
            'wsgi.multithread': False,
            'wsgi.multiprocess': False,
            "SERVER_SOFTWARE": pulsar.SERVER_SOFTWARE,
            "REQUEST_METHOD": native_str(p.get_method()),
            "QUERY_STRING": p.get_query_string(),
            "RAW_URI": p.get_url(),
            "SERVER_PROTOCOL": protocol,
            'CONTENT_TYPE': '',
            'pulsar.connection': self.connection,
            'pulsar.cfg': self.cfg
        }
        url_scheme = "http"
        forward = self.address
        server = '%s:%s' % self.transport.address
        script_name = os.environ.get("SCRIPT_NAME", "")
        for header, value in self._request_headers:
            header = header.lower()
            if header in HOP_HEADERS:
                self.headers[header] = value
            if header == 'x-forwarded-for':
                forward = value
            elif header == "x-forwarded-protocol" and value == "ssl":
                url_scheme = "https"
            elif header == "x-forwarded-ssl" and value == "on":
                url_scheme = "https"
            elif header == "host":
                server = value
            elif header == "script_name":
                script_name = value
            elif header == "content-type":
                environ['CONTENT_TYPE'] = value
                continue
            elif header == "content-length":
                environ['CONTENT_LENGTH'] = value
                continue
            key = 'HTTP_' + header.upper().replace('-', '_')
            environ[key] = value
        environ['wsgi.url_scheme'] = url_scheme
        if is_string(forward):
            # we only took the last one
            # http://en.wikipedia.org/wiki/X-Forwarded-For
            if forward.find(",") >= 0:
                forward = forward.rsplit(",", 1)[1].strip()
            remote = forward.split(":")
            if len(remote) < 2:
                remote.append('80')
        else:
            remote = forward
        environ['REMOTE_ADDR'] = remote[0]
        environ['REMOTE_PORT'] = str(remote[1])
        server =  host_and_port_default(url_scheme, server)
        environ['SERVER_NAME'] = socket.getfqdn(server[0])
        environ['SERVER_PORT'] = server[1]
        path_info = p.get_path()
        if path_info is not None:
            if script_name:
                path_info = path_info.split(script_name, 1)[1]
            environ['PATH_INFO'] = unquote(path_info)
        environ['SCRIPT_NAME'] = script_name
        self.keep_alive = keep_alive(self.headers, p.get_version())
        self.headers.update([('Server', self.SERVER_SOFTWARE),
                             ('Date', format_date_time(time.time()))])
        return environ