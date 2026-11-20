"""Public web startup, fixed synthetic data, AI disabled."""
import os
from pathlib import Path
import runpy
import sys

ROOT=Path(__file__).resolve().parent
sys.path[:0]=[str(ROOT),str(ROOT/'frontend'),str(ROOT/'ia/src')]
os.environ['DEMO_PUBLIC_WEB']='1'
os.environ['DEMO_DISABLE_AI']='1'
os.environ['DEMO_WEB_PORT']=os.environ.get('PORT','10000')

if __name__=='__main__':
    from portable import initialize
    initialize(ROOT)
    runpy.run_path(str(ROOT/'frontend/app.py'),run_name='__main__')
