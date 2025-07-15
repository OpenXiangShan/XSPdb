
all: test

init:
	@python3 -m pip install -r requirements.txt
	@if [ ! -d ready-to-run ]; then \
		if [ ! -f ready-to-run.tar.gz ]; then \
			echo "ready-to-run directory and ready-to-run.tar.gz not found, downloading..."; \
			wget https://github.com/OpenXiangShan/XSPdb/releases/download/v0.1.0-test/ready-to-run.tar.gz; \
		else \
			echo "ready-to-run.tar.gz already exists, skipping download."; \
		fi; \
	else \
		echo "ready-to-run directory already exists, skipping download."; \
	fi
	@if [ ! -d XSPython ]; then \
		if [ ! -f XSPython.tar.gz ]; then \
			echo "XSPython directory and XSPython.tar.gz not found, downloading..."; \
			wget https://github.com/OpenXiangShan/XSPdb/releases/download/v0.1.0-test/XSPython.tar.gz; \
		else \
			echo "XSPython.tar.gz already exists, skipping download."; \
		fi; \
	else \
		echo "XSPython directory already exists, skipping download."; \
	fi

ready-to-run: init
	@if [ ! -d ready-to-run ]; then \
		echo "ready-to-run directory not found, extracting..."; \
		tar -xzf ready-to-run.tar.gz; \
	else \
		echo "ready-to-run directory already exists, skipping extraction."; \
	fi


XSPython: init
	@if [ ! -d XSPython ]; then \
		echo "XSPython directory not found, extracting..."; \
		tar -xzf XSPython.tar.gz; \
	else \
		echo "XSPython directory already exists, skipping extraction."; \
	fi


test: XSPython ready-to-run
	LD_PRELOAD=XSPython/xspcomm/libxspcomm.so.0.0.1 PYTHONPATH=. python3 example/test.py


clean:
	rm -rf ready-to-run.tar.gz
	rm -rf XSPython.tar.gz
