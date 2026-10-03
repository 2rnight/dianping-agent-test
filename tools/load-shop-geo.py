#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 MySQL 里的店铺坐标导入 Redis GEO（键：shop:geo:<typeId>），让"附近商铺"能查出数据。

为什么需要它：
  前端 shop-list.html 里 params 写死了 x/y（杭州西湖坐标），所以后端
  ShopServiceImpl#queryShopByType() 会走 Redis GEO 分支：从 shop:geo:<typeId> 里按
  半径 5000m 搜店铺。Redis 里没有这个键 → 返回空数组 → 页面一家店都看不到。
  项目自带的 HmDianPingApplicationTests#loadShopDate() 做的就是这个事，
  这个脚本是它的命令行版（不用开 IDEA，也不依赖 Java）。

用法（在项目根目录执行）：
  python tools/load-shop-geo.py --dry-run      # 只打印要执行的命令
  python tools/load-shop-geo.py                # 真的导入（Redis 跑在 docker 里）
  python tools/load-shop-geo.py --reset        # 先删掉旧的 shop:geo:* 再导入
  python tools/load-shop-geo.py --redis-cmd "redis-cli -h 127.0.0.1 -p 6379"

导完验证：
  docker exec -i hmdp-redis redis-cli ZCARD shop:geo:1     # 美食：应该是 9
  docker exec -i hmdp-redis redis-cli ZCARD shop:geo:2     # KTV ：应该是 5
然后刷新 http://localhost:8080/shop-list.html?type=1&name=美食
"""

import argparse
import os
import shlex
import subprocess
import sys

DEFAULT_REDIS_CMD = "docker exec -i hmdp-redis redis-cli"
DEFAULT_MYSQL_CMD = "docker exec -i hmdp-mysql mysql -uroot -p123456 -N -B dingping"
SHOP_GEO_KEY = "shop:geo:"


def run(cmd, text, what, quiet=False):
    argv = shlex.split(cmd, posix=(os.name != "nt"))
    if not quiet:
        print("[run] %s" % " ".join(argv))
    try:
        proc = subprocess.run(argv, input=text, text=True, capture_output=True,
                              encoding="utf-8", errors="replace")
    except FileNotFoundError:
        print("[ERROR] 找不到命令：%s" % argv[0])
        print("        没装 docker / mysql 客户端的话，加 --dry-run 只打印命令。")
        sys.exit(2)
    if proc.stdout and proc.stdout.strip() and not quiet:
        lines = proc.stdout.strip().splitlines()
        for line in lines[:20]:
            print("  <- %s" % line)
        if len(lines) > 20:
            print("  <- ...(还有 %d 行)" % (len(lines) - 20))
    if proc.returncode != 0:
        print("[ERROR] %s 失败（exit=%d）" % (what, proc.returncode))
        if proc.stderr:
            print(proc.stderr.strip()[:2000])
        sys.exit(3)
    return proc.stdout or ""


def main():
    parser = argparse.ArgumentParser(description="把店铺坐标导入 Redis GEO（shop:geo:<typeId>）")
    parser.add_argument("--redis-cmd", default=DEFAULT_REDIS_CMD, help="能执行 redis 命令的命令行（支持 stdin）")
    parser.add_argument("--mysql-cmd", default=DEFAULT_MYSQL_CMD, help="能执行 SQL 的命令行（支持 stdin，-N -B 输出制表符分隔）")
    parser.add_argument("--reset", action="store_true", help="导入前先 DEL 掉所有 shop:geo:*")
    parser.add_argument("--dry-run", action="store_true", help="只打印命令，不连中间件")
    args = parser.parse_args()

    # 1) 从 MySQL 读店铺：id / type_id / 经度x / 纬度y
    sql = "SELECT id, type_id, x, y FROM tb_shop ORDER BY type_id, id;\n"
    if args.dry_run:
        print("(dry-run) 会执行 SQL：%s" % sql.strip())
        rows = [(1, 1, 120.1492, 30.3161), (2, 1, 120.1515, 30.3334), (10, 2, 120.1491, 30.3247)]
        print("(dry-run) 用 3 条示例数据代替真实查询结果")
    else:
        print("=" * 72)
        print("读取 MySQL 店铺坐标 ...")
        out = run(args.mysql_cmd, sql, "查询 tb_shop")
        rows = []
        for line in out.strip().splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            try:
                rows.append((int(parts[0]), int(parts[1]), float(parts[2]), float(parts[3])))
            except ValueError:
                continue
    if not rows:
        print("[ERROR] 没读到任何店铺数据，先确认 tb_shop 有数据：")
        print('        docker exec hmdp-mysql mysql -uroot -p123456 -N -B -e "select count(*) from dingping.tb_shop;"')
        sys.exit(3)

    # 2) 按 type_id 分组，拼 GEOADD 命令（GEOADD key lon lat member [lon lat member ...]）
    by_type = {}
    for sid, tid, x, y in rows:
        by_type.setdefault(tid, []).append((sid, x, y))

    cmds = []
    if args.reset and not args.dry_run:
        cmds.append("DEL " + " ".join("%s%d" % (SHOP_GEO_KEY, t) for t in sorted(by_type)))
    for tid, items in sorted(by_type.items()):
        args_list = []
        for sid, x, y in items:
            args_list += ["%.6f" % x, "%.6f" % y, str(sid)]
        cmds.append("GEOADD %s%d %s" % (SHOP_GEO_KEY, tid, " ".join(args_list)))

    detail = ", ".join("%d(共%d家)" % (t, len(v)) for t, v in sorted(by_type.items()))
    print("=" * 72)
    print("共 %d 家店铺，分 %d 个类型：%s" % (len(rows), len(by_type), detail))
    print("=" * 72)
    if args.dry_run:
        for c in cmds:
            print("  %s" % c)
        print("(dry-run) 没有真的写 Redis")
        return
    run(args.redis_cmd, "\n".join(cmds) + "\n", "写入 Redis GEO")

    # 3) 回读验证
    verify = "\n".join("ZCARD %s%d" % (SHOP_GEO_KEY, t) for t in sorted(by_type)) + "\n"
    print("-" * 72)
    print("回读验证：")
    out = run(args.redis_cmd, verify, "ZCARD 校验", quiet=False)
    got = [l.strip() for l in out.strip().splitlines() if l.strip()]
    ok = True
    for (tid, items), g in zip(sorted(by_type.items()), got):
        mark = "ok" if g.isdigit() and int(g) == len(items) else "??"
        if mark == "??":
            ok = False
        print("  %s %s%d 期望 %d 家，实际 %s" % (mark, SHOP_GEO_KEY, tid, len(items), g))
    print("-" * 72)
    if ok:
        print("搞定！刷新页面看看：")
        for tid in sorted(by_type):
            print("  http://localhost:8080/shop-list.html?type=%d&name=<分类名>" % tid)
        print("注意：只有 tb_shop 里真实存在的分类才有店铺（种子数据只有 美食=1、KTV=2）。")
    else:
        print("[warn] 数量对不上，检查是不是有别的进程在改 shop:geo:* ")


if __name__ == "__main__":
    main()
