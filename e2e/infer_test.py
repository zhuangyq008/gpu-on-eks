"""推理验收测试：在 vLLM 容器里运行，直接连本 Pod 的 127.0.0.1:8000（不经过 port-forward，结果才准确）。

    # 选一个已就绪的副本；脚本会并发 8 路请求，有真实流量时请在低峰期运行
    POD=$(kubectl -n qwen-vl get pod -l app=qwen-vl \
          -o jsonpath='{range .items[?(@.status.containerStatuses[0].ready==true)]}{.metadata.name}{"\n"}{end}' | head -1)
    kubectl -n qwen-vl exec -i "$POD" -c vllm -- python3 - < e2e/infer_test.py

1. 纯文本   2. 图片 → 结构化 JSON（模拟制造业铭牌/质检，校验全部 7 个字段）   3. 8 路并发，测 TTFT 和吞吐
任何一项失败时退出码为 1。
"""

import base64
import concurrent.futures
import io
import json
import sys
import time
import urllib.request

from PIL import Image, ImageDraw, ImageFont

URL = "http://127.0.0.1:8000/v1/chat/completions"
NO_THINK = {"enable_thinking": False}


def post(payload, stream=False):
    req = urllib.request.Request(URL, data=json.dumps({**payload, "stream": stream}).encode(),
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=300)


def nameplate_png():
    img = Image.new("RGB", (800, 480), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 40)
        small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 30)
    except OSError:
        font = small = ImageFont.load_default()
    d.rectangle([20, 20, 780, 460], outline="black", width=6)
    d.text((50, 50), "ACME INDUSTRIAL MOTOR", fill="black", font=font)
    d.text((50, 140), "MODEL: AX-7500", fill="black", font=small)
    d.text((50, 200), "SERIAL: SN-2026-11-0387", fill="black", font=small)
    d.text((50, 260), "POWER: 7.5 kW   380V 50Hz", fill="black", font=small)
    d.ellipse([560, 300, 740, 440], outline="green", width=8)
    d.text((598, 345), "PASS", fill="green", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def chat(messages, max_tokens=256):
    t0 = time.time()
    with post({"model": "qwen-vl", "messages": messages, "max_tokens": max_tokens, "temperature": 0,
               "chat_template_kwargs": NO_THINK}) as resp:
        body = json.load(resp)
    return time.time() - t0, body


def streamed(messages, max_tokens=128):
    """返回 (TTFT, 总耗时, 生成 token 数)"""
    t0 = time.time()
    ttft, tokens = None, 0
    payload = {"model": "qwen-vl", "messages": messages, "max_tokens": max_tokens, "temperature": 0,
               "chat_template_kwargs": NO_THINK, "stream_options": {"include_usage": True}}
    with post(payload, stream=True) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[5:])
            if chunk.get("choices") and chunk["choices"][0]["delta"].get("content") and ttft is None:
                ttft = time.time() - t0
            if chunk.get("usage"):
                tokens = chunk["usage"]["completion_tokens"]
    return ttft, time.time() - t0, tokens


print("== 1. 纯文本 ==")
secs, body = chat([{"role": "user", "content": "用一句话介绍你自己。"}], 64)
print(f"{secs:.2f}s  {body['usage']}\n  {body['choices'][0]['message']['content']!r}")

print("\n== 2. 图片 → JSON ==")
image = {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{nameplate_png()}"}}
prompt = ("识别这张设备铭牌，只输出 JSON，字段：manufacturer, model, serial, power_kw(数字), "
          "voltage, frequency, qc_result(PASS/FAIL)。")
secs, body = chat([{"role": "user", "content": [image, {"type": "text", "text": prompt}]}], 256)
answer = body["choices"][0]["message"]["content"]
print(f"{secs:.2f}s  {body['usage']}\n  {answer}")
failures = []
try:
    parsed = json.loads(answer.strip().removeprefix("```json").removesuffix("```").strip())
    checks = {
        "manufacturer": "ACME" in str(parsed.get("manufacturer", "")).upper(),
        "model": str(parsed.get("model", "")).upper() == "AX-7500",
        "serial": str(parsed.get("serial", "")).upper() == "SN-2026-11-0387",
        "power_kw": str(parsed.get("power_kw", "")).strip() in ("7.5", "7.50"),
        "voltage": "380" in str(parsed.get("voltage", "")),
        "frequency": "50" in str(parsed.get("frequency", "")),
        "qc_result": str(parsed.get("qc_result", "")).upper() == "PASS",
    }
    print("  校验：", checks)
    failures += [f"字段 {k} 不对" for k, ok in checks.items() if not ok]
except ValueError as exc:
    print("  不是合法 JSON：", exc)
    failures.append("图片 → JSON 的输出不是合法 JSON")

print("\n== 3. 8 路并发（每路 1 张图 + 128 token 输出）==")
messages = [{"role": "user", "content": [image, {"type": "text", "text": "描述这张图片的内容。"}]}]
t0 = time.time()
with concurrent.futures.ThreadPoolExecutor(8) as pool:
    results = list(pool.map(lambda _: streamed(messages), range(8)))
wall = time.time() - t0
ttfts = sorted(r[0] for r in results if r[0] is not None)
total_tokens = sum(r[2] for r in results)
print(f"  墙钟 {wall:.1f}s | TTFT p50 {ttfts[len(ttfts)//2]:.2f}s  max {ttfts[-1]:.2f}s | "
      f"单请求耗时 max {max(r[1] for r in results):.1f}s | 输出吞吐 {total_tokens / wall:.0f} tok/s")
if len(ttfts) < 8:
    failures.append(f"8 路并发里只有 {len(ttfts)} 路返回了内容")

print("\n结果：" + ("通过" if not failures else "失败：" + "；".join(failures)))
sys.exit(1 if failures else 0)
