# Mech Royale Python Bot

`strategy_bot.py` 是当前策略入口，`arena_geometry.py` 负责地形和建筑遮挡检测，`mechfight.py` 处理 `mechfight/1` 消息。`example_bot.py` 保留为官方基线。协议见 [protocol.md](protocol.md)。

本轮候选全程采用练习赛打法，优先发育、逐个接战和脱战回血。

地面路线在初始化时预计算 4 米通行网格，直线路径受阻时使用 A* 绕行。近距离闪避与侧移使用即时碰撞检查。练习赛遭多方向追击或低血量时，可选择安全区内、相对起跳点高 6–22 米的遮挡屋顶。

## 线上练习赛记录

分数由队伍提供。榜首参考值为 **5.58**，记录时的最后一次队伍得分为 **2.88**。

| 次序 | 提交 | 本轮改动 | 得分 | 较上一版 |
| --- | --- | --- | ---: | ---: |
| 1 | [`8de229c`](https://github.com/Yiyang-Jin/mech-royale/commit/8de229c17e8409b28b912e84978e30853b42e53f) | 初始策略 | 2.17 | — |
| 2 | [`407574c`](https://github.com/Yiyang-Jin/mech-royale/commit/407574c9e23b0a92dac211ed5066aedc7be238f7) | 按武器调整拉扯距离、掩体后停火回血 | 2.63 | +0.46 |
| 3 | [`b5d86b8`](https://github.com/Yiyang-Jin/mech-royale/commit/b5d86b812c8e1b6b5e29d12593b9ef6beed6d2c1) | 双武器协同、迫击炮落点与开火优先级 | 2.75 | +0.12 |
| 4 | [`517194a`](https://github.com/Yiyang-Jin/mech-royale/commit/517194a56a1b920204b548c30c0868a6edf0733b) | 屋顶脱战、横移换向、近身反击条件、主动发育和搜索航点 | 2.88 | +0.13 |

## 本轮合并候选

从已测 2.88 分的 `517194a` 派生，四项改动共用一次练习赛评测。

- 锁定练习赛模式。
- 地面加速保留 65 能量，20 米内有敌人时允许消耗保留能量。脚底超过屋檐 0.5 米后开始横移。
- 预计算 4 米地面通行网格，用 A* 规划受阻路线。
- 激光蓄满或目标失去有效射线时松手。空槽搜索提高激光与闪电的拾取优先级。

## 本地检查

`strategy_bot.py` 顶部的 `PRACTICE` 可调整练习赛战术参数。

在项目根目录的 PowerShell 中运行官方录制回放检查：

```powershell
python -m unittest -v test_strategy
python check/stress.py
python check/check.py -- python -u strategy_bot.py
```

启动 Docker Desktop 的 Linux 容器引擎后，构建镜像并按赛事运行限制检查：

```powershell
docker build --platform linux/amd64 -t mech-royale-bot .
python check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m --memory-swap 512m --pids-limit 128 --cap-drop ALL --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 --volume "${PWD}/arena:/arena:ro" mech-royale-bot
docker run --rm --network none --cpus 1 --memory 512m --memory-swap 512m --pids-limit 128 --cap-drop ALL --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 --volume "${PWD}:/suite:ro" --entrypoint python3 mech-royale-bot /suite/check/stress.py
```

录制回放检查验证协议、响应时限和正常退出。压力测试在实地图上构造 15 台对手、30 枚弹体和 3 个空投，记录决策延迟和 Linux 进程峰值内存。实战成绩由赛事平台评估。提交时使用公开 GitHub 仓库，保留根目录的 `Dockerfile`，并在赛事页面填写仓库 URL。
