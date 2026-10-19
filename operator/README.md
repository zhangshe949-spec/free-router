# FreeRouter Operator

Kubernetes Operator for managing FreeRouter deployments (kopf-based).

## Quick Start

```bash
# Install CRD
kubectl apply -f crds/freerouter.yaml

# Run operator locally (dev)
pip install -e .
free-router-operator

# Or build and deploy
docker build -t free-router-operator:latest -f Dockerfile ..
docker push free-router-operator:latest
# Then deploy via Helm chart (see charts/free-router-operator/)
```

## Custom Resource Example

```yaml
apiVersion: freerouter.io/v1alpha1
kind: FreeRouter
metadata:
  name: my-freerouter
  namespace: default
spec:
  replicas: 3
  image: free-router:latest
  config:
    server:
      host: 0.0.0.0
      port: 8787
    providers:
      kilogw:
        base_url: https://api.kilo.ai/api/openrouter
  autoscaling:
    enabled: true
    minReplicas: 2
    maxReplicas: 10
    targetCPUUtilizationPercentage: 70
  ingress:
    enabled: true
    host: freerouter.example.com
  serviceMonitor:
    enabled: true
```

## Dry-run (local rendering)

```bash
python -m operator.handler dry-run
```

## Status

Operator reports status via `.status.conditions`, `.status.readyReplicas`, `.status.currentReplicas`.