'''Pulsar is shipped with four WSGI application handlers which facilitate the
development of server-side python web applications.

.. note::

    A **WSGI application handler** is always a callable, either a function
    or a callable instance, which accepts two positional arguments:
    *environ* and *start_response*. When called by the server,
    the application object must return an iterable yielding zero or more bytes. 


WsgiHandler
======================

The first and most basic application handler is the :class:`WsgiHandler`
which is a step above the :ref:`hello callable <tutorials-hello-world>`
in the tutorial. It accepts two iterables, a list of wsgi middleware
and an optional list of response middleware.

Response middleware is a callable of the form::

    def my_response_middleware(environ, response):
        ...
        
where *environ* is the WSGI environ dictionary and *response* is an instance
of :class:`WsgiResponse`. 

.. autoclass:: WsgiHandler
   :members:
   :member-order: bysource
   

.. _apps-wsgi-router:

Router
======================

Next up is routing. Routing is the process of match and
parse the URL to something we can use. Pulsar provides a flexible integrated
routing system you can use for that. It works by creating a
:class:`Router` instance with its own ``rule`` and, optionally, additional
sub-routers for handling additional urls::

    class Page(Router):
        
        def get(self, request):
            "This method handle request with get-method" 
            ...
            
        def post(self, request):
            "This method handle request with post-method" 
            ...
            
    middleware = Page('/bla')
    
The ``middleware`` constructed can be used to serve ``get`` and ``post`` methods
at ``/bla``.
The :class:`Router` introduces a new element into pulsar WSGI handlers, the
:class:`WsgiRequest` instance ``request``, which is a light-weight
wrapper of the WSGI environ.

.. autoclass:: Router
   :members:
   :member-order: bysource


Media Router
======================

The :class:`MediaRouter` is a spcialised :class:`Router` for serving static
files such ass ``css``, ``javascript``, images and so forth.

.. autoclass:: MediaRouter
   :members:
   :member-order: bysource
   
   
Lazy Wsgi Handler
======================

.. autoclass:: LazyWsgi
   :members:
   :member-order: bysource
   
.. _WSGI: http://www.wsgi.org
'''
import os
import re
import stat
import mimetypes
from email.utils import parsedate_tz, mktime_tz

from pulsar.utils.httpurl import http_date, CacheControl, remove_double_slash
from pulsar.utils.structures import AttributeDictionary
from pulsar.utils.log import LocalMixin
from pulsar import Http404, PermissionDenied, HttpException, async

from .route import Route
from .utils import wsgi_request
from .content import Html
from .wrappers import WsgiResponseGenerator

__all__ = ['WsgiHandler', 'LazyWsgi', 'Router',
           'MediaRouter', 'FileRouter', 'MediaMixin']


class WsgiHandler(object):
    '''An handler for application conforming to python WSGI_.

.. attribute:: middleware

    List of callable WSGI middleware callable which accept
    ``environ`` and ``start_response`` as arguments.
    The order matter, since the response returned by the callable
    is the non ``None`` value returned by a middleware.

.. attribute:: response_middleware

    List of functions of the form::

        def ..(environ, start_response, response):
            ...

    where ``response`` is the first not ``None`` value returned by
    the middleware.

'''
    def __init__(self, middleware=None, response_middleware=None, **kwargs):
        if middleware:
            middleware = list(middleware)
        self.middleware = middleware or []
        self.response_middleware = response_middleware or []

    def __call__(self, environ, start_response):
        '''The WSGI callable'''
        response = None
        for middleware in self.middleware:
            response = middleware(environ, start_response)
            if response is not None:
                break
        if response is None:
            raise Http404(environ.get('PATH_INFO','/'))
        if hasattr(response, 'middleware'):
            response.middleware.extend(self.response_middleware)
        return response
    

class LazyWsgi(LocalMixin):
    '''A :ref:`wsgi handler <apps-wsgi-handlers>` which loads its middleware the
first time it is called. Subclasses must implement the :meth:`setup` method.

This application handler is particularly useful when working in multiprocessing
mode so that wsgi middleware can be rebuild consistently in ever application
domain without causing serialization issues.'''        
    def __call__(self, environ, start_response):
        return self.middleware(environ, start_response)
    
    @property
    def middleware(self):
        '''The lazy middleware.'''
        m = self.local.middleware
        if m is None:
            self.local.middleware = m = self.setup()
        return m
    
    def setup(self):
        '''The setup function for this :class:`LazyWsgi`. Called once only
the first time this application handler is invoked. This **must** be implemented
by subclasses and **must** return a
:ref:`wsgi application handler <apps-wsgi-handlers>`.'''
        raise NotImplementedError
    
    
    
class Router(object):
    '''A WSGI application which handle multiple
:ref:`routes <apps-wsgi-route>`. user must implement the HTTP method
required by her application. For example if the route needs to serve a ``GET``
request, the ``get(self, request)`` method must be implemented.
    
.. attribute:: route

    The :ref:`Route <apps-wsgi-route>` served by this :class:`Router`.

.. attribute:: routes

    List of children :class:`Router` of this :class:`Router`.

.. attribute:: parent

    The parent :class:`Router` of this :class:`Router`.
        
.. attribute:: default_content_type

    Class attribute which specify the default content type for
    this :class:`Router`. Overwritten during initialization by the optional
    ``content_type`` parameter.
    
.. attribute:: parameters

    A :class:`pulsar.utils.structures.AttributeDictionary` of parameters.
'''
    creation_count = 0
    default_content_type=None
    _parent = None
    def __init__(self, rule, *routes, **parameters):
        self.__class__.creation_count += 1
        self.creation_count = self.__class__.creation_count
        if not isinstance(rule, Route):
            rule = Route(rule)
        self.route = rule
        self.routes = []
        for router in routes:
            self.add_child(router)
        self.parameters = AttributeDictionary()
        rule_methods = []
        for name, callable in self.__class__.__dict__.items():
            rule_method = getattr(callable, 'rule_method', None)
            if isinstance(rule_method, tuple):
                rule_method = list(rule_method)
                rule_method.append(name)
                rule_methods.append(rule_method)
        # Create the method handler
        for rule_method in sorted(rule_methods, key=lambda r: r[3]):
            rule, method, params, count, name = rule_method
            rparameters = params.copy()
            handler = getattr(self, name)
            if rparameters.pop('async', False): # asynchronous method
                handler = async(handler)
            router = self.add_child(Router(rule, **rparameters))
            setattr(router, method, getattr(self, name))
        self.setup(**parameters)
        
    def setup(self, content_type=None, **parameters):
        self.parameters.content_type = content_type or self.default_content_type
        for name, value in parameters.items():
            if not hasattr(self, name) and hasattr(value, '__call__'):
                setattr(self, name, value)
            else:
                self.parameters[name] = value
    
    @property
    def root(self):
        if self.parent:
            return self.parent.root
        else:
            return self
     
    @property
    def parent(self):
        return self._parent
       
    def __repr__(self):
        return self.route.__repr__()
        
    def __call__(self, environ, start_response):
        path = environ.get('PATH_INFO') or '/'
        path = path[1:]
        router_args = self.resolve(path)
        if router_args:
            router, args = router_args
            request = wsgi_request(environ, start_response, router, args)
            request.response.content_type = self.content_type(request)
            method = request.method.lower()
            callable = getattr(router, method, None)
            if callable is None:
                raise HttpException(status=405,
                                    msg='Method "%s" not allowed' % method)
            return WsgiResponseGenerator(request, callable)
        
    def resolve(self, path, urlargs=None):
        '''Resolve a path and return a ``(handler, urlargs)`` tuple or
``None`` if the path could not be resolved.'''
        urlargs = urlargs if urlargs is not None else {}
        match = self.route.match(path)
        if match is None:
            return
        if '__remaining__' in match:
            for handler in self.routes:
                view_args = handler.resolve(path, urlargs)
                if view_args is None:
                    continue
                #remaining_path = match.pop('__remaining__','')
                #urlargs.update(match)
                return view_args
        else:
            return self, match
    
    def add_child(self, router):
        '''Add a new :class:`Router` to the :attr:`routes` list.'''
        assert isinstance(router, Router), 'Not a valid Router'
        assert router is not self, 'cannot add self to children'
        for r in self.routes:
            if r.route == router.route:
                r.paramaters.update(router.parameters)
                return r
        if router.parent:
            router.parent.remove_child(router)
        router._parent = self
        self.routes.append(router)
        return router
        
    def remove_child(self, router):
        '''remove a :class:`Router` from the :attr:`routes` list.'''
        if router in self.routes:
             self.routes.remove(router)
             router._parent = None
        
    def link(self, *args, **urlargs):
        '''Return an anchor :class:`Html` element with the `href` attribute
set to the url of this :class:`Router`.'''
        if len(args) > 1:
            raise ValueError
        url = self.route.url(**urlargs)
        if len(args) == 1:
            text = args[0]
        else:
            text = url
        return Html('a', text, href=url)
    
    def sitemap(self, root=None):
        '''This utility method returns a sitemap starting at root.
If *root* is ``None`` it starts from this :class:`Router`.

:param request: a :ref:`wsgi request wrapper <app-wsgi-request>`
:param root: Optional url path where to start the sitemap.
    By default it starts from this :class:`Router`. Pass `"/"` to
    start from the root :class:`Router`.
:param levels: Number of nested levels to include.
:return: A list of children
'''
        if not root:
            root = self
        else:
            handler_urlargs = self.root.resolve(root[1:])
            if handler_urlargs:
                root, urlargs = handler_urlargs
            else:
                return []
        return list(self.routes)
    
    def content_type(self, request):
        '''The content type of this :class:`Router`. By default it returns
the :attr:`default_content_type`. Override if you need to.'''
        return self.parameters.content_type
    
    def encoding(self, request):
        '''The encoding to use for the response. By default it
returns ``utf-8``.'''
        return 'utf-8'
    

class MediaMixin(Router):
    default_content_type = 'application/octet-stream'
    cache_control = CacheControl(maxage=86400)
    
    def serve_file(self, request, fullpath):
        # Respect the If-Modified-Since header.
        statobj = os.stat(fullpath)
        content_type, encoding = mimetypes.guess_type(fullpath)
        response = request.response
        if content_type: 
            response.content_type = content_type
        response.encoding = encoding
        if not self.was_modified_since(request.environ.get(
                                            'HTTP_IF_MODIFIED_SINCE'),
                                       statobj[stat.ST_MTIME],
                                       statobj[stat.ST_SIZE]):
            response.status_code = 304
        else:
            response.content = open(fullpath, 'rb').read()
            response.headers["Last-Modified"] = http_date(statobj[stat.ST_MTIME])
        return response.start()

    def was_modified_since(self, header=None, mtime=0, size=0):
        '''Check if an item was modified since the user last downloaded it

:param header: the value of the ``If-Modified-Since`` header. If this is None,
    simply return ``True``.
:param mtime: the modification time of the item in question.
:param size: the size of the item.
'''
        try:
            if header is None:
                raise ValueError
            matches = re.match(r"^([^;]+)(; length=([0-9]+))?$", header,
                               re.IGNORECASE)
            header_mtime = mktime_tz(parsedate_tz(matches.group(1)))
            header_len = matches.group(3)
            if header_len and int(header_len) != size:
                raise ValueError()
            if mtime > header_mtime:
                raise ValueError()
        except (AttributeError, ValueError, OverflowError):
            return True
        return False
    
    def directory_index(self, request, fullpath):
        names = [Html('a', '../', href='../', cn='folder')]
        files = []
        for f in sorted(os.listdir(fullpath)):
            if not f.startswith('.'):
                if os.path.isdir(os.path.join(fullpath, f)):
                    names.append(Html('a', f, href=f+'/', cn='folder'))
                else:
                    files.append(Html('a', f, href=f))
        names.extend(files)
        return self.static_index(request, names)
    
    def html_title(self, request):
        return 'Index of %s' % request.path
    
    def static_index(self, request, links):
        title = Html('h2', self.html_title(request))
        list = Html('ul', *[Html('li', a) for a in links])
        body = Html('div', title, list)
        doc = request.html_document(title=title, body=body)
        return doc.http_response(request)
    
    
class MediaRouter(MediaMixin):
    '''A :class:`Router` for serving static media files from a given 
directory.

:param rute: The top-level url for this router. For example ``/media``
    will serve the ``/media/<path:path>`` :class:`Route`.
:param path: Check the :attr:`path` attribute.
:param show_indexes: Check the :attr:`show_indexes` attribute.

.. attribute::    path

    The file-system path of the media files to serve.
    
.. attribute::    show_indexes

    If ``True`` (default), the router will serve media file directories as
    well as media files.
'''
    def __init__(self, rute, path, show_indexes=True):
        super(MediaRouter, self).__init__('%s/<path:path>' % rute)
        self._show_indexes = show_indexes
        self._file_path = path
        
    def get(self, request):
        paths = request.urlargs['path'].split('/')
        if len(paths) == 1 and paths[0] == '':
            paths.pop(0)
        fullpath = os.path.join(self._file_path, *paths)
        if os.path.isdir(fullpath):
            if self._show_indexes:
                return self.directory_index(request, fullpath)
            else:
                raise PermissionDenied()
        elif os.path.exists(fullpath):
            return self.serve_file(request, fullpath)
        else:
            raise Http404


class FileRouter(MediaMixin):
    
    def __init__(self, route, file_path):
        super(FileRouter, self).__init__(route)
        self._file_path = file_path
        
    def get(self, request):
        return self.serve_file(request, self._file_path)
    