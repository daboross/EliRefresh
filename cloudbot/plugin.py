import asyncio
import enum
import glob
import importlib
import inspect
import logging
import os
import re

import sqlalchemy

from cloudbot.event import Event
from cloudbot.util import botvars

logger = logging.getLogger("cloudbot")


class HookType(enum.Enum):
    command = 1,
    regex = 2,
    event = 4,
    onload = 5,
    irc_raw = 6,
    sieve = 3,

# cloudbot.hook imports plugin.HookType, so to not cause a circular import error, we import cloudbot.hook after defining
#  the HookType enum. TODO: is there *any* better way to do this?
import cloudbot.hook


def find_hooks(parent, module):
    """
    :type parent: Plugin
    :type module: object
    :rtype: (list[CommandHook], list[RegexHook], list[RawHook], list[SieveHook], List[EventHook], list[OnloadHook])
    """
    # set the loaded flag
    module._cloudbot_loaded = True
    command = []
    regex = []
    raw = []
    sieve = []
    event = []
    onload = []
    type_lists = {HookType.command: command, HookType.regex: regex, HookType.irc_raw: raw, HookType.sieve: sieve,
                  HookType.event: event, HookType.onload: onload}
    for name, func in module.__dict__.items():
        if hasattr(func, "_cloudbot_hook"):
            # if it has cloudbot hook
            func_hooks = func._cloudbot_hook

            for hook_type, func_hook in func_hooks.items():
                type_lists[hook_type].append(_hook_type_to_plugin[hook_type](parent, func_hook))

            # delete the hook to free memory
            del func._cloudbot_hook

    return command, regex, raw, sieve, event, onload


def find_tables(code):
    """
    :type code: object
    :rtype: list[sqlalchemy.Table]
    """
    tables = []
    for name, obj in code.__dict__.items():
        if isinstance(obj, sqlalchemy.Table) and obj.metadata == botvars.metadata:
            # if it's a Table, and it's using our metadata, append it to the list
            tables.append(obj)

    return tables


class PluginManager:
    """
    PluginManager is the core of CloudBot plugin loading.

    PluginManager loads Plugins, and adds their Hooks to easy-access dicts/lists.

    Each Plugin represents a file, and loads hooks onto itself using find_hooks.

    Plugins are the lowest level of abstraction in this class. There are four different plugin types:
    - CommandPlugin is for bot commands
    - RawPlugin hooks onto irc_raw irc lines
    - RegexPlugin loads a regex parameter, and executes on irc lines which match the regex
    - SievePlugin is a catch-all sieve, which all other plugins go through before being executed.

    :type bot: cloudbot.bot.CloudBot
    :type plugins: dict[str, Plugin]
    :type commands: dict[str, CommandHook]
    :type raw_triggers: dict[str, list[RawHook]]
    :type catch_all_triggers: list[RawHook]
    :type event_type_hooks: dict[cloudbot.event.EventType, list[EventHook]]
    :type regex_hooks: list[(re.__Regex, RegexHook)]
    :type sieves: list[SieveHook]
    """

    def __init__(self, bot):
        """
        Creates a new PluginManager. You generally only need to do this from inside cloudbot.bot.CloudBot
        :type bot: cloudbot.bot.CloudBot
        """
        self.bot = bot

        self.plugins = {}
        self.commands = {}
        self.raw_triggers = {}
        self.catch_all_triggers = []
        self.event_type_hooks = {}
        self.regex_hooks = []
        self.sieves = []
        self._hook_waiting_queues = {}

    @asyncio.coroutine
    def load_all(self, plugin_dir):
        """
        Load a plugin from each *.py file in the given directory.

        Won't load any plugins listed in "disabled_plugins".

        :type plugin_dir: str
        """
        path_list = glob.iglob(os.path.join(plugin_dir, '*.py'))
        # Load plugins asynchronously :O
        yield from asyncio.gather(*[self.load_plugin(path) for path in path_list], loop=self.bot.loop)

    @asyncio.coroutine
    def load_plugin(self, path):
        """
        Loads a plugin from the given path and plugin object, then registers all hooks from that plugin.

        Won't load any plugins listed in "disabled_plugins".

        :type path: str
        """
        file_path = os.path.abspath(path)
        file_name = os.path.basename(path)
        title = os.path.splitext(file_name)[0]

        if "plugin_loading" in self.bot.config:
            pl = self.bot.config.get("plugin_loading")

            if pl.get("use_whitelist", False):
                if title not in pl.get("whitelist", []):
                    logger.info('Not loading plugin module "{}": plugin not whitelisted'.format(file_name))
                    return
            else:
                if title in pl.get("blacklist", []):
                    logger.info('Not loading plugin module "{}": plugin blacklisted'.format(file_name))
                    return

        # make sure to unload the previously loaded plugin from this path, if it was loaded.
        if file_name in self.plugins:
            yield from self._unload(file_path)

        module_name = "plugins.{}".format(title)
        try:
            plugin_module = importlib.import_module(module_name)
            # if this plugin was loaded before, reload it
            if hasattr(plugin_module, "_cloudbot_loaded"):
                importlib.reload(plugin_module)
        except Exception:
            logger.exception("Error loading {}:".format(file_name))
            return

        # create the plugin
        plugin = Plugin(file_path, file_name, title, plugin_module)

        # proceed to register hooks

        # create database tables
        yield from plugin.create_tables(self.bot)

        # run onload hooks
        for onload_hook in plugin.run_on_load:
            success = yield from self.launch(onload_hook, Event(bot=self.bot, hook=onload_hook))
            if not success:
                logger.warning("Not registering hooks from plugin {}: onload hook errored".format(plugin.title))

                # unregister databases
                plugin.unregister_tables(self.bot)
                return

        self.plugins[plugin.file_name] = plugin

        # register commands
        for command_hook in plugin.commands:
            for alias in command_hook.aliases:
                if alias in self.commands:
                    logger.warning(
                        "Plugin {} attempted to register command {} which was already registered by {}. "
                        "Ignoring new assignment.".format(plugin.title, alias, self.commands[alias].plugin.title))
                else:
                    self.commands[alias] = command_hook
            self._log_hook(command_hook)

        # register raw hooks
        for raw_hook in plugin.raw_hooks:
            if raw_hook.is_catch_all():
                self.catch_all_triggers.append(raw_hook)
            else:
                for trigger in raw_hook.triggers:
                    if trigger in self.raw_triggers:
                        self.raw_triggers[trigger].append(raw_hook)
                    else:
                        self.raw_triggers[trigger] = [raw_hook]
            self._log_hook(raw_hook)

        # register events
        for event_hook in plugin.events:
            for event_type in event_hook.types:
                if event_type in self.event_type_hooks:
                    self.event_type_hooks[event_type].append(event_hook)
                else:
                    self.event_type_hooks[event_type] = [event_hook]
            self._log_hook(event_hook)

        # register regexps
        for regex_hook in plugin.regexes:
            for regex_match in regex_hook.regexes:
                self.regex_hooks.append((regex_match, regex_hook))
            self._log_hook(regex_hook)

        # register sieves
        for sieve_hook in plugin.sieves:
            self.sieves.append(sieve_hook)
            self._log_hook(sieve_hook)

        # we don't need this anymore
        del plugin.run_on_load

    @asyncio.coroutine
    def _unload(self, path):
        """
        Unloads the plugin from the given path, unregistering all hooks from the plugin.

        Returns True if the plugin was unloaded, False if the plugin wasn't loaded in the first place.

        :type path: str
        :rtype: bool
        """
        file_name = os.path.basename(path)
        title = os.path.splitext(file_name)[0]
        if "disabled_plugins" in self.bot.config and title in self.bot.config['disabled_plugins']:
            # this plugin hasn't been loaded, so no need to unload it
            return False

        # make sure this plugin is actually loaded
        if not file_name in self.plugins:
            return False

        # get the loaded plugin
        plugin = self.plugins[file_name]

        # unregister commands
        for command_hook in plugin.commands:
            for alias in command_hook.aliases:
                if alias in self.commands and self.commands[alias] == command_hook:
                    # we need to make sure that there wasn't a conflict, so we don't delete another plugin's command
                    del self.commands[alias]

        # unregister raw hooks
        for raw_hook in plugin.raw_hooks:
            if raw_hook.is_catch_all():
                self.catch_all_triggers.remove(raw_hook)
            else:
                for trigger in raw_hook.triggers:
                    assert trigger in self.raw_triggers  # this can't be not true
                    self.raw_triggers[trigger].remove(raw_hook)
                    if not self.raw_triggers[trigger]:  # if that was the last hook for this trigger
                        del self.raw_triggers[trigger]

        # unregister events
        for event_hook in plugin.events:
            for event_type in event_hook.types:
                assert event_type in self.event_type_hooks  # this can't be not true
                self.event_type_hooks[event_type].remove(event_hook)
                if not self.event_type_hooks[event_type]:  # if that was the last hook for this event type
                    del self.event_type_hooks[event_type]

        # unregister regexps
        for regex_hook in plugin.regexes:
            for regex_match in regex_hook.regexes:
                self.regex_hooks.remove((regex_match, regex_hook))

        # unregister sieves
        for sieve_hook in plugin.sieves:
            self.sieves.remove(sieve_hook)

        # unregister databases
        plugin.unregister_tables(self.bot)

        # remove last reference to plugin
        del self.plugins[plugin.file_name]

        if self.bot.config.get("logging", {}).get("show_plugin_loading", True):
            logger.info("Unloaded all plugins from {}".format(plugin.title))

        return True

    def _log_hook(self, hook):
        """
        Logs registering a given hook

        :type hook: Hook
        """
        if self.bot.config.get("logging", {}).get("show_plugin_loading", True):
            logger.info("Loaded {}".format(hook))
            logger.debug("Loaded {}".format(repr(hook)))

    # TODO: create remove_hook() method
    def add_hook(self, hook_type, function, *args, **kwargs):
        """
        Add an internal hook, like a plugin @hook.X, but for methods in the core. Kind of like an internal event system.
        :param hook_type: The type of the hook (command, regex, event, sieve, or irc_raw)
        :param function: The function to call
        :param args: Arguments to pass to the hook, dependent on the hook type
        :param kwargs: Keyword arguments to pass to the hook, dependent on the hook type
        :type hook_type: HookType
        """
        # Get the plugin, or create it - we want one unique plugin for each core file.
        file = inspect.getmodule(function).__file__
        # filename is used as the unique key for the plugin.
        # we prepend internal/ so that this isn't confused with internal plugins.
        # We *do* assume here that no core files will have the same basename, even if they are in different directories.
        # I think that is a sane assumption.
        filename = "internal/" + os.path.basename(file)
        if filename in self.plugins:
            plugin = self.plugins[filename]
        else:
            filepath = os.path.abspath(file)
            title = os.path.splitext(filename)[0]
            plugin = Plugin(filepath, filename, title)
            self.plugins[filename] = plugin

        # we don't allow onload hooks for internal. We don't have to check a valid type otherwise, because
        # the _hook_name_to_hook[hook_type] call will raise a KeyError already.
        if hook_type is HookType.onload:
            raise ValueError("onload hooks not allowed")

        # this might seem a little hacky, but I think it's a good design choice.
        # hook.py is in charge of argument processing, so it should process them here to
        _processing_hook = cloudbot.hook._hook_name_to_hook[hook_type](function)
        _processing_hook.add_hook(*args, **kwargs)
        # create the *Hook object
        hook = _hook_type_to_plugin[hook_type](plugin, _processing_hook)

        # Register the hook.
        # I *think* this is the best way to do this, there might be a more pythonic way though, not sure.
        if hook_type is HookType.command:
            # TODO: Should we disallow internal command hooks? It seems like it could be a bad design choice
            #  to allow them
            for alias in hook.aliases:
                if alias in self.commands:
                    logger.warning(
                        "Internal file {} attempted to register command {} which was already registered by {}. "
                        "Ignoring new assignment.".format(plugin.title, alias, self.commands[alias].plugin.title))
                else:
                    self.commands[alias] = hook
        elif hook_type is HookType.irc_raw:
            if hook.is_catch_all():
                self.catch_all_triggers.append(hook)
            else:
                for trigger in hook.triggers:
                    if trigger in self.raw_triggers:
                        self.raw_triggers[trigger].append(hook)
                    else:
                        self.raw_triggers[trigger] = [hook]
        elif hook_type is HookType.event:
            for event_type in hook.types:
                if event_type in self.event_type_hooks:
                    self.event_type_hooks[event_type].append(hook)
                else:
                    self.event_type_hooks[event_type] = [hook]
        elif hook_type is HookType.regex:
            for regex_match in hook.regexes:
                self.regex_hooks.append((regex_match, hook))
        elif hook_type is HookType.sieve:
            self.sieves.append(hook)

        # Log the hook. TODO: Do we want to do this for internal hooks?
        self._log_hook(hook)

    def _prepare_parameters(self, hook, event):
        """
        Prepares arguments for the given hook

        :type hook: cloudbot.plugin.Hook
        :type event: cloudbot.event.Event
        :rtype: list
        """
        parameters = []
        for required_arg in hook.required_args:
            if hasattr(event, required_arg):
                value = getattr(event, required_arg)
                parameters.append(value)
            else:
                logger.error("Plugin {} asked for invalid argument '{}', cancelling execution!"
                             .format(hook.description, required_arg))
                logger.debug("Valid arguments are: {} ({})".format(dir(event), event))
                return None
        return parameters

    def _execute_hook_threaded(self, hook, event):
        """
        :type hook: Hook
        :type event: cloudbot.event.Event
        """
        event.prepare_threaded()

        parameters = self._prepare_parameters(hook, event)
        if parameters is None:
            return None

        try:
            return hook.function(*parameters)
        finally:
            event.close_threaded()

    @asyncio.coroutine
    def _execute_hook_sync(self, hook, event):
        """
        :type hook: Hook
        :type event: cloudbot.event.Event
        """
        yield from event.prepare()

        parameters = self._prepare_parameters(hook, event)
        if parameters is None:
            return None

        try:
            return (yield from hook.function(*parameters))
        finally:
            yield from event.close()

    @asyncio.coroutine
    def _execute_hook(self, hook, event):
        """
        Runs the specific hook with the given bot and event.

        Returns False if the hook errored, True otherwise.

        :type hook: cloudbot.plugin.Hook
        :type event: cloudbot.event.Event
        :rtype: bool
        """
        try:
            # _internal_run_threaded and _internal_run_coroutine prepare the database, and run the hook.
            # _internal_run_* will prepare parameters and the database session, but won't do any error catching.
            if hook.threaded:
                out = yield from self.bot.loop.run_in_executor(None, self._execute_hook_threaded, hook, event)
            else:
                out = yield from self._execute_hook_sync(hook, event)
        except Exception:
            logger.exception("Error in hook {}".format(hook.description))
            return False

        if out is not None:
            # if there are multiple items in the response, return them on multiple lines
            if isinstance(out, (list, tuple)):
                event.reply(out[0])
                for line in out[1:]:
                    event.message(line)
            else:
                event.reply(str(out))
        return True

    @asyncio.coroutine
    def _sieve(self, sieve, event, hook):
        """
        :type sieve: cloudbot.plugin.Hook
        :type event: cloudbot.event.Event
        :type hook: cloudbot.plugin.Hook
        :rtype: cloudbot.event.Event
        """
        try:
            if sieve.threaded:
                result = yield from self.bot.loop.run_in_executor(None, sieve.function, self.bot, event, hook)
            else:
                result = yield from sieve.function(self.bot, event, hook)
        except Exception:
            logger.exception("Error running sieve {} on {}:".format(sieve.description, hook.description))
            return None
        else:
            return result

    @asyncio.coroutine
    def launch(self, hook, event):
        """
        Dispatch a given event to a given hook using a given bot object.

        Returns False if the hook didn't run successfully, and True if it ran successfully.

        :type event: cloudbot.event.Event | cloudbot.event.CommandEvent
        :type hook: cloudbot.plugin.Hook | cloudbot.plugin.CommandHook
        :rtype: bool
        """
        if hook.type is not HookType.onload:  # we don't need sieves on onload hooks.
            for sieve in self.bot.plugin_manager.sieves:
                event = yield from self._sieve(sieve, event, hook)
                if event is None:
                    return False

        if hook.type is HookType.command and hook.auto_help and not event.text and hook.doc is not None:
            event.notice_doc()
            return False

        if hook.single_thread:
            # There should only be one running instance of this hook, so let's wait for the last event to be processed
            # before starting this one.

            key = (hook.plugin.title, hook.function_name)
            if key in self._hook_waiting_queues:
                queue = self._hook_waiting_queues[key]
                if queue is None:
                    # there's a hook running, but the queue hasn't been created yet, since there's only one hook
                    queue = asyncio.Queue()
                    self._hook_waiting_queues[key] = queue
                assert isinstance(queue, asyncio.Queue)
                # create a future to represent this task
                future = asyncio.Future()
                queue.put_nowait(future)
                # wait until the last task is completed
                yield from future
            else:
                # set to None to signify that this hook is running, but there's no need to create a full queue
                # in case there are no more hooks that will wait
                self._hook_waiting_queues[key] = None

            # Run the plugin with the message, and wait for it to finish
            result = yield from self._execute_hook(hook, event)

            queue = self._hook_waiting_queues[key]
            if queue is None or queue.empty():
                # We're the last task in the queue, we can delete it now.
                del self._hook_waiting_queues[key]
            else:
                # set the result for the next task's future, so they can execute
                next_future = yield from queue.get()
                next_future.set_result(None)
        else:
            # Run the plugin with the message, and wait for it to finish
            result = yield from self._execute_hook(hook, event)

        # Return the result
        return result


class Plugin:
    """
    Each Plugin represents a plugin file, and contains loaded hooks.

    :type file_path: str
    :type file_name: str
    :type title: str
    :type commands: list[CommandHook]
    :type regexes: list[RegexHook]
    :type raw_hooks: list[RawHook]
    :type sieves: list[SieveHook]
    :type events: list[EventHook]
    :type tables: list[sqlalchemy.Table]
    """

    def __init__(self, filepath, filename, title, code=None):
        """
        :param code: Optional code argument, should be specified for all *actual* plugins.
                        If provided, all hooks will be retrieved and attached to this plugin from the code.
        :type filepath: str
        :type filename: str
        :type code: object
        """
        self.file_path = filepath
        self.file_name = filename
        self.title = title
        if code is not None:
            self.commands, self.regexes, self.raw_hooks, self.sieves, self.events, self.run_on_load = find_hooks(self,
                                                                                                                 code)
            # we need to find tables for each plugin so that they can be unloaded from the global metadata when the
            # plugin is reloaded
            self.tables = find_tables(code)

    @asyncio.coroutine
    def create_tables(self, bot):
        """
        Creates all sqlalchemy Tables that are registered in this plugin

        :type bot: cloudbot.bot.CloudBot
        """
        if self.tables:
            # if there are any tables

            logger.info("Registering tables for {}".format(self.title))

            for table in self.tables:
                if not (yield from bot.loop.run_in_executor(None, table.exists, bot.db_engine)):
                    yield from bot.loop.run_in_executor(None, table.create, bot.db_engine)

    def unregister_tables(self, bot):
        """
        Unregisters all sqlalchemy Tables registered to the global metadata by this plugin
        :type bot: cloudbot.bot.CloudBot
        """
        if self.tables:
            # if there are any tables
            logger.info("Unregistering tables for {}".format(self.title))

            for table in self.tables:
                bot.db_metadata.remove(table)


class Hook:
    """
    Each hook is specific to one function. This class is never used by itself, rather extended.

    :type type: HookType
    :type plugin: Plugin
    :type function: callable
    :type function_name: str
    :type required_args: list[str]
    :type threaded: bool
    :type ignore_bots: bool
    :type permissions: list[str]
    :type single_thread: bool
    """
    type = None  # to be assigned in subclasses

    def __init__(self, plugin, func_hook):
        """
        :type plugin: Plugin
        :type func_hook: hook._Hook
        """
        self.plugin = plugin
        self.function = func_hook.function
        self.function_name = self.function.__name__

        self.required_args = inspect.getargspec(self.function)[0]
        if self.required_args is None:
            self.required_args = []

        if asyncio.iscoroutine(self.function) or asyncio.iscoroutinefunction(self.function):
            self.threaded = False
        else:
            self.threaded = True

        self.ignore_bots = func_hook.kwargs.pop("ignorebots", False)
        self.permissions = func_hook.kwargs.pop("permissions", [])
        self.single_thread = func_hook.kwargs.pop("singlethread", False)

        if func_hook.kwargs:
            # we should have popped all the args, so warn if there are any left
            logger.warning("Ignoring extra args {} from {}".format(func_hook.kwargs, self.description))

    @property
    def description(self):
        return "{}:{}".format(self.plugin.title, self.function_name)

    def __repr__(self):
        return "type: {}, plugin: {}, ignore_bots: {}, permissions: {}, single_thread: {}, threaded: {}".format(
            self.type.name, self.plugin.title, self.ignore_bots, self.permissions, self.single_thread, self.threaded
        )


class CommandHook(Hook):
    """
    :type name: str
    :type aliases: list[str]
    :type doc: str
    :type auto_help: bool
    """
    type = HookType.command

    def __init__(self, plugin, cmd_hook):
        """
        :type plugin: Plugin
        :type cmd_hook: cloudbot.util.hook._CommandHook
        """
        self.auto_help = cmd_hook.kwargs.pop("autohelp", True)

        self.name = cmd_hook.main_alias
        self.aliases = list(cmd_hook.aliases)  # turn the set into a list
        self.aliases.remove(self.name)
        self.aliases.insert(0, self.name)  # make sure the name, or 'main alias' is in position 0
        self.doc = cmd_hook.doc

        super().__init__(plugin, cmd_hook)

    def __repr__(self):
        return "Command[name: {}, aliases: {}, {}]".format(self.name, self.aliases[1:], Hook.__repr__(self))

    def __str__(self):
        return "command {} from {}".format("/".join(self.aliases), self.plugin.file_name)


class RegexHook(Hook):
    """
    :type regexes: set[re.__Regex]
    """
    type = HookType.regex

    def __init__(self, plugin, regex_hook):
        """
        :type plugin: Plugin
        :type regex_hook: cloudbot.util.hook._RegexHook
        """
        self.regexes = regex_hook.regexes

        super().__init__(plugin, regex_hook)

    def __repr__(self):
        return "Regex[regexes: [{}], {}]".format(", ".join(regex.pattern for regex in self.regexes),
                                                 Hook.__repr__(self))

    def __str__(self):
        return "regex {} from {}".format(self.function_name, self.plugin.file_name)


class RawHook(Hook):
    """
    :type triggers: set[str]
    """
    type = HookType.irc_raw

    def __init__(self, plugin, irc_raw_hook):
        """
        :type plugin: Plugin
        :type irc_raw_hook: cloudbot.util.hook._RawHook
        """
        super().__init__(plugin, irc_raw_hook)

        self.triggers = irc_raw_hook.triggers

    def is_catch_all(self):
        return "*" in self.triggers

    def __repr__(self):
        return "Raw[triggers: {}, {}]".format(list(self.triggers), Hook.__repr__(self))

    def __str__(self):
        return "irc raw {} ({}) from {}".format(self.function_name, ",".join(self.triggers), self.plugin.file_name)


class SieveHook(Hook):
    type = HookType.sieve

    def __init__(self, plugin, sieve_hook):
        """
        :type plugin: Plugin
        :type sieve_hook: cloudbot.util.hook._SieveHook
        """
        super().__init__(plugin, sieve_hook)

    def __repr__(self):
        return "Sieve[{}]".format(Hook.__repr__(self))

    def __str__(self):
        return "sieve {} from {}".format(self.function_name, self.plugin.file_name)


class EventHook(Hook):
    """
    :type types: set[cloudbot.event.EventType]
    """
    type = HookType.event

    def __init__(self, plugin, event_hook):
        """
        :type plugin: Plugin
        :type event_hook: cloudbot.util.hook._EventHook
        """
        super().__init__(plugin, event_hook)

        self.types = event_hook.types

    def __repr__(self):
        return "Event[types: {}, {}]".format(list(self.types), Hook.__repr__(self))

    def __str__(self):
        return "event {} ({}) from {}".format(self.function_name, ",".join(str(t) for t in self.types),
                                              self.plugin.file_name)


class OnloadHook(Hook):
    type = HookType.onload

    def __init__(self, plugin, on_load_hook):
        """
        :type plugin: Plugin
        :type on_load_hook: cloudbot.util.hook._OnLoadHook
        """
        super().__init__(plugin, on_load_hook)

    def __repr__(self):
        return "Onload[{}]".format(Hook.__repr__(self))

    def __str__(self):
        return "onload {} from {}".format(self.function_name, self.plugin.file_name)


_hook_type_to_plugin = {
    HookType.command: CommandHook,
    HookType.regex: RegexHook,
    HookType.irc_raw: RawHook,
    HookType.sieve: SieveHook,
    HookType.event: EventHook,
    HookType.onload: OnloadHook
}
