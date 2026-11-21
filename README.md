# Mech Royale Practice Bot

[probe_bot.py](probe_bot.py) 是容器入口，采用纯 PVE 的 78m／38% 底座。开局优先拾取可用近枪，随后按 72m 和单挑斩杀距离作战。机枪使用横移历史修正提前量，满蓄激光可从被墙挡住的主目标转向通视目标。

在 Linux 容器中运行固定 12 种子 Practice 对照。

```sh
g++ -std=c++17 -O3 -DNDEBUG -shared -fPIC v2/core.cpp -o v2/core.so
python -m unittest test_strategy v2.test_simulator
python check/check.py --map-dir arena -- python -u -m probe_bot
PYTHONHASHSEED=0 python -m v2.arena_sim --mode practice --jobs 8 \
  --variant p406 --variant clear-waves
```

模拟器的死亡记录包含最后击杀武器、攻击者档位、死前两秒受击及当时的武器配置。Medium 命中率按实际发射且以 Medium 为目标的机枪弹统计。
