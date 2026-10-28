import sys, os, logging
sys.path.insert(0, os.getcwd())
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s | %(message)s", level=logging.DEBUG)
logging.getLogger("apscheduler").setLevel(logging.INFO)
import runpy
runpy.run_path("bot.py", run_name="__main__")
