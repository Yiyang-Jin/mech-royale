FROM python:3.13-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends g++ && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY v2/core.cpp .
RUN g++ -std=c++17 -O3 -DNDEBUG -shared -fPIC core.cpp -o core.so

FROM python:3.13-slim
WORKDIR /bot
COPY mechfight.py arena_geometry.py canary_path.py canary_bot.py strategy_bot.py probe_bot.py practice_bot.py ./
COPY v2/native.py v2/
COPY --from=build /build/core.so v2/core.so
ENTRYPOINT ["python3", "-u", "-m", "practice_bot"]
