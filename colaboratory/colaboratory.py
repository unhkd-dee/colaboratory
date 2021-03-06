# coding: utf-8
"""Colaboratory: the Jupyter Collaborative Computational Laboratory.
"""

# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.

from __future__ import print_function

import errno
import json
import logging
import os
import random
import select
import signal
import socket
import sys
import threading
import webbrowser

# check for pyzmq 2.1.11
from IPython.utils.zmqrelated import check_for_zmq
check_for_zmq('2.1.11', 'IPython.html')

from jinja2 import Environment, FileSystemLoader

# Install the pyzmq ioloop. This has to be done before anything else from
# tornado is imported.
from zmq.eventloop import ioloop
ioloop.install()

# check for tornado 3.1.0
msg = "The Jupyter Colaboratory requires tornado >= 3.1.0"
try:
    import tornado
except ImportError:
    raise ImportError(msg)
try:
    version_info = tornado.version_info
except AttributeError:
    raise ImportError(msg + ", but you have < 1.1.0")
if version_info < (3,1,0):
    raise ImportError(msg + ", but you have %s" % tornado.version)

from tornado import httpserver
from tornado import web
from tornado.log import LogFormatter

from IPython.html import DEFAULT_STATIC_FILES_PATH
from IPython.html.log import log_request
from IPython.html.services.kernels.kernelmanager import MappingKernelManager

from IPython.html.base.handlers import (FileFindHandler, IPythonHandler)

from IPython.config.application import catch_config_error
from IPython.core.application import (
    BaseIPythonApplication, base_flags, base_aliases,
)
from IPython.core.profiledir import ProfileDir
from IPython.kernel import KernelManager
from IPython.kernel.zmq.session import default_secure, Session
from IPython.utils.importstring import import_item
from IPython.utils import submodule
from IPython.utils.traitlets import (
    Dict, Unicode, Integer, List, Bool, Bytes,
    DottedObjectName,
)

#-----------------------------------------------------------------------------
# Module globals
#-----------------------------------------------------------------------------
pjoin = os.path.join

here = os.path.dirname(__file__)
RESOURCES = pjoin(here, 'resources')

_examples = """
colab                       # start the server
colab --profile=sympy       # use the sympy profile
"""

#-----------------------------------------------------------------------------
# Helper functions
#-----------------------------------------------------------------------------

def random_ports(port, n):
    """Generate a list of n random ports near the given port.

    The first 5 ports will be sequential, and the remaining n-5 will be
    randomly selected in the range [port-2*n, port+2*n].
    """
    for i in range(min(5, n)):
        yield port + i
    for i in range(n-5):
        yield max(1, port + random.randint(-2*n, 2*n))

def load_handlers(name):
    """Load the (URL pattern, handler) tuples for each component."""
    name = 'IPython.html.' + name
    mod = __import__(name, fromlist=['default_handlers'])
    return mod.default_handlers

#-----------------------------------------------------------------------------
# The Tornado web application
#-----------------------------------------------------------------------------

class SingleStaticFileHandler(web.StaticFileHandler):
    def get_absolute_path(self, root, path):
        p = os.path.abspath(os.path.join(self.root, self.default_filename))
        return p


class NotebookHandler(IPythonHandler):
    def get(self, path='', name=None):
        self.write(self.render_template('notebook.html',
            raw='1',
            app_mode=False))

class WelcomeHandler(IPythonHandler):
    def get(self, path='', name=None):
        self.write(self.render_template('welcome.html',
            raw='1',
            app_mode=False))


class ColaboratoryWebApplication(web.Application):

    def __init__(self, ipython_app, kernel_manager, notebook_manager,
                 session_manager, log,
                 settings_overrides, jinja_env_options):

        settings = self.init_settings(
            ipython_app, kernel_manager, notebook_manager,
            session_manager, log,
            settings_overrides, jinja_env_options)
        handlers = self.init_handlers(settings)

        super(ColaboratoryWebApplication, self).__init__(handlers, **settings)

    def init_settings(self, ipython_app, kernel_manager, notebook_manager,
                      session_manager,
                      log, settings_overrides,
                      jinja_env_options=None):
        template_path = settings_overrides.get("template_path", os.path.join(RESOURCES, "colab"))
        jenv_opt = jinja_env_options if jinja_env_options else {}
        env = Environment(loader=FileSystemLoader(template_path),**jenv_opt )
        settings = dict(
            # basics
            log_function=log_request,
            base_url='/',
            template_path=template_path,

            # authentication
            cookie_secret=ipython_app.cookie_secret,
            login_url='/login',
            password=ipython_app.password,

            # managers
            kernel_manager=kernel_manager,
            notebook_manager=notebook_manager,
            session_manager=session_manager,

            # IPython stuff
            config=ipython_app.config,
            jinja2_env=env,
        )

        # allow custom overrides for the tornado web app.
        settings.update(settings_overrides)
        return settings

    def init_handlers(self, settings):
        # Load the (URL pattern, handler) tuples for each component.
        here = os.path.dirname(__file__)
        colab = pjoin(RESOURCES, 'colab')
        handlers = [(r'/', web.RedirectHandler, {'url':'/welcome'}),
                    (r'/welcome(/?)', WelcomeHandler, {}),
                    (r'/notebook(/?)', NotebookHandler, {}),
                    (r'/colab/(.*)', web.StaticFileHandler,
                        {'path': colab}),
                    (r'/extern/(.*)', web.StaticFileHandler,
                        {'path': pjoin(RESOURCES, 'extern')}),
                    (r'/closure/(.*)', web.StaticFileHandler,
                        {'path': pjoin(RESOURCES, 'closure-library', 'closure', 'goog')}),
                    (r'/ipython/(.*)', FileFindHandler,
                        {'path': [pjoin(RESOURCES, 'ipython_patch'), DEFAULT_STATIC_FILES_PATH]}),
        ]
        
        handlers.extend(load_handlers('base.handlers'))
        handlers.extend(load_handlers('services.kernels.handlers'))
        handlers.extend(load_handlers('services.sessions.handlers'))
        
        return handlers


#-----------------------------------------------------------------------------
# Aliases and Flags
#-----------------------------------------------------------------------------

flags = dict(base_flags)
flags['no-browser']=(
    {'ColaboratoryApp' : {'open_browser' : False}},
    "Don't open the notebook in a browser after startup."
)

# Add notebook manager flags
aliases = dict(base_aliases)

aliases.update({
    'ip': 'ColaboratoryApp.ip',
    'port': 'ColaboratoryApp.port',
    'port-retries': 'ColaboratoryApp.port_retries',
    'transport': 'KernelManager.transport',
    'keyfile': 'ColaboratoryApp.keyfile',
    'certfile': 'ColaboratoryApp.certfile',
    'browser': 'ColaboratoryApp.browser',
})

#-----------------------------------------------------------------------------
# ColaboratoryApp
#-----------------------------------------------------------------------------

class ColaboratoryApp(BaseIPythonApplication):

    name = 'jupyter-colaboratory'

    description = """
        The Jupyter Colaboratory.

        This launches a Tornado based HTML Server that can run local Jupyter
    kernels while storing the notebook files in Google Drive, supporting
    real-time collaborative editing of the notebooks.
    """
    examples = _examples
    aliases = aliases
    flags = flags

    classes = [
        KernelManager, ProfileDir, Session, MappingKernelManager,
    ]
    flags = Dict(flags)
    aliases = Dict(aliases)

    kernel_argv = List(Unicode)

    _log_formatter_cls = LogFormatter

    def _log_level_default(self):
        return logging.INFO

    def _log_datefmt_default(self):
        """Exclude date from default date format"""
        return "%H:%M:%S"

    # create requested profiles by default, if they don't exist:
    auto_create = Bool(True)

    # Network related information.

    ip = Unicode('127.0.0.1', config=True,
        help="The IP address the notebook server will listen on."
    )

    def _ip_changed(self, name, old, new):
        if new == u'*': self.ip = u''

    port = Integer(8844, config=True,
        help="The port the notebook server will listen on."
    )
    port_retries = Integer(50, config=True,
        help="The number of additional ports to try if the specified port is not available."
    )

    certfile = Unicode(u'', config=True,
        help="""The full path to an SSL/TLS certificate file."""
    )

    keyfile = Unicode(u'', config=True,
        help="""The full path to a private key file for usage with SSL/TLS."""
    )

    cookie_secret = Bytes(b'', config=True,
        help="""The random bytes used to secure cookies.
        By default this is a new random number every time you start the Notebook.
        Set it to a value in a config file to enable logins to persist across server sessions.

        Note: Cookie secrets should be kept private, do not share config files with
        cookie_secret stored in plaintext (you can read the value from a file).
        """
    )
    def _cookie_secret_default(self):
        return os.urandom(1024)

    password = Unicode(u'', config=True,
                      help="""Hashed password to use for web authentication.

                      To generate, type in a python/IPython shell:

                        from IPython.lib import passwd; passwd()

                      The string should be of the form type:salt:hashed-password.
                      """
    )

    open_browser = Bool(True, config=True,
                        help="""Whether to open in a browser after starting.
                        The specific browser used is platform dependent and
                        determined by the python standard library `webbrowser`
                        module, unless it is overridden using the --browser
                        (ColaboratoryApp.browser) configuration option.
                        """)

    browser = Unicode(u'', config=True,
                      help="""Specify what command to use to invoke a web
                      browser when opening the notebook. If not specified, the
                      default browser will be determined by the `webbrowser`
                      standard library module, which allows setting of the
                      BROWSER environment variable to override it.
                      """)

    webapp_settings = Dict(config=True,
            help="Supply overrides for the tornado.web.Application that the "
                 "IPython notebook uses.")

    jinja_environment_options = Dict(config=True,
            help="Supply extra arguments that will be passed to Jinja environment.")

    notebook_manager_class = DottedObjectName('IPython.html.services.notebooks.filenbmanager.FileNotebookManager',
        config=True,
        help='The notebook manager class to use.'
    )
    kernel_manager_class = DottedObjectName('IPython.html.services.kernels.kernelmanager.MappingKernelManager',
        config=True,
        help='The kernel manager class to use.'
    )
    session_manager_class = DottedObjectName('IPython.html.services.sessions.sessionmanager.SessionManager',
        config=True,
        help='The session manager class to use.'
    )

    trust_xheaders = Bool(False, config=True,
        help=("Whether to trust or not X-Scheme/X-Forwarded-Proto and X-Real-Ip/X-Forwarded-For headers"
              "sent by the upstream reverse proxy. Necessary if the proxy handles SSL")
    )

    info_file = Unicode()

    def _info_file_default(self):
        info_file = "nbserver-%s.json"%os.getpid()
        return os.path.join(self.profile_dir.security_dir, info_file)

    def init_kernel_argv(self):
        """construct the kernel arguments"""
        # Kernel should get *absolute* path to profile directory
        self.kernel_argv = ["--profile-dir", self.profile_dir.location]

    def init_configurables(self):
        # force Session default to be secure
        default_secure(self.config)
        kls = import_item(self.kernel_manager_class)
        self.kernel_manager = kls(
            parent=self, log=self.log, kernel_argv=self.kernel_argv,
            connection_dir = self.profile_dir.security_dir,
        )
        kls = import_item(self.notebook_manager_class)
        self.notebook_manager = kls(parent=self, log=self.log)
        kls = import_item(self.session_manager_class)
        self.session_manager = kls(parent=self, log=self.log)

    def init_logging(self):
        # This prevents double log messages because tornado use a root logger that
        # self.log is a child of. The logging module dipatches log messages to a log
        # and all of its ancenstors until propagate is set to False.
        self.log.propagate = False

        # hook up tornado 3's loggers to our app handlers
        logger = logging.getLogger('tornado')
        logger.propagate = True
        logger.parent = self.log
        logger.setLevel(self.log.level)

    def init_webapp(self):
        """initialize tornado webapp and httpserver"""
        self.web_app = ColaboratoryWebApplication(
            self, self.kernel_manager, self.notebook_manager,
            self.session_manager,
            self.log, self.webapp_settings,
            self.jinja_environment_options
        )
        if self.certfile:
            ssl_options = dict(certfile=self.certfile)
            if self.keyfile:
                ssl_options['keyfile'] = self.keyfile
        else:
            ssl_options = None
        self.web_app.password = self.password
        self.http_server = httpserver.HTTPServer(self.web_app, ssl_options=ssl_options,
                                                 xheaders=self.trust_xheaders)
        if not self.ip:
            warning = "WARNING: The notebook server is listening on all IP addresses"
            if ssl_options is None:
                self.log.critical(warning + " and not using encryption. This "
                    "is not recommended.")
            if not self.password:
                self.log.critical(warning + " and not using authentication. "
                    "This is highly insecure and not recommended.")
        success = None
        for port in random_ports(self.port, self.port_retries+1):
            try:
                self.http_server.listen(port, self.ip)
            except socket.error as e:
                if e.errno == errno.EADDRINUSE:
                    self.log.info('The port %i is already in use, trying another random port.' % port)
                    continue
                elif e.errno in (errno.EACCES, getattr(errno, 'WSAEACCES', errno.EACCES)):
                    self.log.warn("Permission to listen on port %i denied" % port)
                    continue
                else:
                    raise
            else:
                self.port = port
                success = True
                break
        if not success:
            self.log.critical('ERROR: the notebook server could not be started because '
                              'no available port could be found.')
            self.exit(1)

    @property
    def display_url(self):
        ip = self.ip if self.ip else '[all ip addresses on your system]'
        return self._url(ip)

    @property
    def connection_url(self):
        ip = self.ip if self.ip else 'localhost'
        return self._url(ip)

    def _url(self, ip):
        proto = 'https' if self.certfile else 'http'
        return "%s://%s:%i" % (proto, ip, self.port)

    def init_signal(self):
        if not sys.platform.startswith('win'):
            signal.signal(signal.SIGINT, self._handle_sigint)
        signal.signal(signal.SIGTERM, self._signal_stop)
        if hasattr(signal, 'SIGUSR1'):
            # Windows doesn't support SIGUSR1
            signal.signal(signal.SIGUSR1, self._signal_info)
        if hasattr(signal, 'SIGINFO'):
            # only on BSD-based systems
            signal.signal(signal.SIGINFO, self._signal_info)

    def _handle_sigint(self, sig, frame):
        """SIGINT handler spawns confirmation dialog"""
        # register more forceful signal handler for ^C^C case
        signal.signal(signal.SIGINT, self._signal_stop)
        # request confirmation dialog in bg thread, to avoid
        # blocking the App
        thread = threading.Thread(target=self._confirm_exit)
        thread.daemon = True
        thread.start()

    def _restore_sigint_handler(self):
        """callback for restoring original SIGINT handler"""
        signal.signal(signal.SIGINT, self._handle_sigint)

    def _confirm_exit(self):
        """confirm shutdown on ^C

        A second ^C, or answering 'y' within 5s will cause shutdown,
        otherwise original SIGINT handler will be restored.

        This doesn't work on Windows.
        """
        info = self.log.info
        info('interrupted')
        print(self.notebook_info())
        sys.stdout.write("Shutdown this notebook server (y/[n])? ")
        sys.stdout.flush()
        r,w,x = select.select([sys.stdin], [], [], 5)
        if r:
            line = sys.stdin.readline()
            if line.lower().startswith('y') and 'n' not in line.lower():
                self.log.critical("Shutdown confirmed")
                ioloop.IOLoop.instance().stop()
                return
        else:
            print("No answer for 5s:", end=' ')
        print("resuming operation...")
        # no answer, or answer is no:
        # set it back to original SIGINT handler
        # use IOLoop.add_callback because signal.signal must be called
        # from main thread
        ioloop.IOLoop.instance().add_callback(self._restore_sigint_handler)

    def _signal_stop(self, sig, frame):
        self.log.critical("received signal %s, stopping", sig)
        ioloop.IOLoop.instance().stop()

    def _signal_info(self, sig, frame):
        print(self.notebook_info())

    def init_components(self):
        """Check the components submodule, and warn if it's unclean"""
        status = submodule.check_submodule_status()
        if status == 'missing':
            self.log.warn("components submodule missing, running `git submodule update`")
            submodule.update_submodules(submodule.ipython_parent())
        elif status == 'unclean':
            self.log.warn("components submodule unclean, you may see 404s on static/components")
            self.log.warn("run `setup.py submodule` or `git submodule update` to update")

    @catch_config_error
    def initialize(self, argv=None):
        super(ColaboratoryApp, self).initialize(argv)
        self.init_logging()
        self.init_kernel_argv()
        self.init_configurables()
        self.init_components()
        self.init_webapp()
        self.init_signal()

    def cleanup_kernels(self):
        """Shutdown all kernels.

        The kernels will shutdown themselves when this process no longer exists,
        but explicit shutdown allows the KernelManagers to cleanup the connection files.
        """
        self.log.info('Shutting down kernels')
        self.kernel_manager.shutdown_all()

    def notebook_info(self):
        "Return the current working directory and the server url information"
        info = self.notebook_manager.info_string() + "\n"
        info += "%d active kernels \n" % len(self.kernel_manager._kernels)
        return info + "The IPython Notebook is running at: %s" % self.display_url

    def server_info(self):
        """Return a JSONable dict of information about this server."""
        return {'url': self.connection_url,
                'hostname': self.ip if self.ip else 'localhost',
                'port': self.port,
                'secure': bool(self.certfile),
               }

    def write_server_info_file(self):
        """Write the result of server_info() to the JSON file info_file."""
        with open(self.info_file, 'w') as f:
            json.dump(self.server_info(), f, indent=2)

    def remove_server_info_file(self):
        """Remove the nbserver-<pid>.json file created for this server.

        Ignores the error raised when the file has already been removed.
        """
        try:
            os.unlink(self.info_file)
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise

    def start(self):
        """ Start the IPython Notebook server app, after initialization

        This method takes no arguments so all configuration and initialization
        must be done prior to calling this method."""
        if self.subapp is not None:
            return self.subapp.start()

        info = self.log.info
        for line in self.notebook_info().split("\n"):
            info(line)
        info("Use Control-C to stop this server and shut down all kernels (twice to skip confirmation).")

        self.write_server_info_file()

        if self.open_browser:
            try:
                browser = webbrowser.get(self.browser or None)
            except webbrowser.Error as e:
                self.log.warn('No web browser found: %s.' % e)
                browser = None
            if browser:
                b = lambda : browser.open(self.connection_url,
                                          new=2)
                threading.Thread(target=b).start()
        try:
            ioloop.IOLoop.instance().start()
        except KeyboardInterrupt:
            info("Interrupted...")
        finally:
            self.cleanup_kernels()
            self.remove_server_info_file()


#-----------------------------------------------------------------------------
# Main entry point
#-----------------------------------------------------------------------------

launch_new_instance = ColaboratoryApp.launch_instance
