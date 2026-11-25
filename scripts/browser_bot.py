"""Drive a tournament bot with the same protocol messages as the native runner.

The Play page's bundle (scripts/build_web_bots.py) holds this file, the bot
index, and the Python bots the page offers. A bot is found by its id in the
index: its first file is a module that defines one subclass of the kit's Bot.
"""

import importlib
import inspect
import json
from pathlib import Path
import time

import bitbath as bb


def bot_class(module):
    """The one subclass of the kit's Bot that a module defines."""
    found = [c for _, c in inspect.getmembers(module, inspect.isclass)
             if issubclass(c, bb.Bot) and c.__module__ == module.__name__]
    if len(found) != 1:
        raise ValueError(f"{module.__name__} should define one bot, not {len(found)}")
    return found[0]


def initialize(name, message, dispatch):
    global bot
    index = json.loads(Path(__file__).with_name("index.json").read_text(encoding="utf-8"))["bots"]
    bot = bot_class(importlib.import_module(Path(index[name]["files"][0]).stem))()

    def send(line):
        response = dispatch(line)
        if response is None or not isinstance(response, str):
            raise bb.GameOver()
        bb._read_line = iter(response.splitlines()).__next__

    bot._send = send
    bb._parse_init(bot, iter(message.splitlines()).__next__)
    bot.state = None
    bot.on_init()


def turn(message):
    lines = iter(message.splitlines())
    bot._turn_start = time.monotonic()
    bot.state = bb._parse_turn(bot, next(lines), lines.__next__)
    try:
        bot.on_turn(bot.state)
    except bb.GameOver:
        pass
