from datetime import datetime
import time

start = datetime(2025, 8, 4, 0, 6, 0)

while(True):
    now = datetime.now()
    delta = now - start

    days = delta.days
    hours, remainder = divmod(delta.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    print(f"{days}:{hours}:{minutes}:{seconds}")
    time.sleep(1)