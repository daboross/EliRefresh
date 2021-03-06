import asyncio
from random import random
import socket

from cloudbot import hook

@asyncio.coroutine
@hook.command("josephus", "jose")
def josephus(text):
    """[size] [every x] [starting person] - calculates who dies last """
    split = text.split()
    if len(split) != 3:
        return "Not enough / too many arguments. {}".format(len(split))
    size, every_x, current = [int(x) for x in split]
    alive, till_kill = [True] * size, 0
    while True:
        if alive[current]:
            if sum(alive) == 1:
                break
            elif till_kill == 0:
                alive[current] = False
                till_kill = every_x - 1
            else:
                till_kill -= 1
        current += 1 if current < size - 1 else 1 - size
    return "Josephus should be at position {} to survive.".format(current)


@asyncio.coroutine
@hook.regex("(?i)(^ )*pets Eliralin *$")
def pet(action, nick):
    r = random()
    if r > 0.7:
        action("huggles {}".format(nick))


@asyncio.coroutine
@hook.command("hug", "huggle", autohelp=False)
def huggle(text, action, nick):
    """[user] - huggles [user]"""
    if text:
        action("huggles {}".format(text))
    else:
        action("huggles {}".format(nick))


@asyncio.coroutine
@hook.command(autohelp=False)
def colors(text):
    """- shows IRC colors"""
    if text:
        intinp = int(text)
        if intinp > 70:
            return "Please use a number smaller than or equal to 70"
        forrange = range(intinp)
    else:
        forrange = range(30)
    result = ""
    for i in forrange if text else range(30):
        result += "\x03{0:02d} {0}".format(i)
    return result


@asyncio.coroutine
@hook.command(permissions=["adminonly"])
def tree(text, message, notice):
    """[type] [text] - makes a tree"""
    type_input = text.split(None, 1)
    if len(type_input) < 2:
        notice("tree [type] [text] - Tree text")
        return
    tree_type = type_input[0]
    if tree_type == "1":
        func = lambda c: c[1:-1]
    elif tree_type == "2":
        func = lambda c: c[2:]
    elif tree_type == "3":
        func = lambda c: c[:-2]
    else:
        return "Invalid tree type '{}'.".format(tree_type)
    current = type_input[1]
    spaces = 7
    while len(current) > 0:
        spaces += 1
        message(spaces * ' ' + current)
        current = func(current)

    message((spaces - 1) * ' ' + ('----' if len(type_input[1]) % 2 == 0 else '---'))


@hook.command()
def dns(text):
    """<domain> - resolves the IP of <domain>"""
    try:
        socket.setdefaulttimeout(5)
        ip = None
        for info in socket.getaddrinfo(text, 80, 0, 0, socket.SOL_TCP):
            print(info)
            if ip is None:
                ip = info[-1][0]
            else:
                ip = "{}, {}".format(ip, info[-1][0])
        return "{} resolves to {}".format(text, ip)
    except socket.gaierror:
        return "Resolve Failed!"


@hook.command()
def rdns(text):
    """<ip> - Resolves the hostname of <ip>"""
    try:
        socket.setdefaulttimeout(5)
        domain = socket.gethostbyaddr(text)[0]
        return "{} resolves to {}".format(text, domain)
    except (socket.gaierror, socket.error):
        return "Resolve Failed!"
