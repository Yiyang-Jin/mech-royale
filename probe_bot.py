from canary_bot import GroundCanary
from mechfight import run


class StopAt160(GroundCanary):
    def act(self, obs):
        if obs["t"] > 160:
            return {"tick": obs["tick"]}
        return super().act(obs)


if __name__ == "__main__":
    run(StopAt160)
