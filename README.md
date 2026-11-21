# Mech Royale Python Bot

`strategy_bot.py` 是当前策略入口，`arena_geometry.py` 负责地形和建筑遮挡检测，`mechfight.py` 处理 `mechfight/1` 消息。`example_bot.py` 保留为官方基线。协议见 [protocol.md](protocol.md)。

本轮候选全程采用练习赛打法，优先发育、逐个接战和脱战回血。

导航在初始化时建立 4 米地面和屋顶通行图。地面边权包含坡度、窄路与死胡同惩罚，屋顶通过独立的起跳边和下楼边连接到地面。起跳前按当前机体尺寸检查净空，并要求预计耗能外还有 15 点余量。

交战与脱战分别用同一张图做局部加权 Dijkstra，范围 70 米，每次最多展开 720 个节点。路径代价叠加对手射程、蓄力激光、预计迫击炮落点与毒圈。落脚点按射程、遮挡和路线代价评分，对上一目标 8 米内给予粘滞加分。候选落脚点采用地图射线检查。预计算图保留可站立的低层屋顶；同一格存在重叠屋顶时只保留一个屋顶节点。

## 线上练习赛记录

分数由队伍提供。榜首参考值为 **5.58**，记录时的最后一次队伍得分为 **2.88**。

| 次序 | 提交 | 本轮改动 | 得分 | 较上一版 |
| --- | --- | --- | ---: | ---: |
| 1 | [`8de229c`](https://github.com/Yiyang-Jin/mech-royale/commit/8de229c17e8409b28b912e84978e30853b42e53f) | 初始策略 | 2.17 | — |
| 2 | [`407574c`](https://github.com/Yiyang-Jin/mech-royale/commit/407574c9e23b0a92dac211ed5066aedc7be238f7) | 按武器调整拉扯距离、掩体后停火回血 | 2.63 | +0.46 |
| 3 | [`b5d86b8`](https://github.com/Yiyang-Jin/mech-royale/commit/b5d86b812c8e1b6b5e29d12593b9ef6beed6d2c1) | 双武器协同、迫击炮落点与开火优先级 | 2.75 | +0.12 |
| 4 | [`517194a`](https://github.com/Yiyang-Jin/mech-royale/commit/517194a56a1b920204b548c30c0868a6edf0733b) | 屋顶脱战、横移换向、近身反击条件、主动发育和搜索航点 | 2.88 | +0.13 |

## 导航重写候选

基于 `65195a8`，重新实现路线选点、路径跟随和屋顶转移。爬升阶段先对准并刹停，脚底超过屋檐 0.72 米后开始横移。爬升受阻时取消路线并短期屏蔽该连接。工作区回放为静态协议录制流，比赛中的屋顶转移和成绩由线上评测验证。

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
