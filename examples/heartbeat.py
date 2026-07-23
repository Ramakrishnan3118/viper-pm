"""Demo app: prints a heartbeat; crashes on demand via env CRASH_AFTER."""
import os
import sys
import time

worker = os.environ.get("VIPER_WORKER_ID", "?")
crash_after = float(os.environ.get("CRASH_AFTER", "0"))
start = time.time()

print(f"heartbeat worker {worker} starting (pid {os.getpid()})")
while True:
    print(f"worker {worker} alive at {time.strftime('%H:%M:%S')}")
    if crash_after and time.time() - start > crash_after:
        print(f"worker {worker} simulating a crash!", file=sys.stderr)
        sys.exit(1)
    time.sleep(2)
