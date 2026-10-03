#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
秒杀并发压测脚本（只用 Python 标准库，不需要 pip install，Windows / macOS / Linux 都能跑）。

它做的事：用 N 个线程 + 栅栏（所有线程同时发车）打同一个秒杀接口，
把返回按"成功 / 库存不足 / 不能重复下单 / 未登录 / 其它"分类统计，
再算吞吐量和延迟分位，并把"怎么校验有没有超卖/重复下单"的命令直接打出来。

用法（在项目根目录执行，先把应用起起来）：
  # 200 个不同用户，同时抢 1 号券（一人一单 + 超卖 的主要场景）
  python tools/seckill-stress.py --voucher-id 1 --tokens tokens.txt --threads 200

  # 同一个用户并发打 50 次，验证"一人一单"在并发下只成功 1 次
  python tools/seckill-stress.py --voucher-id 1 --tokens one-token.txt --repeat 50 --threads 50

  # 走 nginx 全链路（默认直连 8081 应用）；注意 nginx 的接口前缀是 /api，不能省
  python tools/seckill-stress.py --voucher-id 1 --tokens tokens.txt --base-url http://localhost:8080/api

  # 换个接口也能用：500 并发打同一个店铺（测缓存击穿）
  python tools/seckill-stress.py --tokens tokens.txt --repeat 5 --threads 200 \
      --path /shop/1 --method GET

参数说明：
  --tokens      一行一个 token 的文件（tools/init-test-tokens.py 生成的，或你自己登录拿到的）
  --repeat      每个 token 发几次请求（默认 1；>1 就是"同一用户重复请求"，用于验证一人一单）
  --path/--method  默认 POST /voucher-order/seckill/<券id>，可换成任意接口（如 GET /shop/1、PUT /blog/like/1）
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from queue import Queue

CATEGORIES = ["SUCCESS", "OUT_OF_STOCK", "DUPLICATE_ORDER", "NOT_STARTED", "UNAUTHORIZED",
              "BAD_REQUEST", "SERVER_ERROR", "TRANSPORT_ERROR", "OTHER_FAIL"]

# 业务失败信息 -> 分类（对应 VoucherOrderServiceImpl 里的 Result.fail 文案）
MSG_MAP = {
    "库存不足": "OUT_OF_STOCK",
    "不能重复下单": "DUPLICATE_ORDER",
    "秒杀尚未开始": "NOT_STARTED",
    "秒杀已结束": "NOT_STARTED",
}


def load_tokens(path):
    if not os.path.exists(path):
        print("[ERROR] 找不到 token 文件：%s" % path)
        print("        先生成：python tools/init-test-tokens.py --count 200")
        sys.exit(2)
    tokens = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                tokens.append(line)
    if not tokens:
        print("[ERROR] token 文件是空的：%s" % path)
        sys.exit(2)
    return tokens


def do_request(url, token, timeout, method="POST"):
    """发一次请求，返回 (分类, 明细, 耗时秒)。"""
    body = b"" if method.upper() not in ("GET", "HEAD") else None
    req = urllib.request.Request(url, data=body, method=method.upper())
    req.add_header("authorization", token)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:            # 401 / 500 等
        status = e.code
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
    except Exception as e:                          # 连接失败 / 超时
        return "TRANSPORT_ERROR", "%s: %s" % (type(e).__name__, e), time.perf_counter() - t0

    cost = time.perf_counter() - t0

    if status == 401:
        return "UNAUTHORIZED", "HTTP 401 (token 不存在或已过期)", cost
    if status >= 500:
        return "SERVER_ERROR", "HTTP %d %s" % (status, body[:200]), cost
    if status != 200:
        return "BAD_REQUEST", "HTTP %d %s" % (status, body[:200]), cost

    try:
        result = json.loads(body)
    except Exception:
        return "OTHER_FAIL", "不是 JSON：%s" % body[:200], cost

    if result.get("success"):
        return "SUCCESS", "orderId=%s" % result.get("data"), cost

    msg = (result.get("errorMsg") or "").strip()
    return MSG_MAP.get(msg, "OTHER_FAIL"), msg or body[:200], cost


def worker(tasks, url, timeout, method, barrier, results, lock):
    barrier.wait()
    while True:
        try:
            token = tasks.get_nowait()
        except Exception:
            return
        try:
            cat, detail, cost = do_request(url, token, timeout, method)
        except Exception as e:                      # 兜底，别让线程静默死掉
            cat, detail, cost = "TRANSPORT_ERROR", "%s: %s" % (type(e).__name__, e), 0.0
        with lock:
            results.append((cat, detail, cost))


def percentile(sorted_values, q):
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, int(q * len(sorted_values)))
    return sorted_values[idx]


def main():
    parser = argparse.ArgumentParser(description="秒杀接口并发压测")
    parser.add_argument("--voucher-id", type=int, default=None, help="秒杀券 id（默认路径模式下必填，用来拼 URL 和打印校验命令）")
    parser.add_argument("--tokens", default="tokens.txt", help="token 文件，一行一个")
    parser.add_argument("--base-url", default="http://localhost:8081", help="默认直连应用；走 nginx 填 http://localhost:8080/api（/api 前缀不能省）")
    parser.add_argument("--path", default=None, help="接口路径，默认 /voucher-order/seckill/<券id>；可换成 /shop/1 等")
    parser.add_argument("--method", default="POST", help="请求方法，默认 POST（测 /shop/{id} 用 GET，测点赞用 PUT）")
    parser.add_argument("--threads", type=int, default=200, help="并发线程数（=同时发车的请求数）")
    parser.add_argument("--repeat", type=int, default=1, help="每个 token 发几次请求，默认 1")
    parser.add_argument("--timeout", type=float, default=15.0, help="单请求超时秒数")
    args = parser.parse_args()

    if args.path is None and args.voucher_id is None:
        print("[ERROR] 用默认秒杀路径时必须给 --voucher-id，例如 --voucher-id 1")
        print("        想压别的接口就加 --path（和 --method），例如 --path /shop/1 --method GET")
        sys.exit(2)

    tokens = load_tokens(args.tokens)
    path = args.path or "/voucher-order/seckill/%d" % args.voucher_id
    url = args.base_url.rstrip("/") + path

    tasks = Queue()
    if args.repeat <= 1:
        for t in tokens:                            # 每个用户各打一次
            tasks.put(t)
    else:
        for _ in range(args.repeat):                # 同样的 token 反复打
            for t in tokens:
                tasks.put(t)
    total = tasks.qsize()
    n_threads = max(1, min(args.threads, total))

    print("=" * 72)
    print("target     : %s %s" % (args.method.upper(), url))
    print("tokens     : %d 个（%s），每个发 %d 次" % (len(tokens), args.tokens, args.repeat))
    print("requests   : %d，并发线程 %d，栅栏同步同时发车" % (total, n_threads))
    print("=" * 72)
    print("发车中 ...")

    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(n_threads + 1)
    threads = [threading.Thread(target=worker, args=(tasks, url, args.timeout, args.method, barrier, results, lock),
                                daemon=True) for _ in range(n_threads)]
    for t in threads:
        t.start()

    # 主线程也等栅栏：所有 worker 就绪后一起释放，尽量让请求在同一瞬间打出去
    barrier.wait()
    wall_start = time.perf_counter()
    for t in threads:
        t.join()
    wall = time.perf_counter() - wall_start

    # ---------------- 统计 ----------------
    counter = {c: 0 for c in CATEGORIES}
    samples = {c: None for c in CATEGORIES}
    for cat, detail, _ in results:
        counter[cat] = counter.get(cat, 0) + 1
        if samples[cat] is None:
            samples[cat] = detail

    durations = sorted(cost for _, _, cost in results)
    n = len(results)
    ok = counter["SUCCESS"]

    print("")
    print("================= RESULT =================")
    print("requests     : %d" % n)
    print("wall time    : %.3f s" % wall)
    print("throughput   : %.1f req/s" % (n / wall if wall > 0 else 0.0))
    print("latency (ms) : avg %.1f | p50 %.1f | p95 %.1f | p99 %.1f | max %.1f"
          % (1000 * sum(durations) / n if n else 0,
             1000 * percentile(durations, 0.50), 1000 * percentile(durations, 0.95),
             1000 * percentile(durations, 0.99), 1000 * (durations[-1] if durations else 0)))
    print("-------------- 结果分类 -----------------")
    for cat in CATEGORIES:
        c = counter.get(cat, 0)
        if c or cat in ("SUCCESS", "OUT_OF_STOCK", "DUPLICATE_ORDER", "UNAUTHORIZED"):
            extra = "  e.g. %s" % samples[cat] if samples.get(cat) else ""
            print("%-16s %5d  %5.1f%%%s" % (cat, c, 100.0 * c / n if n else 0.0, extra))

    # ---------------- 怎么校验 ----------------
    vid = args.voucher_id
    is_seckill = args.path is None  # 用默认路径时才是秒杀接口，才打印券相关校验
    print("")
    if is_seckill:
        print("================= 接下来这样校验 =================")
        print("成功下单（Redis 侧通过 Lua 校验）的有 %d 次。等 10~20 秒让 RabbitMQ 消费完，再跑下面几条：" % ok)
        print("")
        print("  # 1) Redis 剩余库存  ==  初始库存 - 成功次数，且 >= 0（负数=超卖）")
        print("  docker exec -i hmdp-redis redis-cli GET seckill:stock:%d" % vid)
        print("")
        print("  # 2) Redis 已下单用户数  ==  成功次数（每个用户最多一个）")
        print("  docker exec -i hmdp-redis redis-cli SCARD seckill:order:%d" % vid)
        print("")
        print("  # 3) MySQL 订单数（最终一致后应等于成功次数）")
        print("  docker exec hmdp-mysql mysql -uroot -p123456 -N -B -e \"select count(*) from dingping.tb_voucher_order where voucher_id=%d;\"" % vid)
        print("")
        print("  # 4) MySQL 剩余库存（不能为负；与 Redis 库存最终一致）")
        print("  docker exec hmdp-mysql mysql -uroot -p123456 -N -B -e \"select stock from dingping.tb_seckill_voucher where voucher_id=%d;\"" % vid)
        print("")
        print("  # 5) 一人多单检查：必须返回空结果")
        print("  docker exec hmdp-mysql mysql -uroot -p123456 -N -B -e \"select user_id,count(*) c from dingping.tb_voucher_order where voucher_id=%d group by user_id having c>1;\"" % vid)
        print("")
        print("清理现场（下次重测前重置）：")
        print("  python tools/init-test-tokens.py --count %d --reset-voucher %d --stock 100 --reset-db" % (min(len(tokens), 200), vid))
    else:
        print("自定义路径模式（%s %s）：业务正确性请按该接口自己的语义校验，" % (args.method.upper(), path))
        print("比如 /shop/{id} 看日志里缓存是否只重建了一次，点赞接口看 DB 的 liked 与 Redis ZSet 是否只 +1。")

    bad = counter["TRANSPORT_ERROR"] + counter["SERVER_ERROR"] + counter["UNAUTHORIZED"]
    if bad:
        print("")
        print("[warn] 有 %d 个请求没拿到正常业务响应（TRANSPORT/SERVER/UNAUTHORIZED），先解决它，否则统计不可信。" % bad)
        if counter["UNAUTHORIZED"]:
            print("       401 说明 token 失效了：重新跑 tools/init-test-tokens.py，或确认 --tokens 文件是本次生成的。")
    if counter["NOT_STARTED"]:
        print("[warn] 有请求返回“秒杀尚未开始/已结束”：检查 tb_seckill_voucher 的 begin_time/end_time 是否包含当前时间。")
    if counter["OTHER_FAIL"] and samples.get("OTHER_FAIL"):
        print("[warn] 出现未分类的业务失败，样例：%s" % samples["OTHER_FAIL"])
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
