"""Jupyter notebook application."""
from __future__ import annotations

import os
import re
import typing as t
from pathlib import Path

from jupyter_client.utils import ensure_async  # type: ignore[attr-defined]
from jupyter_core.application import base_aliases
from jupyter_core.paths import jupyter_config_dir
from jupyter_server.base.handlers import JupyterHandler
from jupyter_server.extension.handler import (
    ExtensionHandlerJinjaMixin,
    ExtensionHandlerMixin,
)
from jupyter_server.serverapp import flags
from jupyter_server.utils import url_escape, url_is_absolute
from jupyter_server.utils import url_path_join as ujoin
from jupyterlab.commands import (
    get_app_dir,
    get_user_settings_dir,
    get_workspaces_dir,
)
from jupyterlab_server import LabServerApp
from jupyterlab_server.config import (
    LabConfig,
    get_page_config,
    recursive_update,
)
from jupyterlab_server.handlers import _camelCase, is_url
from notebook_shim.shim import NotebookConfigShimMixin
from tornado import web
from traitlets import Bool, Unicode, default
from traitlets.config.loader import Config

from ._version import __version__

HERE = Path(__file__).parent.resolve()

Flags = t.Dict[t.Union[str, t.Tuple[str, ...]], t.Tuple[t.Union[t.Dict[str, t.Any], Config], str]]

app_dir = Path(get_app_dir())
version = __version__


class NotebookBaseHandler(ExtensionHandlerJinjaMixin, ExtensionHandlerMixin, JupyterHandler):
    """Base handler for notebook-related pages."""

    @property
    def custom_css(self) -> bool:
        return self.settings.get("custom_css", True)

    def get_page_config(self) -> dict[str, t.Any]:
        """Generate page configuration for the frontend."""
        config = LabConfig()
        app: JupyterNotebookApp = self.extensionapp  # type: ignore[assignment]
        base_url = self.settings.get("base_url", "/")
        page_config_data = self.settings.setdefault("page_config_data", {})
        page_config = {
            **page_config_data,
            "appVersion": version,
            "baseUrl": self.base_url,
            "terminalsAvailable": self.settings.get("terminals_available", False),
            "token": self.settings["token"],
            "fullStaticUrl": ujoin(self.base_url, "static", self.name),
            "frontendUrl": ujoin(self.base_url, "/"),
            "exposeAppInBrowser": app.expose_app_in_browser,
        }

        server_root = self.settings.get("server_root_dir", "").replace(os.sep, "/")
        server_root = os.path.normpath(Path(server_root).expanduser())
        try:
            page_config["preferredPath"] = "/" + os.path.relpath(self.serverapp.preferred_dir, server_root) \
                if self.serverapp.preferred_dir != server_root else "/"
        except Exception as e:
            self.log.error(f"Error determining preferred path: {e}")
            page_config["preferredPath"] = "/"

        mathjax_config = self.settings.get("mathjax_config", "TeX-AMS_HTML-full,Safe")
        mathjax_url = self.settings.get(
            "mathjax_url",
            "https://cdnjs.cloudflare.com/ajax/libs/mathjax/2.7.7/MathJax.js",
        )
        if not url_is_absolute(mathjax_url) and not mathjax_url.startswith(self.base_url):
            mathjax_url = ujoin(self.base_url, mathjax_url)

        page_config.update({
            "mathjaxConfig": mathjax_config,
            "fullMathjaxUrl": mathjax_url,
            "jupyterConfigDir": jupyter_config_dir(),
        })

        for name in config.trait_names():
            page_config[_camelCase(name)] = getattr(app, name)

        for name in config.trait_names():
            if name.endswith("_url"):
                full_name = _camelCase("full_" + name)
                full_url = getattr(app, name)
                if not is_url(full_url):
                    full_url = ujoin(base_url, full_url)
                page_config[full_name] = full_url

        labextensions_path = app.extra_labextensions_path + app.labextensions_path
        recursive_update(
            page_config,
            get_page_config(
                labextensions_path,
                logger=self.log,
            ),
        )

        page_config_hook = self.settings.get("page_config_hook")
        if page_config_hook:
            page_config = page_config_hook(self, page_config)

        return page_config


class TreeHandler(NotebookBaseHandler):
    """Handler for displaying directory structure and file redirection."""

    @web.authenticated
    async def get(self, path: str = "") -> None:
        """Display appropriate page for the given path."""
        path = path.strip("/")
        cm = self.contents_manager

        if await ensure_async(cm.dir_exists(path=path)):
            if await ensure_async(cm.is_hidden(path)) and not cm.allow_hidden:
                self.log.info("Refusing to serve hidden directory, via 404 Error")
                raise web.HTTPError(404)

            page_config = self.get_page_config()
            page_config["treePath"] = path
            tpl = self.render_template("tree.html", page_config=page_config)
            return self.write(tpl)

        if await ensure_async(cm.file_exists(path)):
            model = await ensure_async(cm.get(path, content=False))
            url = ujoin(self.base_url, "notebooks", url_escape(path)) \
                if model["type"] == "notebook" \
                else ujoin(self.base_url, "files", url_escape(path))
            self.log.debug("Redirecting %s to %s", self.request.path, url)
            self.redirect(url)
            return None

        raise web.HTTPError(404)


class ConsoleHandler(NotebookBaseHandler):
    """Handler for console page."""

    @web.authenticated
    def get(self, path: str | None = None) -> t.Any:
        """Get the console page."""
        tpl = self.render_template("consoles.html", page_config=self.get_page_config())
        return self.write(tpl)


class TerminalHandler(NotebookBaseHandler):
    """Handler for terminal page."""

    @web.authenticated
    def get(self, path: str | None = None) -> t.Any:
        """Get the terminal page."""
        tpl = self.render_template("terminals.html", page_config=self.get_page_config())
        return self.write(tpl)


class FileHandler(NotebookBaseHandler):
    """Handler for file page."""

    @web.authenticated
    def get(self, path: str | None = None) -> t.Any:
        """Get the file page."""
        tpl = self.render_template("edit.html", page_config=self.get_page_config())
        return self.write(tpl)


class NotebookHandler(NotebookBaseHandler):
    """Handler for notebook page."""

    @web.authenticated
    def get(self, path: str | None = None) -> t.Any:
        """Get the notebook page."""
        tpl = self.render_template("notebooks.html", page_config=self.get_page_config())
        return self.write(tpl)


class CustomCssHandler(NotebookBaseHandler):
    """Handler for serving custom CSS."""

    @web.authenticated
    def get(self) -> t.Any:
        """Serve the custom CSS file."""
        self.set_header("Content-Type", "text/css")
        page_config = self.get_page_config()
        custom_css_file = f"{page_config['jupyterConfigDir']}/custom/custom.css"

        if not Path(custom_css_file).is_file():
            static_path_root = re.match("^(.*?)static", page_config["staticDir"])
            if static_path_root:
                custom_css_file = f"{static_path_root.groups()[0]}custom/custom.css"

        try:
            with Path(custom_css_file).open() as css_f:
                return self.write(css_f.read())
        except IOError as e:
            self.log.error(f"Error reading custom CSS file: {e}")
            raise web.HTTPError(500, "Custom CSS file not found.")


aliases = dict(base_aliases)


class JupyterNotebookApp(NotebookConfigShimMixin, LabServerApp):
    """Jupyter Notebook server application."""

    name = "notebook"
    app_name = "Jupyter Notebook"
    description = "Jupyter Notebook - A web-based notebook environment for interactive computing"
    version = version
    app_version = Unicode(version, help="The version of the application.")
    extension_url = "/"
    default_url = Unicode("/tree", config=True, help="The default URL to redirect to from `/`")
    file_url_prefix = "/tree"
    load_other_extensions = True
    app_dir = app_dir
    subcommands: dict[str, t.Any] = {}

    expose_app_in_browser = Bool(
        False,
        config=True,
        help="Whether to expose the global app instance to browser via window.jupyterapp",
    )

    custom_css = Bool(
        True,
        config=True,
        help="""Whether custom CSS is loaded on the page.
        Defaults to True and custom CSS is loaded.
        """,
    )

    flags: Flags = flags
    flags["expose-app-in-browser"] = (
        {"JupyterNotebookApp": {"expose_app_in_browser": True}},
        "Expose the global app instance to browser via window.jupyterapp.",
    )

    flags["no-custom-css"] = (
        {"JupyterNotebookApp": {"custom_css": False}},
        "Disable the loading of custom CSS.",
    )

    def initialize(self, argv: t.List[str] | None = None) -> None:
        """Initialize the Jupyter Notebook application."""
        super().initialize(argv)
        self.config = self.config or Config()
        self.config_file = self.config_file or os.path.join(
            self.config_dir, "jupyter_notebook_config.py"
        )

    @default("server_root_dir")
    def _default_server_root_dir(self) -> str:
        """Default server root directory."""
        return str(Path(self.app_dir).resolve())

    def init_webapp(self) -> None:
        """Initialize the web application."""
        self.webapp = self.webapp or self.create_webapp()
        self.webapp.settings.update({"custom_css": self.custom_css})

    def init_configurables(self) -> None:
        """Initialize application configurables."""
        super().init_configurables()
        self.web_app_config = self.web_app_config or self.create_web_app_config()
        self.web_app_config.settings.update({"custom_css": self.custom_css})

    def start(self) -> None:
        """Start the Jupyter Notebook application."""
        self.init_configurables()
        self.init_webapp()
        super().start()

    def create_webapp(self) -> web.Application:
        """Create the Tornado web application."""
        return web.Application(
            [
                (r"/tree/(.*)", TreeHandler),
                (r"/consoles/(.*)", ConsoleHandler),
                (r"/terminals/(.*)", TerminalHandler),
                (r"/files/(.*)", FileHandler),
                (r"/notebooks/(.*)", NotebookHandler),
                (r"/custom/custom.css", CustomCssHandler),
            ],
            default_handler_class=web.ErrorHandler,
            default_handler_args=(404,),
            **self.web_app_config.settings,
        )

    def create_web_app_config(self) -> dict[str, t.Any]:
        """Create the configuration for the Tornado web application."""
        return {"settings": {"custom_css": self.custom_css}}

if __name__ == "__main__":
    JupyterNotebookApp.launch_instance()
