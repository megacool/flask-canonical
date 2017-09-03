import time
import re
from collections import OrderedDict
from logging import getLogger

from flask import request, _app_ctx_stack as stack
from werkzeug.routing import RequestRedirect, MethodNotAllowed, NotFound

HAS_SQLALCHEMY = False
try:
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    HAS_SQLALCHEMY = True
except ImportError:
    pass


WHITESPACE_RE = re.compile(r'\s')


class CanonicalLogger(object):

    def __init__(self, app=None):
        if app is not None:
            self.init_app(app)


    def init_app(self, app):
        self.app = app

        app.before_request(self._before_request)
        app.after_request(self._after_request)
        app.teardown_request(self._teardown_request)

        self.logger = getLogger('%s.canonical' % app.name)


    def _before_request(self):
        store_prop('canonical_start_time', time.time())


    def _after_request(self, response):
        store_prop('canonical_response_status', response.status_code)
        return response


    def add(self, key, value):
        self._add_extra(key, value)


    def add_measure(self, key, value):
        self._add_extra('measure#%s' % key, '%.3fs' % value)


    def _add_extra(self, key, value):
        get_context().setdefault('canonical_log_extra', []).append((key, value))


    @property
    def tag(self):
        return get_prop('canonical_tag') or get_default_tag(self.app)


    @tag.setter
    def tag(self, tag):
        store_prop('canonical_tag', tag)


    def _teardown_request(self, exception):
        params = OrderedDict((
            ('fwd', ','.join(request.access_route)),
            ('tag', self.tag),
            ('method', request.method),
            ('path', request.full_path if request.args else request.path),
            ('status', get_prop('canonical_response_status', 500)),
            ('request_user_agent', request.headers.get('user-agent')),
        ))

        timing_database = get_prop('canonical_timing_database')
        if timing_database:
            self.add_measure('timing_database', timing_database)

        self.add_measure('timing_total', time.time() - get_prop('canonical_start_time'))

        for key, value in get_prop('canonical_log_extra', ()):
            params[key] = value

        if exception:
            params['error'] = exception.__class__.__name__
            params['error_msg'] = str(exception)

        log_line_items = (format_key_value_pair(key, val) for (key, val) in params.items())
        self.logger.info(' '.join(log_line_items))


def store_prop(key, value):
    get_context()[key] = value


def get_prop(key, default=None):
    return get_context().get(key, default)


def get_context():
    app_context = stack.top

    if not app_context:
        return {}

    _context = getattr(app_context, 'canonical', None)
    if not _context:
        _context = {}
        setattr(app_context, 'canonical', _context)

    return _context


def get_default_tag(app):
    '''Get the name of the view function used to prevent having to set the tag
    manually for every endpoint'''
    view_func = get_view_function(app, request.path, request.method)
    if view_func:
        return view_func.__name__


def get_view_function(app, url, method):
    """Match a url and return the view and arguments
    it will be called with, or None if there is no view.
    Creds: http://stackoverflow.com/a/38488506
    """

    adapter = app.create_url_adapter(request)

    try:
        match = adapter.match(url, method=method)
    except RequestRedirect as e:
        # recursively match redirects
        return get_view_function(app, e.new_url, method)
    except (MethodNotAllowed, NotFound):
        # no match
        return None

    try:
        return app.view_functions[match[0]]
    except KeyError:
        # no view is associated with the endpoint
        return None


def format_key_value_pair(key, value):
    if value:
        value = str(value)
    else:
        value = ''

    should_quote = WHITESPACE_RE.search(value)

    if should_quote:
        value = '"%s"' % value

    return '%s=%s' % (key, value)


if HAS_SQLALCHEMY:
    # Register as event handler on the database to track time spent
    @event.listens_for(Engine, "before_cursor_execute")
    def before_cursor_execute(conn, cursor, statement,
                            parameters, context, executemany):
        conn.info.setdefault('query_start_time', []).append(time.time())


    @event.listens_for(Engine, "after_cursor_execute")
    def after_cursor_execute(conn, cursor, statement,
                            parameters, context, executemany):
        total = time.time() - conn.info['query_start_time'].pop(-1)
        timing_database = get_prop('canonical_timing_database', 0)
        store_prop('canonical_timing_database', timing_database + total)