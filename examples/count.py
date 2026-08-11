import json
import time


for step in range(5):
    print(json.dumps({"step": step, "loss": 5 - step}), flush=True)
    time.sleep(0.1)

with open("metrics.json", "w", encoding="utf-8") as handle:
    json.dump({"final_loss": 1}, handle)
