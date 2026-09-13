import multiprocessing

# PyInstaller worker/resource-tracker invocations must exit through their own
# handler before the GUI's argument parser or lifecycle is imported.
multiprocessing.freeze_support()
from knowledge_distiller.v1.adapters.python_policy import check_current
check_current()

from knowledge_distiller.v1.mac_app import main

main()
