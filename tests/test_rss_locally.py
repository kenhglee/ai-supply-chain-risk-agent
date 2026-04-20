import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
    
from app.workflows.supplier_risk_agent import run_supplier_risk_flow

sample_event = {
    "source": "local-test",
    "alerts": []
}

result = run_supplier_risk_flow(sample_event)
print(result)
