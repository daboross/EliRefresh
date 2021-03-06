import asyncio
import re

from sqlalchemy import Table, Column, String, UniqueConstraint

from cloudbot import hook
from cloudbot.events import CommandEvent
from cloudbot.util import formatting, botvars


table = Table(
    "prefixes",
    botvars.metadata,
    Column("connection", String),
    Column("channel", String),
    Column("prefix", String),
    UniqueConstraint("connection", "channel", "prefix")
)


def _load_db_query(db):
    query = db.execute(table.select())
    return [(row["connection"], row["channel"], row["prefix"]) for row in query]


@asyncio.coroutine
@hook.onload()
def load_command_re(async, db):
    """
    :type db: sqlalchemy.orm.Session
    """
    global chan_re
    channel_prefixes = {}
    for connection, channel, prefix in (yield from async(_load_db_query, db)):
        key = (connection, channel)
        if key in channel_prefixes:
            channel_prefixes[key].append(re.escape(prefix))
        else:
            channel_prefixes[key] = [re.escape(prefix)]

    chan_re = {}
    for key, _prefixes in channel_prefixes.items():
        command_re = r"([{}])(\w+)(?:$|\s+)(.*)".format("".join(_prefixes))
        chan_re[key] = re.compile(command_re)


@asyncio.coroutine
@hook.command(permissions=["botcontrol"])
def addprefix(text, conn, chan, async, db, logger):
    """<prefix> - adds a command prefix <prefix> to the current channel
    :type text: str
    :type conn: cloudbot.connection.Connection
    :type chan: str
    :type db: sqlalchemy.orm.Session
    """
    logger.info("Adding prefix {} to {}".format(text, chan))
    yield from async(db.execute, table.insert().values(connection=conn.name, channel=chan, prefix=text))
    yield from async(db.commit)
    yield from load_command_re(async, db)
    return "Added command prefix {} to {}".format(text, chan)


@asyncio.coroutine
@hook.command(permissions=["botcontrol"])
def delprefix(text, conn, chan, async, db, logger):
    """<prefix> - removes command prefix <prefix> from the current channel
    :type text: str
    :type conn: cloudbot.connection.Connection
    :type chan: str
    :type db: sqlalchemy.orm.Session
    """
    logger.info("Removing prefix {} from {}".format(text, chan))
    yield from async(db.execute, table.delete().where(table.c.connection == conn.name)
                     .where(table.c.channel == chan).where(table.c.prefix == text))
    yield from async(db.commit)
    yield from load_command_re(async, db)
    return "Removed command prefix {} from {}".format(text, chan)


def _list_prefix_query(db, conn, chan):
    return [row["prefix"] for row in
            db.execute(table.select().where(table.c.connection == conn).where(table.c.channel == chan))]


@asyncio.coroutine
@hook.command(permissions=["botcontrol"], autohelp=False)
def prefixes(text, conn, chan, async, db):
    """[channel] - shows prefixes for [channel], or the caller's channel if no channel is specified
    :type text: str
    :type conn: cloudbot.connection.Connection
    :type chan: str
    :type db: sqlalchemy.orm.Session
    """
    if text:
        if not text.startswith("#"):
            chan = "#" + text
        else:
            chan = text

    _prefixes = [conn.config.get('command_prefix', '.')] + (yield from async(_list_prefix_query, db, conn.name, chan))
    return "Prefixes for {}: {}".format(chan, ", ".join(_prefixes))


@asyncio.coroutine
@hook.irc_raw("PRIVMSG")
def run_extra_prefix(event, bot, conn, chan, content):
    """
    :type event: cloudbot.events.BaseEvent
    :type bot: cloudbot.bot.CloudBot
    :type conn: cloudbot.connection.Connection
    :type chan: str
    :type content: str
    """
    key = (conn.name, chan)
    if key in chan_re:
        match = chan_re[key].match(content)
        if match:
            command = match.group(2).lower()
            if command in bot.plugin_manager.commands:
                command_hook = bot.plugin_manager.commands[command]
                event = CommandEvent(triggered_command=command, hook=command_hook, text=match.group(3).strip(),
                                     base_event=event)
                yield from bot.plugin_manager.launch(command_hook, event)
            else:
                potential_matches = []
                for potential_match, plugin in bot.plugin_manager.commands.items():
                    if potential_match.startswith(command):
                        potential_matches.append((potential_match, plugin))
                if potential_matches:
                    if len(potential_matches) == 1:
                        command_hook = potential_matches[0][1]
                        event = CommandEvent(triggered_command=command, hook=command_hook, text=match.group(3).strip(),
                                             base_event=event)
                        yield from bot.plugin_manager.launch(command_hook, event)
                    else:
                        event.notice("Possible matches: {}".format(
                            formatting.get_text_list([command for command, plugin in potential_matches])))
