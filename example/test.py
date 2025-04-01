import sys
import signal
from XSPython import DUTSimTop, difftest as df, xsp
from XSPdb import *

def handle_sigint(signum, frame):
    print("\nReceived SIGINT, exit.")
    sys.exit(0)
signal.signal(signal.SIGINT, handle_sigint)

def test_sim_top():
    dut = DUTSimTop()
    XSPdb(dut, df, xsp).set_trace()
    while True:
        dut.Step(1000)

if __name__ == "__main__":
    test_sim_top()
