from collections import namedtuple
from copy import deepcopy
from glob import glob
import importlib
import os
from typing import cast, Any, Dict, Iterator, List, Optional, Tuple, Type, Union

import appdirs
import pkg_resources

from . import exceptions
from . import fmt
from . import serialize


CONFIG_KEY = "PLUGINS"


class BasePlugin:
    """
    Tutor plugins are defined by a name and an object that implements one or more of the
    following properties:

    `config` (dict str->dict(str->str)): contains "add", "set", "default" keys. Entries
    in these dicts will be added or override the global configuration. Keys in "add" and
    "set" will be prefixed by the plugin name in uppercase.

    `patches` (dict str->str): entries in this dict will be used to patch the rendered
    Tutor templates. For instance, to add "somecontent" to a template that includes '{{
    patch("mypatch") }}', set: `patches["mypatch"] = "somecontent"`. It is recommended
    to store all patches in separate files, and to dynamically list patches by listing
    the contents of a "patches"  subdirectory.

    `templates` (str): path to a directory that includes new template files for the
    plugin. It is recommended that all files in the template directory are stored in a
    `myplugin` folder to avoid conflicts with other plugins. Plugin templates are useful
    for content re-use, e.g: "{% include 'myplugin/mytemplate.html'}".

    `hooks` (dict str->list[str]): hooks are commands that will be run at various points
    during the lifetime of the platform. For instance, to run `service1` and `service2`
    in sequence during initialization, you should define:

        hooks["init"] = ["service1", "service2"]

    It is then assumed that there are `myplugin/hooks/service1/init` and
    `myplugin/hooks/service2/init` templates in the plugin `templates` directory.

    `command` (click.Command): if a plugin exposes a `command` attribute, users will be able to run it from the command line as `tutor pluginname`.
    """

    INSTALLED: List["BasePlugin"] = []
    _IS_LOADED = False

    def __init__(self, name: str, obj: Any) -> None:
        self.name = name
        self.config = cast(
            Dict[str, Dict[str, Any]], get_callable_attr(obj, "config", {})
        )
        self.patches = cast(
            Dict[str, str], get_callable_attr(obj, "patches", default={})
        )
        self.hooks = cast(
            Dict[str, Union[Dict[str, str], List[str]]],
            get_callable_attr(obj, "hooks", default={}),
        )
        self.templates_root = cast(
            Optional[str], get_callable_attr(obj, "templates", default=None)
        )
        self.command = getattr(obj, "command", None)

    def config_key(self, key: str) -> str:
        """
        Config keys in the "add" and "defaults" dicts should be prefixed by the plugin name, in uppercase.
        """
        return self.name.upper() + "_" + key

    @property
    def config_add(self) -> Dict[str, Any]:
        return self.config.get("add", {})

    @property
    def config_set(self) -> Dict[str, Any]:
        return self.config.get("set", {})

    @property
    def config_defaults(self) -> Dict[str, Any]:
        return self.config.get("defaults", {})

    @property
    def version(self) -> str:
        raise NotImplementedError

    @classmethod
    def iter_installed(cls) -> Iterator["BasePlugin"]:
        if not cls._IS_LOADED:
            for plugin in cls.iter_load():
                cls.INSTALLED.append(plugin)
            cls._IS_LOADED = True
        yield from cls.INSTALLED

    @classmethod
    def iter_load(cls) -> Iterator["BasePlugin"]:
        raise NotImplementedError


class EntrypointPlugin(BasePlugin):
    """
    Entrypoint plugins are regular python packages that have a 'tutor.plugin.v0' entrypoint.

    The API for Tutor plugins is currently in development. The entrypoint will switch to
    'tutor.plugin.v1' once it is stabilised.
    """

    ENTRYPOINT = "tutor.plugin.v0"

    def __init__(self, entrypoint: pkg_resources.EntryPoint) -> None:
        super().__init__(entrypoint.name, entrypoint.load())
        self.entrypoint = entrypoint

    @property
    def version(self) -> str:
        if not self.entrypoint.dist:
            return "0.0.0"
        return self.entrypoint.dist.version

    @classmethod
    def iter_load(cls) -> Iterator["EntrypointPlugin"]:
        for entrypoint in pkg_resources.iter_entry_points(cls.ENTRYPOINT):
            yield cls(entrypoint)


class OfficialPlugin(BasePlugin):
    """
    Official plugins have a "plugin" module which exposes a __version__ attribute.
    Official plugins should be manually added by calling `OfficialPlugin.load()`.
    """

    @classmethod
    def load(cls, name: str) -> BasePlugin:
        plugin = cls(name)
        cls.INSTALLED.append(plugin)
        return plugin

    def __init__(self, name: str):
        self.module = importlib.import_module("tutor{}.plugin".format(name))
        super().__init__(name, self.module)

    @property
    def version(self) -> str:
        version = getattr(self.module, "__version__")
        if not isinstance(version, str):
            raise TypeError("OfficialPlugin __version__ must be 'str'")
        return version

    @classmethod
    def iter_load(cls) -> Iterator[BasePlugin]:
        yield from []


class DictPlugin(BasePlugin):
    ROOT_ENV_VAR_NAME = "TUTOR_PLUGINS_ROOT"
    ROOT = os.path.expanduser(
        os.environ.get(ROOT_ENV_VAR_NAME, "")
    ) or appdirs.user_data_dir(appname="tutor-plugins")

    def __init__(self, data: Dict[str, Any]):
        Module = namedtuple("Module", data.keys())  # type: ignore
        obj = Module(**data)  # type: ignore
        super().__init__(data["name"], obj)
        self._version = data["version"]

    @property
    def version(self) -> str:
        if not isinstance(self._version, str):
            raise TypeError("DictPlugin.__version__ must be str")
        return self._version

    @classmethod
    def iter_load(cls) -> Iterator[BasePlugin]:
        for path in glob(os.path.join(cls.ROOT, "*.yml")):
            with open(path) as f:
                data = serialize.load(f)
                if not isinstance(data, dict):
                    raise exceptions.TutorError(
                        "Invalid plugin: {}. Expected dict.".format(path)
                    )
                try:
                    yield cls(data)
                except KeyError as e:
                    raise exceptions.TutorError(
                        "Invalid plugin: {}. Missing key: {}".format(path, e.args[0])
                    )


class Plugins:
    PLUGIN_CLASSES: List[Type[BasePlugin]] = [
        OfficialPlugin,
        EntrypointPlugin,
        DictPlugin,
    ]

    def __init__(self, config: Dict[str, Any]):
        self.config = deepcopy(config)
        self.patches: Dict[str, Dict[str, str]] = {}
        self.hooks: Dict[str, Dict[str, Union[Dict[str, str], List[str]]]] = {}
        self.template_roots: Dict[str, str] = {}

        for plugin in self.iter_enabled():
            for patch_name, content in plugin.patches.items():
                if patch_name not in self.patches:
                    self.patches[patch_name] = {}
                self.patches[patch_name][plugin.name] = content

            for hook_name, services in plugin.hooks.items():
                if hook_name not in self.hooks:
                    self.hooks[hook_name] = {}
                self.hooks[hook_name][plugin.name] = services

    @classmethod
    def clear(cls) -> None:
        for PluginClass in cls.PLUGIN_CLASSES:
            PluginClass.INSTALLED.clear()

    @classmethod
    def iter_installed(cls) -> Iterator[BasePlugin]:
        """
        Iterate on all installed plugins. Plugins are deduplicated by name. The list of installed plugins is cached to
        prevent too many re-computations, which happens a lot.
        """
        installed_plugin_names = set()
        for PluginClass in cls.PLUGIN_CLASSES:
            for plugin in PluginClass.iter_installed():
                if plugin.name not in installed_plugin_names:
                    installed_plugin_names.add(plugin.name)
                    yield plugin

    def iter_enabled(self) -> Iterator[BasePlugin]:
        for plugin in self.iter_installed():
            if is_enabled(self.config, plugin.name):
                yield plugin

    def iter_patches(self, name: str) -> Iterator[Tuple[str, str]]:
        plugin_patches = self.patches.get(name, {})
        plugins = sorted(plugin_patches.keys())
        for plugin in plugins:
            yield plugin, plugin_patches[plugin]

    def iter_hooks(
        self, hook_name: str
    ) -> Iterator[Tuple[str, Union[Dict[str, str], List[str]]]]:
        yield from self.hooks.get(hook_name, {}).items()


def get_callable_attr(
    plugin: Any, attr_name: str, default: Optional[Any] = None
) -> Optional[Any]:
    attr = getattr(plugin, attr_name, default)
    if callable(attr):
        attr = attr()
    return attr


def is_installed(name: str) -> bool:
    for plugin in iter_installed():
        if name == plugin.name:
            return True
    return False


def iter_installed() -> Iterator[BasePlugin]:
    yield from Plugins.iter_installed()


def enable(config: Dict[str, Any], name: str) -> None:
    if not is_installed(name):
        raise exceptions.TutorError("plugin '{}' is not installed.".format(name))
    if is_enabled(config, name):
        return
    if CONFIG_KEY not in config:
        config[CONFIG_KEY] = []
    config[CONFIG_KEY].append(name)
    config[CONFIG_KEY].sort()


def disable(config: Dict[str, Any], name: str) -> None:
    fmt.echo_info("Disabling plugin {}...".format(name))
    for plugin in Plugins(config).iter_enabled():
        if name == plugin.name:
            # Remove "set" config entries
            for key, value in plugin.config_set.items():
                config.pop(key, None)
                fmt.echo_info("    Removed config entry {}={}".format(key, value))
    # Remove plugin from list
    while name in config[CONFIG_KEY]:
        config[CONFIG_KEY].remove(name)
    fmt.echo_info("    Plugin disabled")


def iter_enabled(config: Dict[str, Any]) -> Iterator[BasePlugin]:
    yield from Plugins(config).iter_enabled()


def is_enabled(config: Dict[str, Any], name: str) -> bool:
    return name in config.get(CONFIG_KEY, [])


def iter_patches(config: Dict[str, str], name: str) -> Iterator[Tuple[str, str]]:
    yield from Plugins(config).iter_patches(name)


def iter_hooks(
    config: Dict[str, Any], hook_name: str
) -> Iterator[Tuple[str, Union[Dict[str, str], List[str]]]]:
    yield from Plugins(config).iter_hooks(hook_name)
