# Mech Royale Python Bot

`strategy_bot.py` 是当前策略入口，`arena_geometry.py` 负责地形和建筑遮挡检测，`mechfight.py` 处理 `mechfight/1` 消息。`example_bot.py` 保留为官方基线。协议见 [protocol.md](protocol.md)。

策略默认采用练习赛打法，优先发育、逐个接战和脱战回血。首轮缩圈前出现未计入自己的击杀的减员，或观察到其他机甲的光束在另一台机甲附近结束时，策略会切换为更保守的乱斗打法。赛制由观测推断，可能漏判或误判。

导航使用局部避障与掩体检测。练习赛遭多方向追击或低血量时，会在可用建筑中寻找有遮挡的屋顶落点，按靠近外墙、起跳爬升、过檐和进入掩体的顺序移动。落点在安全区外或能量不足时会继续走地面路线。

线上练习赛首版 **2.17 分**（`8de229c`），拉扯和回血版 **2.63 分**（`407574c`），火控版 **2.75 分**（`b5d86b8`）。队伍报告的榜首参考为 **5.58 分**。当前候选加入屋顶脱战、横移换向和更严格的近身反击门控，也包含尚未线上评测的发育与搜索调整。`strategy_bot.py` 顶部的 `PRACTICE` 和 `FREE_FOR_ALL` 可分别调整战术参数。

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
