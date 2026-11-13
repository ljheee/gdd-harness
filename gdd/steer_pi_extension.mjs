/**
 * GDD working 补充（steer）的 pi 扩展。
 *
 * 队列的读写与加锁仍由 Python 侧 gdd/steer.py 负责（与 `gdd steer` 命令行同一份队列），
 * 扩展在每次工具结果后调用 `python gdd/steer.py take <root>` 取出补充，
 * 再用 pi.sendUserMessage(..., {deliverAs:"steer"}) 送入同一会话。
 *
 * 约束：同一轮只送一次（turn_start 重置）；门禁会话不加载本扩展；
 * 本轮没有工具调用时由 orchestrator 的 resume 兜底补送。
 */
import { spawnSync } from "node:child_process";

function defaultDrain() {
  const root = process.env.GDD_STEER_ROOT || "";
  const script = process.env.GDD_STEER_SCRIPT || "";
  const python = process.env.GDD_STEER_PY || "python3";
  if (!root || !script) return "";
  const res = spawnSync(python, [script, "take", root], {
    encoding: "utf8",
    timeout: 10000,
    env: process.env,
  });
  if (res.error || res.status !== 0) return "";
  return (res.stdout || "").trim();
}

export function registerSteer(pi, { drain } = {}) {
  const readQueue = typeof drain === "function" ? drain : defaultDrain;
  let sentThisTurn = false;

  pi.on("turn_start", () => {
    sentThisTurn = false;
  });

  pi.on("tool_result", async () => {
    if (sentThisTurn) return;
    const text = readQueue();
    if (!text) return;
    sentThisTurn = true;
    await pi.sendUserMessage(text, { deliverAs: "steer" });
  });
}

export default function (pi) {
  registerSteer(pi);
}
