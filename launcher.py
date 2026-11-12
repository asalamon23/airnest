import sys, os, threading, time, webbrowser
# The original monitor starts its server on import as __main__.
import runpy

def open_dashboard():
    time.sleep(2)
    webbrowser.open('http://localhost:8088')

if __name__ == '__main__':
    os.chdir(os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(__file__))
    threading.Thread(target=open_dashboard, daemon=True).start()
    runpy.run_module('hcv400', run_name='__main__')
