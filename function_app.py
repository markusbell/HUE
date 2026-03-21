import os
import sys
import azure.functions as func

# Ensure local package folder "src" is importable (contains python_hue_v2).
ROOT = os.path.dirname(__file__)
SRC_PATH = os.path.join(ROOT, "src")
if SRC_PATH not in sys.path:
    sys.path.insert(0, SRC_PATH)

from Bells.HUEBridge import bp as hue_bp  # noqa: E402

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)
app.register_functions(hue_bp)