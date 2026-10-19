"""Kopf handlers for FreeRouter CRD reconciliation."""
import kopf
import kubernetes
from kubernetes.client import (
    V1Deployment,
    V1Service,
    V1ConfigMap,
    V1Secret,
    V1HorizontalPodAutoscaler,
    V1PodDisruptionBudget,
    V1Ingress,
    V1ObjectMeta,
    V1LabelSelector,
    V1PodTemplateSpec,
    V1PodSpec,
    V1Container,
    V1ContainerPort,
    V1EnvVar,
    V1VolumeMount,
    V1Volume,
    V1ConfigMapVolumeSource,
    V1SecretVolumeSource,
    V1ResourceRequirements,
    V1ServicePort,
    V1ServiceSpec,
    V1HorizontalPodAutoscalerSpec,
    V1CrossVersionObjectReference,
    V1PodDisruptionBudgetSpec,
    V1IngressSpec,
    V1IngressRule,
    V1HTTPIngressRuleValue,
    V1HTTPIngressPath,
    V1IngressBackend,
    V1IngressServiceBackend,
    V1ServiceBackendPort,
)
# V1ServiceMonitor is a CRD from Prometheus Operator, use CustomObjectsApi instead
# from kubernetes.client import V1ServiceMonitor
from kubernetes.client import V1Endpoints
import yaml
import json


LABELS = {
    "app.kubernetes.io/name": "free-router",
    "app.kubernetes.io/managed-by": "free-router-operator",
}

def _common_labels(name: str) -> dict:
    return {**LABELS, "app.kubernetes.io/instance": name}


def _make_deployment(spec, name, namespace, config_hash):
    """Build Deployment object."""
    container = V1Container(
        name="free-router",
        image=spec.get("image", "free-router:latest"),
        image_pull_policy="IfNotPresent",
        ports=[V1ContainerPort(container_port=8787, name="http")],
        env=[
            V1EnvVar(name="FREE_ROUTER_CONFIG", value="/config/config.yaml"),
        ],
        resources=V1ResourceRequirements(
            limits=spec.get("resources", {}).get("limits"),
            requests=spec.get("resources", {}).get("requests"),
        ) if spec.get("resources") else None,
        volume_mounts=[
            V1VolumeMount(name="config", mount_path="/config", read_only=True),
            V1VolumeMount(name="keys", mount_path="/keys", read_only=True),
        ],
        liveness_probe=kubernetes.client.V1Probe(
            http_get=kubernetes.client.V1HTTPGetAction(path="/health", port=8787),
            initial_delay_seconds=10,
            period_seconds=10,
        ),
        readiness_probe=kubernetes.client.V1Probe(
            http_get=kubernetes.client.V1HTTPGetAction(path="/health", port=8787),
            initial_delay_seconds=5,
            period_seconds=5,
        ),
    )

    volumes = [
        V1Volume(name="config", config_map=V1ConfigMapVolumeSource(name=f"{name}-config")),
        V1Volume(name="keys", secret=V1SecretVolumeSource(secret_name=f"{name}-keys")),
    ]

    template = V1PodTemplateSpec(
        metadata=V1ObjectMeta(labels=_common_labels(name), annotations={"config-hash": config_hash}),
        spec=V1PodSpec(containers=[container], volumes=volumes),
    )

    selector = V1LabelSelector(match_labels=_common_labels(name))

    return V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=V1ObjectMeta(name=name, namespace=namespace, labels=_common_labels(name)),
        spec=kubernetes.client.V1DeploymentSpec(
            replicas=spec.get("replicas", 2),
            selector=selector,
            template=template,
        ),
    )


def _make_service(name, namespace):
    return V1Service(
        api_version="v1",
        kind="Service",
        metadata=V1ObjectMeta(name=name, namespace=namespace, labels=_common_labels(name)),
        spec=V1ServiceSpec(
            selector=_common_labels(name),
            ports=[V1ServicePort(port=80, target_port=8787, name="http")],
        ),
    )


def _make_configmap(spec, name, namespace):
    config_yaml = yaml.dump(spec.get("config", {}), allow_unicode=True, sort_keys=False)
    return V1ConfigMap(
        api_version="v1",
        kind="ConfigMap",
        metadata=V1ObjectMeta(name=f"{name}-config", namespace=namespace, labels=_common_labels(name)),
        data={"config.yaml": config_yaml},
    )


def _make_secret(name, namespace):
    return V1Secret(
        api_version="v1",
        kind="Secret",
        metadata=V1ObjectMeta(name=f"{name}-keys", namespace=namespace, labels=_common_labels(name)),
        type="Opaque",
        string_data={},  # User populates keys via separate mechanism
    )


def _make_hpa(spec, name, namespace):
    if not spec.get("autoscaling", {}).get("enabled", True):
        return None
    hpa_spec = spec["autoscaling"]
    return V1HorizontalPodAutoscaler(
        api_version="autoscaling/v2",
        kind="HorizontalPodAutoscaler",
        metadata=V1ObjectMeta(name=f"{name}-hpa", namespace=namespace, labels=_common_labels(name)),
        spec=V1HorizontalPodAutoscalerSpec(
            scale_target_ref=V1CrossVersionObjectReference(
                api_version="apps/v1",
                kind="Deployment",
                name=name,
            ),
            min_replicas=hpa_spec.get("minReplicas", 2),
            max_replicas=hpa_spec.get("maxReplicas", 10),
            metrics=[
                kubernetes.client.V1MetricSpec(
                    type="Resource",
                    resource=kubernetes.client.V1ResourceMetricSource(
                        name="cpu",
                        target=kubernetes.client.V1MetricTarget(
                            type="Utilization",
                            average_utilization=hpa_spec.get("targetCPUUtilizationPercentage", 70),
                        ),
                    ),
                ),
            ],
        ),
    )


def _make_pdb(name, namespace):
    return V1PodDisruptionBudget(
        api_version="policy/v1",
        kind="PodDisruptionBudget",
        metadata=V1ObjectMeta(name=f"{name}-pdb", namespace=namespace, labels=_common_labels(name)),
        spec=V1PodDisruptionBudgetSpec(
            min_available="50%",
            selector=V1LabelSelector(match_labels=_common_labels(name)),
        ),
    )


def _make_servicemonitor(name, namespace):
    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "ServiceMonitor",
        "metadata": {
            "name": f"{name}-sm",
            "namespace": namespace,
            "labels": _common_labels(name),
        },
        "spec": {
            "selector": {"matchLabels": _common_labels(name)},
            "endpoints": [{"port": "http", "interval": "30s", "path": "/metrics"}],
        },
    }


def _make_ingress(spec, name, namespace):
    ingress_spec = spec.get("ingress", {})
    if not ingress_spec.get("enabled", False):
        return None
    host = ingress_spec.get("host", f"{name}.example.com")
    tls_secret = ingress_spec.get("tlsSecret")

    rules = [
        V1IngressRule(
            host=host,
            http=V1HTTPIngressRuleValue(
                paths=[
                    V1HTTPIngressPath(
                        path="/",
                        path_type="Prefix",
                        backend=V1IngressBackend(
                            service=V1IngressServiceBackend(
                                name=name,
                                port=V1ServiceBackendPort(number=80),
                            ),
                        ),
                    ),
                ],
            ),
        ),
    ]

    tls = []
    if tls_secret:
        tls.append(kubernetes.client.V1IngressTLS(hosts=[host], secret_name=tls_secret))

    return V1Ingress(
        api_version="networking.k8s.io/v1",
        kind="Ingress",
        metadata=V1ObjectMeta(
            name=f"{name}-ingress",
            namespace=namespace,
            labels=_common_labels(name),
            annotations={
                "nginx.ingress.kubernetes.io/proxy-body-size": "50m",
                "nginx.ingress.kubernetes.io/proxy-read-timeout": "300",
                "nginx.ingress.kubernetes.io/proxy-send-timeout": "300",
            },
        ),
        spec=V1IngressSpec(tls=tls, rules=rules),
    )


def _config_hash(spec):
    """Compute a hash of the config-relevant spec fields for rollout triggering."""
    import hashlib
    relevant = {
        "config": spec.get("config", {}),
        "image": spec.get("image"),
        "resources": spec.get("resources"),
        "env": spec.get("env", {}),
    }
    return hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()[:12]


@kopf.on.create("freerouter.io", "v1alpha1", "freerouters")
@kopf.on.update("freerouter.io", "v1alpha1", "freerouters")
async def reconcile(spec, name, namespace, logger, **kwargs):
    """Reconcile FreeRouter resources."""
    config_hash = _config_hash(spec)

    # Initialize API clients
    apps_api = kubernetes.client.AppsV1Api()
    core_api = kubernetes.client.CoreV1Api()
    autoscaling_api = kubernetes.client.AutoscalingV2Api()
    policy_api = kubernetes.client.PolicyV1Api()
    networking_api = kubernetes.client.NetworkingV1Api()
    custom_api = kubernetes.client.CustomObjectsApi()

    resources_to_apply = []

    # ConfigMap
    resources_to_apply.append(("configmap", _make_configmap(spec, name, namespace), core_api.replace_namespaced_config_map))
    # Secret
    resources_to_apply.append(("secret", _make_secret(name, namespace), core_api.replace_namespaced_secret))
    # Deployment
    resources_to_apply.append(("deployment", _make_deployment(spec, name, namespace, config_hash), apps_api.replace_namespaced_deployment))
    # Service
    resources_to_apply.append(("service", _make_service(name, namespace), core_api.replace_namespaced_service))

    # Optional resources
    hpa = _make_hpa(spec, name, namespace)
    if hpa:
        resources_to_apply.append(("hpa", hpa, autoscaling_api.replace_namespaced_horizontal_pod_autoscaler))

    pdb = _make_pdb(name, namespace)
    resources_to_apply.append(("pdb", pdb, policy_api.replace_namespaced_pod_disruption_budget))

    sm = _make_servicemonitor(name, namespace)
    try:
        resources_to_apply.append(("servicemonitor", sm, lambda n, ns, b: custom_api.replace_namespaced_custom_object(
            group="monitoring.coreos.com", version="v1", namespace=ns, plural="servicemonitors", name=n, body=b
        )))
    except Exception:
        logger.warning("ServiceMonitor CRD not found, skipping")

    ingress = _make_ingress(spec, name, namespace)
    if ingress:
        resources_to_apply.append(("ingress", ingress, networking_api.replace_namespaced_ingress))

    # Apply all resources
    for res_type, obj, replace_fn in resources_to_apply:
        try:
            replace_fn(obj.metadata.name, namespace, obj)
            logger.info(f"{res_type} {obj.metadata.name} reconciled")
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 404:
                # Create if not exists
                create_fn = getattr(replace_fn.__self__, replace_fn.__name__.replace("replace", "create"))
                create_fn(namespace, obj)
                logger.info(f"{res_type} {obj.metadata.name} created")
            else:
                logger.error(f"{res_type} {obj.metadata.name} failed: {e}")
                raise

    # Update status
    try:
        deployment = apps_api.read_namespaced_deployment(name, namespace)
        status = {
            "conditions": [
                {"type": "Ready", "status": "True", "reason": "Reconciled", "message": "All resources reconciled"}
            ],
            "observedGeneration": deployment.metadata.generation,
            "readyReplicas": deployment.status.ready_replicas or 0,
            "currentReplicas": deployment.status.replicas or 0,
        }
        custom_api.patch_namespaced_custom_object_status(
            group="freerouter.io", version="v1alpha1", namespace=namespace,
            plural="freerouters", name=name, body={"status": status},
        )
    except Exception as e:
        logger.warning(f"Status update failed: {e}")


@kopf.on.delete("freerouter.io", "v1alpha1", "freerouters")
async def cleanup(name, namespace, logger, **kwargs):
    """Cleanup is handled by Kubernetes garbage collection (ownerReferences)."""
    logger.info(f"FreeRouter {name} marked for deletion, GC will handle children")


# Dry-run helper for local testing
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "dry-run":
        # Print all resources as YAML
        class MockSpec(dict):
            def __init__(self):
                super().__init__({
                    "replicas": 2,
                    "image": "free-router:latest",
                    "config": {"server": {"port": 8787}},
                    "autoscaling": {"enabled": True},
                    "ingress": {"enabled": False},
                })
            def get(self, k, d=None):
                return super().get(k, d)

        spec = MockSpec()
        name = "test-fr"
        namespace = "default"
        config_hash = _config_hash(spec)

        print("---")
        print(yaml.dump(_make_configmap(spec, name, namespace).to_dict()))
        print("---")
        print(yaml.dump(_make_secret(name, namespace).to_dict()))
        print("---")
        print(yaml.dump(_make_deployment(spec, name, namespace, config_hash).to_dict()))
        print("---")
        print(yaml.dump(_make_service(name, namespace).to_dict()))
        hpa = _make_hpa(spec, name, namespace)
        if hpa:
            print("---")
            print(yaml.dump(hpa.to_dict()))
        print("---")
        print(yaml.dump(_make_pdb(name, namespace).to_dict()))
        print("---")
        print(yaml.dump(_make_servicemonitor(name, namespace).to_dict()))
        ingress = _make_ingress({"ingress": {"enabled": True, "host": "fr.example.com"}}, name, namespace)
        if ingress:
            print("---")
            print(yaml.dump(ingress.to_dict()))