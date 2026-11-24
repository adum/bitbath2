"""Drive a tournament bot with the same protocol messages as the native runner."""

import time

import bitbath as bb
from bastion_bot import Bastion
from naval_bot import NavalBot
from smart_bot import SmartBot
from vanguard_bot import VanguardBot


def initialize(name, message, dispatch):
    global bot
    bot = {"bastion": Bastion, "smart": SmartBot, "vanguard": VanguardBot, "naval": NavalBot}[name]()

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
