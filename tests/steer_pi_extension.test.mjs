import test from "node:test";
import assert from "node:assert/strict";
import { registerSteer } from "../gdd/steer_pi_extension.mjs";

function fakePi() {
  const handlers = {};
  const sent = [];
  return {
    handlers,
    sent,
    on(event, fn) {
      handlers[event] = fn;
    },
    async sendUserMessage(text, options) {
      sent.push({ text, options });
    },
  };
}

test("tool_result 每轮最多送一次，turn_start 后重置", async () => {
  const pi = fakePi();
  let drains = 0;
  registerSteer(pi, { drain: () => (++drains === 1 ? "补充一" : "补充二") });
  await pi.handlers.turn_start();
  await pi.handlers.tool_result({});
  await pi.handlers.tool_result({});
  assert.equal(pi.sent.length, 1, "同一轮第二次工具结果不再发送");
  assert.equal(pi.sent[0].text, "补充一");
  assert.deepEqual(pi.sent[0].options, { deliverAs: "steer" });

  await pi.handlers.turn_start();
  await pi.handlers.tool_result({});
  assert.equal(pi.sent.length, 2, "下一轮可再送");
  assert.equal(pi.sent[1].text, "补充二");
});

test("空队列不发送", async () => {
  const pi = fakePi();
  registerSteer(pi, { drain: () => "" });
  await pi.handlers.turn_start();
  await pi.handlers.tool_result({});
  assert.equal(pi.sent.length, 0);
});

test("发送失败后同一轮不会重复消费（sent 标记在发送前置位）", async () => {
  const pi = fakePi();
  let drains = 0;
  pi.sendUserMessage = async () => {
    throw new Error("boom");
  };
  registerSteer(pi, { drain: () => (++drains === 1 ? "x" : "y") });
  await pi.handlers.turn_start();
  await assert.rejects(() => pi.handlers.tool_result({}));
  await pi.handlers.tool_result({});
  assert.equal(drains, 1, "发送失败后本轮不再读取队列");
});
