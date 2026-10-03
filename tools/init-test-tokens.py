#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
并发压测前的数据准备脚本（只用 Python 标准库，Windows / macOS / Linux 都能跑）。

它做两件事：
  1) 直接往 Redis 写 N 个登录态 login:token:<token>（字段与 /user/login 写进去的完全一致：
     id / nickName / icon），并把 token 一行一个写到 tokens.txt 给压测脚本用。
     之所以能直接写：登录拦截器 RefreshTokenInterceptor 只认 Redis 里的这个 Hash，
     所以"压测用的登录态"不需要真的走一遍短信验证码。
  2) 可选：把某张秒杀券的库存 / 订单集合重置成干净状态（Redis + MySQL 一起重置）。

用法（在项目根目录执行）：
  # 看一眼要执行哪些命令，不连任何中间件（推荐先跑这个）
  python tools/init-test-tokens.py --count 200 --dry-run

  # 真的写：Redis 跑在 docker 里，默认就是这条命令
  python tools/init-test-tokens.py --count 200

  # 顺带把某张券重置成 100 库存（用户 1..200 抢 100 张，才能真正测出超卖/一人一单）
  python tools/init-test-tokens.py --count 200 --reset-voucher <券id> --stock 100 --reset-db
  # 注意：<券id> 必须是 tb_seckill_voucher 里真实存在的秒杀券 id（--reset-db 时会自动校验并警告）

  # Redis 是原生安装（不在 docker 里）时：
  python tools/init-test-tokens.py --count 200 --redis-cmd "redis-cli -h 127.0.0.1 -p 6379"
"""

import argparse
import os
import shlex
import subprocess
import sys
import uuid

DEFAULT_REDIS_CMD = "docker exec -i hmdp-redis redis-cli"
DEFAULT_MYSQL_CMD = "docker exec -i hmdp-mysql mysql -uroot -p123456 dingping"
# 与 UserServiceImpl.login 保持一致（拦截器每次请求会把它续期到 30 分钟）
DEFAULT_TTL = 3600


def run_piped(cmd, text, what):
    """把 text 通过 stdin 喂给 cmd（docker exec -i ... 用的就是这种方式）。"""
    argv = shlex.split(cmd, posix=(os.name != "nt"))
    print("[run] %s" % " ".join(argv))
    try:
        proc = subprocess.run(
            argv, input=text, text=True, capture_output=True, encoding="utf-8", errors="replace"
        )
    except FileNotFoundError:
        print("[ERROR] 找不到命令：%s" % argv[0])
        print("        没装 docker / redis-cli 的话，加 --dry-run 只生成命令，或者用 --redis-cmd 指定自己的命令。")
        sys.exit(2)
    if proc.stdout and proc.stdout.strip():
        # redis-cli / mysql 会回显结果，最多打 20 行，避免刷屏
        lines = proc.stdout.strip().splitlines()
        for line in lines[:20]:
            print("  <- %s" % line)
        if len(lines) > 20:
            print("  <- ...(还有 %d 行)" % (len(lines) - 20))
    if proc.returncode != 0:
        print("[ERROR] %s 执行失败（exit=%d）" % (what, proc.returncode))
        if proc.stderr:
            print(proc.stderr.strip()[:2000])
        sys.exit(3)
    print("[ok] %s 完成" % what)


def run_capture(cmd, text, what):
    """执行命令并返回 stdout（失败/命令不存在时返回 None，不退出）。"""
    argv = shlex.split(cmd, posix=(os.name != "nt"))
    try:
        proc = subprocess.run(
            argv, input=text, text=True, capture_output=True, encoding="utf-8", errors="replace"
        )
    except FileNotFoundError:
        print("[warn] 找不到命令 %s，跳过「%s」" % (argv[0], what))
        return None
    if proc.returncode != 0:
        print("[warn] %s 执行失败：%s" % (what, (proc.stderr or "").strip()[:300]))
        return None
    return proc.stdout or ""


def check_voucher_exists(mysql_cmd, vid):
    """
    校验这张券真的在 tb_seckill_voucher 里。
    最常见的坑：Redis 里有 seckill:stock:<id>（手动 SET 的"孤儿键"），
    但 MySQL 里根本没有这张秒杀券 —— 压测会全部返回「优惠券不存在」。
    """
    sql = "SELECT COUNT(*) FROM tb_seckill_voucher WHERE voucher_id = %d;\n" % vid
    out = run_capture(mysql_cmd, sql, "校验券是否存在")
    if out is None:
        return None                      # 查不了（没装 mysql 客户端等），不阻塞流程
    lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
    if not lines:
        return None
    if lines[-1] == "0":
        print("")
        print("!" * 72)
        print("[WARN] tb_seckill_voucher 里没有 voucher_id = %d 这张券！" % vid)
        print("       压测会全部返回「优惠券不存在」（checkTimeWindow 先查这张表，查不到直接 fail）。")
        print("       Redis 的 seckill:stock:%d 只是手写的孤儿键，不能说明券存在。" % vid)
        print("       先看清楚所有秒杀券的 id：")
        print('       docker exec hmdp-mysql mysql -uroot -p123456 -N -B -e "select v.id, v.title, v.type, s.stock, s.begin_time, s.end_time from dingping.tb_voucher v join dingping.tb_seckill_voucher s on s.voucher_id = v.id;"')
        print("!" * 72)
        return False
    print("[ok] 券 %d 存在于 tb_seckill_voucher" % vid)
    return True


def build_token_commands(tokens, user_ids, ttl):
    """生成 redis-cli 的批量命令：HSET 建 Hash + EXPIRE 设过期。"""
    commands = []
    for token, uid in zip(tokens, user_ids):
        key = "login:token:%s" % token
        commands.append("HSET %s id %d nickName u%d" % (key, uid, uid))
        commands.append("EXPIRE %s %d" % (key, ttl))
    return commands


def main():
    parser = argparse.ArgumentParser(description="生成压测用登录 token（直接写 Redis）")
    parser.add_argument("--count", type=int, default=200, help="生成多少个 token（=多少个不同用户）")
    parser.add_argument("--out", default="tokens.txt", help="token 输出文件，一行一个（默认 tokens.txt）")
    parser.add_argument("--user-ids", default="", help="逗号分隔的用户 id，默认 1..count（tb_user 里 id 1..1009 都存在）")
    parser.add_argument("--ttl", type=int, default=DEFAULT_TTL, help="token 过期秒数（默认 3600）")
    parser.add_argument("--redis-cmd", default=DEFAULT_REDIS_CMD, help="能执行 redis 命令的命令行，需支持从 stdin 读命令")
    parser.add_argument("--reset-voucher", type=int, default=None, help="重置这张秒杀券的 Redis 状态：seckill:stock:/seckill:order:")
    parser.add_argument("--stock", type=int, default=None, help="配合 --reset-voucher：把库存设成多少")
    parser.add_argument("--reset-db", action="store_true", help="配合 --reset-voucher：同时重置 MySQL 的库存和订单")
    parser.add_argument("--mysql-cmd", default=DEFAULT_MYSQL_CMD, help="能执行 SQL 的命令行，需支持从 stdin 读 SQL")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要执行的命令，不连中间件")
    args = parser.parse_args()

    if args.count <= 0:
        print("[ERROR] --count 必须大于 0")
        sys.exit(1)

    if args.user_ids:
        try:
            user_ids = [int(x) for x in args.user_ids.split(",") if x.strip()]
        except ValueError:
            print("[ERROR] --user-ids 只能写数字，例如 --user-ids 1,2,3")
            sys.exit(1)
        if len(user_ids) < args.count:
            print("[ERROR] --user-ids 只给了 %d 个，不够 --count %d 用" % (len(user_ids), args.count))
            sys.exit(1)
        user_ids = user_ids[: args.count]
    else:
        user_ids = list(range(1, args.count + 1))

    tokens = [uuid.uuid4().hex for _ in range(args.count)]

    # ---- 1. token ----
    cmds = build_token_commands(tokens, user_ids, args.ttl)
    print("=" * 72)
    print("准备 %d 个登录态：用户 %d..%d，token TTL %d 秒" % (args.count, user_ids[0], user_ids[-1], args.ttl))
    print("=" * 72)
    if args.dry_run:
        print("(dry-run) 将执行 %d 条 redis 命令，前 4 条：" % len(cmds))
        for line in cmds[:4]:
            print("  %s" % line)
    else:
        run_piped(args.redis_cmd, "\n".join(cmds) + "\n", "写入 Redis 登录态")

    # ---- 2. 券的库存 / 已下单集合重置 ----
    sql = None
    if args.reset_voucher is not None:
        vid = args.reset_voucher
        stock = args.stock if args.stock is not None else 100
        reset_cmds = [
            "DEL seckill:order:%d" % vid,
            "SET seckill:stock:%d %d" % (vid, stock),
        ]
        print("-" * 72)
        print("重置券 %d：Redis 库存 = %d，已下单用户集合清空" % (vid, stock))
        if args.dry_run:
            for line in reset_cmds:
                print("  %s" % line)
        else:
            run_piped(args.redis_cmd, "\n".join(reset_cmds) + "\n", "重置 Redis 券状态")

        sql = (
            "UPDATE tb_seckill_voucher SET stock = %d WHERE voucher_id = %d;\n"
            "DELETE FROM tb_voucher_order WHERE voucher_id = %d;\n"
            "SELECT voucher_id, stock, begin_time, end_time FROM tb_seckill_voucher WHERE voucher_id = %d;\n"
            % (stock, vid, vid, vid)
        )
        if args.reset_db and not args.dry_run:
            exists = check_voucher_exists(args.mysql_cmd, vid)
            if exists is False:
                print("[skip] 券不存在，跳过 MySQL 重置（UPDATE 会是 0 行，改了也没意义）")
            else:
                run_piped(args.mysql_cmd, sql, "重置 MySQL 券状态")
        else:
            print("  还要同步重置 MySQL（--reset-db 会自动执行，或自己复制到客户端跑）：")
            for line in sql.strip().splitlines():
                print("    %s" % line)

    # ---- 3. 写 tokens.txt ----
    if args.dry_run:
        print("-" * 72)
        print("(dry-run) 会把 %d 个 token 写到 %s" % (len(tokens), args.out))
    else:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("\n".join(tokens) + "\n")
        print("-" * 72)
        print("已写出 %d 个 token -> %s" % (len(tokens), os.path.abspath(args.out)))

    print("-" * 72)
    print("下一步：python tools/seckill-stress.py --voucher-id <券id> --tokens %s --threads %d"
          % (args.out, min(args.count, 200)))
    print("注意：token 会在 %d 秒后过期（压测中每个请求会自动续期到 30 分钟）。" % args.ttl)


if __name__ == "__main__":
    main()
