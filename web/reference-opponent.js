"use strict";

// Each game has its own bot and referee mirror.
class ReferenceOpponent {
  static async create(id) {
    if (location.protocol === "file:") {
      throw new Error("Open the hosted Play page or run bitbath serve to play this opponent.");
    }
    const bot = new ReferenceOpponent();
    try {
      await bot.request({ action: "load", id }, 90000);
      return bot;
    } catch (error) {
      await bot.stop();
      throw error;
    }
  }

  constructor() {
    this.worker = new Worker("reference-worker.js");
    this.clock = true;
    this.synced = 0;
    this.stderr = "";
  }

  request(message, timeout = 30000) {
    return new Promise((resolve, reject) => {
      const finish = (error, value) => {
        clearTimeout(timer);
        this.cancel = null;
        if (error) reject(error);
        else resolve(value);
      };
      const timer = setTimeout(() => {
        this.worker.terminate();
        finish(new Error("The opponent took too long to respond."));
      }, timeout);
      this.cancel = () => finish(new Error("Game closed."));
      this.worker.onerror = (event) => finish(new Error(event.message || "Could not load the opponent."));
      this.worker.onmessage = ({ data }) => {
        this.stderr += data.stderr || "";
        finish(data.error && new Error(data.error), data.orders);
      };
      this.worker.postMessage(message);
    });
  }

  init(lines, setup) {
    return this.request({ action: "init", lines, setup });
  }

  async turn(lines, send, script) {
    const orders = await this.request({ action: "turn", lines, steps: script.slice(this.synced) });
    for (const { line, reply } of orders) {
      const actual = send(line);
      if (actual === null) break;
      if (actual.join("\n") !== reply) throw new Error("The opponent and game engine disagree on an order.");
    }
    this.synced = script.length;
  }

  takeStderr() {
    const log = this.stderr;
    this.stderr = "";
    return log;
  }

  async stop() {
    this.worker.terminate();
    if (this.cancel) this.cancel();
  }
}
