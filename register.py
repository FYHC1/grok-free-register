#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""English alias: python register.py ...  ->  注册.py"""
from pathlib import Path
import runpy
import sys

sys.argv[0] = str(Path(__file__).with_name("注册.py"))
runpy.run_path(str(Path(__file__).with_name("注册.py")), run_name="__main__")
