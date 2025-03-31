
all: test


ready-to-run:
	wget https://github.com/OpenXiangShan/XSPdb/releases/download/v0.1.0-test/ready-to-run.tar.gz
	tar -xzf ready-to-run.tar.gz


XSPython:
	wget https://github.com/OpenXiangShan/XSPdb/releases/download/v0.1.0-test/XSPython.tar.gz
	tar -xzf XSPython.tar.gz


test: XSPython ready-to-run
	LD_PRELOAD=XSPython/xspcomm/libxspcomm.so.0.0.1 PYTHONPATH=. python3 example/test.py


clean:
	rm -rf ready-to-run.tar.gz
	rm -rf XSPython.tar.gz
