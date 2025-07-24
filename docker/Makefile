#***************************************************************************************
# Copyright (c) 2025 Beijing Institute of Open Source Chip (BOSC)
# Copyright (c) 2025 Institute of Computing Technology, Chinese Academy of Sciences
#
# XiangShan is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#          http://license.coscl.org.cn/MulanPSL2
#
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
#
# See the Mulan PSL v2 for more details.
#***************************************************************************************

PYTHON_DEP_FILE    = ./xspdb/requirements.txt
PDB_PYTHONPATH    := ./xspdb
PDB_LD_PRELOAD    := ./xspdb/pyxscore/xspcomm/libxspcomm.so.0.0.1

PDB_ARGS          ?=
IMAGE_FILE        ?= 
BATCH_ARGS        ?=
$(if $(strip $(BATCH_ARGS)), $(eval BATCH_ARGS := --batch 1 $(BATCH_ARGS)))

.PHONY: pdb-run check-deps

pdb-run: check-deps
	LD_PRELOAD="$(PDB_LD_PRELOAD)" \
	PYTHONPATH="$(PDB_PYTHONPATH)" \
  	python3 pdb-run.py $(PDB_ARGS) $(BATCH_ARGS) $(if $(IMAGE_FILE),-i $(IMAGE_FILE))

check-deps:
	@[ -f "$(PYTHON_DEP_FILE)" ] || { echo "Error: $(PYTHON_DEP_FILE) not found"; exit 1; }; \
	grep -v '^[#[:space:]]*$$' $(PYTHON_DEP_FILE) | while read -r req; do \
		ver=$$(pip show "$${req%%>=*}" 2>/dev/null | awk '/^Version:/{print $$2}') || exit 1; \
		[ -n "$$ver" ] && [ "$$(printf "$$ver\n$${req##*>=}" | sort -V | head -n1)" = "$${req##*>=}" ] || \
			{ echo "Error: Dependency requirement not satisfied, Please run 'pip install -r $(PYTHON_DEP_FILE)'"; exit 1; }; \
	done
