FROM python:3.13-slim
WORKDIR /bot
COPY mechfight.py arena_geometry.py canary_path.py canary_bot.py strategy_bot.py probe_bot.py ./
ENTRYPOINT ["python3", "-u", "-m", "probe_bot"]
