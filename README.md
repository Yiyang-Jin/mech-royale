# Mech Royale Practice Bot

[practice_bot.py](practice_bot.py) 是容器入口。主策略沿用 78m 激光拉扯、38% 开始撤退及原有蛇形走位。敌机正在蓄力时，C++ 用实际速度和碰撞检查能否在 0.4 秒内移动最多 4m 并进入实体掩体。比赛协议见 [protocol.md](protocol.md)。

[probe_bot.py](probe_bot.py) 是独立的留活口探针。只有零击杀、开局未满 90 秒、缺少激光或 8 个 Cube，并且已知唯一敌机在 45m 外且低于 350 HP 时，才延后补刀。GitHub 分支为 codex/pve-wave-probe。

在 Linux 容器中运行固定 12 种子 Practice 对照。

```sh
g++ -std=c++17 -O3 -DNDEBUG -shared -fPIC v2/core.cpp -o v2/core.so
python -m unittest test_strategy v2.test_simulator
python check/check.py --map-dir arena -- python -u -m practice_bot
PYTHONHASHSEED=0 python -m v2.arena_sim --mode practice --jobs 8 \
  --variant p406 --variant retreat56 --variant range92-retreat45 --variant laser-cover
```

模拟器的死亡记录包含最后击杀武器、攻击者档位、死前两秒受击及当时的武器配置。Medium 命中率按实际发射且以 Medium 为目标的机枪弹统计。
