import azure.functions as func
from Bells.HUEBridge import bp as hue_bp

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)
app.register_functions(hue_bp)