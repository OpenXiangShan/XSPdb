#coding=utf-8

from XSPython import DUTSimTop, difftest as df, xsp
from XSPdb import *

def test_sim_top():
    dut = DUTSimTop()
    XSPdb(dut, df, xsp).set_trace()
    while True:
        dut.Step(1000)

if __name__ == "__main__":
    from bdb import BdbQuit
    try:
        test_sim_top()
    except BdbQuit:
        pass
