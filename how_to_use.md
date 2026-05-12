# Method - 1
## Start Server
uvicorn resource_advisor.api.app:create_app --factory --host 0.0.0.0 --port 8000

## Get recommendations
curl -X POST http://localhost:8000/v1/recommend \
  -H "Content-Type: application/json" \
  -d '{
    "resource_url": "/subscriptions/<SUB_ID>/resourceGroups/<RG>/providers/Microsoft.Compute/virtualMachines/<VM_NAME>",
    "lookback_days": 30
  }'


# Method - 2
## CLI Based
python main.py "<full_resource_id>"
# or verbose:
python main.py "<full_resource_id>" --verbose
