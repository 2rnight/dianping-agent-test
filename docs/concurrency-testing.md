# 并发测试指南（黑马点评 / hmdp）

> 本文回答一个问题：**这个项目怎么测并发，怎么判断测出来的结果是对的。**
> 配套两个脚本（都在 `tools/` 下，只用 Python 标准库，不用 pip install）：
> - `tools/init-test-tokens.py`：造压测用的登录态 token、重置券库存
> - `tools/seckill-stress.py`：多线程同时发车打秒杀接口，直接给出业务结果分类 + 校验命令

---

## 0. 先想清楚：并发要测哪些点，通过标准是什么

| # | 考点 | 怎么触发 | 通过标准 |
|---|---|---|---|
| 1 | **秒杀不超卖** | 200 个不同用户抢只有 10 张库存的券 | 成功次数 ≤ 10；Redis 库存 = 初始 − 成功数 且 ≥ 0；MySQL 库存 ≥ 0 |
| 2 | **一人一单（并发下）** | 同一个 token 并发打 50 次 | 只成功 1 次，其余全是「不能重复下单」；DB 里该 user_id 只有 1 单 |
| 3 | **异步下单最终一致** | 压完等 10~20 秒再查库 | MySQL 订单数 = 成功次数（RabbitMQ 消费完） |
| 4 | **缓存击穿** | 逻辑过期 + 500 并发打同一个 shopId | 缓存重建只发生 1 次（日志/DB 查询次数为 1） |
| 5 | **全局唯一 ID** | 300 线程 × 100 次 `RedisIdWorker.nextId` | 3 万个 ID 无重复 |
| 6 | **点赞一人一赞** | 同一 token 并发 `PUT /blog/like/{id}` | 预期 liked 只 +1；⚠️ 当前实现是 check-then-act，**这里大概率能测出 bug**，见 §8.4 |
| 7 | **签到不被重复计数** | 同一用户并发 `POST /user/sign` | 当天只记 1 次（bitmap 同一位反复置 1，天然幂等） |

> 最核心、面试也最爱问的是 **1 + 2 + 3**，下面主要围绕秒杀展开，其他考点在 §8。

---

## 1. 第 0 步：把环境跑起来

```bash
# 中间件（MySQL 3306 / Redis 6379 / RabbitMQ 25672+15672）
docker compose up -d
docker compose ps            # 三个都是 Up / healthy

# 应用：IDEA 里跑 HmDianPingApplication，或者命令行
mvn spring-boot:run          # 端口 8081
```

确认应用活着（这个接口不需要登录）：

```bash
curl http://localhost:8081/shop/1
```

**压测打哪个地址？**
- 先打 `http://localhost:8081`（直连 Tomcat，测应用本身）
- 再打 `http://localhost:8080`（走 nginx 全链路）—— nginx 监听 8080，**但接口要走 `/api` 前缀**：
  完整 URL 是 `http://localhost:8080/api/voucher-order/seckill/1`，nginx 会把 `/api` rewrite 掉再转给 8081。
  前缀 /api 一定不能少，否则会被当成静态资源请求（404）

---

## 2. 第 1 步：造一张秒杀券

秒杀接口要的是一张**秒杀券**（`tb_voucher.type = 1` + `tb_seckill_voucher` 里有库存和时间窗），
而且 `begin_time <= 当前时间 <= end_time`，否则所有请求都会返回「秒杀尚未开始 / 已结束」。

### 方式 A：直接写 SQL（推荐，可控）

```sql
-- 1) 建券（自动拿到新 id，不会覆盖已有的 1 号券）
INSERT INTO tb_voucher (shop_id, title, sub_title, rules, pay_value, actual_value, type, status, create_time, update_time)
VALUES (1, '并发压测券', '仅用于压测', '仅测试', 1, 100, 1, 1, NOW(), NOW());
SET @vid = LAST_INSERT_ID();

-- 2) 秒杀信息：库存 10，时间窗 = 昨天 ~ 7 天后
INSERT INTO tb_seckill_voucher (voucher_id, stock, create_time, begin_time, end_time, update_time)
VALUES (@vid, 10, NOW(), DATE_SUB(NOW(), INTERVAL 1 DAY), DATE_ADD(NOW(), INTERVAL 7 DAY), NOW());

-- 3) 记住这个 id，后面压测要用
SELECT @vid AS voucher_id;
```

⚠️ 建完**必须重启应用**，或者直接设 Redis 库存 —— 原因见 §6 的坑 3：
库存是应用启动时 `@PostConstruct preloadSeckillVouchers()` 用 `setIfAbsent` 预热的，
你改了 MySQL 的库存，Redis 里的 `seckill:stock:<id>` 不会自动跟着变。

```bash
# 直接设 Redis 库存（更省事）
docker exec -i hmdp-redis redis-cli SET seckill:stock:<券id> 10
```

### 方式 B：调用项目自己的接口（会自动写 Redis 库存）

`POST /voucher/seckill`（该路径不需要登录）：

```bash
curl -X POST http://localhost:8081/voucher/seckill \
  -H "Content-Type: application/json" \
  -d '{"shopId":1,"title":"并发压测券","subTitle":"测试","rules":"仅测试",
       "payValue":1,"actualValue":100,"type":1,"status":1,
       "stock":10,"beginTime":"2026-01-01T00:00:00","endTime":"2027-01-01T00:00:00"}'
```

返回体里的 `data` 就是新券 id。`VoucherServiceImpl.addSeckillVoucher()` 会顺手把库存写进 Redis，
所以这条路径不需要重启应用。

---

## 3. 第 2 步：造登录态（最容易卡住的一步）

秒杀接口 `POST /voucher-order/seckill/{id}` 需要登录：请求头带 `authorization: <token>`，
`RefreshTokenInterceptor` 拿 token 去 Redis 里读 `login:token:<token>` 这个 Hash。

### 方式 A：脚本直接写 Redis（推荐）

```bash
# 200 个用户（id 1..200），生成 200 个 token 到 tokens.txt
python tools/init-test-tokens.py --count 200

# 先看一眼会执行什么命令、不连中间件
python tools/init-test-tokens.py --count 200 --dry-run

# Redis 是原生安装（不在 docker 里）
python tools/init-test-tokens.py --count 200 --redis-cmd "redis-cli -h 127.0.0.1 -p 6379"
```

它写入的内容和 `/user/login` 写进去的完全一致（`id` / `nickName` / `icon` 三个字段 + TTL）：

```
HSET login:token:<uuid> id 1 nickName u1
EXPIRE login:token:<uuid> 3600
```

> 跳过短信验证码是**刻意**的：压测关心的是并发逻辑，不是登录流程。1000 个手机号走一遍
> `/user/code` + `/user/login` 又慢又容易被 2 分钟验证码 TTL 拖死。

### 方式 B：手工走一遍真实登录（想验证登录链路时用）

```bash
# 1) 发验证码
curl -X POST "http://localhost:8081/user/code?phone=13686869696"

# 2) 注意！接口不返回验证码了，要去 Redis 里捞（或看应用控制台日志「短信验证码发送成功：xxxxxx」）
docker exec -i hmdp-redis redis-cli GET login:code:13686869696

# 3) 登录，返回的 data 就是 token
curl -X POST http://localhost:8081/user/login \
  -H "Content-Type: application/json" \
  -d '{"phone":"13686869696","code":"<上一步捞到的6位数字>"}'
```

> ⚠️ 仓库里现成的 `src/test/java/com/hmdp/VoucherOrderControllerTest#login()`（"登录1000个用户"）
> **现在跑不通**：它假设 `/user/code` 会把验证码放在返回体里（`result.getData()`），
> 但 `UserServiceImpl.sendCode()` 现在是 `return Result.ok()`（验证码只写 Redis + 打日志），
> 于是 `result.getData().toString()` 会 NPE，最后卡在"token 数量 != 手机号数量"的断言上。
> 要用它的话，把拿验证码那一步改成从 Redis 读（注入 `StringRedisTemplate`）。或者直接用方式 A。

**token 有效期**：登录接口设 30 分钟，拦截器每次请求都会续期到 30 分钟。
脚本默认给 3600 秒，压测过程中不会掉线；但如果隔太久（超过 TTL）再打，会全部 401，重跑脚本即可。

---

## 4. 第 3 步：把券重置成干净状态（每次重测前必做）

不重置的话：Redis 里的 `seckill:stock:<id>` 还是上次扣完的库存（全返回"库存不足"），
`seckill:order:<id>` 集合里还留着上次那批用户（全返回"不能重复下单"）。

> ⚠️ **`<券id>` 必须是 `tb_seckill_voucher` 里真实存在的秒杀券**。加 `--reset-db` 时脚本会先查一次，
> 不存在就打印警告并跳过 MySQL 重置 —— 因为 Redis 的 `seckill:stock:<id>` 可以靠手动 `SET` 造出来
> （"孤儿键"），但接口里 `checkTimeWindow()` 会先查 `tb_seckill_voucher`，查不到直接返回
> **「优惠券不存在」**。仓库自带的 `hmdp.sql` 里 `tb_voucher` 只有 id=1 的**普通券**（`type=0`），
> `tb_seckill_voucher` 是空的，别拿 id=1 当秒杀券压。

```bash
# 重置成：Redis + MySQL 库存都是 10，订单清空
python tools/init-test-tokens.py --count 200 --reset-voucher <券id> --stock 10 --reset-db

# 没有 docker、想自己复制 SQL 跑（不加 --reset-db 时脚本会把 SQL 打出来）
python tools/init-test-tokens.py --count 200 --reset-voucher <券id> --stock 10 --dry-run
```

---

## 5. 第 4 步：发压

### 方式 A：`tools/seckill-stress.py`（推荐，能直接给出业务结果分类）

```bash
# 场景1：200 个不同用户，同时抢 10 张券 —— 测超卖 + 一人一单 + 最终一致
python tools/seckill-stress.py --voucher-id <券id> --tokens tokens.txt --threads 200

# 场景2：同一个用户并发 50 次 —— 专测"一人一单"的并发安全
# （Windows 没有 head 的话：python -c "open('one-token.txt','w').write(open('tokens.txt').readline())"）
head -1 tokens.txt > one-token.txt
python tools/seckill-stress.py --voucher-id <券id> --tokens one-token.txt --repeat 50 --threads 50

# 场景3：走 nginx 全链路
python tools/seckill-stress.py --voucher-id <券id> --tokens tokens.txt --threads 200 --base-url http://localhost:8080/api
```

脚本做的事：N 个线程用 `threading.Barrier` 一起发车（尽量让请求挤在同一瞬间），
然后按 6 种结果分类统计、算吞吐和延迟分位，最后把校验命令直接打出来。

输出格式如下（**数字是示例**，实际取决于你的机器和库存）：

```
================= RESULT =================
requests     : 200
wall time    : 0.412 s
throughput   : 485.4 req/s
latency (ms) : avg 61.2 | p50 44.8 | p95 152.3 | p99 208.7 | max 260.1
-------------- 结果分类 -----------------
SUCCESS             10   5.0%  e.g. orderId=1823012345678901248
OUT_OF_STOCK       190  95.0%  e.g. 库存不足
DUPLICATE_ORDER      0   0.0%
UNAUTHORIZED         0   0.0%
```

### 方式 B：JMeter（GUI，看吞吐/延迟更专业）

1. 下载 JMeter 5.6.3，双击 `bin/jmeter.bat`
2. 右键 Test Plan → Add → Threads (Users) → **Thread Group**
   - Number of Threads: `200`，Ramp-up: `0` 或 `1`，Loop Count: `1`
   - ⚠️ Ramp-up 设成 1 秒以内才是"并发"，设成 200 就是"每分钟 200 个请求"的慢跑
3. Thread Group → Add → Config Element → **CSV Data Set Config**
   - Filename: `tokens.txt`（一行一个 token，脚本生成的）
   - Variable Names: `token`，Recycle on EOF: `False`，Sharing mode: `All threads`
4. Thread Group → Add → Sampler → **HTTP Request**
   - Method: `POST`，Path: `/voucher-order/seckill/<券id>`，Server: `localhost`，Port: `8081`
   - 走 nginx 的话：Port 改 `8080`，Path 前面加 `/api`（`/api/voucher-order/seckill/<券id>`）
5. HTTP Request → Add → Config Element → **HTTP Header Manager**：`authorization` = `${token}`
6. Add → Listener → **聚合报告**（Average / 90% / 99% / Throughput / Error%）

> JMeter 判断"业务成功"要在响应体里做断言，比较麻烦。**分工建议**：
> JMeter 只看性能（吞吐量、P95、错误率），业务正确性（成功了几单、有没有超卖）
> 交给 `seckill-stress.py` 或下面的 SQL 统计。

### 方式 C：ab（简单粗暴，只测吞吐）

```bash
ab -n 1000 -c 200 -H "authorization: <一个有效token>" -p /dev/null -T application/json \
   http://localhost:8081/voucher-order/seckill/<券id>
```

同一个 token 反复打只会得到「不能重复下单」，所以 ab 只适合看**接口吞吐上限**，不能测业务正确性。

### 方式 D：IDEA 里写 JUnit 多线程（和仓库现有风格一致）

仓库里已有现成的基础设施可以参考：
- `HmDianPingApplicationTests#testIdWorker`：300 线程 × 100 次
- `VoucherOrderControllerTest`：1000 用户并发登录

骨架：

```java
int threads = 200;
ExecutorService es = Executors.newFixedThreadPool(threads);
CountDownLatch ready = new CountDownLatch(threads);   // 等所有线程就绪
CountDownLatch start = new CountDownLatch(1);         // 一起发车
List<Future<String>> futures = new ArrayList<>();   // 每个请求拿到的响应体 JSON
for (int i = 0; i < threads; i++) {
    final String token = tokens.get(i);
    futures.add(es.submit(() -> {
        ready.countDown();
        start.await();                                 // 卡在这里，等发令枪
        return mockMvc.perform(MockMvcRequestBuilders
                        .post("/voucher-order/seckill/" + voucherId)
                        .header("authorization", token))
                .andReturn().getResponse().getContentAsString();
    }));
}
ready.await();
start.countDown();                                     // 发令！200 个请求同时打出去
```

> 注意：`MockMvc` 是**模拟请求**，不走真实 Tomcat 线程池、不经过 nginx。
> 想测真实容器并发（Tomcat 线程池、连接数、nginx 转发），用方式 A/B/C 打 HTTP。

---

## 6. 第 5 步：判读结果（重点）

### 6.1 结果分类含义

| 分类 | 出现原因 | 说明 |
|---|---|---|
| `SUCCESS` | Lua 校验通过，返回订单号 | 订单是**异步**落库的，可能要过几秒才出现在 MySQL 里 |
| `OUT_OF_STOCK` | `Result.fail("库存不足")` | 库存扣完了，正常 |
| `DUPLICATE_ORDER` | `Result.fail("不能重复下单")` | 该用户在 `seckill:order:<id>` 集合里，正常 |
| `UNAUTHORIZED` | HTTP 401 | token 没写进 Redis / 已过期 —— **统计不可信，重跑第 2 步** |
| `OTHER_FAIL` | 其它 `errorMsg` | 常见：`秒杀尚未开始/已结束`（时间窗）、`秒杀失败`（Redis 库存键不存在）、`优惠券不存在` |
| `TRANSPORT_ERROR` / `SERVER_ERROR` | 连接失败 / 5xx | 应用挂了或连接打满，先解决 |

### 6.2 五条校验命令（等 10~20 秒让 RabbitMQ 消费完再查）

```bash
# 1) Redis 剩余库存：必须 == 初始库存 − 成功次数，且 >= 0（不会为负，Lua 里 stock<=0 就返回了）
docker exec -i hmdp-redis redis-cli GET seckill:stock:<券id>

# 2) Redis 已下单用户数：必须 == 成功次数（Lua 里 sismember + sadd 保证一人一次）
docker exec -i hmdp-redis redis-cli SCARD seckill:order:<券id>

# 3) MySQL 订单数：最终一致后必须 == 成功次数（≠ 说明消息还没消费完 / 消费出错了）
docker exec hmdp-mysql mysql -uroot -p123456 -N -B -e \
  "select count(*) from dingping.tb_voucher_order where voucher_id=<券id>;"

# 4) MySQL 剩余库存：不能为负（扣减语句带了 and stock > 0）
docker exec hmdp-mysql mysql -uroot -p123456 -N -B -e \
  "select stock from dingping.tb_seckill_voucher where voucher_id=<券id>;"

# 5) 一人多单检查：必须返回空结果
docker exec hmdp-mysql mysql -uroot -p123456 -N -B -e \
  "select user_id,count(*) c from dingping.tb_voucher_order where voucher_id=<券id> group by user_id having c>1;"
```

> 说明：脚本默认按 `docker exec` 的容器名（`hmdp-redis` / `hmdp-mysql`）+ 库名 `dingping` 打印命令，
> 容器名、库名、密码跟你本地不一致的话，改脚本里的 `--redis-cmd` / `--mysql-cmd` 或直接改这几条命令。
> MySQL 用 Docker 时注意：`docker exec` 里连的是容器内 3306，跟宿主机映射端口无关。

### 6.3 一致性怎么看

- **看超卖**：订单数 > 初始库存 ⇒ 超卖。这是最有说服力的一条（Redis 库存和 MySQL 库存都要看）
- **看一人一单**：第 5 条 SQL 有结果 ⇒ 一人多单
- **看最终一致**：订单数 < 成功次数 时，先去 RabbitMQ 后台 <http://localhost:15672>（guest/guest）
  看 Queues：`QA` 有积压说明消费跟不上（消费者 `concurrency: 5`、`prefetch 10`）；
  消息在 QA 里超过 10 秒会进死信队列 `QD`，`QD` 也有消费者兜底，靠订单号幂等去重
- **看缓存击穿**：开 debug 日志（`logging.level.com.hmdp: debug`），重建缓存时只应出现一次
- **看 DB 压力**：`VoucherOrderServiceImpl.checkTimeWindow()` 每个请求都会 `getById(voucherId)` 查一次 MySQL，
  所以秒杀接口的 DB QPS ≈ 请求 QPS（这本身也是个可优化点：券信息完全可以缓存）。
  压测时连 MySQL 一起观察，高并发下这里先扛不住

### 6.4 怎么确认这套压测"确实有鉴别力"

跑通不算数，要确认它**真能发现错误**：

1. 同一 token 并发 200 次 → 必须只 1 单。出现多单 = 一人一单有并发漏洞
2. 库存设成 0 再压 → 必须全部「库存不足」。出现 SUCCESS = 库存判断有漏洞
3. 把券的 `end_time` 改成昨天 → 必须全部「秒杀已结束」。若还有 SUCCESS = 时间窗判断没生效
4. 把 MySQL 库存改成 5、Redis 库存也改 5，200 个用户压 → 成功数必须**正好 5**

---

## 7. 常见坑（踩过一次就记住了）

1. **401 一片**：token 没写进 Redis / 过期了 / 忘带 `authorization` 头 → 重跑 `init-test-tokens.py`
2. **全「库存不足」**：上次压测把 Redis 库存扣光了，没重置 → `--reset-voucher <id> --stock N`
3. **全「秒杀失败」**：`seckill:stock:<id>` 键根本不存在（Lua 里 `stock == nil` 返回 -1）
   → 因为库存预热的 `@PostConstruct` 用的是 `setIfAbsent`（只补不覆盖），
   你从 SQL 新建券/改库存后，要么重启应用，要么手动 `SET seckill:stock:<id> <库存>`
4. **全「秒杀尚未开始 / 已结束」**：`begin_time/end_time` 没把当前时间包进去（最常见的翻车点）
5. **全「不能重复下单」**：`seckill:order:<id>` 里有上次的用户；或 token 数量 < 并发线程数，
   同一批 token 被复用
6. **并发线程数 ≠ 真实并发**：Tomcat 默认 `server.tomcat.threads.max=200`，
   线程数开 500 只会排队（表现为延迟飙升、吞吐不变），要测更高得先调大这个值
7. **压完立刻查库对不上**：下单是异步的（Lua 只做预检 + 发 MQ），等 10~20 秒再查
8. **别在压测中途重启应用**：`@PostConstruct` 会用 `setIfAbsent` 把「数据库库存」当基准，
   可能把已经扣过的 Redis 库存重置（只在键不存在时）
9. **PowerShell 与 `docker exec -i` 的 stdin 重定向**：PowerShell 不支持 `<`，
   要 `cmd /c "docker exec -i ... < file"`，或者干脆用本文的 Python 脚本（脚本内部走管道，跨平台）
10. **压测数据会残留**：订单、券库存、Redis 键都是真实数据，测完用 §4 的命令清一遍；
    别在生产/别人的库上压

---

## 8. 其他并发考点怎么测

### 8.1 缓存击穿（逻辑过期 + 互斥重建）

`ShopServiceImpl.queryById` 走的是 `CacheClient.queryWithLogicalExpire`（逻辑过期 + 抢锁后异步重建）。

```bash
# 1) 把 1 号店铺写进缓存，并把逻辑过期时间设成 0 秒（一写进去就是"逻辑过期"状态）
#    现成的 HmDianPingApplicationTests#testSaveShop 写的是 30 分钟逻辑过期，
#    改成 ShopServiceImpl#saveShop2Redis(1L, 0L) 才会立刻进入"该重建了"的状态

# 2) 500 并发打同一个店铺（GET，且覆盖默认路径）
python tools/seckill-stress.py --tokens tokens.txt --repeat 5 --threads 200 \
  --path /shop/1 --method GET --base-url http://localhost:8081
```

`/shop/**` 不需要登录，`--path` 覆盖默认路径、`--method GET` 覆盖默认 POST 就能复用这个脚本
（此时 `--voucher-id` 可以不传，脚本也不会再打印秒杀那套校验命令）。
**通过标准**：日志里"重建缓存"只出现 1 次；数据库只被查了 1 次（其它线程拿的是旧数据 / 等锁后读新缓存）。

> 顺便说一句：仓库里 `CacheClient.queryWithPassThrough`（缓存穿透 + 空值缓存）目前**没有任何地方调用**，
> 店铺走的是逻辑过期这条路径。想测"缓存穿透"（500 并发打不存在的 id，DB 只被查 1 次），
> 得先把 `queryById` 换成 pass-through 版本。

### 8.2 一人一单的两种实现对比

项目里有两套"一人一单"的写法：Redis 分布式锁（`SimpleRedisLock` / Redisson）和现在的 Lua 原子脚本。
想复现"没有原子性会怎样"（负向验证，跑完记得改回来）：把 `VoucherOrderServiceImpl` 里的 Lua 调用
换成"`GET` 判库存 → `DECRBY` 扣减"的两步写法，并去掉 `sadd` 那一人一单的判断，再用同一批 token 并发压 ——
你会看到成功次数超过库存（超卖）、同一用户出现多单，正好和 Lua 版本的干净结果形成对比。

### 8.3 全局唯一 ID（`RedisIdWorker`）

`HmDianPingApplicationTests#testIdWorker` 已经是 300 线程 × 100 次，但在 `System.out` 打印。
改成把 3 万个 id 收进 `ConcurrentHashMap.newKeySet()` 或 `Collections.synchronizedSet`，
最后断言 `set.size() == 30000`，才是真正在测"不重复"。

### 8.4 点赞 / 签到（一个能测出问题，一个是天然安全的）

```bash
# 点赞：同一个用户并发 200 次，看 liked 会不会被 + 多次
python tools/seckill-stress.py --tokens one-token.txt --repeat 200 --threads 200 \
  --path /blog/like/<blogId> --method PUT --base-url http://localhost:8081
```

（点赞接口需要登录，所以 token 必须；`/blog/like/{id}` 是 PUT 方法，别忘 `--method PUT`。）

- **正确预期**：同一个用户点 200 次，`tb_blog.liked` 只 +1，`blog:liked:<id>` 里只有 1 条
- **实测大概率翻车**：`BlogServiceImpl.updateLike` 是"先查 ZSet 有没有点过 → 再 `liked=liked+1` → 再 `ZADD`"
  的 check-then-act 写法，**没有加锁、也不是原子操作**。并发下多个线程会同时看到 `score == null`，
  于是 `liked` 被 + 了很多次（`ZADD` 本身幂等，所以 ZSet 里还是 1 条）。
  这正好是一个可以拿去面试/写进简历复盘的并发问题：修法是用 Redisson 锁，或者把"判断 + 计数"做成原子操作
  （Redis Lua / 数据库条件更新 `update tb_blog set liked = liked + 1 where id = ? and ...`）。

```bash
# 验证
docker exec hmdp-mysql mysql -uroot -p123456 -N -B -e "select liked from dingping.tb_blog where id=<blogId>;"
docker exec -i hmdp-redis redis-cli ZCARD blog:liked:<blogId>     # 应该是 1
```

- **签到**（`POST /user/sign`）用 `SETBIT key offset 1` 写 bitmap，同一个 bit 反复置 1 天然幂等，
  并发打 100 次也只是签到 1 天：`GET /user/sign/count` 返回 1 即正确。

---

## 9. 速查表（照抄即可）

```bash
# ① 环境
docker compose up -d && mvn spring-boot:run

# ② 造券（SQL）后记住 id，并设 Redis 库存
docker exec -i hmdp-redis redis-cli SET seckill:stock:<券id> 10

# ③ 造 200 个登录态 + 重置券状态
python tools/init-test-tokens.py --count 200 --reset-voucher <券id> --stock 10 --reset-db

# ④ 200 并发抢 10 张券
python tools/seckill-stress.py --voucher-id <券id> --tokens tokens.txt --threads 200

# ⑤ 等 10~20 秒，跑脚本打印出来的 5 条校验命令
```

**通过标准一句话**：成功次数 ≤ 初始库存、Redis 库存 = 初始 − 成功次数、订单数 = 成功次数、无 user_id 重复、无 401/5xx。

---

## 附：造的券在前端不显示？（排错）

前端的券列表只在**店铺详情页**（`shop-detail.html?id=<shopId>`）展示，首页不展示。
它调用 `GET /api/voucher/list/<shopId>`，最终执行的是 `VoucherMapper.xml` 里这条 SQL：

```sql
SELECT ... FROM tb_voucher v
LEFT JOIN tb_seckill_voucher sv ON v.id = sv.voucher_id
WHERE v.shop_id = #{shopId} AND v.status = 1
```

所以"能显示"要同时满足 4 个条件：

| 条件 | 不满足时的现象 | 怎么查 |
|---|---|---|
| `tb_voucher.shop_id` = 你打开的那个店铺 id，且该店铺真实存在（种子数据只有店铺 **1~14**） | 任何店铺页都不显示 | 下面的诊断 SQL |
| `tb_voucher.status = 1` | 不显示 | 同上 |
| `tb_seckill_voucher.end_time > now()`（前端 `v-if="!isEnd(v)"` 直接隐藏卡片） | 不显示 | 同上 |
| 打开的是店铺详情页、且经过 nginx（`localhost:8080`，baseURL=`/api`） | 页面空白/报错 | 直接 curl `/api/voucher/list/<shopId>` |

```sql
-- 一条 SQL 看清全部条件
SELECT v.id, v.shop_id, v.status, v.type, v.title,
       sv.stock, sv.begin_time, sv.end_time,
       (sv.end_time > NOW()) AS not_ended,
       s.id AS shop_exists
FROM tb_voucher v
LEFT JOIN tb_seckill_voucher sv ON sv.voucher_id = v.id
LEFT JOIN tb_shop s ON s.id = v.shop_id
WHERE v.id = <券id>;
```

对照着修：

```sql
UPDATE tb_voucher SET shop_id = 1, status = 1 WHERE id = <券id>;   -- 挂到 1 号店铺并上架
UPDATE tb_seckill_voucher SET end_time = DATE_ADD(NOW(), INTERVAL 7 DAY) WHERE voucher_id = <券id>;
```

改完**不用重启应用**（券列表没走缓存），直接刷新 `http://localhost:8080/shop-detail.html?id=1`。

⚠️ 两个容易搞混的点：
1. 页面上"剩余 X 张"读的是 **MySQL 的 `sv.stock`**，秒杀扣减用的是 **Redis 的 `seckill:stock:<id>`**。
   两边不一致时会出现"页面显示剩余很多、点抢购却说库存不足"（或反过来）。压测重置时两个都要重置。
2. 库存为 0 **不会让卡片消失**，只是按钮变灰并提示"库存不足，请刷新再试试"；卡片真的不见，
   基本就是上面 4 个条件之一。

---

## 附：店铺列表查不出数据？（附近商铺 / Redis GEO）

**现象**：打开 `shop-list.html?type=1&name=美食`，一家店都没有。

**原因链**（三处代码连起来看就明白了）：

1. 前端 `shop-list.html` 的 `params` 里**写死了坐标**：
   ```js
   x: 120.149993, // 经度
   y: 30.334229   // 纬度
   ```
   所以每次请求都带 x/y：`GET /api/shop/of/type?typeId=1&current=1&sortBy=&x=120.149993&y=30.334229`
2. 后端 `ShopServiceImpl#queryShopByType()` 一看 x/y 都不为 null，**就走 Redis GEO 分支**，
   不再走数据库分页：从 `shop:geo:<typeId>` 里按 **半径 5000m** 搜店铺：
   ```java
   String key = SHOP_GEO_KEY + typeId;              // shop:geo:1
   stringRedisTemplate.opsForGeo().search(key, GeoReference.fromCoordinate(x, y), new Distance(5000), ...);
   ```
3. `shop:geo:*` 这些键**不会自动生成**，必须手动导入（项目自带的
   `HmDianPingApplicationTests#loadShopDate()` 就是干这个的）。没导过 → 搜出来是空数组 → 页面空白。

**验证**（应该是空/nil，那就对上了）：

```cmd
docker exec -i hmdp-redis redis-cli KEYS "shop:geo:*"
docker exec -i hmdp-redis redis-cli ZCARD shop:geo:1
```

**导入坐标，二选一**：

```cmd
:: 方式 A：命令行脚本（推荐）
python tools/load-shop-geo.py --dry-run     :: 先看要执行什么
python tools/load-shop-geo.py               :: 真导入，完了会自动 ZCARD 回读校验

:: 方式 B：IDEA 里跑 JUnit 测试
::   HmDianPingApplicationTests#loadShopDate()
```

导入后应该是：`shop:geo:1` = **9 家**（美食），`shop:geo:2` = **5 家**（KTV）。
刷新 `http://localhost:8080/shop-list.html?type=1&name=美食` 就能看到店铺，并且列表里带距离。

**另外两个必须知道的坑**：

1. **只有 2 个分类有店铺**。种子数据里 `tb_shop_type` 有 10 个分类，但 `tb_shop` 只有
   `type_id = 1`（美食，9 家）和 `type_id = 2`（KTV，5 家）的数据。
   点"丽人·美发""健身运动"等另外 8 个分类，**本来就查不到店铺**，不是 bug。
2. **GEO 分支有 5km 半径限制**，前端写死的坐标是杭州西湖附近，14 家种子店铺都在 5km 内
   （最远的 2816m），所以能全部显示；但你自己往 `tb_shop` 里加的店铺如果坐标离得远，
   即使导入了 GEO 也不会出现在列表里 —— 那种情况要在前端把 x/y 去掉（走数据库分页），
   或者把 `queryShopByType` 里的 `new Distance(5000)` 调大。

> 顺带一提：列表页顶部"距离 / 人气 / 评分"三个排序按钮里的 `sortBy` 参数**后端并没有接收**
> （`queryShopByType` 的形参只有 typeId/current/x/y），所以点了不会真的排序 —— 知道就行，
> 想实现的话是在 SQL/Redis 那层加 order by。
